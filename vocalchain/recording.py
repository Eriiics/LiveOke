"""Grabaciones de VocalChain: carpetas, info.json/info.txt, letra.txt, WAV->MP3 y YouTube (yt-dlp).

Flujo (el motor C++ graba la salida Monitor en un WAV 32f temporal):
  1. rec = Recording.start(mode="song"|"session", instrumental=..., preset=..., key=...)
     -> crea la carpeta y devuelve rec.wav_tmp (ruta que se manda al motor en rec_start)
  2. (modo Sesión) al cambiar de canción: rec.add_marker(t_seg, instrumental, link, key)
  3. al parar: rec.finish(duration_s, markers_del_motor) -> convierte a MP3 en un hilo,
     escribe info.json / info.txt y crea letra.txt si no existe. Si la conversión falla, el WAV queda.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

RECORD_ROOT = Path.home() / "Music" / "VocalChain"
_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(s: str, maxlen: int = 80) -> str:
    s = _INVALID.sub(" ", s or "").strip().rstrip(". ")
    s = re.sub(r"\s+", " ", s)
    return (s[:maxlen].rstrip(". ") or "Sin titulo")


def _unique_dir(base: Path) -> Path:
    p, i = base, 2
    while p.exists():
        p = base.with_name(f"{base.name} ({i})")
        i += 1
    return p


def fmt_time(t: float) -> str:
    t = max(0, int(round(t)))
    h, m, s = t // 3600, (t // 60) % 60, t % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# --------------------------------------------------------------------------- info

@dataclass
class Marker:
    t: float
    instrumental: str = ""
    youtube_url: str = ""
    key: str = ""
    bpm: Optional[float] = None


@dataclass
class RecordingInfo:
    mode: str = "song"             # "song" | "session"
    instrumental: str = ""
    youtube_url: str = ""
    channel: str = ""
    key: str = ""
    bpm: Optional[float] = None
    date: str = ""
    duration_s: float = 0.0
    preset: str = ""
    chain: List[str] = field(default_factory=list)
    notes: str = ""
    audio_file: str = ""
    markers: List[Marker] = field(default_factory=list)
    app_version: str = "0.3"

    @classmethod
    def from_dict(cls, d: dict) -> "RecordingInfo":
        d = dict(d)
        d["markers"] = [Marker(**m) for m in d.get("markers", [])]
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})


def info_text(info: RecordingInfo) -> str:
    lines = [
        f"VocalChain — {'Sesión' if info.mode == 'session' else 'Canción'}",
        "",
        f"Instrumental : {info.instrumental or '-'}",
        f"YouTube      : {info.youtube_url or '-'}",
        f"Canal        : {info.channel or '-'}",
        f"Tonalidad    : {info.key or '-'}",
        f"BPM          : {info.bpm if info.bpm else '-'}",
        f"Fecha        : {info.date}",
        f"Duración     : {fmt_time(info.duration_s)}",
        f"Preset       : {info.preset or '-'}",
        f"Cadena       : {' → '.join(info.chain) if info.chain else '-'}",
        f"Archivo      : {info.audio_file}",
    ]
    if info.markers:
        lines += ["", "Canciones en la sesión:"]
        for i, m in enumerate(info.markers, 1):
            extra = " · ".join(x for x in (m.key, m.youtube_url) if x)
            lines.append(f"  {i:2d}. {fmt_time(m.t):>8}  {m.instrumental or '(sin nombre)'}"
                         + (f"   [{extra}]" if extra else ""))
    if info.notes:
        lines += ["", "Notas:", info.notes]
    return "\n".join(lines) + "\n"


def write_info(folder: Path, info: RecordingInfo) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / "info.json.tmp"
    tmp.write_text(json.dumps(asdict(info), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, folder / "info.json")
    (folder / "info.txt").write_text(info_text(info), encoding="utf-8")


def read_info(folder: Path) -> Optional[RecordingInfo]:
    try:
        return RecordingInfo.from_dict(json.loads((folder / "info.json").read_text(encoding="utf-8")))
    except Exception:
        return None


def ensure_lyrics(folder: Path, text: str = "") -> Path:
    """Crea letra.txt si no existe (nunca sobrescribe lo que el usuario escribió)."""
    p = folder / "letra.txt"
    if not p.exists():
        p.write_text(text or "", encoding="utf-8")
    return p


def list_recordings(root: Path = RECORD_ROOT) -> List[tuple]:
    """[(carpeta, RecordingInfo)] más recientes primero."""
    out = []
    if root.exists():
        for d in root.iterdir():
            if d.is_dir():
                info = read_info(d)
                if info:
                    out.append((d, info))
    out.sort(key=lambda x: x[1].date, reverse=True)
    return out


def open_folder(path: Path) -> None:
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


# --------------------------------------------------------------------------- WAV -> MP3

def _read_wav_header(f):
    """(fmt_tag, canales, frecuencia, bits, offset_datos, bytes_datos). RIFF/RF64, PCM 16/24/32,
    float 32 y WAVE_FORMAT_EXTENSIBLE (lo que escribe JUCE)."""
    riff = f.read(12)
    if len(riff) < 12 or riff[8:12] != b"WAVE" or riff[:4] not in (b"RIFF", b"RF64"):
        raise ValueError("no es un WAV")
    fmt = None
    ds64_data = None
    while True:
        hdr = f.read(8)
        if len(hdr) < 8:
            raise ValueError("WAV sin bloque data")
        cid, size = hdr[:4], struct.unpack("<I", hdr[4:])[0]
        if cid == b"ds64":
            body = f.read(size)
            ds64_data = struct.unpack("<Q", body[8:16])[0]
        elif cid == b"fmt ":
            body = f.read(size)
            tag, ch, rate, _, _, bits = struct.unpack("<HHIIHH", body[:16])
            if tag == 0xFFFE and len(body) >= 26:
                tag = struct.unpack("<H", body[24:26])[0]
            fmt = (tag, ch, rate, bits)
        elif cid == b"data":
            if fmt is None:
                raise ValueError("WAV sin fmt")
            off = f.tell()
            if size == 0xFFFFFFFF and ds64_data is not None:
                size = ds64_data
            if size == 0 or size == 0xFFFFFFFF:          # escritor interrumpido: usar hasta el final
                f.seek(0, 2)
                size = f.tell() - off
                f.seek(off)
            return fmt + (off, size)
        else:
            f.seek(size + (size & 1), 1)
            continue
        if size & 1:
            f.seek(1, 1)


def wav_to_mp3(wav: Path, mp3: Path, bitrate: int = 192, quality: int = 2,
               progress: Optional[Callable[[float], None]] = None) -> Path:
    """Convierte por bloques de ~1 s (memoria baja aunque la sesión dure horas)."""
    import array
    import lameenc
    try:
        import numpy as _np
    except Exception:
        _np = None

    with open(wav, "rb") as f:
        tag, ch, rate, bits, off, nbytes = _read_wav_header(f)
        if nbytes < (bits // 8) * ch:
            raise ValueError("la grabación quedó vacía (¿el motor estaba recibiendo audio?)")
        if ch not in (1, 2):
            raise ValueError(f"canales no soportados: {ch}")
        bps = bits // 8
        frame = bps * ch
        enc = lameenc.Encoder()
        enc.set_bit_rate(bitrate)
        enc.set_in_sample_rate(rate)
        enc.set_channels(ch)
        enc.set_quality(quality)
        part = mp3.with_suffix(mp3.suffix + ".part")
        f.seek(off)
        try:
            _encode_into(f, part, enc, nbytes, frame, tag, bits, _np, progress)
        except BaseException:
            try:
                part.unlink()
            except OSError:
                pass
            raise
    os.replace(part, mp3)
    return mp3


def _encode_into(f, part, enc, nbytes, frame, tag, bits, _np, progress):
    """Codifica bloques de ~1 s del WAV abierto en `part`."""
    import array
    rate_chunk = frame * 48000
    done = 0
    with open(part, "wb") as out:
        while done < nbytes:
            raw = f.read(min(rate_chunk, nbytes - done))
            if not raw:
                break
            raw = raw[: len(raw) - len(raw) % frame]
            done += len(raw)
            if _np is not None and tag == 3 and bits == 32:
                a = _np.frombuffer(raw, dtype="<f4")
                pcm = (_np.clip(a, -1.0, 1.0) * 32767.0).astype("<i2")
                out.write(enc.encode(pcm.tobytes()))
                if progress:
                    progress(done / max(1, nbytes))
                continue
            if tag == 3 and bits == 32:
                a = array.array("f")
                a.frombytes(raw)
                pcm = array.array("h", (32767 if x >= 1.0 else -32768 if x <= -1.0 else int(x * 32767.0)
                                        for x in a))
            elif tag == 1 and bits == 16:
                pcm = array.array("h")
                pcm.frombytes(raw)
            elif tag == 1 and bits == 24:
                pcm = array.array("h", (int.from_bytes(raw[i + 1:i + 3], "little", signed=True)
                                        for i in range(0, len(raw), 3)))
            elif tag == 1 and bits == 32:
                a = array.array("i")
                a.frombytes(raw)
                pcm = array.array("h", (x >> 16 for x in a))
            else:
                raise ValueError(f"formato WAV no soportado: tag={tag} bits={bits}")
            if sys.byteorder != "little":
                pcm.byteswap()
            out.write(enc.encode(pcm.tobytes()))
            if progress:
                progress(done / max(1, nbytes))
        out.write(enc.flush())


def wav_duration(wav: Path) -> float:
    with open(wav, "rb") as f:
        tag, ch, rate, bits, off, nbytes = _read_wav_header(f)
    return nbytes / float(rate * ch * (bits // 8))


# --------------------------------------------------------------------------- grabación en curso

class Recording:
    """mode="song": carpeta '<Instrumental> - <fecha>' con su MP3.
    mode="session": carpeta 'Sesión <fecha>' con un MP3 continuo y la lista de marcas."""

    def __init__(self, folder: Path, info: RecordingInfo):
        self.folder = folder
        self.info = info
        self.wav_tmp = folder / "grabacion.tmp.wav"
        self.mp3_path = folder / info.audio_file
        self.converting = False
        self.error: Optional[str] = None

    @classmethod
    def start(cls, mode: str = "song", instrumental: str = "", youtube_url: str = "", channel: str = "",
              key: str = "", bpm: Optional[float] = None, preset: str = "", chain: Optional[List[str]] = None,
              lyrics: str = "", root: Path = RECORD_ROOT, now: Optional[_dt.datetime] = None) -> "Recording":
        now = now or _dt.datetime.now()
        stamp = now.strftime("%Y-%m-%d %H-%M")
        if mode == "session":
            title = f"Sesión {stamp}"
        else:
            title = f"{safe_name(instrumental) if instrumental else 'Grabación'} - {stamp}"
        folder = _unique_dir(root / title)
        folder.mkdir(parents=True)
        info = RecordingInfo(mode=mode, instrumental=instrumental if mode == "song" else "",
                             youtube_url=youtube_url if mode == "song" else "",
                             channel=channel if mode == "song" else "", key=key, bpm=bpm,
                             date=now.isoformat(timespec="seconds"), preset=preset, chain=list(chain or []),
                             audio_file=f"{safe_name(folder.name)}.mp3")
        rec = cls(folder, info)
        if mode == "session" and instrumental:
            rec.add_marker(0.0, instrumental, youtube_url, key, bpm)
        write_info(folder, info)
        ensure_lyrics(folder, lyrics)
        return rec

    def add_marker(self, t: float, instrumental: str = "", youtube_url: str = "", key: str = "",
                   bpm: Optional[float] = None) -> None:
        self.info.markers.append(Marker(round(float(t), 2), instrumental, youtube_url, key, bpm))
        write_info(self.folder, self.info)

    def update(self, **fields) -> None:
        for k, v in fields.items():
            if hasattr(self.info, k):
                setattr(self.info, k, v)
        write_info(self.folder, self.info)

    def finish(self, duration_s: Optional[float] = None, engine_markers: Optional[list] = None,
               on_done: Optional[Callable[["Recording"], None]] = None, bitrate: int = 192,
               blocking: bool = False) -> None:
        """engine_markers: [{"t": seg, "name": ...}] de rec_stop (tiempo exacto en muestras)."""
        if engine_markers:
            for em, m in zip(engine_markers, [m for m in self.info.markers if m.t > 0] or []):
                m.t = round(float(em.get("t", m.t)), 2)
        if duration_s is None and self.wav_tmp.exists():
            try:
                duration_s = wav_duration(self.wav_tmp)
            except Exception:
                pass
        self.info.duration_s = float(duration_s or 0.0)
        write_info(self.folder, self.info)

        def work():
            self.converting = True
            try:
                wav_to_mp3(self.wav_tmp, self.mp3_path, bitrate=bitrate)
                try:
                    self.wav_tmp.unlink()
                except OSError:
                    pass
            except Exception as e:
                self.error = f"No se pudo convertir a MP3: {e}. El audio quedó en {self.wav_tmp.name}"
                self.info.audio_file = self.wav_tmp.name
                write_info(self.folder, self.info)
            finally:
                self.converting = False
                if on_done:
                    on_done(self)

        if blocking:
            work()
        else:
            threading.Thread(target=work, name="mp3-encode", daemon=True).start()


# --------------------------------------------------------------------------- YouTube (yt-dlp)

def find_ffmpeg() -> Optional[str]:
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def clean_title_for_search(title: str) -> str:
    t = re.sub(r"\s+[-–—]\s+(YouTube|Mozilla Firefox|Google Chrome|Microsoft Edge|Opera|Brave).*$", "", title or "",
               flags=re.I)
    t = re.sub(r"^\(\d+\)\s*", "", t)
    t = re.sub(r"[\"“”«»\[\]]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def yt_search(query: str, timeout: float = 20.0) -> Optional[dict]:
    """Primer resultado de YouTube (ytsearch1). {title,url,channel,duration} o None.
    La interfaz pide confirmación antes de guardar el link."""
    import yt_dlp
    q = clean_title_for_search(query)
    if not q:
        return None
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": "in_playlist",
            "socket_timeout": timeout, "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        res = ydl.extract_info(f"ytsearch1:{q}", download=False)
    entries = (res or {}).get("entries") or []
    if not entries:
        return None
    e = entries[0]
    vid = e.get("id")
    url = e.get("url") or (f"https://www.youtube.com/watch?v={vid}" if vid else "")
    if url and not url.startswith("http"):
        url = f"https://www.youtube.com/watch?v={url}"
    return {"title": e.get("title", ""), "url": url, "channel": e.get("channel") or e.get("uploader") or "",
            "duration": e.get("duration")}


_YT_RE = re.compile(r"(?:youtube\.com/(?:watch\?v=|shorts/|live/)|youtu\.be/|music\.youtube\.com/watch\?v=)"
                    r"([A-Za-z0-9_-]{11})")


def normalize_youtube_url(text: str) -> Optional[str]:
    m = _YT_RE.search(text or "")
    return f"https://www.youtube.com/watch?v={m.group(1)}" if m else None


def yt_download_audio(url: str, folder: Path, name: Optional[str] = None,
                      progress: Optional[Callable[[float], None]] = None) -> Path:
    """Descarga la instrumental a la carpeta. Con ffmpeg -> MP3 192k; sin ffmpeg -> audio original."""
    import yt_dlp
    folder.mkdir(parents=True, exist_ok=True)
    ff = find_ffmpeg()
    base = safe_name(name) if name else "%(title).80s"
    outtmpl = str(folder / f"{base} (instrumental).%(ext)s")

    def hook(d):
        if progress and d.get("status") == "downloading":
            tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if tot:
                progress(min(1.0, d.get("downloaded_bytes", 0) / tot))

    opts = {"format": "bestaudio/best", "outtmpl": outtmpl, "noplaylist": True, "quiet": True,
            "no_warnings": True, "progress_hooks": [hook], "overwrites": False}
    if ff:
        opts["ffmpeg_location"] = ff
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                   "preferredquality": "192"}]
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = Path(ydl.prepare_filename(info))
    if ff:
        path = path.with_suffix(".mp3")
    return path
