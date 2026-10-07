"""Volumen de Windows (el mismo de la barra de tareas) para un dispositivo de salida.

Todo lo COM vive en un hilo propio: comtypes inicializa COM al importarse y no
queremos tocar el hilo de Qt ni el de audio. La interfaz solo lee valores en
caché y encola cambios, así que nunca se bloquea.
"""
from __future__ import annotations

import importlib.util
import math
import queue
import threading
import time

AVAILABLE = importlib.util.find_spec("pycaw") is not None and importlib.util.find_spec("comtypes") is not None


def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())


class SystemVolume:
    def __init__(self, device_name: str | None = None):
        self.device_name = device_name      # None = salida predeterminada de Windows
        self.found_name = ""
        self.scalar: float | None = None     # 0..1 como el % de Windows
        self.level_db: float | None = None
        self.muted = False
        self.min_db, self.max_db = -65.25, 0.0
        self.error = "" if AVAILABLE else "Falta pycaw (pip install pycaw)"
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._last_user_change = 0.0
        self._thread = None
        if AVAILABLE:
            self._thread = threading.Thread(target=self._run, daemon=True, name="winvolume")
            self._thread.start()

    @property
    def ready(self) -> bool:
        return self.scalar is not None

    # -- API para la interfaz (no bloquea) ---------------------------------
    def set_db(self, db: float):
        """Fija el volumen en dB del dispositivo (0 dB = 100%). -inf o muy bajo = silencio."""
        self._last_user_change = time.monotonic()
        self._q.put(("db", db))

    def set_mute(self, on: bool):
        self._last_user_change = time.monotonic()
        self._q.put(("mute", bool(on)))

    def user_recently_changed(self, seconds=0.6) -> bool:
        return time.monotonic() - self._last_user_change < seconds

    def stop(self):
        self._stop.set()
        self._q.put(("stop", None))

    # -- hilo COM ----------------------------------------------------------
    def _find(self, AudioUtilities):
        if self.device_name:
            want = _norm(self.device_name)
            try:
                from pycaw.constants import EDataFlow, DEVICE_STATE
                devs = AudioUtilities.GetAllDevices(EDataFlow.eRender.value, DEVICE_STATE.ACTIVE.value)
            except Exception:
                devs = AudioUtilities.GetAllDevices()
            for d in devs:
                name = _norm(str(d.FriendlyName or ""))
                if name and (name == want or name.startswith(want[:31]) or want.startswith(name[:31])):
                    return d
        return AudioUtilities.GetSpeakers()

    def _run(self):
        try:
            import comtypes
            comtypes.CoInitialize()
        except Exception:
            pass
        try:
            from pycaw.pycaw import AudioUtilities
            dev = self._find(AudioUtilities)
            if dev is None:
                self.error = "No encontré el dispositivo de salida de Windows"
                return
            ep = getattr(dev, "EndpointVolume", None)
            if ep is None:  # pycaw antiguo: GetSpeakers devuelve un IMMDevice
                from comtypes import CLSCTX_ALL
                from pycaw.pycaw import IAudioEndpointVolume
                ep = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None).QueryInterface(IAudioEndpointVolume)
                self.found_name = self.device_name or "Salida predeterminada"
            else:
                self.found_name = str(dev.FriendlyName or self.device_name or "")
            try:
                rng = ep.GetVolumeRange()
                self.min_db, self.max_db = float(rng[0]), float(rng[1])
            except Exception:
                pass
        except Exception as e:
            self.error = f"Volumen de Windows no disponible: {e}"
            return

        while not self._stop.is_set():
            # aplicar solo el último cambio pendiente (el fader manda muchos)
            last = {}
            try:
                item = self._q.get(timeout=0.25)
                while True:
                    kind, val = item
                    last[kind] = val
                    item = self._q.get_nowait()
            except queue.Empty:
                pass
            if "stop" in last:
                break
            try:
                if "db" in last:
                    db = last["db"]
                    if db is None or db == -math.inf or db <= self.min_db + 0.5:
                        ep.SetMasterVolumeLevelScalar(0.0, None)
                    else:
                        ep.SetMasterVolumeLevel(float(min(max(db, self.min_db), self.max_db)), None)
                if "mute" in last:
                    ep.SetMute(1 if last["mute"] else 0, None)
                self.scalar = float(ep.GetMasterVolumeLevelScalar())
                self.level_db = float(ep.GetMasterVolumeLevel())
                self.muted = bool(ep.GetMute())
                self.error = ""
            except Exception as e:
                self.error = f"Error de volumen de Windows: {e}"
                time.sleep(1.0)


# ---------------------------------------------------------------------------
# Salida predeterminada de Windows (para mandar la música por VB-Cable)
# ---------------------------------------------------------------------------
def _com_call(fn, timeout=8.0):
    """Ejecuta fn en un hilo con COM propio (nunca en el hilo de Qt)."""
    box = {}

    def target():
        try:
            import comtypes
            comtypes.CoInitialize()
        except Exception:
            comtypes = None
        try:
            box["v"] = fn()
        except Exception as e:
            box["e"] = e
        finally:
            try:
                if comtypes:
                    comtypes.CoUninitialize()
            except Exception:
                pass

    th = threading.Thread(target=target, daemon=True)
    th.start()
    th.join(timeout)
    if "e" in box:
        raise box["e"]
    return box.get("v")


def list_render_devices() -> list[tuple[str, str]]:
    """[(id, nombre)] de las salidas activas de Windows."""
    if not AVAILABLE:
        return []

    def go():
        from pycaw.constants import DEVICE_STATE, EDataFlow
        from pycaw.pycaw import AudioUtilities
        return [(d.id, str(d.FriendlyName or "")) for d in
                AudioUtilities.GetAllDevices(EDataFlow.eRender.value, DEVICE_STATE.ACTIVE.value)]
    return _com_call(go) or []


def get_default_render() -> tuple[str, str] | None:
    if not AVAILABLE:
        return None

    def go():
        from pycaw.pycaw import AudioUtilities
        d = AudioUtilities.GetSpeakers()
        if d is None:
            return None
        if hasattr(d, "FriendlyName"):
            return (d.id, str(d.FriendlyName or ""))
        return (d.GetId(), "")
    return _com_call(go)


def set_default_render(dev_id: str):
    """Cambia la salida predeterminada de Windows (multimedia y consola)."""
    def go():
        from pycaw.constants import ERole
        from pycaw.pycaw import AudioUtilities
        AudioUtilities.SetDefaultDevice(dev_id, [ERole.eConsole, ERole.eMultimedia])
    _com_call(go)


def find_render(name_part: str) -> tuple[str, str] | None:
    n = (name_part or "").lower()
    for dev_id, name in list_render_devices():
        if n in name.lower():
            return dev_id, name
    return None
