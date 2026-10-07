"""Tiras del mezclador: canales (Mic, PC), buses de efectos y salidas."""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from ..remote import MIC_CHANNEL_CHOICES, OUTPUT_LABELS, OUTPUTS
from . import theme
from .widgets import Fader, Knob, LabeledKnob


def _btn(text, obj="small", checkable=True, tip=""):
    b = QPushButton(text)
    b.setObjectName(obj)
    b.setCheckable(checkable)
    b.setToolTip(tip)
    b.setFocusPolicy(Qt.NoFocus)
    return b


class StripWidget(QFrame):
    selected = Signal(object)        # Strip
    remove_requested = Signal(object)
    renamed = Signal(object)

    def __init__(self, strip, buses: list, engine=None, parent=None):
        super().__init__(parent)
        self.strip = strip
        self.engine = engine
        self.setObjectName("strip")
        self.setProperty("kind", "bus" if strip.kind == "bus" else "input")
        self.setFixedWidth(150)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        # cabecera
        head = QHBoxLayout()
        self.title = QLabel(strip.name)
        self.title.setObjectName("h1")
        self.title.setToolTip("Doble clic para renombrar" if strip.kind == "bus" else "")
        head.addWidget(self.title, 1)
        if strip.source == "mic":
            self.clip = QLabel("CLIP")
            self.clip.setToolTip("La entrada del micrófono está saturando ANTES de la app.\n"
                                 "Baja la perilla de gain del input en la Focusrite (el aro debe quedar verde, no rojo).")
            self.clip.setStyleSheet("color: #2d313a; font-weight: 800; font-size: 10px;")
            head.addWidget(self.clip)
        if strip.kind == "bus":
            rm = _btn("✕", checkable=False, tip="Quitar bus")
            rm.clicked.connect(lambda: self.remove_requested.emit(self.strip))
            head.addWidget(rm)
            self.title.mouseDoubleClickEvent = lambda e: self._rename()
        lay.addLayout(head)
        self.sub = QLabel({"mic": "🎤 Micrófono", "pc": "🔊 Música del PC"}.get(strip.source, "Bus de efectos"))
        self.sub.setObjectName("dim")
        lay.addWidget(self.sub)

        if strip.source == "mic":
            self.in_ch = QComboBox()
            self.in_ch.addItems(MIC_CHANNEL_CHOICES)
            self.in_ch.setCurrentText(strip.input_channel)
            self.in_ch.setToolTip("Entrada de la interfaz donde está el micrófono (ej. Input 1 de la Focusrite)")
            self.in_ch.currentTextChanged.connect(lambda t: setattr(self.strip, "input_channel", t))
            row = QHBoxLayout()
            row.addWidget(QLabel("Entrada"))
            row.addWidget(self.in_ch, 1)
            lay.addLayout(row)

        # botón de cadena
        self.fx_btn = QPushButton()
        self.fx_btn.setToolTip("Editar la cadena de efectos")
        self.fx_btn.clicked.connect(lambda: self.selected.emit(self.strip))
        lay.addWidget(self.fx_btn)
        self.refresh_fx()

        # envíos (solo canales de entrada)
        self.send_knobs: dict[str, Knob] = {}
        if strip.kind == "input" and buses:
            grid = QGridLayout()
            grid.setSpacing(2)
            for i, b in enumerate(buses):
                k = Knob(0, 1, strip.sends.get(b.id, 0.0), default=0.0, fmt=lambda v: f"Envío {v * 100:.0f}%")
                k.changed.connect(lambda v, bid=b.id: self._send(bid, v))
                self.send_knobs[b.id] = k
                grid.addWidget(LabeledKnob(b.name[:8], k), i // 3, i % 3)
            box = QLabel("Envíos")
            box.setObjectName("dim")
            lay.addWidget(box)
            lay.addLayout(grid)

        # pan
        pan = Knob(-1, 1, strip.pan, default=0.0, bipolar=True,
                   fmt=lambda v: "Centro" if abs(v) < 0.01 else (f"I {abs(v) * 100:.0f}" if v < 0 else f"D {v * 100:.0f}"))
        pan.changed.connect(lambda v: setattr(self.strip, "pan", v))
        prow = QHBoxLayout()
        prow.addStretch(1)
        prow.addWidget(LabeledKnob("Pan", pan))
        prow.addStretch(1)
        lay.addLayout(prow)

        # fader
        self.fader = Fader(strip.gain_db)
        self.fader.changed.connect(self._fader_moved)
        lay.addWidget(self.fader, 1)

        # mute / solo
        ms = QHBoxLayout()
        self.mute = _btn("M", "mute", tip="Silenciar")
        self.mute.setChecked(strip.mute)
        self.mute.toggled.connect(self._mute_toggled)
        self.solo = _btn("S", "solo", tip="Solo")
        self.solo.toggled.connect(lambda b: setattr(self.strip, "solo", b))
        ms.addWidget(self.mute)
        ms.addWidget(self.solo)
        lay.addLayout(ms)

        # ruteo a salidas
        rlay = QHBoxLayout()
        self.route_btns = {}
        for o in OUTPUTS:
            b = _btn(OUTPUT_LABELS[o][:3].upper(), "route", tip=f"Enviar a la salida {OUTPUT_LABELS[o]}")
            b.setChecked(strip.outputs.get(o, False))
            b.toggled.connect(lambda on, o=o: self.strip.outputs.__setitem__(o, on))
            self.route_btns[o] = b
            rlay.addWidget(b)
        lay.addLayout(rlay)
        self.info = QLabel("")
        self.info.setObjectName("dim")
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        self.warn = QLabel("")
        self.warn.setObjectName("warn")
        self.warn.setWordWrap(True)
        lay.addWidget(self.warn)

    # -- volumen de Windows (solo PC) ---------------------------------------
    def _sysvol(self):
        if self.strip.source != "pc" or self.engine is None:
            return None
        return self.engine.sysvol if self.engine.mixer.pc_system_volume else None

    def _fader_moved(self, db):
        sv = self._sysvol()
        if sv is not None:
            sv.set_db(db)
        else:
            self.strip.gain_db = db

    def _mute_toggled(self, on):
        sv = self._sysvol()
        if sv is not None:
            sv.set_mute(on)
        else:
            self.strip.mute = on

    # ----------------------------------------------------------------------
    def _send(self, bid, v):
        self.strip.sends = {**self.strip.sends, bid: v}

    def _rename(self):
        name, ok = QInputDialog.getText(self, "Renombrar bus", "Nombre:", text=self.strip.name)
        if ok and name.strip():
            self.strip.name = name.strip()
            self.title.setText(self.strip.name)
            self.renamed.emit(self.strip)

    def refresh_fx(self):
        names = [fx.title for fx in self.strip.chain]
        txt = f"⛓ Cadena ({len(names)})"
        self.fx_btn.setText(txt)
        self.fx_btn.setToolTip("\n".join(names) or "Sin efectos")

    def set_selected(self, on: bool):
        self.setProperty("selected", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def tick(self, blocked: set[str]):
        s = self.strip
        if s.source == "mic":
            clipping = time.monotonic() - s.last_clip < 2.0
            self.clip.setStyleSheet(
                f"color: {'#fff' if clipping else '#2d313a'}; background: {theme.DANGER if clipping else 'transparent'};"
                " font-weight: 800; font-size: 10px; border-radius: 3px; padding: 0 3px;")
            self.warn.setText("Baja el gain" if clipping else "")
        self.fader.meter.set_level(s.peak)
        if s.source != "pc":
            return

        sv = self._sysvol()
        if sv is not None:
            self.sub.setText("🔊 Vol. Windows")
            self.sub.setToolTip(f"Este fader mueve el volumen de Windows de:\n{sv.found_name or sv.device_name}")
            if sv.ready and not self.fader.slider.isSliderDown() and not sv.user_recently_changed():
                self.fader.set_db(sv.level_db if sv.scalar > 0.001 else -120.0)
                self.mute.blockSignals(True)
                self.mute.setChecked(sv.muted)
                self.mute.blockSignals(False)
            if sv.ready:
                self.fader.label.setText(f"Windows {sv.scalar * 100:.0f}%")
            self.warn.setText(sv.error)
        else:
            self.sub.setText("🔊 Música del PC")
            self.sub.setToolTip("")
            self.warn.setText("")

        mon = self.route_btns["monitor"]
        mon.setEnabled("monitor" not in blocked)
        mon.blockSignals(True)
        mon.setChecked(s.outputs.get("monitor", False) and "monitor" not in blocked)
        mon.blockSignals(False)
        if "monitor" in blocked:
            mon.setToolTip("Windows ya reproduce este audio directo en tu interfaz/audífonos;\n"
                           "mandarlo también al monitor lo haría sonar doble y saturado.")
            self.info.setText("Lo oyes directo desde Windows (sin pasar por la app).")
        else:
            mon.setToolTip(f"Enviar a la salida {OUTPUT_LABELS['monitor']}")
            self.info.setText("")


class OutputWidget(QFrame):
    def __init__(self, name: str, bus, device_label: str, parent=None):
        super().__init__(parent)
        self.bus = bus
        self.setObjectName("strip")
        self.setProperty("kind", "out")
        self.setFixedWidth(130)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        t = QLabel(OUTPUT_LABELS[name])
        t.setObjectName("h1")
        lay.addWidget(t)
        self.dev = QLabel(device_label)
        self.dev.setObjectName("dim")
        self.dev.setWordWrap(True)
        lay.addWidget(self.dev)
        self.fader = Fader(bus.gain_db)
        self.fader.changed.connect(lambda db: setattr(self.bus, "gain_db", db))
        lay.addWidget(self.fader, 1)
        m = _btn("M", "mute", tip="Silenciar salida")
        m.setChecked(bus.mute)
        m.toggled.connect(lambda b: setattr(self.bus, "mute", b))
        lim = _btn("LIM", tip="Limitador de seguridad (evita clips)")
        lim.setChecked(bus.safety_limiter)
        lim.toggled.connect(lambda b: setattr(self.bus, "safety_limiter", b))
        row = QHBoxLayout()
        row.addWidget(m)
        row.addWidget(lim)
        lay.addLayout(row)

    def tick(self):
        self.fader.meter.set_level(self.bus.peak)


class AddBusWidget(QWidget):
    add_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        b = QPushButton("＋\nBus")
        b.setFixedSize(60, 80)
        b.setToolTip("Agregar un bus de efectos (para envíos)")
        b.clicked.connect(self.add_requested.emit)
        lay.addWidget(b)
        lay.addStretch(1)
