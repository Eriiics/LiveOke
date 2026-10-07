"""Controles reutilizables: medidor, fader en dB, perilla y controles de parámetro."""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QSizePolicy, QSlider, QVBoxLayout, QWidget,
)

from . import theme

# ---------------------------------------------------------------------------
# Curva del fader: posición 0..1 <-> dB (0.63 ≈ 0 dB, 1.0 = +12 dB)
# ---------------------------------------------------------------------------
FADER_STEPS = 1000


def pos_to_db(p: float) -> float:
    if p <= 0.0005:
        return -math.inf
    return 20 * math.log10(4 * p ** 3)


def db_to_pos(db: float) -> float:
    if db == -math.inf or db < -90:
        return 0.0
    return min(1.0, (10 ** (db / 20) / 4) ** (1 / 3))


def fmt_db(db: float) -> str:
    if db == -math.inf or db < -90:
        return "-∞"
    return f"{db:+.1f}"


class Meter(QWidget):
    """Medidor de picos vertical con retención del máximo."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.level = 0.0
        self.hold = 0.0
        self._hold_t = 0
        self.setFixedWidth(10)
        self.setMinimumHeight(120)

    def set_level(self, lin: float):
        self.level = lin
        if lin >= self.hold:
            self.hold, self._hold_t = lin, 30
        elif self._hold_t > 0:
            self._hold_t -= 1
        else:
            self.hold *= 0.92
        self.update()

    @staticmethod
    def _y(lin, h):
        db = 20 * math.log10(max(lin, 1e-6))
        frac = min(1.0, max(0.0, (db + 60) / 66))  # -60..+6 dB
        return h - frac * h

    def paintEvent(self, e):
        p = QPainter(self)
        r = self.rect()
        p.fillRect(r, QColor("#0b0c0e"))
        h = r.height()
        grad = QLinearGradient(0, h, 0, 0)
        grad.setColorAt(0.0, QColor(theme.ACCENT_2))
        grad.setColorAt(0.75, QColor("#a6e35a"))
        grad.setColorAt(0.88, QColor(theme.ACCENT))
        grad.setColorAt(1.0, QColor(theme.DANGER))
        y = self._y(self.level, h)
        p.fillRect(QRectF(1, y, r.width() - 2, h - y), grad)
        yh = self._y(self.hold, h)
        p.fillRect(QRectF(1, yh, r.width() - 2, 2), QColor(theme.DANGER if self.hold >= 0.99 else theme.TEXT))
        p.setPen(QColor(0, 0, 0, 120))
        for db in (-48, -36, -24, -12, -6, 0):
            yy = h - (db + 60) / 66 * h
            p.drawLine(0, int(yy), r.width(), int(yy))


class Fader(QWidget):
    """Fader vertical en dB con medidor al lado. Doble clic = 0 dB."""
    changed = Signal(float)

    def __init__(self, db: float = 0.0, parent=None):
        super().__init__(parent)
        self.slider = QSlider(Qt.Vertical)
        self.slider.setRange(0, FADER_STEPS)
        self.slider.setMinimumHeight(150)
        self.meter = Meter()
        self.label = QLabel()
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setObjectName("dim")
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addStretch(1)
        row.addWidget(self.slider)
        row.addWidget(self.meter)
        row.addStretch(1)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addLayout(row, 1)
        lay.addWidget(self.label)
        self.slider.valueChanged.connect(self._moved)
        self.slider.mouseDoubleClickEvent = lambda ev: self.set_db(0.0, emit=True)
        self.set_db(db)

    def set_db(self, db: float, emit=False):
        self.slider.blockSignals(True)
        self.slider.setValue(round(db_to_pos(db) * FADER_STEPS))
        self.slider.blockSignals(False)
        self.label.setText(fmt_db(db) + " dB")
        if emit:
            self.changed.emit(db)

    def _moved(self, v):
        db = pos_to_db(v / FADER_STEPS)
        db = -120.0 if db == -math.inf else db
        self.label.setText(fmt_db(db) + " dB")
        self.changed.emit(db)


class Knob(QWidget):
    """Perilla pequeña. Arrastrar vertical, rueda del mouse, doble clic = valor por defecto."""
    changed = Signal(float)

    def __init__(self, minimum=0.0, maximum=1.0, value=0.0, default=None, bipolar=False, size=34, fmt=None, parent=None):
        super().__init__(parent)
        self.min, self.max = minimum, maximum
        self.value = value
        self.default = value if default is None else default
        self.bipolar = bipolar
        self.fmt = fmt or (lambda v: f"{v:.2f}")
        self.setFixedSize(size, size)
        self._drag = None
        self.setCursor(Qt.SizeVerCursor)
        self._update_tip()

    def _update_tip(self):
        self.setToolTip(self.fmt(self.value))

    def set_value(self, v, emit=False):
        v = min(self.max, max(self.min, v))
        if v != self.value:
            self.value = v
            self._update_tip()
            self.update()
            if emit:
                self.changed.emit(v)

    def mousePressEvent(self, e):
        self._drag = (e.position().y(), self.value)

    def mouseMoveEvent(self, e):
        if self._drag:
            y0, v0 = self._drag
            fine = 0.25 if e.modifiers() & Qt.ShiftModifier else 1.0
            dv = (y0 - e.position().y()) / 150 * (self.max - self.min) * fine
            self.set_value(v0 + dv, emit=True)

    def mouseReleaseEvent(self, e):
        self._drag = None

    def mouseDoubleClickEvent(self, e):
        self.set_value(self.default, emit=True)

    def wheelEvent(self, e):
        step = (self.max - self.min) / 50
        self.set_value(self.value + step * (1 if e.angleDelta().y() > 0 else -1), emit=True)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        s = min(self.width(), self.height())
        r = QRectF(4, 4, s - 8, s - 8)
        frac = (self.value - self.min) / (self.max - self.min or 1)
        start = 225  # grados
        span = -270
        p.setPen(QPen(QColor("#2d313a"), 3, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(r, start * 16, span * 16)
        p.setPen(QPen(QColor(theme.ACCENT_2), 3, Qt.SolidLine, Qt.RoundCap))
        if self.bipolar:
            mid = start + span / 2
            p.drawArc(r, int(mid * 16), int((frac - 0.5) * span * 16))
        else:
            p.drawArc(r, start * 16, int(frac * span * 16))
        ang = math.radians(start + frac * span)
        c = r.center()
        rad = r.width() / 2 - 4
        p.setPen(QPen(QColor(theme.TEXT), 2, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(c, QPointF(c.x() + rad * math.cos(ang), c.y() - rad * math.sin(ang)))


class LabeledKnob(QWidget):
    def __init__(self, title: str, knob: Knob, parent=None):
        super().__init__(parent)
        self.knob = knob
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        t = QLabel(title)
        t.setObjectName("dim")
        t.setAlignment(Qt.AlignCenter)
        f = QFont(t.font())
        f.setPointSizeF(7.5)
        t.setFont(f)
        lay.addWidget(knob, 0, Qt.AlignHCenter)
        lay.addWidget(t)


# ---------------------------------------------------------------------------
# Control genérico para un Param de un efecto
# ---------------------------------------------------------------------------
class ParamControl(QWidget):
    changed = Signal(str, object)

    def __init__(self, param, value, sources: list[str] | None = None, parent=None):
        super().__init__(parent)
        self.param = param
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        name = QLabel(param.label)
        name.setMinimumWidth(110)
        lay.addWidget(name)
        if param.kind == "float":
            self.slider = QSlider(Qt.Horizontal)
            self.slider.setRange(0, 1000)
            self.slider.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self.value_lbl = QLabel()
            self.value_lbl.setMinimumWidth(78)
            self.value_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            lay.addWidget(self.slider, 1)
            lay.addWidget(self.value_lbl)
            self.slider.setValue(self._to_pos(float(value)))
            self._show(float(value))
            self.slider.valueChanged.connect(self._slid)
            self.slider.mouseDoubleClickEvent = lambda ev: self.slider.setValue(self._to_pos(float(param.default)))
        elif param.kind in ("choice", "source"):
            self.combo = QComboBox()
            items = list(sources or []) if param.kind == "source" else list(param.choices)
            if value not in items:
                items.insert(0, str(value))
            self.combo.addItems(items)
            self.combo.setCurrentText(str(value))
            self.combo.currentTextChanged.connect(lambda t: self.changed.emit(param.id, t))
            lay.addWidget(self.combo, 1)
        elif param.kind == "bool":
            self.check = QCheckBox()
            self.check.setChecked(bool(value))
            self.check.toggled.connect(lambda b: self.changed.emit(param.id, b))
            lay.addWidget(self.check, 1)

    def _to_pos(self, v):
        p = self.param
        if p.log and p.min > 0:
            frac = math.log(v / p.min) / math.log(p.max / p.min)
        else:
            frac = (v - p.min) / ((p.max - p.min) or 1)
        return round(min(1, max(0, frac)) * 1000)

    def _from_pos(self, pos):
        p = self.param
        frac = pos / 1000
        if p.log and p.min > 0:
            return p.min * (p.max / p.min) ** frac
        return p.min + frac * (p.max - p.min)

    def _show(self, v):
        d = self.param.decimals
        self.value_lbl.setText(f"{v:.{d}f} {self.param.unit}".strip())

    def _slid(self, pos):
        v = self._from_pos(pos)
        if self.param.decimals == 0:
            v = round(v)
        self._show(v)
        self.changed.emit(self.param.id, v)


class RawParamControl(QWidget):
    """Parámetro nativo de un VST: slider 0..1 mostrando el texto del propio plugin."""

    def __init__(self, cpp_param, parent=None):
        super().__init__(parent)
        self.p = cpp_param
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        name = QLabel(cpp_param.name)
        name.setMinimumWidth(130)
        name.setToolTip(cpp_param.name)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 1000)
        self.value_lbl = QLabel()
        self.value_lbl.setMinimumWidth(90)
        self.value_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(name)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.value_lbl)
        self.refresh()
        self.slider.valueChanged.connect(self._slid)

    def refresh(self):
        try:
            self.slider.blockSignals(True)
            self.slider.setValue(round(self.p.raw_value * 1000))
            self.slider.blockSignals(False)
            self.value_lbl.setText(f"{self.p.string_value} {self.p.label or ''}".strip())
        except Exception:
            pass

    def _slid(self, v):
        try:
            self.p.raw_value = v / 1000
            self.value_lbl.setText(f"{self.p.string_value} {self.p.label or ''}".strip())
        except Exception as e:
            self.value_lbl.setText(str(e)[:20])
