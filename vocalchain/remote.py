"""Conexión con VocalEngine (el motor de audio en C++).

La interfaz no procesa audio: manda comandos JSON al motor por TCP local y
recibe medidores ~30 veces por segundo. Las clases Remote* imitan a las del
mezclador viejo (Strip, efectos...) para que la interfaz las use igual.
"""
from __future__ import annotations

import itertools
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from PySide6.QtCore import QObject, Signal

from . import devices as dv
from . import keyparse
from .keydetect import KeyDetector
from .music import NOTE_NAMES, Key

APP_ROOT = Path(__file__).resolve().parent.parent
ENGINE_EXE = APP_ROOT / "engine" / "VocalEngine.exe"
CONFIG_DIR = Path.home() / ".vocalchain"
UI_SETTINGS = CONFIG_DIR / "ui_settings.json"
DIAG_FILE = CONFIG_DIR / "diag.log"
PORT = 47800
OUTPUTS = ("monitor", "stream")
OUTPUT_LABELS = {"monitor": "Monitor", "stream": "Stream"}
MIC_CHANNEL_CHOICES = [str(i) for i in range(1, 9)] + ["1+2"]
CABLE_RENDER = "CABLE Input"
CABLE_CAPTURE = "CABLE Output (VB-Audio Virtual Cable)"

_AUTOTUNE_RE = re.compile(r"auto.?tune|autopitch|graillon|melodyne|pitch.?correct|retune|antares|melda|waves tune|vocal tune", re.I)
_AUTOKEY_RE = re.compile(r"auto.?key", re.I)


class EngineError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Parámetros (formato que entiende widgets.ParamControl)
# ---------------------------------------------------------------------------
@dataclass
class Param:
    id: str
    label: str
    kind: str = "float"
    min: float = 0.0
    max: float = 1.0
    default: Any = 0.0
    unit: str = ""
    choices: list = field(default_factory=list)
    log: bool = False
    decimals: int = 1

    @staticmethod
    def from_json(d: dict) -> "Param":
        return Param(d["id"], d["label"], d.get("kind", "float"), d.get("min", 0), d.get("max", 1),
                     d.get("default", 0), d.get("unit", ""), d.get("choices", []), d.get("log", False),
                     d.get("decimals", 1))


# ---------------------------------------------------------------------------
# Cliente TCP
# ---------------------------------------------------------------------------
class EngineClient(QObject):
    meters = Signal(dict)
    event = Signal(dict)
    connection_changed = Signal(bool)

    def __init__(self, port: int = PORT):
        super().__init__()
        self.port = port
        self.sock: socket.socket | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, tuple[threading.Event, list]] = {}
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.connected = False

    # -- conexión -----------------------------------------------------------
    def _try_connect(self) -> bool:
        try:
            s = socket.create_connection(("127.0.0.1", self.port), timeout=0.5)
        except OSError:
            return False
        s.settimeout(None)
        self.sock = s
        self.connected = True
        threading.Thread(target=self._reader, args=(s,), daemon=True, name="engine-reader").start()
        self.connection_changed.emit(True)
        return True

    def ensure_running(self, wait_s: float = 45.0) -> str | None:
        """Conecta con el motor; si no está abierto, lo lanza. Devuelve un error o None."""
        if self.connected:
            return None
        if self._try_connect():
            return None
        if not ENGINE_EXE.exists():
            return f"No encontré el motor en {ENGINE_EXE}"
        flags = 0
        if sys.platform == "win32":
            flags = 0x00000200 | 0x08000000  # CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        try:
            self.proc = subprocess.Popen([str(ENGINE_EXE), "--port", str(self.port)], cwd=str(ENGINE_EXE.parent),
                                         creationflags=flags)
        except OSError as e:
            return f"No pude abrir el motor: {e}"
        t0 = time.time()
        while time.time() - t0 < wait_s:   # al abrir puede tardar cargando plugins (iLok, etc.)
            if self._try_connect():
                return None
            if self.proc.poll() is not None:
                return f"El motor se cerró al iniciar (código {self.proc.returncode}). Revisa {CONFIG_DIR / 'engine.log'}"
            time.sleep(0.25)
        return "El motor no respondió a tiempo"

    def close(self):
        s, self.sock = self.sock, None
        self.connected = False
        if s:
            try:
                s.close()
            except OSError:
                pass

    def _reader(self, s: socket.socket):
        buf = b""
        try:
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line.decode("utf-8", "replace"))
                    except ValueError:
                        continue
                    if "event" in msg:
                        if msg["event"] == "meters":
                            self.meters.emit(msg)
                        else:
                            self.event.emit(msg)
                    elif "id" in msg:
                        with self._lock:
                            slot = self._pending.pop(msg["id"], None)
                        if slot:
                            slot[1].append(msg)
                            slot[0].set()
        except OSError:
            pass
        if s is self.sock or self.sock is None:
            self.connected = False
            self.sock = None
            with self._lock:
                for ev, box in self._pending.values():
                    box.append({"ok": False, "error": "motor desconectado"})
                    ev.set()
                self._pending.clear()
            self.connection_changed.emit(False)

    # -- comandos -------------------------------------------------------------
    def call(self, cmd: str, timeout: float = 10.0, **kw) -> dict:
        if not self.connected or self.sock is None:
            raise EngineError("El motor no está conectado")
        mid = next(self._ids)
        ev, box = threading.Event(), []
        with self._lock:
            self._pending[mid] = (ev, box)
        data = (json.dumps({"cmd": cmd, "id": mid, **kw}) + "\n").encode("utf-8")
        try:
            with self._send_lock:
                self.sock.sendall(data)
        except OSError as e:
            with self._lock:
                self._pending.pop(mid, None)
            raise EngineError(f"No pude hablar con el motor: {e}")
        if not ev.wait(timeout):
            with self._lock:
                self._pending.pop(mid, None)
            raise EngineError(f"El motor no respondió a '{cmd}'")
        resp = box[0]
        if not resp.get("ok"):
            raise EngineError(resp.get("error", "error desconocido"))
        return resp.get("data")

    def call_async(self, cmd: str, done=None, timeout: float = 60.0, **kw):
        """Llama en un hilo aparte; done(result, error) se invoca desde ese hilo."""
        def run():
            try:
                r = self.call(cmd, timeout=timeout, **kw)
                if done:
                    done(r, None)
            except Exception as e:  # noqa: BLE001
                if done:
                    done(None, str(e))
        threading.Thread(target=run, daemon=True).start()

    def fire(self, cmd: str, **kw):
        """Manda sin esperar respuesta (para sliders)."""
        try:
            self.call_async(cmd, None, **kw)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Espejos de los objetos del motor
# ---------------------------------------------------------------------------
class RawParam:
    """Parámetro nativo de un plugin (VST3)."""

    def __init__(self, fx: "RemoteFx", d: dict):
        self.fx = fx
        self.index = d["index"]
        self.name = d["name"]
        self._value = d["value"]
        self.string_value = d.get("text", "")
        self.label = d.get("label", "")
        self.num_steps = d.get("steps", 0)

    @property
    def raw_value(self) -> float:
        return self._value

    @raw_value.setter
    def raw_value(self, v: float):
        self._value = v
        r = self.fx.engine.client.call("set_plugin_param", strip=self.fx.strip.id, slot=self.fx.uid, index=self.index, value=v)
        self.string_value = (r or {}).get("text", self.string_value)


class RemoteFx:
    def __init__(self, strip: "RemoteStrip", d: dict):
        self.strip = strip
        self.engine = strip.engine
        self.status = ""
        self._raw_cache: list[RawParam] | None = None
        self.update(d)

    def update(self, d: dict):
        self.uid = d["uid"]
        self.type_id = d["type"]
        self._title = d.get("title", self.type_id)
        self._enabled = d.get("enabled", True)
        self.failed = d.get("failed", False)
        self.error = d.get("error", "")
        self.values = dict(d.get("params", {}))
        self.path = d.get("path", "")
        self.plugin_name = d.get("plugin_name", "")
        self.has_editor = d.get("has_editor", False)
        self.latency = int(d.get("latency", 0) or 0)   # muestras que agrega el plugin
        if self.is_plugin:
            self.values.setdefault("auto_key", self.engine.settings.get("auto_key", {}).get(self.uid, True))

    # -- propiedades usadas por la interfaz ---------------------------------
    @property
    def is_plugin(self) -> bool:
        return self.type_id == "vst3"

    @property
    def title(self) -> str:
        return self._title

    @property
    def display_name(self) -> str:
        return self._title

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, on: bool):
        self._enabled = bool(on)
        self.engine.client.call("set_fx", strip=self.strip.id, slot=self.uid, enabled=bool(on))

    @property
    def looks_like_autotune(self) -> bool:
        return self.is_plugin and bool(_AUTOTUNE_RE.search(f"{self.plugin_name} {self.path}")) and not self.is_autokey

    @property
    def is_proq(self) -> bool:
        return self.is_plugin and bool(re.search(r"pro.?q", f"{self.plugin_name} {self.path}", re.I))

    @property
    def is_autokey(self) -> bool:
        return self.is_plugin and bool(_AUTOKEY_RE.search(f"{self.plugin_name} {self.path}"))

    def specs(self) -> list[Param]:
        if self.is_plugin:
            if self.looks_like_autotune:
                return [Param("auto_key", "Ajustar tonalidad automáticamente", "bool", default=True)]
            return []
        info = self.engine.builtin_specs.get(self.type_id)
        return list(info["params"]) if info else []

    def get(self, pid):
        return self.values.get(pid)

    def set(self, pid: str, value):
        self.values[pid] = value
        if self.is_plugin:
            if pid == "auto_key":
                self.engine.settings.setdefault("auto_key", {})[self.uid] = bool(value)
                self.engine.save_settings()
            return
        self.engine.client.fire("set_fx", strip=self.strip.id, slot=self.uid, params={pid: value})

    # -- plugins ------------------------------------------------------------
    def raw_params(self, refresh: bool = False) -> list[RawParam]:
        if not self.is_plugin or self.failed:
            return []
        if self._raw_cache is None or refresh:
            try:
                data = self.engine.client.call("plugin_params", strip=self.strip.id, slot=self.uid)
                self._raw_cache = [RawParam(self, d) for d in data]
            except EngineError:
                self._raw_cache = []
        return self._raw_cache

    def show_editor(self):
        self.engine.client.call("open_editor", strip=self.strip.id, slot=self.uid)

    def set_param_text(self, index: int, texts: list[str]) -> str | None:
        try:
            r = self.engine.client.call("set_plugin_param_text", strip=self.strip.id, slot=self.uid, index=index, texts=texts)
            return r.get("text")
        except EngineError:
            return None

    def on_key(self, key: Key | None):
        """Pone la tonalidad en Auto-Tune (u otro autotune VST) buscando sus parámetros Key/Scale."""
        if key is None or not self.looks_like_autotune or not self.values.get("auto_key", True):
            return
        params = self.raw_params(refresh=True)
        root = NOTE_NAMES[key.root]
        flats = {"C#": "Db", "D#": "Eb", "F#": "Gb", "G#": "Ab", "A#": "Bb"}
        root_texts = [root, flats.get(root, root), f"{root}/{flats.get(root, root)}", f"{flats.get(root, root)}/{root}"]
        mode_texts = ["major", "maj", "mayor"] if key.mode == "major" else ["minor", "min", "menor", "natural minor"]
        done = []
        for p in params:
            n = p.name.lower()
            if re.search(r"\b(key|root|tonic|tonalidad|nota)\b", n) and "detect" not in n:
                t = self.set_param_text(p.index, root_texts)
                if t:
                    done.append(f"{p.name}={t}")
            elif re.search(r"\b(scale|mode|escala)\b", n):
                t = self.set_param_text(p.index, mode_texts)
                if t:
                    done.append(f"{p.name}={t}")
        self.status = ("Tonalidad → " + ", ".join(done)) if done else "No encontré los parámetros Key/Scale de este plugin"
        self._raw_cache = None


class RemoteOutputs(dict):
    """dict de salidas de un canal que avisa al motor al cambiar."""

    def __init__(self, strip: "RemoteStrip", d: dict):
        super().__init__(d)
        self.strip = strip

    def __setitem__(self, k, v):
        super().__setitem__(k, bool(v))
        self.strip.engine.client.fire("set_strip", strip=self.strip.id, outputs={k: bool(v)})


class RemoteStrip:
    def __init__(self, engine: "RemoteEngine", d: dict):
        self.engine = engine
        self.peak = 0.0
        self.in_peak = 0.0
        self.clip_count = 0
        self.last_clip = 0.0
        self.cpu_us = 0.0          # tiempo que tarda su cadena por bloque (máximo reciente)
        self.chain: list[RemoteFx] = []
        self.update(d)

    def update(self, d: dict):
        self.id = d["id"]
        self.name = d["name"]
        self.kind = d.get("kind", "input")
        self.source = d.get("source") or None
        self.input_mode = d.get("input_mode", "asio")
        self.channels = list(d.get("channels", [0]))
        self._gain_db = d.get("gain_db", 0.0)
        self._pan = d.get("pan", 0.0)
        self._mute = d.get("mute", False)
        self._solo = d.get("solo", False)
        self._sends = dict(d.get("sends", {}))
        self.outputs = RemoteOutputs(self, d.get("outputs", {"monitor": True, "stream": True}))
        old = {fx.uid: fx for fx in self.chain}
        chain = []
        for fd in d.get("chain", []):
            fx = old.get(fd["uid"])
            if fx:
                fx.update(fd)
            else:
                fx = RemoteFx(self, fd)
            chain.append(fx)
        self.chain = chain

    def _set(self, **kw):
        self.engine.client.fire("set_strip", strip=self.id, **kw)

    @property
    def gain_db(self):
        return self._gain_db

    @gain_db.setter
    def gain_db(self, v):
        self._gain_db = float(v)
        self._set(gain_db=float(v))

    @property
    def pan(self):
        return self._pan

    @pan.setter
    def pan(self, v):
        self._pan = float(v)
        self._set(pan=float(v))

    @property
    def mute(self):
        return self._mute

    @mute.setter
    def mute(self, v):
        self._mute = bool(v)
        self._set(mute=bool(v))

    @property
    def solo(self):
        return self._solo

    @solo.setter
    def solo(self, v):
        self._solo = bool(v)
        self._set(solo=bool(v))

    @property
    def sends(self):
        return dict(self._sends)

    @sends.setter
    def sends(self, d):
        self._sends = dict(d)
        self._set(sends={k: float(v) for k, v in d.items()})

    @property
    def removable(self) -> bool:
        return self.id not in ("mic", "pc")

    @property
    def input_channel(self) -> str:
        if self.channels == [0, 1]:
            return "1+2"
        return str((self.channels or [0])[0] + 1)

    @input_channel.setter
    def input_channel(self, sel: str):
        self.channels = [0, 1] if sel == "1+2" else [max(0, int(sel) - 1)]
        self.engine.client.call("set_strip", strip=self.id, channels=self.channels)


class RemoteOutput:
    def __init__(self, engine: "RemoteEngine", name: str, d: dict):
        self.engine = engine
        self.name = name
        self.peak = 0.0
        self.pre_peak = 0.0
        self.limit_hits = 0
        self._gain_db = d.get("gain_db", 0.0)
        self._mute = d.get("mute", False)
        self._limiter = d.get("limiter", True)

    def _set(self, **kw):
        self.engine.client.fire("set_output", output=self.name, **kw)

    @property
    def gain_db(self):
        return self._gain_db

    @gain_db.setter
    def gain_db(self, v):
        self._gain_db = float(v)
        self._set(gain_db=float(v))

    @property
    def mute(self):
        return self._mute

    @mute.setter
    def mute(self, v):
        self._mute = bool(v)
        self._set(mute=bool(v))

    @property
    def safety_limiter(self):
        return self._limiter

    @safety_limiter.setter
    def safety_limiter(self, v):
        self._limiter = bool(v)
        self._set(limiter=bool(v))


class RemoteMixer:
    def __init__(self, engine: "RemoteEngine"):
        self.engine = engine
        self.strips: list[RemoteStrip] = []
        self.outputs = {o: RemoteOutput(engine, o, {}) for o in OUTPUTS}
        self.blocked: set[str] = set()
        self.pc_system_volume = False

    def load(self, state: dict):
        old = {s.id: s for s in self.strips}
        strips = []
        for sd in state.get("strips", []):
            s = old.get(sd["id"])
            if s:
                s.update(sd)
            else:
                s = RemoteStrip(self.engine, sd)
            strips.append(s)
        self.strips = strips
        for o in OUTPUTS:
            self.outputs[o] = RemoteOutput(self.engine, o, state.get("outputs", {}).get(o, {}))

    def strip(self, sid):
        return next((s for s in self.strips if s.id == sid), None)

    def by_name(self, name):
        return next((s for s in self.strips if s.name == name), None)

    @property
    def inputs(self):
        return [s for s in self.strips if s.kind == "input"]

    @property
    def buses(self):
        return [s for s in self.strips if s.kind == "bus"]


# ---------------------------------------------------------------------------
# Captura del audio del PC para detectar la tonalidad (no toca el audio que suena)
# ---------------------------------------------------------------------------
class PcAnalyzer:
    def __init__(self, detector: KeyDetector):
        self.detector = detector
        self.device = ""
        self.error = ""
        self._stop = threading.Event()
        self._thread = None

    def start(self, speaker_hint: str = CABLE_RENDER):
        self.stop()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(speaker_hint, self._stop), daemon=True, name="pc-analyzer")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self, hint: str, stop: threading.Event):
        if not dv.SC_AVAILABLE:
            self.error = "soundcard no disponible"
            return
        try:
            sc = dv._sc()
            spk = None
            for s in sc.all_speakers():
                if hint.lower() in s.name.lower():
                    spk = s
            spk = spk or sc.default_speaker()
            self.device = spk.name
            mic = sc.get_microphone(id=spk.name, include_loopback=True)
            sr = self.detector.sr
            with mic.recorder(samplerate=sr, channels=2, blocksize=4096) as rec:
                while not stop.is_set():
                    data = rec.record(numframes=2048)
                    self.detector.push(np.asarray(data, dtype=np.float32).mean(axis=1))
        except Exception as e:  # noqa: BLE001
            self.error = f"Análisis del PC: {e}"


# ---------------------------------------------------------------------------
# Motor remoto (lo que usa la interfaz)
# ---------------------------------------------------------------------------
class RemoteEngine(QObject):
    state_changed = Signal()
    meters_updated = Signal()

    def __init__(self):
        super().__init__()
        self.client = EngineClient()
        self.client.meters.connect(self._on_meters)
        self.client.event.connect(self._on_event)
        self.client.connection_changed.connect(self._on_connection)
        self.mixer = RemoteMixer(self)
        self.builtin_specs: dict[str, dict] = {}
        self.settings = self._load_settings()
        self.manual_key: Key | None = None
        mk = self.settings.get("manual_key")
        if mk:
            self.manual_key = Key(mk[0], mk[1])
        self.detected_key: Key | None = None
        self.key_detector = KeyDetector(48000)
        self.analyzer = PcAnalyzer(self.key_detector)
        self.errors: list[str] = []
        self.notes: list[str] = []
        self.stats: dict = {}
        self.audio: dict = {}
        self.running = False
        self.sysvol = None
        self._prev_default = None
        self._stop = threading.Event()
        self._key_thread = None

    # -- ajustes de la interfaz ------------------------------------------------
    @staticmethod
    def _load_settings() -> dict:
        try:
            return json.loads(UI_SETTINGS.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {"windows_to_cable": True, "use_autokey": True, "auto_key": {}}

    def save_settings(self):
        self.settings["manual_key"] = [self.manual_key.root, self.manual_key.mode] if self.manual_key else None
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            UI_SETTINGS.write_text(json.dumps(self.settings, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    # -- estado ---------------------------------------------------------------
    @property
    def key(self) -> Key | None:
        return self.manual_key or self.detected_key

    @property
    def mode(self) -> str:
        return self.stats.get("type", "")

    @property
    def sr(self) -> int:
        return int(self.stats.get("sr", self.audio.get("sample_rate", 48000)) or 48000)

    @property
    def block(self) -> int:
        return int(self.stats.get("block", 0) or 0)

    @property
    def latency_ms(self) -> float:
        return float(self.stats.get("latency_ms", 0.0) or 0.0)

    @property
    def cpu(self) -> float:
        return float(self.stats.get("cpu", 0.0) or 0.0)

    @property
    def rec(self) -> dict:
        return self.stats.get("rec", {}) or {}

    @property
    def block_us(self) -> float:
        return 1e6 * self.block / self.sr if self.block else 0.0

    @property
    def total_xruns(self) -> int:
        x = self.stats.get("xruns", 0)
        return max(0, int(x or 0)) + int(self.stats.get("late", 0) or 0)

    # -- ciclo de vida --------------------------------------------------------
    def start(self) -> bool:
        self.errors = []
        self.notes = []
        err = self.client.ensure_running()
        if err:
            self.errors.append(err)
            return False
        try:
            self.builtin_specs = {
                t["type"]: {"label": t["label"], "category": t["category"], "params": [Param.from_json(p) for p in t["params"]]}
                for t in self.client.call("describe")
            }
            self.refresh_state()
            self.audio = self.client.call("audio_devices").get("current", {})
            for k, label in (("error", "Audio"), ("pc_error", "Audio del PC"), ("stream_error", "Stream")):
                if self.audio.get(k):
                    self.errors.append(f"{label}: {self.audio[k]}")
        except EngineError as e:
            self.errors.append(str(e))
            return False
        if self.settings.get("windows_to_cable", True):
            self.route_windows_to_cable(True)
        self.analyzer.start(CABLE_RENDER if self.settings.get("windows_to_cable", True) else "")
        self._stop.clear()
        self._key_thread = threading.Thread(target=self._key_loop, daemon=True, name="key-loop")
        self._key_thread.start()
        self.running = True
        return True

    def stop(self, quit_engine: bool = True):
        self._stop.set()
        self.analyzer.stop()
        if quit_engine and self.client.connected:
            try:
                self.client.call("quit", timeout=5)
            except EngineError:
                pass
        self.client.close()
        self.route_windows_to_cable(False)
        self.running = False

    def refresh_state(self):
        state = self.client.call("get_state")
        self.audio_state = state.get("audio", {})
        self.mixer.load(state)
        self.state_changed.emit()

    def save(self):
        self.save_settings()
        if self.client.connected:
            try:
                self.client.call("save")
            except EngineError:
                pass

    # -- Windows → VB-Cable ------------------------------------------------------
    def route_windows_to_cable(self, on: bool):
        from . import winvolume as wv
        if not wv.AVAILABLE:
            return
        try:
            if on:
                cur = wv.get_default_render()
                cable = wv.find_render(CABLE_RENDER)
                if cable is None:
                    self.errors.append("No encontré VB-Cable (CABLE Input) en Windows")
                    return
                if cur and cur[0] != cable[0]:
                    self._prev_default = cur
                    self.settings["restore_output"] = list(cur)
                    self.save_settings()
                    wv.set_default_render(cable[0])
                    self.notes.append(f"Salida de Windows → {cable[1]} (se devuelve a {cur[1]} al cerrar)")
            else:
                prev = self._prev_default or (tuple(self.settings["restore_output"]) if self.settings.get("restore_output") else None)
                if prev:
                    wv.set_default_render(prev[0])
                    self.settings.pop("restore_output", None)
                    self.save_settings()
                    self._prev_default = None
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"No pude cambiar la salida de Windows: {e}")

    # -- eventos del motor --------------------------------------------------------
    def _on_connection(self, ok: bool):
        if not ok and self.running:
            self.errors.append("Se perdió la conexión con el motor de audio (¿se cerró o falló un plugin?). Pulsa ⏻ para reabrirlo.")
            self.running = False
            self.state_changed.emit()

    def _on_event(self, ev: dict):
        if ev.get("event") == "error":
            self.errors.append(ev.get("message", "error del motor"))
        elif ev.get("event") == "audio_changed":
            pass

    def _on_meters(self, m: dict):
        now = time.monotonic()
        for sid, v in m.get("strips", {}).items():
            s = self.mixer.strip(sid)
            if not s:
                continue
            s.peak = max(v.get("peak", 0.0), s.peak * 0.8)
            s.in_peak = max(v.get("in", 0.0), s.in_peak * 0.8)
            clips = v.get("clips", 0)
            if clips > s.clip_count:
                s.last_clip = now
            s.clip_count = clips
            cpu = float(v.get("cpu_us", 0.0) or 0.0)
            s.cpu_us = cpu if cpu >= s.cpu_us else s.cpu_us * 0.9 + cpu * 0.1
        for uid, val in m.get("slots", {}).items():
            for s in self.mixer.strips:
                for fx in s.chain:
                    if fx.uid == uid:
                        fx.status = f"-{val:.1f} dB" if val > 0.05 else "sin reducción"
        for o, v in m.get("out", {}).items():
            out = self.mixer.outputs.get(o)
            if out:
                out.peak = max(v.get("peak", 0.0), out.peak * 0.8)
                out.pre_peak = max(v.get("pre", 0.0), out.pre_peak * 0.8)
                out.limit_hits = v.get("hits", 0)
        self.stats = m.get("stats", {})
        self.meters_updated.emit()

    # -- tonalidad + diagnóstico (hilo propio) ---------------------------------------
    def _key_loop(self):
        n = 0
        while not self._stop.is_set():
            try:
                self.detected_key = self.key_detector.update()
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"Detector de tonalidad: {e}")
            n += 1
            if n % 4 == 0:
                self._write_diag()
            if n % 4 == 2 and self.settings.get("use_autokey", True):
                self._poll_autokey()
            self._stop.wait(0.5)

    def _poll_autokey(self):
        """Lee Auto-Key (si está en la cadena del PC) y usa su resultado si expone la tonalidad."""
        for s in self.mixer.strips:
            for fx in s.chain:
                if not fx.is_autokey or not fx.enabled or fx.failed:
                    continue
                params = fx.raw_params(refresh=True)
                key_txt = scale_txt = None
                for p in params:
                    n = p.name.lower()
                    if re.search(r"\bkey\b", n) and key_txt is None:
                        key_txt = p.string_value          # Auto-Key 2: "Key/Scale" = "A Minor"
                    elif re.search(r"\bscale\b", n) and scale_txt is None:
                        scale_txt = p.string_value
                self.autokey_params = [(p.name, p.string_value) for p in params[:40]]
                k = parse_key_text(key_txt, scale_txt)
                if k is not None:
                    self.autokey_key = k
                    if self.detected_key is None or self.detected_key.confidence < 0.85:
                        self.detected_key = Key(k.root, k.mode, 0.9)
                return

    def _write_diag(self):
        st = self.stats
        mic = self.mixer.by_name("Mic")
        db = lambda v: f"{20 * np.log10(max(v, 1e-6)):+.1f}"  # noqa: E731
        line = (f"{time.strftime('%H:%M:%S')} motor={st.get('type', '-')}/{st.get('device', '-')} sr={st.get('sr')} "
                f"bloque={st.get('block')} latencia={st.get('latency_ms', 0):.1f}ms cpu={st.get('cpu', 0) * 100:.0f}% "
                f"xruns={st.get('xruns')} tarde={st.get('late')} proc_max={st.get('proc_max_ms', 0):.2f}ms "
                f"pc={'on' if st.get('pc_active') else 'off'} pc_buf={st.get('pc_ms', 0):.1f}ms pc_under={st.get('pc_under')} "
                f"ratio={st.get('pc_ratio', 1):.5f} mic_in={db(mic.in_peak) if mic else '-'} clips={mic.clip_count if mic else 0} "
                f"tono={self.key.short if self.key else '-'} analizador={self.analyzer.device or self.analyzer.error or '-'}")
        if getattr(self, "autokey_params", None):
            line += f" autokey={self.autokey_params[:12]}"
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            if DIAG_FILE.exists() and DIAG_FILE.stat().st_size > 300_000:
                DIAG_FILE.write_text(DIAG_FILE.read_text(encoding="utf-8")[-100_000:], encoding="utf-8")
            with DIAG_FILE.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def parse_key_text(key_txt: str | None, scale_txt: str | None = None) -> Key | None:
    """Auto-Key 2 da un solo texto ("A Minor", "Chromatic" mientras escucha). Si el plugin separa
    tonalidad y escala, se juntan. Devuelve None si todavía no detectó nada."""
    if not key_txt:
        return None
    k = keyparse.parse_key_text(key_txt)
    if k is None:
        return None
    minor = k.minor
    st = (scale_txt or "").lower()
    if not re.search(r"(minor|menor|major|mayor|\bm\b|maj)", key_txt.lower()):
        if "minor" in st or "menor" in st:
            minor = True
        elif "major" in st or "mayor" in st:
            minor = False
    return Key(k.root, "minor" if minor else "major", 0.9)


def find_vst3(extra_dirs: list[str] = ()) -> list[Path]:
    dirs = [r"C:\Program Files\Common Files\VST3", os.path.expandvars(r"%LOCALAPPDATA%\Programs\Common\VST3"), *extra_dirs]
    found = []
    for base in dirs:
        if not base or not os.path.isdir(base):
            continue
        for root, sub, files in os.walk(base):
            for d in list(sub):
                if d.lower().endswith(".vst3"):
                    found.append(Path(root) / d)
                    sub.remove(d)
            for f in files:
                if f.lower().endswith(".vst3"):
                    found.append(Path(root) / f)
    return sorted(set(found), key=lambda p: p.stem.lower())
