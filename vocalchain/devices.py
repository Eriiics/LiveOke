"""Listado de dispositivos de audio.

- sounddevice (PortAudio): micrófono, salidas y entradas tipo "CABLE Output".
  En Windows se carga la versión de PortAudio CON ASIO (Focusrite, etc.).
- soundcard (WASAPI): captura loopback = "lo que suena en el PC" sin cables virtuales.
Los dispositivos se guardan por nombre + API (los índices cambian entre reinicios).
"""
from __future__ import annotations

import importlib.util
import os
import sys
import threading
from dataclasses import dataclass

# --- sounddevice con ASIO ----------------------------------------------------
# sounddevice trae en Windows un PortAudio con ASIO; se activa con esta variable
# ANTES de importarlo. Si por algún motivo falla, se carga el normal.
ASIO_ENABLED = False
if sys.platform == "win32":
    os.environ.setdefault("SD_ENABLE_ASIO", "1")
try:
    import sounddevice as sd
    ASIO_ENABLED = "SD_ENABLE_ASIO" in os.environ
    SD_ERROR = ""
except Exception as e:  # pragma: no cover
    sd = None
    SD_ERROR = str(e)
    if os.environ.pop("SD_ENABLE_ASIO", None) is not None:
        try:
            import sounddevice as sd
            SD_ERROR = ""
        except Exception as e2:
            sd = None
            SD_ERROR = str(e2)

# soundcard se importa SOLO en hilos de fondo: al importarse inicializa COM en modo
# multihilo y eso choca con Qt (que necesita el hilo principal en modo STA para
# arrastrar y soltar, diálogos de archivos, etc.).
SC_AVAILABLE = importlib.util.find_spec("soundcard") is not None
SC_ERROR = "" if SC_AVAILABLE else "pip install soundcard"


def _sc():
    import soundcard  # noqa: PLC0415
    return soundcard


def run_in_worker(fn, timeout=10.0):
    """Ejecuta fn() en un hilo aparte (apartamento COM propio) y devuelve su resultado."""
    box = {}

    def target():
        try:
            box["v"] = fn()
        except Exception as e:  # pragma: no cover
            box["e"] = e

    th = threading.Thread(target=target, daemon=True)
    th.start()
    th.join(timeout)
    if "e" in box:
        raise box["e"]
    return box.get("v")


# --- APIs de audio (drivers) -------------------------------------------------
ASIO = "ASIO"
WASAPI = "Windows WASAPI"
API_ORDER = [ASIO, WASAPI, "Windows WDM-KS", "Windows DirectSound", "MME",
             "Core Audio", "ALSA", "PulseAudio", "JACK Audio Connection Kit"]
API_LABELS = {
    ASIO: "ASIO — interfaz de audio (Focusrite, etc.), la menor latencia",
    WASAPI: "WASAPI — Windows moderno, buena latencia",
    "Windows WDM-KS": "WDM-KS — baja latencia, a veces inestable",
    "Windows DirectSound": "DirectSound — compatibilidad",
    "MME": "MME — compatibilidad (latencia alta)",
}


@dataclass(frozen=True)
class DeviceRef:
    name: str
    api: str = ""
    kind: str = "input"   # input | output | loopback

    @property
    def label(self) -> str:
        if self.kind == "loopback":
            return f"🔁 {self.name} (lo que suena)"
        return f"{self.name}  [{self.api.replace('Windows ', '')}]" if self.api else self.name

    @property
    def is_asio(self) -> bool:
        return self.api == ASIO

    def to_dict(self):
        return {"name": self.name, "api": self.api, "kind": self.kind}

    @staticmethod
    def from_dict(d):
        return DeviceRef(d.get("name", ""), d.get("api", ""), d.get("kind", "input")) if d else None


def _api_name(idx: int) -> str:
    return sd.query_hostapis(idx)["name"]


def _rank(api: str) -> int:
    try:
        return API_ORDER.index(api)
    except ValueError:
        return len(API_ORDER)


def _sorted(devs):
    return sorted(devs, key=lambda d: (_rank(d.api), d.name.lower()))


def list_apis() -> list[str]:
    """APIs que tienen al menos un dispositivo, en orden de preferencia (ASIO primero)."""
    if sd is None:
        return []
    apis = set()
    for d in sd.query_devices():
        if d["max_input_channels"] > 0 or d["max_output_channels"] > 0:
            apis.add(_api_name(d["hostapi"]))
    return sorted(apis, key=_rank)


def _list(kind_key: str, kind: str, api: str | None) -> list[DeviceRef]:
    if sd is None:
        return []
    out, seen = [], set()
    for d in sd.query_devices():
        if d[kind_key] <= 0:
            continue
        a = _api_name(d["hostapi"])
        if api is not None and a != api:
            continue
        if (d["name"], a) in seen:
            continue
        seen.add((d["name"], a))
        out.append(DeviceRef(d["name"], a, kind))
    return _sorted(out)


def list_inputs(api: str | None = None) -> list[DeviceRef]:
    return _list("max_input_channels", "input", api)


def list_outputs(api: str | None = None) -> list[DeviceRef]:
    return _list("max_output_channels", "output", api)


def windows_api() -> str | None:
    """API "normal" para cosas que no pueden ir por ASIO (cables virtuales, Stream)."""
    apis = list_apis()
    for a in (WASAPI, "Windows DirectSound", "MME"):
        if a in apis:
            return a
    non_asio = [a for a in apis if a != ASIO]
    return non_asio[0] if non_asio else None


def input_channels(ref: DeviceRef) -> int:
    idx = find_index(ref, "input")
    if idx is None:
        return 0
    return int(sd.query_devices(idx)["max_input_channels"])


def list_loopbacks() -> list[DeviceRef]:
    if not SC_AVAILABLE:
        return []
    try:
        return run_in_worker(lambda: [DeviceRef(s.name, "WASAPI loopback", "loopback") for s in _sc().all_speakers()]) or []
    except Exception:
        return []


def default_loopback() -> DeviceRef | None:
    if not SC_AVAILABLE:
        return None
    try:
        return run_in_worker(lambda: DeviceRef(_sc().default_speaker().name, "WASAPI loopback", "loopback"))
    except Exception:
        return None


def _default(kind: str) -> DeviceRef | None:
    """Dispositivo predeterminado de Windows, preferentemente en WASAPI."""
    if sd is None:
        return None
    key = "default_input_device" if kind == "input" else "default_output_device"
    try:
        for i, h in enumerate(sd.query_hostapis()):
            if h["name"] == WASAPI and h[key] >= 0:
                d = sd.query_devices(h[key])
                return DeviceRef(d["name"], WASAPI, kind)
        i = sd.default.device[0 if kind == "input" else 1]
        d = sd.query_devices(i)
        return DeviceRef(d["name"], _api_name(d["hostapi"]), kind)
    except Exception:
        return None


def default_input() -> DeviceRef | None:
    return _default("input")


def default_output() -> DeviceRef | None:
    return _default("output")


def find_index(ref: DeviceRef, kind: str) -> int | None:
    """Índice de sounddevice para una referencia guardada (mismo nombre y API, o solo nombre)."""
    if sd is None or ref is None:
        return None
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    fallback = None
    for i, d in enumerate(sd.query_devices()):
        if d[key] <= 0:
            continue
        if d["name"] == ref.name:
            if _api_name(d["hostapi"]) == ref.api:
                return i
            fallback = fallback if fallback is not None else i
    return fallback


_VIRTUAL_HINTS = ("cable", "virtual", "voicemeeter", "vb-audio", "vac ", "blackhole", "obs")


def is_virtual(name: str) -> bool:
    """¿Es un cable virtual (nadie lo escucha directo)? Ej. 'CABLE Input (VB-Audio…)'."""
    n = (name or "").lower()
    return any(h in n for h in _VIRTUAL_HINTS)


def mix_rate(speaker_name: str) -> int | None:
    """Frecuencia a la que Windows usa ese dispositivo (Sonido → Propiedades → Opciones avanzadas)."""
    if not SC_AVAILABLE or not speaker_name:
        return None

    def get():
        sc = _sc()
        from soundcard import mediafoundation as mf  # noqa: PLC0415
        spk = sc.get_speaker(speaker_name)
        client = spk._audio_client()
        pp = mf._ffi.new("WAVEFORMATEXTENSIBLE**")
        try:
            mf._com.check_error(client[0][0].lpVtbl.GetMixFormat(client[0], pp))
            rate = int(pp[0][0].Format.nSamplesPerSec)
            mf._ole32.CoTaskMemFree(pp[0])
            return rate
        finally:
            mf._com.release(client)

    try:
        return run_in_worker(get, timeout=5.0)
    except Exception:
        return None


def same_physical(a: str, b: str) -> bool:
    """¿Dos nombres se refieren al mismo dispositivo? (MME recorta los nombres a 31 caracteres)."""
    a, b = a.lower().strip(), b.lower().strip()
    return bool(a and b) and (a == b or a.startswith(b[:31]) or b.startswith(a[:31]))
