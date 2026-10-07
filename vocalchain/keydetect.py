"""Detección de tonalidad en tiempo real a partir del audio del PC.

Método: cromagrama (energía por clase de nota) acumulado con decaimiento
exponencial + correlación con los perfiles de Krumhansl-Kessler para las
24 tonalidades. Para el autotune da lo mismo si confunde La menor con Do mayor
(son las mismas notas), así que es bastante robusto para lo que lo usamos.
"""
from __future__ import annotations

import threading
from collections import deque

import numpy as np

from .music import Key

KK_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
KK_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


def _zscore(v: np.ndarray) -> np.ndarray:
    v = v - v.mean()
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


_PROFILES = np.array(
    [_zscore(np.roll(KK_MAJOR, r)) for r in range(12)] + [_zscore(np.roll(KK_MINOR, r)) for r in range(12)]
)


def key_scores(chroma: np.ndarray) -> np.ndarray:
    """Correlación de Pearson del cromagrama con las 24 tonalidades (0-11 mayores, 12-23 menores)."""
    return _PROFILES @ _zscore(np.asarray(chroma, dtype=np.float64))


def best_key(chroma: np.ndarray) -> Key:
    s = key_scores(chroma)
    i = int(np.argmax(s))
    return Key(i % 12, "major" if i < 12 else "minor", float(s[i]))


class ChromaExtractor:
    def __init__(self, sample_rate: int, frame: int = 16384, fmin: float = 70.0, fmax: float = 2200.0):
        self.sr = sample_rate
        self.frame = frame
        self.window = np.hanning(frame).astype(np.float32)
        freqs = np.fft.rfftfreq(frame, 1.0 / sample_rate)
        sel = (freqs >= fmin) & (freqs <= fmax)
        self.bins = np.nonzero(sel)[0]
        midi = 69 + 12 * np.log2(freqs[self.bins] / 440.0)
        self.pc = np.round(midi).astype(int) % 12
        # Penaliza bins que caen entre dos notas (desafinados respecto a la grilla)
        self.tuning_w = np.cos(np.pi * (midi - np.round(midi))) ** 2

    def __call__(self, x: np.ndarray) -> np.ndarray | None:
        if np.sqrt(np.mean(x ** 2)) < 1e-3:  # silencio
            return None
        mag = np.abs(np.fft.rfft(x * self.window))[self.bins]
        # Blanqueo simple: quedarse con picos sobre la media local
        k = 15
        base = np.convolve(mag, np.ones(k) / k, mode="same")
        peaks = np.maximum(mag - base, 0.0) * self.tuning_w
        peaks = np.sqrt(peaks)
        c = np.bincount(self.pc, weights=peaks, minlength=12)
        s = c.sum()
        return c / s if s > 0 else None


class KeyDetector:
    """Recibe audio mono del hilo de audio (push) y lo analiza en otro hilo (update)."""

    def __init__(self, sample_rate: int, half_life_s: float = 25.0, min_seconds: float = 6.0):
        self.sr = sample_rate
        self.extract = ChromaExtractor(sample_rate)
        self.frame = self.extract.frame
        self.half_life_s = half_life_s
        self.min_frames = int(min_seconds * sample_rate / self.frame)
        self._pending: deque[np.ndarray] = deque()
        self._lock = threading.Lock()
        self._carry = np.zeros(0, dtype=np.float32)
        self.chroma = np.zeros(12)
        self.frames = 0
        self.key: Key | None = None
        self._challenger: tuple[int, int] | None = None  # (indice, votos)

    def push(self, mono: np.ndarray):
        with self._lock:
            self._pending.append(np.asarray(mono, dtype=np.float32).copy())
            # nunca acumular más de ~10 s sin analizar
            while len(self._pending) > 2000:
                self._pending.popleft()

    def reset(self):
        with self._lock:
            self._pending.clear()
        self._carry = np.zeros(0, dtype=np.float32)
        self.chroma[:] = 0
        self.frames = 0
        self.key = None
        self._challenger = None

    def update(self) -> Key | None:
        """Procesa el audio pendiente. Devuelve la tonalidad estable actual."""
        with self._lock:
            chunks = list(self._pending)
            self._pending.clear()
        if not chunks:
            return self.key
        data = np.concatenate([self._carry] + chunks)
        decay = 0.5 ** ((self.frame / self.sr) / self.half_life_s)
        pos = 0
        while pos + self.frame <= len(data):
            c = self.extract(data[pos:pos + self.frame])
            if c is not None:
                self.chroma = self.chroma * decay + c
                self.frames += 1
            pos += self.frame
        self._carry = data[pos:]
        if self.frames >= self.min_frames:
            self._decide()
        return self.key

    def _decide(self):
        scores = key_scores(self.chroma)
        i = int(np.argmax(scores))
        cand = Key(i % 12, "major" if i < 12 else "minor", float(scores[i]))
        if self.key is None:
            self.key = cand
            return
        cur_i = self.key.root + (0 if self.key.mode == "major" else 12)
        if i == cur_i:
            self.key = cand  # refresca confianza
            self._challenger = None
            return
        # Histéresis: el retador tiene que ganar con margen varias veces seguidas
        if scores[i] - scores[cur_i] < 0.03:
            self._challenger = None
            return
        votes = self._challenger[1] + 1 if self._challenger and self._challenger[0] == i else 1
        self._challenger = (i, votes)
        if votes >= 3:
            self.key = cand
            self._challenger = None
