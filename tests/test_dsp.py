"""Pruebas offline del DSP (no necesitan tarjeta de sonido).

Ejecutar:  python -m pytest tests -q     (o simplemente: python tests/test_dsp.py)
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vocalchain import autotune  # noqa: E402,F401  (registra Auto-Tune)
from vocalchain.autotune import AutoTuneFx, yin_pitch  # noqa: E402
from vocalchain.effects import REGISTRY, DuckerFx, ProcessContext  # noqa: E402
from vocalchain.keydetect import KeyDetector  # noqa: E402
from vocalchain.music import Key  # noqa: E402

SR = 48000


def tone(freq, dur, sr=SR, harmonics=6):
    t = np.arange(int(dur * sr)) / sr
    return sum(np.sin(2 * np.pi * freq * h * t) / h for h in range(1, harmonics + 1)).astype(np.float32)


def midi_hz(m):
    return 440.0 * 2 ** ((m - 69) / 12)


def chord(root_midi, minor, dur):
    third = 3 if minor else 4
    notes = [root_midi - 12, root_midi, root_midi + third, root_midi + 7]
    melody = [root_midi + 12, root_midi + 12 + third, root_midi + 19]
    sig = sum(tone(midi_hz(n), dur) for n in notes)
    seg = int(dur * SR) // len(melody)
    mel = np.concatenate([tone(midi_hz(n), seg / SR) for n in melody])
    sig[: len(mel)] += 0.8 * mel
    return sig / 8


def detect(progression, reps=4):
    audio = np.concatenate([chord(r, m, 1.5) for _ in range(reps) for r, m in progression])
    kd = KeyDetector(SR)
    for i in range(0, len(audio), 512):
        kd.push(audio[i:i + 512])
        if i % (512 * 40) == 0:
            kd.update()
    return kd.update()


def test_key_a_minor():
    # Am - F - C - G (Am / C mayor son las mismas notas: ambas válidas)
    k = detect([(57, True), (53, False), (60, False), (55, False)])
    print("detectado:", k)
    assert k is not None and set(k.pitch_classes()) == set(Key(9, "minor").pitch_classes())


def test_key_e_major():
    # E - B - C#m - A
    k = detect([(52, False), (59, False), (61, True), (57, False)])
    print("detectado:", k)
    assert k is not None and set(k.pitch_classes()) == set(Key(4, "major").pitch_classes())


def test_yin():
    for f in (110.0, 196.0, 440.0, 700.0):
        est, _ = yin_pitch(tone(f, 0.05)[:2048], SR)
        assert est is not None and abs(est - f) / f < 0.01, (f, est)


def test_autotune_corrects_pitch():
    fx = AutoTuneFx(SR)
    fx.set("speed_ms", 0)
    fx.set("key", "C")
    fx.set("scale", "Mayor")
    sung = tone(midi_hz(69.4), 2.0)  # La desafinado +40 cents
    ctx = ProcessContext(SR, 512, {})
    out = []
    for i in range(0, len(sung), 512):
        blk = np.repeat(sung[i:i + 512, None], 2, axis=1)
        out.append(fx.process(blk, ctx)[:, 0])
    out = np.concatenate(out)
    est, _ = yin_pitch(out[-2048:], SR)
    cents = 1200 * np.log2(est / 440.0)
    print(f"salida: {est:.1f} Hz ({cents:+.1f} cents)  estado: {fx.status}")
    assert abs(cents) < 10


def test_all_effects_process():
    ctx = ProcessContext(SR, 512, {"Mic": np.zeros((512, 2), np.float32)})
    x = (np.random.randn(512, 2) * 0.1).astype(np.float32)
    for tid, cls in REGISTRY.items():
        fx = cls(SR)
        for _ in range(4):
            y = fx.process(x.copy(), ctx)
        assert y.shape == (512, 2), (tid, y.shape)
        assert np.all(np.isfinite(y)), tid
        d = fx.to_dict()
        assert type(fx).from_dict(d, SR).to_dict()["type"] == tid


def test_ducker():
    fx = DuckerFx(SR)
    loud = np.ones((512, 2), np.float32) * 0.5
    x = np.ones((512, 2), np.float32) * 0.5
    ctx = ProcessContext(SR, 512, {"Mic": loud})
    for _ in range(50):
        y = fx.process(x, ctx)
    assert y[-1, 0] < 0.5 * 10 ** (-8 / 20)


if __name__ == "__main__":
    for name, f in list(globals().items()):
        if name.startswith("test_"):
            f()
            print("OK", name)
