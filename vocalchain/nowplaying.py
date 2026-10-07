"""¿Qué está sonando? Lee los controles multimedia de Windows (los mismos del
panel de volumen): Spotify, YouTube en Chrome/Edge, Apple Music, etc.

Requiere:  pip install winrt-runtime winrt-Windows.Media.Control winrt-Windows.Foundation winrt-Windows.Storage.Streams
(ya están en requirements.txt). En otros sistemas queda en modo manual.
"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Igual que soundcard, winrt inicializa COM (MTA) al importarse: se importa solo
# dentro del hilo del observador para no romper el hilo principal de Qt.
import importlib.util

MediaManager = None
_BACKEND_ERROR = ""


def _has_backend() -> bool:
    for top in ("winrt", "winsdk"):
        if importlib.util.find_spec(top) is not None:
            return True
    return False


def _load_backend():
    global MediaManager, _BACKEND_ERROR
    if MediaManager is not None:
        return True
    try:
        from winrt.windows.media.control import (  # type: ignore
            GlobalSystemMediaTransportControlsSessionManager as MM,
        )
    except Exception as e:
        try:
            from winsdk.windows.media.control import (  # type: ignore
                GlobalSystemMediaTransportControlsSessionManager as MM,
            )
        except Exception:
            _BACKEND_ERROR = str(e)
            return False
    MediaManager = MM
    return True


def backend_available() -> bool:
    return _has_backend()


@dataclass
class TrackInfo:
    title: str = ""
    artist: str = ""
    album: str = ""
    duration: float | None = None      # segundos
    position: float | None = None      # segundos, al momento de `stamp`
    playing: bool = False
    app: str = ""
    stamp: float = field(default_factory=time.monotonic)
    thumbnail: bytes | None = None
    manual: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return (self.artist.strip().lower(), self.title.strip().lower())

    def current_position(self) -> float | None:
        if self.position is None:
            return None
        if self.playing:
            return self.position + (time.monotonic() - self.stamp)
        return self.position


def _seconds(v) -> float | None:
    if v is None:
        return None
    if hasattr(v, "total_seconds"):
        return v.total_seconds()
    if hasattr(v, "duration"):          # TimeSpan antiguo (unidades de 100 ns)
        return v.duration / 1e7
    try:
        return float(v)
    except Exception:
        return None


def _age_seconds(dt) -> float:
    """Cuánto tiempo pasó desde `last_updated_time`."""
    try:
        if isinstance(dt, datetime):
            now = datetime.now(timezone.utc) if dt.tzinfo else datetime.now()
            return max(0.0, (now - dt).total_seconds())
        if hasattr(dt, "universal_time"):  # 100 ns desde 1601
            epoch_1601 = datetime(1601, 1, 1, tzinfo=timezone.utc)
            t = epoch_1601.timestamp() + dt.universal_time / 1e7
            return max(0.0, time.time() - t)
    except Exception:
        pass
    return 0.0


async def _read_thumbnail(props) -> bytes | None:
    try:
        ref = props.thumbnail
        if ref is None:
            return None
        from winrt.windows.storage.streams import Buffer, InputStreamOptions  # type: ignore
        stream = await ref.open_read_async()
        size = int(stream.size)
        if size <= 0 or size > 5_000_000:
            return None
        buf = Buffer(size)
        await stream.read_async(buf, size, InputStreamOptions.READ_AHEAD)
        return bytes(memoryview(buf))[: buf.length]
    except Exception:
        return None


async def _fetch(want_thumb_for: tuple | None) -> TrackInfo | None:
    mgr = await MediaManager.request_async()
    s = mgr.get_current_session()
    if s is None:
        return None
    props = await s.try_get_media_properties_async()
    info = TrackInfo(
        title=props.title or "",
        artist=props.artist or props.album_artist or "",
        album=props.album_title or "",
        app=getattr(s, "source_app_user_model_id", "") or "",
    )
    try:
        pb = s.get_playback_info()
        info.playing = int(pb.playback_status) == 4  # 4 = Playing
    except Exception:
        info.playing = True
    try:
        tl = s.get_timeline_properties()
        end = _seconds(tl.end_time)
        start = _seconds(tl.start_time) or 0.0
        info.duration = (end - start) if end else None
        pos = _seconds(tl.position)
        if pos is not None:
            if info.playing:
                pos += _age_seconds(tl.last_updated_time)
            info.position = pos - start
    except Exception:
        pass
    if want_thumb_for != info.key:
        info.thumbnail = await _read_thumbnail(props)
    return info


class NowPlayingWatcher:
    """Consulta cada `interval` s. `on_change(track)` se llama al cambiar de canción,
    `on_update(track)` en cada lectura (para la posición). Ambos desde un hilo propio."""

    def __init__(self, on_change=None, on_update=None, interval: float = 1.0):
        self.on_change = on_change
        self.on_update = on_update
        self.interval = interval
        self.track: TrackInfo | None = None
        self.error = "" if backend_available() else "Detección automática de canción no disponible (falta winrt / solo Windows)"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._manual: TrackInfo | None = None

    def start(self):
        if self._thread or not backend_available():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def set_manual(self, artist: str, title: str):
        """Fijar la canción a mano (si la detección falla o el título viene sucio)."""
        self._manual = TrackInfo(title=title, artist=artist, manual=True, position=0.0, playing=True)
        self._publish(self._manual, force=True)

    def clear_manual(self):
        self._manual = None

    def _publish(self, info: TrackInfo | None, force=False):
        prev = self.track
        self.track = info
        changed = force or (info is not None and (prev is None or prev.key != info.key))
        if info is not None and prev is not None and info.thumbnail is None and prev.key == info.key:
            info.thumbnail = prev.thumbnail
        if changed and self.on_change:
            self.on_change(info)
        if self.on_update:
            self.on_update(info)

    def _run(self):
        if not _load_backend():
            self.error = f"No pude cargar los controles multimedia de Windows: {_BACKEND_ERROR}"
            return
        loop = asyncio.new_event_loop()
        while not self._stop.is_set():
            try:
                have = self.track.key if self.track and self.track.thumbnail else None
                info = loop.run_until_complete(_fetch(have))
                if info is not None and info.title:
                    self.error = ""
                    self._manual = None  # apareció una canción real: manda la detección
                    self._publish(info)
            except Exception as e:
                self.error = f"Error leyendo la canción: {e}"
            self._stop.wait(self.interval)
