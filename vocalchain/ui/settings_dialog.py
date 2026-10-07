"""Diálogo de audio: driver, buffer, salidas, VB-Cable y Stream (todo lo abre el motor)."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QSpinBox, QVBoxLayout,
)

from ..remote import CABLE_CAPTURE, EngineError

HELP = """<b>Latencia mínima con la Focusrite:</b> Driver <b>ASIO</b>, buffer <b>64</b> (o 128 si oyes crujidos),
48 kHz. El motor está en C++, así que aguanta buffers chicos.<br><br>
<b>Música del PC por VB-Cable:</b> la app pone <i>CABLE Input</i> como salida de Windows y escucha
<i>CABLE Output</i>. Para que la música llegue con poco retraso: abre <i>VBCABLE_ControlPanel</i>
(en C:\\Program Files\\VB\\CABLE) → <b>Max Latency: 1024 o 2048</b>, y en Windows pon CABLE Input y
CABLE Output a <b>48000 Hz</b> (Sonido → Propiedades → Opciones avanzadas).<br><br>
<b>Stream</b> (opcional): segunda salida para OBS/Discord (no puede ser ASIO)."""


class SettingsDialog(QDialog):
    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Audio")
        self.engine = engine
        self.setMinimumWidth(680)
        lay = QVBoxLayout(self)
        try:
            self.info = engine.client.call("audio_devices", timeout=20)
        except EngineError as e:
            self.info = {"types": {}, "capture_devices": [], "playback_devices": [], "current": {}}
            w = QLabel(f"El motor no responde: {e}")
            w.setObjectName("warn")
            lay.addWidget(w)
        cur = self.info.get("current", {})
        form = QFormLayout()

        self.driver = QComboBox()
        types = list(self.info.get("types", {}).keys())
        types.sort(key=lambda t: (t != "ASIO", t))
        for t in types:
            label = {"ASIO": "ASIO — interfaz de audio (menor latencia)",
                     "Windows Audio": "Windows Audio (WASAPI compartido)",
                     "Windows Audio (Low Latency Mode)": "Windows Audio — baja latencia",
                     "Windows Audio (Exclusive Mode)": "Windows Audio — exclusivo"}.get(t, t)
            self.driver.addItem(label, t)
        if cur.get("driver_type") in types:
            self.driver.setCurrentIndex(types.index(cur["driver_type"]))
        self.device = QComboBox()
        self.driver.currentIndexChanged.connect(self._fill_devices)

        self.rate = QComboBox()
        rates = sorted(set(int(r) for r in cur.get("sample_rates", []) or [44100, 48000, 96000]))
        for r in rates:
            self.rate.addItem(f"{r} Hz", r)
        if cur.get("sample_rate"):
            i = self.rate.findData(int(cur["sample_rate"]))
            if i >= 0:
                self.rate.setCurrentIndex(i)
        self.buffer = QComboBox()
        sizes = cur.get("buffer_sizes") or [32, 64, 96, 128, 192, 256, 512, 1024]
        sr = cur.get("sample_rate") or 48000
        for b in sizes:
            self.buffer.addItem(f"{b} muestras (~{b / sr * 1000:.1f} ms)", int(b))
        if cur.get("buffer_size"):
            i = self.buffer.findData(int(cur["buffer_size"]))
            if i >= 0:
                self.buffer.setCurrentIndex(i)

        self.monitor = QComboBox()
        outs = cur.get("output_channels", []) or ["Salida 1", "Salida 2"]
        for i in range(0, max(1, len(outs) - 1), 2):
            self.monitor.addItem(f"{outs[i]} + {outs[i + 1] if i + 1 < len(outs) else ''}", i)
        j = self.monitor.findData(cur.get("monitor_left", 0))
        if j >= 0:
            self.monitor.setCurrentIndex(j)

        self.pc = QComboBox()
        self.pc.addItem("— Ninguno —", "")
        for n in self.info.get("capture_devices", []):
            self.pc.addItem(n, n)
        want = cur.get("pc_device") or CABLE_CAPTURE
        k = self.pc.findData(want)
        if k < 0:
            k = next((i for i in range(self.pc.count()) if "cable output" in self.pc.itemText(i).lower()), 0)
        self.pc.setCurrentIndex(k)
        self.pc_ms = QSpinBox()
        self.pc_ms.setRange(2, 100)
        self.pc_ms.setSuffix(" ms")
        self.pc_ms.setValue(int(cur.get("pc_buffer_ms", 10)))
        self.pc_ms.setToolTip("Colchón para pasar la música de VB-Cable a la interfaz. Menos = menos retraso;\n"
                              "si la música se corta, súbelo.")

        self.stream = QComboBox()
        self.stream.addItem("— Ninguna —", "")
        for n in self.info.get("playback_devices", []):
            self.stream.addItem(n, n)
        s = self.stream.findData(cur.get("stream_device", ""))
        self.stream.setCurrentIndex(max(0, s))

        form.addRow("🎛 Driver", self.driver)
        form.addRow("🎚 Interfaz", self.device)
        form.addRow("Frecuencia", self.rate)
        form.addRow("Buffer", self.buffer)
        form.addRow("🎧 Monitor (salidas)", self.monitor)
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        form.addRow(line)
        form.addRow("🔊 Música del PC", self.pc)
        form.addRow("Colchón VB-Cable", self.pc_ms)
        form.addRow("📡 Stream", self.stream)
        lay.addLayout(form)

        ins = cur.get("input_channels", [])
        if ins:
            w = QLabel("Entradas de la interfaz: " + ", ".join(f"{i + 1}={n}" for i, n in enumerate(ins))
                       + "  (elige cuál es el micrófono en la tira Mic)")
            w.setObjectName("dim")
            w.setWordWrap(True)
            lay.addWidget(w)

        row = QHBoxLayout()
        self.panel_btn = QPushButton("🎛 Panel del driver (Focusrite)")
        self.panel_btn.setEnabled(bool(cur.get("has_panel")))
        self.panel_btn.clicked.connect(self._panel)
        row.addWidget(self.panel_btn)
        row.addStretch(1)
        lay.addLayout(row)

        self.win_cable = QCheckBox("Al abrir la app, mandar la salida de Windows a VB-Cable (y devolverla al cerrar)")
        self.win_cable.setChecked(engine.settings.get("windows_to_cable", True))
        lay.addWidget(self.win_cable)
        self.autokey = QCheckBox("Usar Auto-Key de Antares (si está en el canal PC) para la tonalidad")
        self.autokey.setChecked(engine.settings.get("use_autokey", True))
        lay.addWidget(self.autokey)

        for k, label in (("error", "Audio"), ("pc_error", "Música del PC"), ("stream_error", "Stream")):
            if cur.get(k):
                w = QLabel(f"⚠ {label}: {cur[k]}")
                w.setObjectName("warn")
                w.setWordWrap(True)
                lay.addWidget(w)

        h = QLabel(HELP)
        h.setWordWrap(True)
        h.setObjectName("dim")
        lay.addWidget(h)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Aplicar")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._fill_devices()

    def _fill_devices(self):
        t = self.driver.currentData()
        cur = self.info.get("current", {})
        names = self.info.get("types", {}).get(t, {}).get("outputs", [])
        self.device.clear()
        for n in names:
            self.device.addItem(n, n)
        i = self.device.findData(cur.get("device", ""))
        if i < 0:
            i = next((k for k, n in enumerate(names) if "focusrite" in n.lower()), 0)
        self.device.setCurrentIndex(max(0, i))

    def _panel(self):
        try:
            self.engine.client.call("asio_panel", timeout=120)
        except EngineError as e:
            QMessageBox.information(self, "Panel del driver", str(e))

    def apply_to(self, engine) -> str | None:
        engine.settings["windows_to_cable"] = self.win_cable.isChecked()
        engine.settings["use_autokey"] = self.autokey.isChecked()
        engine.save_settings()
        try:
            res = engine.client.call(
                "set_audio", timeout=60,
                driver_type=self.driver.currentData() or "ASIO",
                device=self.device.currentData() or "",
                sample_rate=self.rate.currentData() or 48000,
                buffer_size=self.buffer.currentData() or 128,
                monitor_left=self.monitor.currentData() or 0,
                pc_device=self.pc.currentData() or "",
                stream_device=self.stream.currentData() or "",
                pc_buffer_ms=self.pc_ms.value(),
            )
            cur = res.get("current", {})
            errs = [cur.get(k) for k in ("error", "pc_error", "stream_error") if cur.get(k)]
            return " | ".join(errs) if errs else None
        except EngineError as e:
            return str(e)
