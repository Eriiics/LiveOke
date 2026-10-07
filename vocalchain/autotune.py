"""Autotune propio: detección de pitch (YIN) + pitch shifter de baja latencia.

No es Antares, pero suena a autotune: con velocidad 0 da el efecto robot,
con 40-80 ms corrige de forma más natural. Si tienes un autotune VST
(MAutoPitch, Graillon, etc.) también funciona: la app le ajusta la tonalidad.
"""
from __future__ import annotations

import numpy as np

from .effects import Effect, Param, register
from .music import NOTE_NAMES, SCALES, Key


def yin_pitch(x: np.ndarray, sr: int, fmin: float = 70.0, fmax: float = 1100.0, threshold: float = 0.15):
    """Devuelve (frecuencia_hz, aperiodicidad) o (None, 1.0) si no hay nota clara."""
    n = len(x)
    tau_max = min(int(sr / fmin), n // 2)
    tau_min = max(int(sr / fmax), 2)
    w = n - tau_max
    if w <= tau_min:
        return None, 1.0
    x = x.astype(np.float64) - x.mean()
    # Función diferencia d(tau) = e0 + e_tau - 2*acf(tau), con FFT
    size = 1 << int(np.ceil(np.log2(n + w)))
    fx = np.fft.rfft(x, size)
    fw = np.fft.rfft(x[:w][::-1], size)
    conv = np.fft.irfft(fx * fw, size)
    acf = conv[w - 1: w - 1 + tau_max + 1]
    cs = np.concatenate([[0.0], np.cumsum(x * x)])
    e0 = cs[w]
    taus = np.arange(tau_max + 1)
    e_tau = cs[taus + w] - cs[taus]
    d = e0 + e_tau - 2 * acf
    d[0] = 0
    cum = np.cumsum(d[1:])
    cmndf = np.ones_like(d)
    cmndf[1:] = d[1:] * taus[1:] / np.maximum(cum, 1e-12)
    below = np.nonzero(cmndf[tau_min:tau_max] < threshold)[0]
    if len(below) == 0:
        return None, float(cmndf[tau_min:tau_max].min())
    t = below[0] + tau_min
    while t + 1 < tau_max and cmndf[t + 1] < cmndf[t]:
        t += 1
    # interpolación parabólica
    if 1 <= t < tau_max:
        a, b, c = cmndf[t - 1], cmndf[t], cmndf[t + 1]
        den = a - 2 * b + c
        shift = 0.5 * (a - c) / den if abs(den) > 1e-12 else 0.0
    else:
        shift = 0.0
    return sr / (t + shift), float(cmndf[t])


class DopplerShifter:
    """Pitch shifter de línea de retardo con dos lectores cruzados (estilo 'harmonizer').

    Latencia ~ window/2. Funciona por bloques y acepta un ratio que cambia
    suavemente dentro del bloque.
    """

    def __init__(self, sr: int, window_ms: float = 24.0):
        self.w = max(int(sr * window_ms / 1000.0), 64)
        self.size = 1 << int(np.ceil(np.log2(self.w * 4 + 8192)))
        self.buf = np.zeros(self.size, dtype=np.float32)
        self.wpos = 0
        self.phase = 0.0

    @property
    def latency(self) -> int:
        return self.w // 2

    def reset(self):
        self.buf[:] = 0
        self.phase = 0.0

    def _read(self, pos: np.ndarray) -> np.ndarray:
        i0 = np.floor(pos).astype(np.int64)
        frac = (pos - i0).astype(np.float32)
        a = self.buf[i0 & (self.size - 1)]
        b = self.buf[(i0 + 1) & (self.size - 1)]
        return a + (b - a) * frac

    def process(self, x: np.ndarray, ratio0: float, ratio1: float) -> tuple[np.ndarray, np.ndarray]:
        """Devuelve (señal_procesada, señal_seca_alineada)."""
        n = len(x)
        t = self.wpos + np.arange(n)
        self.buf[t & (self.size - 1)] = x
        ratios = np.linspace(ratio0, ratio1, n, dtype=np.float64)
        dphi = (1.0 - ratios) / self.w
        ph = (self.phase + np.concatenate([[0.0], np.cumsum(dphi[:-1])])) % 1.0
        self.phase = float((self.phase + dphi.sum()) % 1.0)
        ph2 = (ph + 0.5) % 1.0
        tap1 = self._read(t - ph * self.w)
        tap2 = self._read(t - ph2 * self.w)
        g1 = np.sin(np.pi * ph) ** 2
        wet = (tap1 * g1 + tap2 * (1.0 - g1)).astype(np.float32)
        dry = self._read(t - self.latency * np.ones(n))
        self.wpos = int((self.wpos + n) % (1 << 40))
        return wet, dry


KEY_CHOICES = ["Auto"] + NOTE_NAMES
SCALE_CHOICES = ["Auto", "Mayor", "Menor", "Cromática"]
_SCALE_MAP = {"Mayor": "major", "Menor": "minor", "Cromática": "chromatic"}


def midi_to_name(m: float) -> str:
    r = int(round(m))
    return f"{NOTE_NAMES[r % 12]}{r // 12 - 1}"


@register
class AutoTuneFx(Effect):
    type_id = "autotune"
    display_name = "Auto-Tune"
    category = "Tono"
    params_spec = [
        Param("key", "Tónica", "choice", default="Auto", choices=KEY_CHOICES),
        Param("scale", "Escala", "choice", default="Auto", choices=SCALE_CHOICES),
        Param("speed_ms", "Velocidad", "float", 0, 250, 25, "ms"),
        Param("amount", "Intensidad", "float", 0, 1, 1.0, "", decimals=2),
        Param("transpose", "Transponer", "float", -12, 12, 0, "st", decimals=0),
        Param("mix", "Mezcla", "float", 0, 1, 1.0, "", decimals=2),
        Param("gate_db", "Umbral voz", "float", -80, -10, -48, "dB"),
    ]

    ANALYSIS = 2048

    def __init__(self, sr):
        super().__init__(sr)
        self._init_dsp()

    def _init_dsp(self):
        self.shifter = DopplerShifter(self.sr)
        self.hist = np.zeros(self.ANALYSIS * 2, dtype=np.float32)
        self.corr = 0.0          # corrección actual (semitonos)
        self.ratio = 1.0
        self.detected: float | None = None   # nota MIDI detectada
        self.target: int | None = None

    def reset(self):
        self._init_dsp()

    def set_sample_rate(self, sr):
        self.sr = sr
        self._init_dsp()

    def resolve_key(self, ctx_key: Key | None) -> tuple[list[int], str]:
        k, s = self.values["key"], self.values["scale"]
        if k == "Auto":
            if ctx_key is None:
                return list(range(12)), "Cromática (esperando tonalidad)"
            root = ctx_key.root
            mode = ctx_key.mode if s == "Auto" else _SCALE_MAP[s]
        else:
            root = NOTE_NAMES.index(k)
            if s == "Auto":
                mode = ctx_key.mode if ctx_key else "major"
            else:
                mode = _SCALE_MAP[s]
        pcs = [(root + i) % 12 for i in SCALES[mode]]
        return pcs, f"{NOTE_NAMES[root]} {({'major': 'mayor', 'minor': 'menor', 'chromatic': 'cromática'})[mode]}"

    def process(self, x, ctx):
        mono = x.mean(axis=1).astype(np.float32)
        n = len(mono)
        self.hist = np.roll(self.hist, -n)
        self.hist[-n:] = mono[-len(self.hist):] if n > len(self.hist) else mono
        frame = self.hist[-self.ANALYSIS:]
        pcs, key_label = self.resolve_key(ctx.key)

        rms = float(np.sqrt(np.mean(frame ** 2)) + 1e-12)
        f0 = None
        if 20 * np.log10(rms) > self.values["gate_db"]:
            f0, aper = yin_pitch(frame, self.sr)
        block_s = n / self.sr
        if f0 is not None:
            midi = 69 + 12 * np.log2(f0 / 440.0)
            base = int(np.floor(midi))
            cands = [m for m in range(base - 6, base + 7) if m % 12 in pcs]
            target = min(cands, key=lambda m: abs(m - midi))
            self.detected, self.target = midi, target
            want = (target - midi) * float(self.values["amount"])
            tau = float(self.values["speed_ms"]) / 1000.0
        else:
            self.detected, self.target = None, None
            want = 0.0
            tau = 0.15  # volver suave a 0 en consonantes/silencios
        a = 1.0 if tau <= 1e-4 else 1.0 - np.exp(-block_s / tau)
        self.corr += (want - self.corr) * a
        total = self.corr + float(self.values["transpose"])
        new_ratio = float(2 ** (total / 12.0))
        wet, dry = self.shifter.process(mono, self.ratio, new_ratio)
        self.ratio = new_ratio

        if self.detected is not None:
            cents = (self.target - self.detected) * 100
            self.status = f"{key_label} · {midi_to_name(self.detected)} → {midi_to_name(self.target)} ({cents:+.0f} ¢)"
        else:
            self.status = f"{key_label} · —"
        m = float(self.values["mix"])
        out = dry * (1 - m) + wet * m
        return np.repeat(out[:, None], x.shape[1], axis=1)


@register
class PitchFx(Effect):
    """Transposición fija (octavador / voz grave o aguda)."""
    type_id = "pitch"
    display_name = "Pitch fijo"
    category = "Tono"
    params_spec = [
        Param("semitones", "Semitonos", "float", -12, 12, 0, "st", decimals=0),
        Param("cents", "Fino", "float", -50, 50, 0, "¢", decimals=0),
        Param("mix", "Mezcla", "float", 0, 1, 1.0, "", decimals=2),
    ]

    def __init__(self, sr):
        super().__init__(sr)
        self.shifter = DopplerShifter(sr, window_ms=40)

    def reset(self):
        self.shifter = DopplerShifter(self.sr, window_ms=40)

    def process(self, x, ctx):
        r = float(2 ** ((round(self.values["semitones"]) + self.values["cents"] / 100.0) / 12.0))
        out = np.empty_like(x)
        m = float(self.values["mix"])
        mono = x.mean(axis=1).astype(np.float32)
        wet, dry = self.shifter.process(mono, r, r)
        mix = dry * (1 - m) + wet * m
        out[:] = mix[:, None]
        return out
