"""Motor de audio: mezclador (canales, buses de envío, salidas) + E/S en tiempo real.

Flujo por bloque:
    Mic ─┐                        ┌─> envíos ─> Bus Reverb ─┐
         ├─ cadena ─ fader ─ pan ─┤                          ├─> Monitor (audífonos)
    PC  ─┘                        └─────────────────────────┴─> Stream  (cable virtual / OBS)

El sidechain usa la señal CRUDA (antes de la cadena) de cualquier canal de entrada.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from pathlib import Path

import numpy as np

from . import autotune  # noqa: F401  registra Auto-Tune y Pitch
from . import devices as dv
from .effects import Effect, ProcessContext, create, db_to_lin
from .keydetect import KeyDetector
from .music import Key
from .ringbuffer import RingBuffer
from .winvolume import SystemVolume

try:
    import pedalboard as pb
except Exception:  # pragma: no cover
    pb = None

OUTPUTS = ("monitor", "stream")
OUTPUT_LABELS = {"monitor": "Monitor", "stream": "Stream"}
CONFIG_DIR = Path.home() / ".vocalchain"
DIAG_FILE = CONFIG_DIR / "diag.log"
MIN_BLOCK = 256          # Python no alcanza a procesar bloques más chicos sin cortes
CLIP_LEVEL = 0.98


def _peak(x: np.ndarray) -> float:
    return float(np.max(np.abs(x))) if x.size else 0.0


MIC_CHANNEL_CHOICES = [str(i) for i in range(1, 9)] + ["1+2"]


def pick_channel(x: np.ndarray, sel: str) -> np.ndarray:
    """Saca el canal mono elegido de una entrada multicanal (con respaldo al canal 1)."""
    if sel == "1+2" and x.shape[1] > 1:
        return x[:, :2].mean(axis=1)
    try:
        i = int(sel) - 1
    except ValueError:
        i = 0
    return x[:, i] if 0 <= i < x.shape[1] else x[:, 0]


class Strip:
    """Un canal de entrada (Mic, PC) o un bus de efectos (Reverb, Delay...)."""

    def __init__(self, name: str, kind: str = "bus", source: str | None = None, sid: str | None = None):
        self.id = sid or uuid.uuid4().hex[:8]
        self.name = name
        self.kind = kind              # "input" | "bus"
        self.source = source          # "mic" | "pc" | None
        self.chain: list[Effect] = []
        self.gain_db = 0.0
        self.pan = 0.0                # -1 izq .. +1 der
        self.mute = False
        self.solo = False
        self.sends: dict[str, float] = {}       # id de bus -> nivel lineal 0..1 (post-fader)
        self.outputs = {o: True for o in OUTPUTS}
        self.input_channel = "1"      # solo Mic: "1".."8" o "1+2"
        self.peak = 0.0
        self.in_peak = 0.0            # nivel crudo de entrada (antes de efectos)
        self.clip_count = 0
        self.last_clip = 0.0
        self.warning = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "kind": self.kind, "source": self.source,
            "gain_db": self.gain_db, "pan": self.pan, "mute": self.mute,
            "sends": self.sends, "outputs": self.outputs, "input_channel": self.input_channel,
            "chain": [fx.to_dict() for fx in self.chain],
        }

    @staticmethod
    def from_dict(d: dict, sr: int) -> "Strip":
        s = Strip(d["name"], d.get("kind", "bus"), d.get("source"), d.get("id"))
        s.gain_db = d.get("gain_db", 0.0)
        s.pan = d.get("pan", 0.0)
        s.mute = d.get("mute", False)
        s.sends = dict(d.get("sends", {}))
        s.outputs.update(d.get("outputs", {}))
        s.input_channel = d.get("input_channel", "1")
        chain = []
        for fd in d.get("chain", []):
            try:
                chain.append(Effect.from_dict(fd, sr))
            except Exception as e:
                print("No pude cargar efecto", fd.get("type"), e)
        s.chain = chain
        return s


class OutputBus:
    def __init__(self, name: str):
        self.name = name
        self.gain_db = 0.0
        self.mute = False
        self.peak = 0.0
        self.safety_limiter = True
        self._limiter = pb.Limiter(threshold_db=-0.8, release_ms=80) if pb else None

    def finish(self, x: np.ndarray, sr: int) -> np.ndarray:
        x = x * db_to_lin(self.gain_db) if not self.mute else np.zeros_like(x)
        pre = _peak(x)
        self.pre_peak = max(pre, getattr(self, "pre_peak", 0.0) * 0.9)
        if pre > 0.9:
            self.limit_hits = getattr(self, "limit_hits", 0) + 1
        if self.safety_limiter and self._limiter is not None:
            x = np.ascontiguousarray(self._limiter.process(np.ascontiguousarray(x.T), sr, reset=False).T)
        np.clip(x, -1.0, 1.0, out=x)
        self.peak = max(_peak(x), self.peak * 0.85)
        return x

    def to_dict(self):
        return {"gain_db": self.gain_db, "mute": self.mute, "safety_limiter": self.safety_limiter}


class Mixer:
    def __init__(self, sr: int):
        self.sr = sr
        self.strips: list[Strip] = []
        self.outputs = {o: OutputBus(o) for o in OUTPUTS}
        self.key: Key | None = None
        self.blocked: set[str] = set()     # salidas a las que PC no puede ir (anti-feedback)
        self.pc_system_volume = False      # el fader PC maneja el volumen de Windows

    # -- estructura ---------------------------------------------------------
    def strip(self, sid: str) -> Strip | None:
        return next((s for s in self.strips if s.id == sid), None)

    def by_name(self, name: str) -> Strip | None:
        return next((s for s in self.strips if s.name == name), None)

    @property
    def inputs(self):
        return [s for s in self.strips if s.kind == "input"]

    @property
    def buses(self):
        return [s for s in self.strips if s.kind == "bus"]

    def add_bus(self, name: str, fx_types: list[str] = ()) -> Strip:
        s = Strip(name, "bus")
        s.chain = [create(t, self.sr) for t in fx_types]
        self.strips = self.strips + [s]
        return s

    def remove_strip(self, sid: str):
        self.strips = [s for s in self.strips if s.id != sid]
        for s in self.strips:
            if sid in s.sends:
                s.sends = {k: v for k, v in s.sends.items() if k != sid}

    def set_sample_rate(self, sr: int):
        self.sr = sr
        for s in self.strips:
            for fx in s.chain:
                fx.set_sample_rate(sr)

    # -- audio ----------------------------------------------------------------
    @staticmethod
    def _run_chain(strip: Strip, x: np.ndarray, ctx: ProcessContext) -> np.ndarray:
        for fx in tuple(strip.chain):
            if not fx.enabled:
                continue
            try:
                y = fx.process(x, ctx)
                if y is not None and y.shape == x.shape and np.all(np.isfinite(y[::64])):
                    x = y.astype(np.float32, copy=False)
            except Exception as e:  # un efecto roto no debe tumbar el audio
                fx.enabled = False
                fx.status = f"ERROR: {e}"
                traceback.print_exc()
        return x

    @staticmethod
    def _pan(x: np.ndarray, pan: float) -> np.ndarray:
        if abs(pan) < 1e-3:
            return x
        a = (pan + 1) * np.pi / 4
        return x * np.array([np.cos(a), np.sin(a)], dtype=np.float32) * np.float32(np.sqrt(2))

    def process(self, raw: dict[str, np.ndarray], n: int) -> dict[str, np.ndarray]:
        strips = tuple(self.strips)
        outs = {o: np.zeros((n, 2), np.float32) for o in OUTPUTS}
        bus_in = {s.id: np.zeros((n, 2), np.float32) for s in strips if s.kind == "bus"}

        # Señales crudas por canal (sidechain)
        sources = {}
        for s in strips:
            if s.kind != "input":
                continue
            x = raw.get(s.source)
            if x is None:
                x = np.zeros((n, 2), np.float32)
            elif s.source == "mic":
                x = np.repeat(pick_channel(x, s.input_channel)[:, None], 2, axis=1)
            pk = _peak(x)
            s.in_peak = max(pk, s.in_peak * 0.9)
            if pk >= CLIP_LEVEL:
                s.clip_count += 1
                s.last_clip = time.monotonic()
            sources[s.name] = x
        ctx = ProcessContext(self.sr, n, sources, self.key)
        any_solo = any(s.solo for s in strips)

        for s in strips:
            if s.kind != "input":
                continue
            y = self._run_chain(s, sources[s.name].copy(), ctx)
            sysvol = s.source == "pc" and self.pc_system_volume
            muted = (s.mute and not sysvol) or (any_solo and not s.solo)
            gain = 0.0 if sysvol else s.gain_db  # en modo Windows el volumen ya viene aplicado
            y = self._pan(y * np.float32(db_to_lin(gain)), s.pan) if not muted else np.zeros_like(y)
            s.peak = max(_peak(y), s.peak * 0.85)
            blocked = self.blocked if s.source == "pc" else set()
            for o in OUTPUTS:
                if s.outputs.get(o) and o not in blocked:
                    outs[o] += y
            if s.source == "pc" and self.blocked:
                continue  # los envíos de PC volverían a entrar por el loopback
            for bid, lvl in s.sends.items():
                if bid in bus_in and lvl > 0:
                    bus_in[bid] += y * np.float32(lvl)

        for s in strips:
            if s.kind != "bus":
                continue
            y = self._run_chain(s, bus_in[s.id], ctx)
            muted = s.mute or (any_solo and not s.solo)
            y = self._pan(y * np.float32(db_to_lin(s.gain_db)), s.pan) if not muted else np.zeros_like(y)
            s.peak = max(_peak(y), s.peak * 0.85)
            for o in OUTPUTS:
                if s.outputs.get(o):
                    outs[o] += y

        return {o: self.outputs[o].finish(outs[o], self.sr) for o in OUTPUTS}

    # -- serialización --------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "strips": [s.to_dict() for s in self.strips],
            "outputs": {o: b.to_dict() for o, b in self.outputs.items()},
        }

    def load_dict(self, d: dict):
        self.strips = [Strip.from_dict(sd, self.sr) for sd in d.get("strips", [])]
        for o, od in d.get("outputs", {}).items():
            if o in self.outputs:
                self.outputs[o].gain_db = od.get("gain_db", 0.0)
                self.outputs[o].mute = od.get("mute", False)
                self.outputs[o].safety_limiter = od.get("safety_limiter", True)


def default_mixer(sr: int) -> Mixer:
    m = Mixer(sr)
    mic = Strip("Mic", "input", "mic")
    gate, eq, comp, tune = (create(t, sr) for t in ("gate", "eq", "compressor", "autotune"))
    eq.set("hpf", 90)
    mic.chain = [gate, eq, comp, tune]
    pc = Strip("PC", "input", "pc")
    pc.outputs["monitor"] = True
    m.strips = [mic, pc]
    rev = m.add_bus("Reverb", ["reverb"])
    dly = m.add_bus("Delay", ["delay"])
    mic.sends = {rev.id: 0.25, dly.id: 0.12}
    return m


def _first_asio() -> tuple[dv.DeviceRef, dv.DeviceRef] | None:
    """Primer dispositivo ASIO con entrada y salida (se evitan los genéricos tipo ASIO4ALL)."""
    try:
        ins = {d.name for d in dv.list_inputs(dv.ASIO)}
        outs = {d.name for d in dv.list_outputs(dv.ASIO)}
    except Exception:
        return None
    both = sorted(ins & outs, key=lambda n: ("asio4all" in n.lower() or "realtek" in n.lower(), n))
    if not both:
        return None
    n = both[0]
    return dv.DeviceRef(n, dv.ASIO, "input"), dv.DeviceRef(n, dv.ASIO, "output")


class AudioEngine:
    """Abre los dispositivos y llama al mezclador en el hilo de audio."""

    def __init__(self):
        self.sr = 48000
        self.block = MIN_BLOCK
        self.mixer = default_mixer(self.sr)
        self.mic_dev: dv.DeviceRef | None = dv.default_input()
        self.monitor_dev: dv.DeviceRef | None = dv.default_output()
        asio = _first_asio()
        if asio:  # interfaz con ASIO (Focusrite, etc.): usarla por defecto para mic + monitor
            self.mic_dev, self.monitor_dev = asio
        self.pc_dev: dv.DeviceRef | None = dv.default_loopback()
        self.stream_dev: dv.DeviceRef | None = None
        self.manual_key: Key | None = None
        self.key_detector = KeyDetector(self.sr)
        self.detected_key: Key | None = None

        self.running = False
        self.errors: list[str] = []
        self.mode = ""                   # driver en uso (para mostrar)
        self.notes: list[str] = []       # avisos informativos (no errores)
        self.sysvol: SystemVolume | None = None
        self.windows_rate: int | None = None
        self.stats = self._new_stats()
        self.total_xruns = 0
        self.latency_ms = 0.0
        self.cpu = 0.0                   # uso de CPU del hilo de audio (0..1)
        self._streams = []
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._rings = {}
        self._proc_lock = threading.Lock()

    # -- estado ---------------------------------------------------------------
    @property
    def key(self) -> Key | None:
        return self.manual_key or self.detected_key

    @property
    def pc_is_direct(self) -> bool:
        """El audio del PC ya suena directo en un dispositivo real (loopback de tus audífonos/interfaz)."""
        return bool(self.pc_dev and self.pc_dev.kind == "loopback" and not dv.is_virtual(self.pc_dev.name))

    def update_blocked(self):
        blocked = set()
        if self.pc_dev and self.pc_dev.kind == "loopback":
            if self.pc_is_direct:
                # Windows ya lo reproduce: mandarlo también al monitor lo duplica (suena doble/saturado)
                blocked.add("monitor")
            for o, dev in (("monitor", self.monitor_dev), ("stream", self.stream_dev)):
                if dev and dv.same_physical(dev.name, self.pc_dev.name):
                    blocked.add(o)
        self.mixer.blocked = blocked
        self.mixer.pc_system_volume = self.pc_is_direct

    def _setup_system_volume(self):
        if not self.pc_is_direct:
            if self.sysvol:
                self.sysvol.stop()
            self.sysvol = None
            return
        if self.sysvol is None or self.sysvol.device_name != self.pc_dev.name:
            if self.sysvol:
                self.sysvol.stop()
            self.sysvol = SystemVolume(self.pc_dev.name)

    # -- diagnóstico ------------------------------------------------------------
    @staticmethod
    def _new_stats():
        return {"callbacks": 0, "xruns": 0, "late": 0, "proc_max": 0.0, "proc_sum": 0.0,
                "frames_min": 0, "frames_max": 0, "last_status": "", "t0": time.monotonic()}

    def _count_status(self, status):
        if status:
            self.total_xruns += 1
            self.stats["xruns"] += 1
            self.stats["last_status"] = str(status)

    def write_diag(self):
        st, self.stats = self.stats, self._new_stats()
        n = max(st["callbacks"], 1)
        dt = max(time.monotonic() - st["t0"], 1e-3)
        mic = self.mixer.by_name("Mic")
        mon = self.mixer.outputs["monitor"]
        pc_ring = self._rings.get("pc")
        db = lambda v: f"{20 * np.log10(max(v, 1e-6)):+.1f}"  # noqa: E731
        line = (
            f"{time.strftime('%H:%M:%S')} modo={self.mode} sr={self.sr} bloque={self.block} "
            f"cb/s={st['callbacks'] / dt:.0f} frames={st['frames_min']}-{st['frames_max']} "
            f"proc_avg={st['proc_sum'] / n * 1000:.2f}ms proc_max={st['proc_max'] * 1000:.2f}ms "
            f"presupuesto={self.block / self.sr * 1000:.2f}ms tarde={st['late']} xruns={st['xruns']} {st['last_status']!r} "
            f"mic_in={db(mic.in_peak) if mic else '-'}dBFS clips={mic.clip_count if mic else 0} "
            f"monitor_pre_lim={db(getattr(mon, 'pre_peak', 0.0))}dBFS lim_hits={getattr(mon, 'limit_hits', 0)} "
            f"pc_under={pc_ring.underruns if pc_ring else '-'} pc_over={pc_ring.overruns if pc_ring else '-'} "
            f"bloqueado={sorted(self.mixer.blocked)} win_rate={self.windows_rate} "
            f"winvol={None if not self.sysvol or self.sysvol.scalar is None else round(self.sysvol.scalar * 100)}%\n"
        )
        try:
            DIAG_FILE.parent.mkdir(parents=True, exist_ok=True)
            if DIAG_FILE.exists() and DIAG_FILE.stat().st_size > 300_000:
                DIAG_FILE.write_text(DIAG_FILE.read_text(encoding="utf-8")[-100_000:], encoding="utf-8")
            with DIAG_FILE.open("a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    # -- procesamiento ----------------------------------------------------------
    def process_block(self, n: int) -> dict[str, np.ndarray]:
        t0 = time.perf_counter()
        raw = {}
        if "mic" in self._rings:
            raw["mic"] = self._rings["mic"].read(n)
        if "pc" in self._rings:
            raw["pc"] = self._rings["pc"].read(n)
            self.key_detector.push(raw["pc"].mean(axis=1))
        self.mixer.key = self.key
        with self._proc_lock:
            outs = self.mixer.process(raw, n)
        dt = time.perf_counter() - t0
        period = n / self.sr
        self.cpu = self.cpu * 0.9 + 0.1 * (dt / period)
        st = self.stats
        st["callbacks"] += 1
        st["proc_sum"] += dt
        st["proc_max"] = max(st["proc_max"], dt)
        st["frames_min"] = n if not st["frames_min"] else min(st["frames_min"], n)
        st["frames_max"] = max(st["frames_max"], n)
        if dt > period * 0.7:
            st["late"] += 1
        return outs

    # -- arranque / parada -------------------------------------------------------
    def start(self):
        if self.running:
            return
        self.errors = []
        self.notes = []
        self.mode = ""
        self._stop.clear()
        self.block = max(self.block, MIN_BLOCK)
        self.update_blocked()
        self._setup_system_volume()
        self.windows_rate = None
        if self.pc_dev and self.pc_dev.kind == "loopback":
            self.windows_rate = dv.mix_rate(self.pc_dev.name)
            uses_asio = any(r is not None and r.is_asio for r in (self.mic_dev, self.monitor_dev))
            if uses_asio and self.windows_rate and self.windows_rate != self.sr:
                # Si ASIO y Windows corren a distinta frecuencia, la interfaz suena mal (crujidos/distorsión)
                self.notes.append(f"Frecuencia ajustada a {self.windows_rate} Hz para coincidir con Windows.")
                self.sr = self.windows_rate
                self.mixer.set_sample_rate(self.sr)
        self.stats = self._new_stats()
        self.total_xruns = 0
        if self.key_detector.sr != self.sr:
            self.key_detector = KeyDetector(self.sr)
        if dv.sd is None:
            self.errors.append(f"sounddevice no disponible: {dv.SD_ERROR}")
            return
        sd = dv.sd
        target = self.block * 2
        self._rings = {}

        def extra(idx):
            try:
                if dv._api_name(sd.query_devices(idx)["hostapi"]) == dv.WASAPI:
                    return sd.WasapiSettings(auto_convert=True)
            except Exception:
                pass
            return None

        def fit(data, nch):
            return data[:, :nch] if nch <= data.shape[1] else np.pad(data, ((0, 0), (0, nch - data.shape[1])))

        mic_idx = dv.find_index(self.mic_dev, "input") if self.mic_dev else None
        mon_idx = dv.find_index(self.monitor_dev, "output") if self.monitor_dev else None
        st_idx = dv.find_index(self.stream_dev, "output") if self.stream_dev else None
        for ref, idx, what in ((self.mic_dev, mic_idx, "la entrada"), (self.monitor_dev, mon_idx, "la salida"),
                               (self.stream_dev, st_idx, "la salida Stream")):
            if ref and idx is None:
                self.errors.append(f"No encontré {what} '{ref.name}' (¿está conectada?)")

        asio_refs = [r for r, i in ((self.mic_dev, mic_idx), (self.monitor_dev, mon_idx), (self.stream_dev, st_idx))
                     if r is not None and i is not None and r.is_asio]
        if len({r.name for r in asio_refs}) > 1:
            self.errors.append("ASIO solo permite UN dispositivo a la vez: usa el mismo para micrófono y monitor.")
            return
        if self.stream_dev and self.stream_dev.is_asio and st_idx is not None:
            self.errors.append("La salida Stream no puede ir por ASIO (ya lo usa el monitor). Elige un dispositivo WASAPI.")
            st_idx = None

        # ¿Entrada y salida por el mismo ASIO? -> un solo stream dúplex (obligatorio en ASIO y el de menor latencia)
        duplex = (mic_idx is not None and mon_idx is not None and self.mic_dev.is_asio and self.monitor_dev.is_asio)
        in_ch = 0
        if mic_idx is not None:
            in_ch = max(1, min(8, int(sd.query_devices(mic_idx)["max_input_channels"])))
            self._rings["mic"] = RingBuffer(self.sr * 2, in_ch, target)

        # Audio del PC (loopback por soundcard o entrada normal)
        try:
            if self.pc_dev and self.pc_dev.kind == "loopback":
                self._start_loopback(self.pc_dev, target)
            elif self.pc_dev:
                pc_idx = dv.find_index(self.pc_dev, "input")
                if pc_idx is None:
                    self.errors.append(f"No encontré la entrada del PC '{self.pc_dev.name}'")
                elif self.pc_dev.is_asio:
                    self.errors.append("El audio del PC no puede ir por ASIO en esta versión; usa loopback o un cable WASAPI.")
                else:
                    ch = min(2, int(sd.query_devices(pc_idx)["max_input_channels"]))
                    ring = RingBuffer(self.sr * 2, 2, target)
                    self._rings["pc"] = ring
                    st = sd.InputStream(device=pc_idx, channels=ch, samplerate=self.sr, blocksize=self.block,
                                        dtype="float32", latency="low", extra_settings=extra(pc_idx),
                                        callback=lambda ind, fr, t, stt, r=ring: r.write(ind))
                    st.start()
                    self._streams.append(st)
        except Exception as e:
            self.errors.append(f"Audio del PC: {e}")

        # Stream (seguidora): lee lo que el procesador deja en un buffer
        follower_ring = RingBuffer(self.sr * 2, 2, target) if st_idx is not None else None
        follower_stream = None
        if follower_ring is not None:
            try:
                ch = min(2, int(sd.query_devices(st_idx)["max_output_channels"]))
                follower_stream = sd.OutputStream(
                    device=st_idx, channels=ch, samplerate=self.sr, blocksize=self.block, dtype="float32",
                    latency="low", extra_settings=extra(st_idx),
                    callback=lambda out, fr, t, stt: out.__setitem__(slice(None), fit(follower_ring.read(fr), out.shape[1])))
            except Exception as e:
                self.errors.append(f"Salida Stream: {e}")
                follower_ring = None

        def deliver(outs):
            if follower_ring is not None:
                follower_ring.write(outs["stream"])

        driver = None
        try:
            if duplex:
                out_ch = min(2, int(sd.query_devices(mon_idx)["max_output_channels"]))
                mic_ring = self._rings["mic"]

                def cb(indata, outdata, frames, t, status):
                    self._count_status(status)
                    mic_ring.write(indata)
                    outs = self.process_block(frames)
                    outdata[:] = fit(outs["monitor"], outdata.shape[1])
                    deliver(outs)

                driver = sd.Stream(device=(mic_idx, mon_idx), channels=(in_ch, out_ch), samplerate=self.sr,
                                   blocksize=self.block, dtype="float32", latency="low", callback=cb)
                self.mode = "ASIO dúplex"
            else:
                if mic_idx is not None:
                    mic_ring = self._rings["mic"]
                    st = sd.InputStream(device=mic_idx, channels=in_ch, samplerate=self.sr, blocksize=self.block,
                                        dtype="float32", latency="low", extra_settings=extra(mic_idx),
                                        callback=lambda ind, fr, t, stt: mic_ring.write(ind))
                    st.start()
                    self._streams.append(st)
                if mon_idx is not None:
                    out_ch = min(2, int(sd.query_devices(mon_idx)["max_output_channels"]))

                    def cb(outdata, frames, t, status):
                        self._count_status(status)
                        outs = self.process_block(frames)
                        outdata[:] = fit(outs["monitor"], outdata.shape[1])
                        deliver(outs)

                    driver = sd.OutputStream(device=mon_idx, channels=out_ch, samplerate=self.sr,
                                             blocksize=self.block, dtype="float32", latency="low",
                                             extra_settings=extra(mon_idx), callback=cb)
                api = self.monitor_dev.api if self.monitor_dev else (self.mic_dev.api if self.mic_dev else "")
                self.mode = api.replace("Windows ", "")
        except Exception as e:
            self.errors.append(f"{'ASIO' if duplex else 'Monitor'}: {e}")
            driver = None

        # arrancar primero la seguidora y al final la que procesa
        if follower_stream is not None:
            try:
                follower_stream.start()
                self._streams.append(follower_stream)
            except Exception as e:
                self.errors.append(f"No pude iniciar Stream: {e}")
                follower_ring = None
        if driver is not None:
            try:
                driver.start()
                self._streams.append(driver)
                try:
                    lat = driver.latency
                    lat = sum(lat) if isinstance(lat, (tuple, list)) else lat
                    self.latency_ms = float(lat) * 1000
                except Exception:
                    self.latency_ms = 0.0
            except Exception as e:
                self.errors.append(f"No pude iniciar el audio: {e}")
                driver = None
        if driver is None:
            # Sin salida monitor: igual procesamos (medidores, tonalidad, Stream) con un reloj propio
            th = threading.Thread(target=self._clock_loop, args=(deliver,), daemon=True)
            th.start()
            self._threads.append(th)

        th = threading.Thread(target=self._analysis_loop, daemon=True)
        th.start()
        self._threads.append(th)
        self.running = True

    def _start_loopback(self, ref: dv.DeviceRef, target: int):
        if not dv.SC_AVAILABLE:
            raise RuntimeError(f"soundcard no disponible: {dv.SC_ERROR}")
        ring = RingBuffer(self.sr * 2, 2, target)
        self._rings["pc"] = ring

        def loop():
            try:
                lb = dv._sc().get_microphone(id=ref.name, include_loopback=True)
                with lb.recorder(samplerate=self.sr, channels=2, blocksize=self.block * 4) as rec:
                    while not self._stop.is_set():
                        data = rec.record(numframes=self.block)
                        ring.write(data)
            except Exception as e:
                self.errors.append(f"Loopback: {e}")

        th = threading.Thread(target=loop, daemon=True)
        th.start()
        self._threads.append(th)

    def _clock_loop(self, deliver=None):
        period = self.block / self.sr
        nxt = time.perf_counter()
        while not self._stop.is_set():
            outs = self.process_block(self.block)
            if deliver:
                deliver(outs)
            nxt += period
            time.sleep(max(0.0, nxt - time.perf_counter()))

    def _analysis_loop(self):
        tick = 0
        while not self._stop.is_set():
            try:
                self.detected_key = self.key_detector.update()
            except Exception as e:
                self.errors.append(f"Detector de tonalidad: {e}")
            tick += 1
            if tick % 4 == 0:
                self.write_diag()
            self._stop.wait(0.5)

    def stop(self):
        self._stop.set()
        for st in self._streams:
            try:
                st.stop()
                st.close()
            except Exception:
                pass
        self._streams = []
        for th in self._threads:
            th.join(timeout=1.0)
        self._threads = []
        self._rings = {}
        self.running = False

    def restart(self):
        self.stop()
        self.start()

    # -- sesión ---------------------------------------------------------------
    def to_dict(self) -> dict:
        ref = lambda r: r.to_dict() if r else None  # noqa: E731
        return {
            "version": 1,
            "audio": {
                "sample_rate": self.sr, "block": self.block,
                "mic": ref(self.mic_dev), "pc": ref(self.pc_dev),
                "monitor": ref(self.monitor_dev), "stream": ref(self.stream_dev),
            },
            "manual_key": [self.manual_key.root, self.manual_key.mode] if self.manual_key else None,
            "mixer": self.mixer.to_dict(),
        }

    def load_dict(self, d: dict):
        a = d.get("audio", {})
        self.sr = int(a.get("sample_rate", self.sr))
        self.block = max(MIN_BLOCK, int(a.get("block", self.block)))
        for attr in ("mic", "pc", "monitor", "stream"):
            if attr in a:
                setattr(self, f"{attr}_dev", dv.DeviceRef.from_dict(a[attr]))
        mk = d.get("manual_key")
        self.manual_key = Key(mk[0], mk[1]) if mk else None
        self.mixer = Mixer(self.sr)
        self.mixer.load_dict(d.get("mixer", {}))
        if not self.mixer.strips:
            self.mixer = default_mixer(self.sr)
        self.key_detector = KeyDetector(self.sr)

    def save(self, path: Path | None = None):
        path = path or CONFIG_DIR / "session.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    def load(self, path: Path | None = None) -> bool:
        path = path or CONFIG_DIR / "session.json"
        if not path.exists():
            return False
        try:
            self.load_dict(json.loads(path.read_text(encoding="utf-8")))
            return True
        except Exception as e:
            self.errors.append(f"No pude cargar la sesión: {e}")
            return False
