"""Simula un PC con Focusrite (ASIO + WASAPI + MME duplicados) sin hardware real.

Comprueba: lista de dispositivos filtrada por driver, que mic+monitor ASIO usan UN stream
dúplex, que el audio del mic llega al monitor y que Stream (WASAPI) recibe la mezcla.
"""
import os
import sys
import threading
import time
import types

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HOSTAPIS = [
    {"name": "MME", "default_input_device": 0, "default_output_device": 1},
    {"name": "Windows WASAPI", "default_input_device": 2, "default_output_device": 3},
    {"name": "ASIO", "default_input_device": 5, "default_output_device": 5},
]
DEVICES = [
    {"name": "Analogue 1 + 2 (Focusrite USB A", "hostapi": 0, "max_input_channels": 2, "max_output_channels": 0},
    {"name": "Speakers (Focusrite USB Audio)", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2},
    {"name": "Analogue 1 + 2 (Focusrite USB Audio)", "hostapi": 1, "max_input_channels": 2, "max_output_channels": 0},
    {"name": "Speakers (Focusrite USB Audio)", "hostapi": 1, "max_input_channels": 0, "max_output_channels": 2},
    {"name": "CABLE Input (VB-Audio Virtual Cable)", "hostapi": 1, "max_input_channels": 0, "max_output_channels": 2},
    {"name": "Focusrite USB ASIO", "hostapi": 2, "max_input_channels": 2, "max_output_channels": 2},
]
opened = []


class _Base:
    def __init__(self, device=None, channels=None, samplerate=None, blocksize=None, callback=None, **kw):
        self.device, self.channels, self.sr, self.block, self.cb = device, channels, samplerate, blocksize, callback
        self.kind = type(self).__name__
        self.latency = (0.004, 0.004) if self.kind == "Stream" else 0.01
        self._stop = threading.Event()
        opened.append(self)

    def start(self):
        self._th = threading.Thread(target=self._run, daemon=True)
        self._th.start()

    def stop(self):
        self._stop.set()

    def close(self):
        pass

    def _run(self):
        n = self.block
        t = 0
        self.out = []
        while not self._stop.is_set():
            ph = (np.arange(n) + t) / self.sr
            mic = np.zeros((n, 2), np.float32)
            mic[:, 1] = 0.3 * np.sin(2 * np.pi * 220 * ph)  # el micrófono está en el Input 2
            if self.kind == "Stream":
                out = np.zeros((n, self.channels[1]), np.float32)
                self.cb(mic[:, : self.channels[0]], out, n, None, None)
                self.out.append(out.copy())
            elif self.kind == "InputStream":
                self.cb(mic[:, : self.channels], n, None, None)
            else:
                out = np.zeros((n, self.channels), np.float32)
                self.cb(out, n, None, None)
                self.out.append(out.copy())
            t += n
            time.sleep(n / self.sr)


fake = types.ModuleType("sounddevice")
fake.query_hostapis = lambda i=None: HOSTAPIS if i is None else HOSTAPIS[i]
fake.query_devices = lambda i=None: DEVICES if i is None else DEVICES[i]
fake.default = types.SimpleNamespace(device=(0, 1))
fake.Stream = type("Stream", (_Base,), {})
fake.InputStream = type("InputStream", (_Base,), {})
fake.OutputStream = type("OutputStream", (_Base,), {})
fake.WasapiSettings = lambda **kw: None
sys.modules["sounddevice"] = fake

from vocalchain import devices as dv  # noqa: E402
from vocalchain.engine import AudioEngine  # noqa: E402


def test_device_lists():
    assert dv.list_apis()[0] == "ASIO"
    asio_in = dv.list_inputs("ASIO")
    assert [d.name for d in asio_in] == ["Focusrite USB ASIO"]
    assert all(d.api == "Windows WASAPI" for d in dv.list_outputs(dv.windows_api()))
    assert dv.default_output().api == "Windows WASAPI"


def test_asio_duplex_flow():
    opened.clear()
    e = AudioEngine()
    asio_in = dv.DeviceRef("Focusrite USB ASIO", "ASIO", "input")
    asio_out = dv.DeviceRef("Focusrite USB ASIO", "ASIO", "output")
    e.mic_dev, e.monitor_dev = asio_in, asio_out
    e.stream_dev = dv.DeviceRef("CABLE Input (VB-Audio Virtual Cable)", "Windows WASAPI", "output")
    e.pc_dev = None
    e.block = 256
    mic = e.mixer.by_name("Mic")
    mic.input_channel = "2"
    mic.chain = []  # señal limpia para medir
    e.start()
    time.sleep(0.6)
    e.stop()
    print("modo:", e.mode, "| latencia:", e.latency_ms, "ms | errores:", e.errors)
    kinds = sorted(s.kind for s in opened)
    print("streams:", kinds)
    assert kinds == ["OutputStream", "Stream"], kinds        # 1 dúplex ASIO + 1 Stream WASAPI
    duplex = next(s for s in opened if s.kind == "Stream")
    assert duplex.channels == (2, 2)
    mon = np.concatenate(duplex.out)
    stream = np.concatenate(next(s for s in opened if s.kind == "OutputStream").out)
    print(f"pico monitor {np.abs(mon).max():.2f}, pico stream {np.abs(stream).max():.2f}")
    assert np.abs(mon).max() > 0.2 and np.abs(stream).max() > 0.1
    assert not e.errors


def test_two_asio_devices_rejected():
    e = AudioEngine()
    e.mic_dev = dv.DeviceRef("Focusrite USB ASIO", "ASIO", "input")
    e.monitor_dev = dv.DeviceRef("Speakers (Focusrite USB Audio)", "Windows WASAPI", "output")
    e.stream_dev = dv.DeviceRef("Focusrite USB ASIO", "ASIO", "output")
    e.pc_dev = None
    e.start()
    e.stop()
    print("errores:", e.errors)
    assert any("Stream no puede ir por ASIO" in x for x in e.errors)


if __name__ == "__main__":
    for name, f in list(globals().items()):
        if name.startswith("test_"):
            f()
            print("OK", name)
