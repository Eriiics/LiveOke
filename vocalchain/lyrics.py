"""Búsqueda de letras en varias fuentes, en orden, hasta encontrar una sincronizada.

Orden por defecto:
  1. Archivos propios: %USERPROFILE%\\.vocalchain\\lyrics\\Artista - Título.lrc (o .txt)
  2. LRCLIB        (sincronizadas, gratis, compara la duración de la canción)
  3. Musixmatch    (sincronizadas, la base más grande; vía syncedlyrics)
  4. NetEase       (sincronizadas, muy buena para pop asiático y latino)
  5. Megalobiz     (sincronizadas, archivos .lrc de usuarios)
  6. Genius        (solo texto)
  7. lyrics.ovh    (solo texto)
Si una fuente trae solo texto, se sigue buscando una sincronizada y se usa el texto
como respaldo. "Otra fuente" en la ventana de letras repite la búsqueda saltándose
las fuentes ya probadas.
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

API = "https://lrclib.net/api"
UA = "VocalChain/0.2 (karaoke app)"
CONFIG = Path.home() / ".vocalchain"
CACHE_DIR = CONFIG / "lyrics_cache"
LOCAL_DIR = CONFIG / "lyrics"

SOURCES = ["Archivo local", "LRCLIB", "Musixmatch", "NetEase", "Megalobiz", "Genius", "lyrics.ovh"]

_JUNK = re.compile(
    r"\s*[\(\[\{][^\)\]\}]*(official|video|audio|lyric|letra|visuali[sz]er|hd|hq|4k|remaster|live|en vivo|"
    r"music video|videoclip|clip oficial|prod\.?|explicit)[^\)\]\}]*[\)\]\}]",
    re.I,
)
_FEAT = re.compile(r"\s*[\(\[]?\s*(feat\.?|ft\.?|featuring)\s+[^\)\]\-]*[\)\]]?", re.I)
_CHANNEL = re.compile(r"\s*(-\s*topic|vevo|official|oficial|records|music|tv)\s*$", re.I)


@dataclass
class Lyrics:
    synced: list[tuple[float, str]] | None = None
    plain: str | None = None
    track: str = ""
    artist: str = ""
    duration: float | None = None
    instrumental: bool = False
    source: str = ""

    @property
    def found(self) -> bool:
        return bool(self.synced or self.plain or self.instrumental)

    def to_json(self) -> dict:
        return {"synced": self.synced, "plain": self.plain, "track": self.track, "artist": self.artist,
                "duration": self.duration, "instrumental": self.instrumental, "source": self.source}

    @staticmethod
    def from_json(d: dict) -> "Lyrics":
        return Lyrics([tuple(x) for x in d["synced"]] if d.get("synced") else None, d.get("plain"),
                      d.get("track", ""), d.get("artist", ""), d.get("duration"), d.get("instrumental", False),
                      d.get("source", ""))


def parse_lrc(text: str) -> list[tuple[float, str]]:
    out = []
    offset = 0.0
    for line in text.splitlines():
        m = re.match(r"\[offset:\s*([+-]?\d+)\]", line.strip(), re.I)
        if m:
            offset = int(m.group(1)) / 1000.0
            continue
        stamps = re.findall(r"\[(\d+):(\d+(?:[.:]\d+)?)\]", line)
        if not stamps:
            continue
        words = re.sub(r"\[[^\]]*\]", "", line).strip()
        words = re.sub(r"<\d+:\d+(?:\.\d+)?>", "", words).strip()  # LRC palabra a palabra
        for mm, ss in stamps:
            t = int(mm) * 60 + float(ss.replace(":", "."))
            out.append((max(0.0, t - offset), words))
    out.sort(key=lambda x: x[0])
    return out


def clean_track(artist: str, title: str) -> tuple[str, str]:
    """Limpia títulos estilo YouTube: 'Artista - Canción (Official Video) ft. X'."""
    artist = (artist or "").strip()
    title = (title or "").strip()
    t = _JUNK.sub("", title)
    t = re.sub(r"\s*\|.*$", "", t)
    if " - " in t and (not artist or _CHANNEL.search(artist) or artist.lower() in t.lower()):
        a, t = t.split(" - ", 1)
        artist = a.strip()
    t = _FEAT.sub("", t).strip(" -–—\"'")
    artist = _CHANNEL.sub("", artist)
    artist = re.split(r"\s*(?:,|&| x | feat\.?| ft\.?)\s*", artist, maxsplit=1, flags=re.I)[0].strip()
    return artist, t.strip()


def _http_json(url: str, timeout=8.0):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Lrclib-Client": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _plausible(lyr: Lyrics, duration: float | None) -> bool:
    """Descarta letras sincronizadas de otra versión (terminan mucho después que la canción)."""
    if not lyr.synced or not duration:
        return True
    return lyr.synced[-1][0] <= duration + 25


# ---------------------------------------------------------------------------
# Fuentes
# ---------------------------------------------------------------------------
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9áéíóúñü]+", " ", s.lower()).strip()


def src_local(artist, title, album, duration) -> Lyrics | None:
    if not LOCAL_DIR.exists():
        return None
    want_t, want_a = _norm(title), _norm(artist)
    for f in LOCAL_DIR.iterdir():
        if f.suffix.lower() not in (".lrc", ".txt"):
            continue
        stem = _norm(f.stem)
        if want_t and want_t in stem and (not want_a or want_a in stem or len(want_t) > 6):
            text = f.read_text(encoding="utf-8", errors="replace")
            synced = parse_lrc(text)
            return Lyrics(synced or None, None if synced else text, title, artist, duration, source="Archivo local")
    return None


def src_lrclib(artist, title, album, duration) -> Lyrics | None:
    def rec_to(rec):
        synced = parse_lrc(rec["syncedLyrics"]) if rec.get("syncedLyrics") else None
        return Lyrics(synced or None, rec.get("plainLyrics") or None, rec.get("trackName", ""), rec.get("artistName", ""),
                      rec.get("duration"), bool(rec.get("instrumental")), "LRCLIB")

    rec = None
    if artist and title:
        params = {"artist_name": artist, "track_name": title}
        if album:
            params["album_name"] = album
        if duration:
            params["duration"] = int(round(duration))
        rec = _http_json(f"{API}/get?{urllib.parse.urlencode(params)}")
        if rec is None and (duration or album):
            rec = _http_json(f"{API}/get?{urllib.parse.urlencode({'artist_name': artist, 'track_name': title})}")
    if rec is None:
        q = {"track_name": title, "artist_name": artist} if artist else {"q": title}
        res = _http_json(f"{API}/search?{urllib.parse.urlencode(q)}") or []
        if not res and artist:
            res = _http_json(f"{API}/search?{urllib.parse.urlencode({'q': f'{artist} {title}'})}") or []

        def score(r):
            s = 10 if r.get("syncedLyrics") else (5 if r.get("plainLyrics") else 0)
            if duration and r.get("duration"):
                s -= min(abs(r["duration"] - duration), 30) / 3
            if artist and artist.lower() in (r.get("artistName") or "").lower():
                s += 4
            return s
        rec = max(res, key=score) if res else None
    return rec_to(rec) if rec else None


def _syncedlyrics_provider(name: str):
    def run(artist, title, album, duration) -> Lyrics | None:
        try:
            from syncedlyrics import providers as P  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            return None
        cls = getattr(P, name, None)
        if cls is None:
            return None
        res = cls().get_lrc(f"{title} {artist}".strip())
        if not res:
            return None
        synced = parse_lrc(res.synced) if getattr(res, "synced", None) else None
        plain = getattr(res, "unsynced", None)
        if not synced and not plain:
            return None
        return Lyrics(synced or None, plain, title, artist, duration, source=name)
    return run


def src_lyrics_ovh(artist, title, album, duration) -> Lyrics | None:
    if not artist:
        return None
    d = _http_json(f"https://api.lyrics.ovh/v1/{urllib.parse.quote(artist)}/{urllib.parse.quote(title)}")
    if d and d.get("lyrics"):
        return Lyrics(None, d["lyrics"].replace("\r\n", "\n").strip(), title, artist, duration, source="lyrics.ovh")
    return None


PROVIDERS = {
    "Archivo local": src_local,
    "LRCLIB": src_lrclib,
    "Musixmatch": _syncedlyrics_provider("Musixmatch"),
    "NetEase": _syncedlyrics_provider("NetEase"),
    "Megalobiz": _syncedlyrics_provider("Megalobiz"),
    "Genius": _syncedlyrics_provider("Genius"),
    "lyrics.ovh": src_lyrics_ovh,
}


# ---------------------------------------------------------------------------
def _cache_path(artist: str, title: str) -> Path:
    h = hashlib.sha1(f"{artist.lower()}|{title.lower()}".encode()).hexdigest()[:16]
    return CACHE_DIR / f"{h}.v2.json"


def fetch(artist: str, title: str, album: str = "", duration: float | None = None,
          use_cache: bool = True, exclude: set[str] | None = None) -> Lyrics:
    artist_c, title_c = clean_track(artist, title)
    exclude = set(exclude or ())
    cp = _cache_path(artist_c, title_c)
    if use_cache and not exclude and cp.exists():
        try:
            return Lyrics.from_json(json.loads(cp.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            pass

    fallback: Lyrics | None = None
    tried = []
    for name in SOURCES:
        if name in exclude:
            continue
        tried.append(name)
        try:
            res = PROVIDERS[name](artist_c, title_c, album, duration)
        except Exception:  # noqa: BLE001  (una fuente caída no detiene la búsqueda)
            res = None
        if not res or not res.found:
            continue
        if res.synced and _plausible(res, duration):
            _save_cache(cp, res)
            return res
        if fallback is None and (res.plain or res.synced or res.instrumental):
            fallback = res
    if fallback is not None:
        _save_cache(cp, fallback)
        return fallback
    return Lyrics(track=title_c, artist=artist_c, source="")


def _save_cache(cp: Path, lyr: Lyrics):
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cp.write_text(json.dumps(lyr.to_json(), ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def line_index(synced: list[tuple[float, str]], t: float) -> int:
    """Índice de la línea que corresponde al tiempo t (-1 antes de la primera)."""
    lo, hi = 0, len(synced)
    while lo < hi:
        mid = (lo + hi) // 2
        if synced[mid][0] <= t:
            lo = mid + 1
        else:
            hi = mid
    return lo - 1
