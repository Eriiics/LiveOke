"""Buffer circular multi-canal, seguro entre hilos, con control de deriva.

Cada dispositivo de audio corre con su propio reloj. Los buffers absorben la
diferencia: si se acumula demasiado audio se descarta lo más viejo (latencia
estable) y si falta se rellena con silencio.
"""
from __future__ import annotations

import threading

import numpy as np


class RingBuffer:
    def __init__(self, capacity: int, channels: int = 2, target_fill: int = 1024):
        self.capacity = int(capacity)
        self.channels = channels
        self.target_fill = int(target_fill)
        self._buf = np.zeros((self.capacity, channels), dtype=np.float32)
        self._read = 0
        self._count = 0
        self._lock = threading.Lock()
        self.underruns = 0
        self.overruns = 0

    @property
    def available(self) -> int:
        return self._count

    def clear(self):
        with self._lock:
            self._read = 0
            self._count = 0

    def write(self, data: np.ndarray):
        data = np.asarray(data, dtype=np.float32)
        if data.ndim == 1:
            data = data[:, None]
        if data.shape[1] != self.channels:
            if data.shape[1] == 1:
                data = np.repeat(data, self.channels, axis=1)
            else:
                data = data[:, : self.channels]
        n = len(data)
        if n == 0:
            return
        with self._lock:
            if n >= self.capacity:
                data = data[-self.capacity:]
                n = len(data)
            free = self.capacity - self._count
            if n > free:  # descartar lo más viejo
                drop = n - free
                self._read = (self._read + drop) % self.capacity
                self._count -= drop
                self.overruns += 1
            w = (self._read + self._count) % self.capacity
            first = min(n, self.capacity - w)
            self._buf[w:w + first] = data[:first]
            if first < n:
                self._buf[: n - first] = data[first:]
            self._count += n

    def read(self, n: int, drift_control: bool = True) -> np.ndarray:
        out = np.zeros((n, self.channels), dtype=np.float32)
        with self._lock:
            # Si el productor va más rápido, saltar adelante para no acumular latencia
            if drift_control and self._count > self.target_fill + 2 * n:
                skip = self._count - (self.target_fill + n)
                self._read = (self._read + skip) % self.capacity
                self._count -= skip
            take = min(n, self._count)
            if take < n:
                self.underruns += 1
            first = min(take, self.capacity - self._read)
            out[:first] = self._buf[self._read:self._read + first]
            if first < take:
                out[first:take] = self._buf[: take - first]
            self._read = (self._read + take) % self.capacity
            self._count -= take
        return out
