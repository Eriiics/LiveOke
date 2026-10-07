"""Efectos de la cadena.

Todos los efectos comparten la misma interfaz (Effect) para que la interfaz
pueda generar los controles sola y para guardarlos/cargarlos en JSON.
Para agregar un efecto nuevo: crear una subclase, definir `type_id`,
`display_name`, `category` y `params_spec`, implementar `process` y
decorarla con @register.
"""
from __future__ import annotations

import base64
import re
import threading
from dataclasses import dataclass, field
from typing import Any, ClassVar

import numpy as np

from .music import NOTE_NAMES, Key

try:
    import pedalboard as pb
except Exception:  # pragma: no cover
    pb = None


# ---------------------------------------------------------------------------
# Parámetros y contexto
# ---------------------------------------------------------------------------
@dataclass
class Param:
    id: str
    label: str
    kind: str = "float"          # float | choice | bool | source
    min: float = 0.0
    max: float = 1.0
    default: Any = 0.0
    unit: str = ""
    choices: list[str] = field(default_factory=list)
    log: bool = False            # slider logarítmico (frecuencias, tiempos)
    decimals: int = 1


@dataclass
class ProcessContext:
    sr: int
    frames: int
    sources: dict[str, np.ndarray]      # nombre de canal -> audio crudo (frames, 2)
    key: Key | None = None               # tonalidad detectada o fijada
    tempo: float | None = None


REGISTRY: dict[str, type["Effect"]] = {}


def register(cls):
    REGISTRY[cls.type_id] = cls
    return cls


class Effect:
    type_id: ClassVar[str] = "base"
    display_name: ClassVar[str] = "Efecto"
    category: ClassVar[str] = "Otros"
    params_spec: ClassVar[list[Param]] = []

    def __init__(self, sr: int):
        self.sr = sr
        self.enabled = True
        self.values: dict[str, Any] = {p.id: p.default for p in self.params_spec}
        self.status = ""  # texto informativo para la UI

    # -- parámetros -------------------------------------------------------
    def specs(self) -> list[Param]:
        return list(self.params_spec)

    def set(self, pid: str, value: Any):
        self.values[pid] = value
        self.on_param(pid, value)

    def get(self, pid: str) -> Any:
        return self.values.get(pid)

    def on_param(self, pid: str, value: Any):
        pass

    # -- audio ------------------------------------------------------------
    def process(self, x: np.ndarray, ctx: ProcessContext) -> np.ndarray:
        return x

    def reset(self):
        pass

    def on_key(self, key: Key | None):
        """Se llama (desde la UI) cuando cambia la tonalidad detectada."""

    def set_sample_rate(self, sr: int):
        self.sr = sr
        self.reset()

    # -- serialización ----------------------------------------------------
    def to_dict(self) -> dict:
        return {"type": self.type_id, "enabled": self.enabled, "values": dict(self.values)}

    @classmethod
    def from_dict(cls, d: dict, sr: int) -> "Effect":
        t = d.get("type")
        if t == VSTEffect.type_id:
            fx = VSTEffect(sr, d.get("path", ""), d.get("plugin_name"))
        else:
            fx = REGISTRY[t](sr)
        fx.enabled = d.get("enabled", True)
        for k, v in d.get("values", {}).items():
            if k in fx.values:
                fx.set(k, v)
        fx.after_load(d)
        return fx

    def after_load(self, d: dict):
        pass

    @property
    def title(self) -> str:
        return self.display_name


def db_to_lin(db: float) -> float:
    return float(10 ** (db / 20.0))


def lin_to_db(x: float) -> float:
    return float(20 * np.log10(max(x, 1e-9)))


# ---------------------------------------------------------------------------
# Efectos basados en pedalboard (DSP en C++, rápidos)
# ---------------------------------------------------------------------------
class PedalboardEffect(Effect):
    def __init__(self, sr: int):
        super().__init__(sr)
        self.board = self.build()
        self.apply_all()

    def build(self):
        raise NotImplementedError

    def apply_all(self):
        for p in self.params_spec:
            self.on_param(p.id, self.values[p.id])

    def process(self, x, ctx):
        y = self.board.process(np.ascontiguousarray(x.T), self.sr, buffer_size=len(x), reset=False)
        y = np.ascontiguousarray(y.T)
        if len(y) != len(x):  # algunos procesos internos tienen latencia variable
            self._fifo = np.concatenate([getattr(self, "_fifo", np.zeros((0, 2), np.float32)), y])
            if len(self._fifo) >= len(x):
                y, self._fifo = self._fifo[: len(x)], self._fifo[len(x):]
            else:
                y = np.zeros_like(x)
        return y

    def reset(self):
        self.board.reset()
        self._fifo = np.zeros((0, 2), np.float32)


@register
class GainFx(PedalboardEffect):
    type_id = "gain"
    display_name = "Ganancia"
    category = "Utilidad"
    params_spec = [Param("gain_db", "Ganancia", "float", -24, 24, 0, "dB")]

    def build(self):
        return pb.Pedalboard([pb.Gain(0)])

    def on_param(self, pid, v):
        self.board[0].gain_db = float(v)


@register
class GateFx(PedalboardEffect):
    type_id = "gate"
    display_name = "Puerta de ruido"
    category = "Dinámica"
    params_spec = [
        Param("threshold_db", "Umbral", "float", -90, 0, -50, "dB"),
        Param("ratio", "Ratio", "float", 1, 30, 10, ":1"),
        Param("attack_ms", "Ataque", "float", 0.1, 50, 1, "ms", log=True),
        Param("release_ms", "Release", "float", 5, 1000, 120, "ms", log=True),
    ]

    def build(self):
        return pb.Pedalboard([pb.NoiseGate()])

    def on_param(self, pid, v):
        setattr(self.board[0], pid, float(v))


@register
class CompressorFx(PedalboardEffect):
    type_id = "compressor"
    display_name = "Compresor"
    category = "Dinámica"
    params_spec = [
        Param("threshold_db", "Umbral", "float", -60, 0, -18, "dB"),
        Param("ratio", "Ratio", "float", 1, 20, 3, ":1"),
        Param("attack_ms", "Ataque", "float", 0.1, 200, 5, "ms", log=True),
        Param("release_ms", "Release", "float", 10, 1000, 120, "ms", log=True),
        Param("makeup_db", "Makeup", "float", 0, 24, 4, "dB"),
    ]

    def build(self):
        return pb.Pedalboard([pb.Compressor(), pb.Gain(0)])

    def on_param(self, pid, v):
        if pid == "makeup_db":
            self.board[1].gain_db = float(v)
        else:
            setattr(self.board[0], pid, float(v))


@register
class EqFx(PedalboardEffect):
    type_id = "eq"
    display_name = "EQ 4 bandas"
    category = "Tono"
    params_spec = [
        Param("hpf", "Pasa altos", "float", 20, 400, 80, "Hz", log=True, decimals=0),
        Param("low_f", "Graves frec", "float", 60, 500, 180, "Hz", log=True, decimals=0),
        Param("low_g", "Graves", "float", -15, 15, 0, "dB"),
        Param("mid_f", "Medios frec", "float", 300, 6000, 2500, "Hz", log=True, decimals=0),
        Param("mid_g", "Medios", "float", -15, 15, 0, "dB"),
        Param("mid_q", "Medios Q", "float", 0.3, 6, 1.0, ""),
        Param("high_f", "Agudos frec", "float", 2000, 16000, 9000, "Hz", log=True, decimals=0),
        Param("high_g", "Agudos", "float", -15, 15, 0, "dB"),
    ]

    def build(self):
        return pb.Pedalboard([pb.HighpassFilter(), pb.LowShelfFilter(), pb.PeakFilter(), pb.HighShelfFilter()])

    def on_param(self, pid, v):
        v = float(v)
        hp, lo, mid, hi = self.board
        {
            "hpf": lambda: setattr(hp, "cutoff_frequency_hz", v),
            "low_f": lambda: setattr(lo, "cutoff_frequency_hz", v),
            "low_g": lambda: setattr(lo, "gain_db", v),
            "mid_f": lambda: setattr(mid, "cutoff_frequency_hz", v),
            "mid_g": lambda: setattr(mid, "gain_db", v),
            "mid_q": lambda: setattr(mid, "q", v),
            "high_f": lambda: setattr(hi, "cutoff_frequency_hz", v),
            "high_g": lambda: setattr(hi, "gain_db", v),
        }[pid]()


@register
class SaturationFx(PedalboardEffect):
    type_id = "saturation"
    display_name = "Saturación"
    category = "Color"
    params_spec = [
        Param("drive_db", "Drive", "float", 0, 40, 8, "dB"),
        Param("mix", "Mezcla", "float", 0, 1, 0.3, "", decimals=2),
    ]

    def build(self):
        return pb.Pedalboard([pb.Distortion()])

    def on_param(self, pid, v):
        if pid == "drive_db":
            self.board[0].drive_db = float(v)

    def process(self, x, ctx):
        wet = super().process(x, ctx)
        wet *= db_to_lin(-self.values["drive_db"] * 0.5)  # compensar volumen
        m = float(self.values["mix"])
        return x * (1 - m) + wet * m


@register
class ChorusFx(PedalboardEffect):
    type_id = "chorus"
    display_name = "Chorus"
    category = "Modulación"
    params_spec = [
        Param("rate_hz", "Velocidad", "float", 0.05, 5, 0.8, "Hz", log=True, decimals=2),
        Param("depth", "Profundidad", "float", 0, 1, 0.25, "", decimals=2),
        Param("centre_delay_ms", "Retardo", "float", 1, 30, 7, "ms"),
        Param("feedback", "Feedback", "float", 0, 0.9, 0.0, "", decimals=2),
        Param("mix", "Mezcla", "float", 0, 1, 0.35, "", decimals=2),
    ]

    def build(self):
        return pb.Pedalboard([pb.Chorus()])

    def on_param(self, pid, v):
        setattr(self.board[0], pid, float(v))


@register
class ReverbFx(PedalboardEffect):
    type_id = "reverb"
    display_name = "Reverb"
    category = "Espacio"
    params_spec = [
        Param("room_size", "Tamaño", "float", 0, 1, 0.6, "", decimals=2),
        Param("damping", "Amortiguación", "float", 0, 1, 0.5, "", decimals=2),
        Param("width", "Ancho", "float", 0, 1, 1.0, "", decimals=2),
        Param("wet_level", "Húmedo", "float", 0, 1, 1.0, "", decimals=2),
        Param("dry_level", "Seco", "float", 0, 1, 0.0, "", decimals=2),
        Param("predelay_ms", "Pre-delay", "float", 0, 150, 20, "ms"),
    ]

    def build(self):
        return pb.Pedalboard([pb.Delay(delay_seconds=0.02, feedback=0.0, mix=1.0), pb.Reverb()])

    def on_param(self, pid, v):
        if pid == "predelay_ms":
            self.board[0].delay_seconds = max(float(v), 0.0) / 1000.0
        else:
            setattr(self.board[1], pid, float(v))

    def process(self, x, ctx):
        # El pre-delay solo debe afectar a la parte húmeda
        dry = float(self.values["dry_level"])
        self.board[1].dry_level = 0.0
        y = super().process(x, ctx)
        return y + x * dry


@register
class DelayFx(PedalboardEffect):
    type_id = "delay"
    display_name = "Delay"
    category = "Espacio"
    params_spec = [
        Param("time_ms", "Tiempo", "float", 10, 2000, 375, "ms", log=True, decimals=0),
        Param("feedback", "Repeticiones", "float", 0, 0.95, 0.35, "", decimals=2),
        Param("mix", "Mezcla", "float", 0, 1, 1.0, "", decimals=2),
        Param("hpf", "Corte graves", "float", 20, 1000, 200, "Hz", log=True, decimals=0),
        Param("lpf", "Corte agudos", "float", 1000, 20000, 6000, "Hz", log=True, decimals=0),
    ]

    def build(self):
        return pb.Pedalboard([pb.HighpassFilter(), pb.LowpassFilter(), pb.Delay()])

    def on_param(self, pid, v):
        v = float(v)
        if pid == "time_ms":
            self.board[2].delay_seconds = v / 1000.0
        elif pid == "feedback":
            self.board[2].feedback = v
        elif pid == "mix":
            self.board[2].mix = v
        elif pid == "hpf":
            self.board[0].cutoff_frequency_hz = v
        elif pid == "lpf":
            self.board[1].cutoff_frequency_hz = v


@register
class LimiterFx(PedalboardEffect):
    type_id = "limiter"
    display_name = "Limitador"
    category = "Dinámica"
    params_spec = [
        Param("threshold_db", "Techo", "float", -24, 0, -1, "dB"),
        Param("release_ms", "Release", "float", 10, 1000, 100, "ms", log=True),
    ]

    def build(self):
        return pb.Pedalboard([pb.Limiter()])

    def on_param(self, pid, v):
        setattr(self.board[0], pid, float(v))


# ---------------------------------------------------------------------------
# Sidechain: ducker
# ---------------------------------------------------------------------------
@register
class DuckerFx(Effect):
    """Baja el volumen de ESTE canal cuando suena la fuente elegida.

    Ej.: en el canal PC con fuente=Mic -> la instrumental baja cuando cantas.
         en el bus Reverb con fuente=Mic -> la reverb se abre en los silencios.
    """
    type_id = "ducker"
    display_name = "Sidechain (ducker)"
    category = "Dinámica"
    params_spec = [
        Param("source", "Fuente", "source", default="Mic"),
        Param("threshold_db", "Umbral", "float", -70, 0, -40, "dB"),
        Param("depth_db", "Reducción", "float", 0, 40, 10, "dB"),
        Param("attack_ms", "Ataque", "float", 1, 300, 15, "ms", log=True),
        Param("release_ms", "Release", "float", 20, 3000, 300, "ms", log=True),
    ]

    def __init__(self, sr):
        super().__init__(sr)
        self._red_db = 0.0
        self._gain = 1.0

    def reset(self):
        self._red_db = 0.0
        self._gain = 1.0

    def process(self, x, ctx):
        key = ctx.sources.get(self.values["source"])
        if key is None:
            self.status = "fuente no disponible"
            return x
        level = lin_to_db(float(np.sqrt(np.mean(key ** 2))))
        over = level - float(self.values["threshold_db"])
        target = float(self.values["depth_db"]) * float(np.clip(over / 6.0, 0.0, 1.0))  # rodilla suave de 6 dB
        block_s = len(x) / self.sr
        tau = (self.values["attack_ms"] if target > self._red_db else self.values["release_ms"]) / 1000.0
        a = 1.0 - np.exp(-block_s / max(tau, 1e-4))
        self._red_db += (target - self._red_db) * a
        g = db_to_lin(-self._red_db)
        ramp = np.linspace(self._gain, g, len(x), dtype=np.float32)[:, None]
        self._gain = g
        self.status = f"-{self._red_db:.1f} dB"
        return x * ramp


# ---------------------------------------------------------------------------
# Plugins VST3 externos
# ---------------------------------------------------------------------------
_KEY_PARAM_RE = re.compile(r"\b(key|root|tonic|tonality|nota|tono)\b", re.I)
_SCALE_PARAM_RE = re.compile(r"\b(scale|mode|escala)\b", re.I)
_AUTOTUNE_NAME_RE = re.compile(r"auto.?tune|autopitch|graillon|melodyne|pitch.?correct|retune|antares|melda|waves tune", re.I)


class VSTEffect(Effect):
    type_id = "vst3"
    display_name = "Plugin VST3"
    category = "Plugins"

    def __init__(self, sr: int, path: str, plugin_name: str | None = None):
        super().__init__(sr)
        self.path = path
        self.plugin_name = plugin_name
        self.plugin = None
        self.error = ""
        self.values = {"auto_key": True}
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        if pb is None:
            self.error = "pedalboard no está instalado"
            return
        try:
            kw = {"plugin_name": self.plugin_name} if self.plugin_name else {}
            self.plugin = pb.load_plugin(self.path, **kw)
        except Exception as e:  # el plugin no carga: la cadena sigue funcionando
            self.error = str(e)
            self.plugin = None

    @property
    def title(self) -> str:
        if self.plugin is not None:
            return self.plugin.name
        return f"VST (error) {self.path.rsplit('/', 1)[-1].rsplit(chr(92), 1)[-1]}"

    @property
    def looks_like_autotune(self) -> bool:
        if self.plugin is None:
            return False
        return bool(_AUTOTUNE_NAME_RE.search(f"{self.plugin.name} {getattr(self.plugin, 'manufacturer_name', '')}"))

    def specs(self) -> list[Param]:
        return [Param("auto_key", "Ajustar tonalidad automáticamente", "bool", default=True)]

    def raw_params(self):
        """Parámetros nativos del plugin (rápido, sin la introspección lenta de pedalboard)."""
        if self.plugin is None:
            return []
        out = []
        for p in self.plugin._parameters:
            if not p.name or p.name.lower().startswith(("midi cc", "bypass")):
                continue
            out.append(p)
        return out

    def process(self, x, ctx):
        if self.plugin is None:
            return x
        y = self.plugin.process(np.ascontiguousarray(x.T), self.sr, buffer_size=len(x), reset=False)
        if y.shape[0] == 1:
            y = np.repeat(y, 2, axis=0)
        return np.ascontiguousarray(y[:2].T)

    def reset(self):
        if self.plugin is not None:
            self.plugin.reset()

    def show_editor(self):
        if self.plugin is not None:
            self.plugin.show_editor()

    # -- tonalidad automática --------------------------------------------
    def on_key(self, key: Key | None):
        if key is None or self.plugin is None or not self.values.get("auto_key", True):
            return
        msgs = []
        for p in self.raw_params():
            if _KEY_PARAM_RE.search(p.name) and self._set_note(p, key.root):
                msgs.append(f"{p.name}={p.string_value}")
            elif _SCALE_PARAM_RE.search(p.name) and self._set_scale(p, key.mode):
                msgs.append(f"{p.name}={p.string_value}")
        self.status = ("Tonalidad -> " + ", ".join(msgs)) if msgs else "no encontré parámetros de tonalidad"

    @staticmethod
    def _candidates(p, steps: int = 240):
        n = p.num_steps if 1 < p.num_steps <= 512 else steps
        for i in range(n):
            raw = i / (n - 1)
            try:
                yield raw, (p.get_text_for_raw_value(raw) or "").strip()
            except Exception:
                return

    def _set_note(self, p, root: int) -> bool:
        sharp = NOTE_NAMES[root]
        flats = {"C#": "Db", "D#": "Eb", "F#": "Gb", "G#": "Ab", "A#": "Bb"}
        wanted = {sharp.lower(), flats.get(sharp, sharp).lower()}
        for raw, text in self._candidates(p):
            parts = {t.strip().lower() for t in re.split(r"[ /|,]+", text) if t.strip()}
            if parts & wanted and not any(len(t) > 2 and t not in ("major", "minor", "maj", "min") for t in parts):
                p.raw_value = raw
                return True
        return False

    def _set_scale(self, p, mode: str) -> bool:
        for raw, text in self._candidates(p):
            t = text.lower()
            if (mode == "major" and ("major" in t or "mayor" in t) and "minor" not in t) or (
                mode == "minor" and ("minor" in t or "menor" in t) and "harm" not in t and "mel" not in t
            ):
                p.raw_value = raw
                return True
        return False

    # -- serialización ----------------------------------------------------
    def to_dict(self) -> dict:
        d = super().to_dict()
        d["path"] = self.path
        if self.plugin_name:
            d["plugin_name"] = self.plugin_name
        if self.plugin is not None:
            try:
                d["state"] = base64.b64encode(self.plugin.raw_state).decode("ascii")
            except Exception:
                pass
        return d

    def after_load(self, d: dict):
        if self.plugin is not None and d.get("state"):
            try:
                self.plugin.raw_state = base64.b64decode(d["state"])
            except Exception as e:
                self.status = f"no pude restaurar el estado: {e}"


def create(type_id: str, sr: int) -> Effect:
    return REGISTRY[type_id](sr)


def available_types() -> dict[str, list[type[Effect]]]:
    cats: dict[str, list[type[Effect]]] = {}
    for cls in REGISTRY.values():
        cats.setdefault(cls.category, []).append(cls)
    return cats
