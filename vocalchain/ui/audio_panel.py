"""Pestaña Audio: latencia de la cadena (cuánto agrega cada plugin), avisos y opciones de rendimiento."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox, QFrame, QHBoxLayout, QLabel, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..latency import PluginLatency, chain_report, fmt_ms
from ..remote import EngineError
from . import theme

LEVEL_COLOR = {"ok": theme.OK, "warn": theme.ACCENT, "bad": theme.DANGER}


class AudioPanel(QWidget):
    open_settings = Signal()

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self._sig = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(10)

        top = QFrame()
        top.setObjectName("strip")
        tl = QVBoxLayout(top)
        tl.setContentsMargins(12, 10, 12, 10)
        row = QHBoxLayout()
        t = QLabel("Latencia")
        t.setObjectName("h1")
        row.addWidget(t)
        self.total = QLabel("—")
        self.total.setStyleSheet("font-size: 24px; font-weight: 800; padding: 0 10px;")
        row.addWidget(self.total)
        self.summary = QLabel("")
        self.summary.setObjectName("dim")
        row.addWidget(self.summary, 1)
        b = QPushButton("⚙ Interfaz y buffer…")
        b.clicked.connect(self.open_settings.emit)
        row.addWidget(b)
        tl.addLayout(row)
        self.hints = QLabel("")
        self.hints.setWordWrap(True)
        self.hints.setStyleSheet(f"color: {theme.ACCENT};")
        tl.addWidget(self.hints)
        lay.addWidget(top)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Canal / efecto", "Latencia", "Muestras", "CPU"])
        self.tree.setColumnWidth(0, 380)
        self.tree.setColumnWidth(1, 110)
        self.tree.setColumnWidth(2, 90)
        self.tree.itemDoubleClicked.connect(self._open_editor)
        self.tree.setToolTip("Doble clic en un plugin para abrir su ventana")
        lay.addWidget(self.tree, 1)

        perf = QFrame()
        perf.setObjectName("strip")
        pl = QVBoxLayout(perf)
        pl.setContentsMargins(12, 10, 12, 10)
        t = QLabel("Rendimiento")
        t.setObjectName("h1")
        pl.addWidget(t)
        self.mt = QCheckBox("Procesamiento multihilo (cada micrófono en un núcleo distinto)")
        self.mt.setToolTip("Solo ayuda si tienes 2 micrófonos con cadenas pesadas (p.ej. Auto-Tune en los dos).\n"
                           "Con un solo micrófono no hace nada; con cadenas ligeras puede ir un poco peor.\n"
                           "Si al activarlo aparecen cortes, desactívalo.")
        self.mt.toggled.connect(self._mt_toggled)
        pl.addWidget(self.mt)
        self.mt_info = QLabel("")
        self.mt_info.setObjectName("dim")
        pl.addWidget(self.mt_info)
        lay.addWidget(perf)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)

    # ------------------------------------------------------------------
    def _mt_toggled(self, on):
        if not self.engine.client.connected:
            return
        try:
            self.engine.client.call("set_multithread", on=bool(on))
        except EngineError as e:
            self.mt_info.setText(f"⚠ {e}")

    def _open_editor(self, item, _col):
        ref = item.data(0, Qt.UserRole + 1)
        s = self.engine.mixer.strip(ref[0]) if ref else None
        fx = next((f for f in s.chain if f.uid == ref[1]), None) if s else None
        if fx is not None and fx.is_plugin and not fx.failed:
            try:
                fx.show_editor()
            except EngineError as e:
                self.hints.setText(f"⚠ {e}")

    def refresh(self):
        if not self.isVisible():
            return
        e = self.engine
        st = e.stats or {}
        if not e.running or not st:
            self.total.setText("—")
            self.summary.setText("Motor apagado")
            return
        sr = float(e.sr)
        block = e.block or 0
        mic_strips = [s for s in e.mixer.inputs if s.source == "mic"]
        # el camino de la voz: cadena del mic principal (los envíos a buses no retrasan la voz seca)
        main = mic_strips[0] if mic_strips else None
        plugins = [PluginLatency(fx.title, fx.latency) for fx in (main.chain if main else [])
                   if fx.enabled and fx.latency > 0]
        rep = chain_report(plugins, sr, block, e.latency_ms or None, st.get("pc_sr"))
        lvl = "bad" if rep["total_ms"] > 15 else ("warn" if rep["total_ms"] > 10 else "ok")
        self.total.setText(fmt_ms(rep["total_ms"]))
        self.total.setStyleSheet(f"font-size: 24px; font-weight: 800; padding: 0 10px; color: {LEVEL_COLOR[lvl]};")
        late = int(st.get("late", 0) or 0)
        self.summary.setText(f"interfaz {fmt_ms(rep['io_ms'])} + plugins {fmt_ms(rep['plugins_ms'])} · "
                             f"{sr / 1000:g} kHz · buffer {block} · bloques tarde {late}")
        self.hints.setText("\n".join("• " + h for h in rep["hints"]) or "✔ Todo bien para cantar en vivo.")

        sig = tuple((s.id, tuple((fx.uid, fx.latency, fx.enabled) for fx in s.chain)) for s in e.mixer.strips)
        if sig != self._sig:
            self._sig = sig
            self.tree.clear()
            for s in e.mixer.strips:
                it = QTreeWidgetItem([s.name, "", "", ""])
                it.setData(0, Qt.UserRole, s.id)
                self.tree.addTopLevelItem(it)
                for fx in s.chain:
                    ms = 1000.0 * fx.latency / sr
                    c = QTreeWidgetItem([("   " if fx.enabled else "   (apagado) ") + fx.title,
                                         fmt_ms(ms) if fx.latency else "0", str(fx.latency), ""])
                    c.setData(0, Qt.UserRole + 1, [s.id, fx.uid])
                    if fx.latency:
                        c.setForeground(1, _brush(LEVEL_COLOR["bad" if ms > 10 else "warn" if ms > 3 else "ok"]))
                    it.addChild(c)
                it.setExpanded(True)
        bus = e.block_us
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            s = e.mixer.strip(it.data(0, Qt.UserRole))
            if s is not None and bus and s.chain:
                pct = 100.0 * s.cpu_us / bus
                it.setText(3, f"{pct:.0f}% ({s.cpu_us / 1000:.2f} ms)")
                it.setForeground(3, _brush(theme.DANGER if pct > 70 else theme.TEXT))
            else:
                it.setText(3, "")

        self.mt.blockSignals(True)
        self.mt.setChecked(bool(st.get("multithread", False)))
        self.mt.blockSignals(False)
        n = len(mic_strips)
        if st.get("multithread"):
            if st.get("parallel"):
                msg = "procesando los micrófonos en paralelo."
            elif n >= 2:
                msg = "esperando audio de la interfaz."
            else:
                msg = "sin efecto (hace falta un segundo micrófono: ＋ Mic 2 en el mezclador)."
            self.mt_info.setText("Activo: " + msg)
        else:
            self.mt_info.setText("Desactivado (recomendado salvo que uses 2 micrófonos con Auto-Tune)."
                                 if n < 2 else "Tienes 2 micrófonos: actívalo si ves CPU alta o cortes.")


def _brush(color):
    from PySide6.QtGui import QBrush, QColor
    return QBrush(QColor(color))
