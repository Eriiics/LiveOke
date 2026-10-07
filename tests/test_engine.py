"""Pruebas del mezclador sin dispositivos: ruteo, envíos, anti-feedback, sesión y rendimiento."""
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vocalchain.engine import AudioEngine, Mixer, default_mixer  # noqa: E402

SR, N = 48000, 512


def blocks(seconds, freq=220.0):
    t = np.arange(int(seconds * SR)) / SR
    sig = (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    for i in range(0, len(sig) - N, N):
        yield np.repeat(sig[i:i + N, None], 2, axis=1)


def test_routing_and_sends():
    m = default_mixer(SR)
    mic, pc = m.by_name("Mic"), m.by_name("PC")
    rev = m.by_name("Reverb")
    pc.mute = True
    for b in blocks(1.0):
        out = m.process({"mic": b, "pc": b}, N)
    assert out["monitor"].shape == (N, 2)
    assert mic.peak > 0.01 and rev.peak > 0.0001, (mic.peak, rev.peak)
    assert pc.peak == 0.0


def test_feedback_guard():
    m = Mixer(SR)
    m.load_dict(default_mixer(SR).to_dict())
    mic = m.by_name("Mic")
    mic.mute = True
    for s in m.buses:
        s.mute = True
    m.blocked = {"monitor"}
    for b in blocks(0.3):
        out = m.process({"pc": b}, N)
    assert np.max(np.abs(out["monitor"])) == 0.0
    assert np.max(np.abs(out["stream"])) > 0.01


def test_session_roundtrip(tmp_path=None):
    e = AudioEngine()
    e.mixer.by_name("Mic").gain_db = -3.5
    e.mixer.add_bus("Slapback", ["delay"])
    d = json.loads(json.dumps(e.to_dict()))
    e2 = AudioEngine()
    e2.load_dict(d)
    assert e2.mixer.by_name("Mic").gain_db == -3.5
    assert e2.mixer.by_name("Slapback").chain[0].type_id == "delay"
    # los envíos apuntan a ids que siguen existiendo
    mic = e2.mixer.by_name("Mic")
    assert all(e2.mixer.strip(bid) for bid in mic.sends)


def test_performance():
    m = default_mixer(SR)
    gen = blocks(3.0)
    times = []
    for b in gen:
        t0 = time.perf_counter()
        m.process({"mic": b, "pc": b}, N)
        times.append(time.perf_counter() - t0)
    budget = N / SR
    p95 = sorted(times)[int(len(times) * 0.95)]
    print(f"bloque {N}: media {np.mean(times) * 1000:.2f} ms, p95 {p95 * 1000:.2f} ms (presupuesto {budget * 1000:.1f} ms)")
    assert p95 < budget * 0.5


if __name__ == "__main__":
    for name, f in list(globals().items()):
        if name.startswith("test_"):
            f()
            print("OK", name)
