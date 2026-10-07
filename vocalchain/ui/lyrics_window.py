"""Ventana de canción actual + letra grande (sincronizada si existe)."""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QStackedWidget, QTextBrowser, QVBoxLayout, QWidget,
)

import html as _html
import os

from ..lyrics import LOCAL_DIR, line_index
from . import theme

OFFSETS_FILE = Path.home() / ".vocalchain" / "lyric_offsets.json"


def _fmt_time(s):
    if s is None:
        return "--:--"
    s = max(0, int(s))
    return f"{s // 60}:{s % 60:02d}"


class LyricView(QWidget):
    """Dibuja la línea actual grande y centrada, con las vecinas atenuadas y scroll suave."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.lines: list[tuple[float, str]] = []
        self.index = -1
        self.progress = 0.0
        self.font_size = 30
        self._scroll = -1.0
        self.setMinimumHeight(300)

    def set_lines(self, lines):
        self.lines = lines or []
        self.index = -1
        self._scroll = -1.0
        self.update()

    def set_time(self, t: float):
        if not self.lines:
            return
        i = line_index(self.lines, t)
        self.index = i
        if 0 <= i < len(self.lines):
            start = self.lines[i][0]
            end = self.lines[i + 1][0] if i + 1 < len(self.lines) else start + 5
            self.progress = min(1.0, max(0.0, (t - start) / max(end - start, 0.1)))
        else:
            self.progress = 0.0
        # scroll suave hacia la línea actual
        self._scroll += (self.index - self._scroll) * 0.25
        if abs(self.index - self._scroll) < 0.002:
            self._scroll = float(self.index)
        self.update()

    def _font(self, dist):
        f = QFont(self.font())
        cur = abs(dist) < 0.5
        f.setPointSizeF(self.font_size * (1.0 if cur else 0.72))
        f.setWeight(QFont.Bold if cur else QFont.DemiBold)
        return f

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor(theme.BG))
        if not self.lines:
            return
        margin = 40
        tw = w - 2 * margin
        # alturas de cada línea con su fuente
        heights = {}
        rng = range(max(0, self.index - 6), min(len(self.lines), self.index + 10))
        for i in rng:
            fm = QFontMetrics(self._font(i - self.index))
            text = self.lines[i][1] or "♪"
            r = fm.boundingRect(0, 0, int(tw), 10000, Qt.TextWordWrap | Qt.AlignHCenter, text)
            heights[i] = r.height() + int(self.font_size * 0.55)
        # posición vertical: centro en la línea "scroll" (interpolada)
        base = int(self._scroll) if self._scroll >= 0 else 0
        frac = self._scroll - base if self._scroll >= 0 else 0.0
        y_center = h * 0.42
        y = y_center - heights.get(base, 0) / 2 - frac * heights.get(base, 0)
        ys = {base: y}
        for i in range(base + 1, rng.stop):
            ys[i] = ys[i - 1] + heights.get(i - 1, 0)
        for i in range(base - 1, rng.start - 1, -1):
            ys[i] = ys[i + 1] - heights.get(i, 0)
        for i in rng:
            if i not in ys:
                continue
            dist = i - self.index
            f = self._font(dist)
            p.setFont(f)
            text = self.lines[i][1] or "♪"
            if dist == 0:
                col = QColor(theme.ACCENT)
            else:
                a = max(40, 210 - abs(dist) * 45)
                col = QColor(232, 230, 227, a)
            p.setPen(col)
            rect = QRectF(margin, ys[i], tw, heights[i])
            p.drawText(rect, Qt.TextWordWrap | Qt.AlignHCenter | Qt.AlignTop, text)
            if dist == 0:
                fm = QFontMetrics(f)
                br = fm.boundingRect(0, 0, int(tw), 10000, Qt.TextWordWrap | Qt.AlignHCenter, text)
                bw = min(br.width(), tw)
                bx = margin + (tw - bw) / 2
                by = ys[i] + br.height() + 6
                p.fillRect(QRectF(bx, by, bw, 3), QColor("#3a3320"))
                p.fillRect(QRectF(bx, by, bw * self.progress, 3), QColor(theme.ACCENT))
        if self.index < 0 and self.lines:
            p.setPen(QColor(theme.DIM))
            f = QFont(self.font())
            f.setPointSizeF(self.font_size * 0.5)
            p.setFont(f)
            p.drawText(QRectF(0, h * 0.15, w, 40), Qt.AlignHCenter, f"Empieza en {_fmt_time(self.lines[0][0])}…")


class ManualSearch(QDialog):
    def __init__(self, artist, title, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Buscar letra a mano")
        form = QFormLayout(self)
        self.artist = QLineEdit(artist)
        self.title = QLineEdit(title)
        form.addRow("Artista", self.artist)
        form.addRow("Canción", self.title)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)


class LyricsWindow(QWidget):
    manual_search = Signal(str, str)
    refetch = Signal()
    next_source = Signal()
    detach_requested = Signal()     # pasar a ventana flotante (otro monitor)
    dock_requested = Signal()       # volver a la pestaña Letra

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Window)
        self.docked = False
        self.setWindowTitle("🎤 Letra — VocalChain")
        self.resize(900, 640)
        self.setObjectName("root")
        self.setStyleSheet(f"QWidget#root {{ background: {theme.BG}; }}")
        self.track = None
        self.lyrics = None
        self.offset = 0.0
        self._offsets = self._load_offsets()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 12)

        head = QHBoxLayout()
        self.cover = QLabel()
        self.cover.setFixedSize(76, 76)
        self.cover.setStyleSheet(f"background: {theme.PANEL}; border-radius: 8px;")
        self.cover.setAlignment(Qt.AlignCenter)
        head.addWidget(self.cover)
        info = QVBoxLayout()
        self.title = QLabel("Nada sonando")
        tf = QFont(self.title.font())
        tf.setPointSizeF(18)
        tf.setBold(True)
        self.title.setFont(tf)
        self.sub = QLabel("")
        self.sub.setObjectName("dim")
        sf = QFont(self.sub.font())
        sf.setPointSizeF(11)
        self.sub.setFont(sf)
        self.meta = QLabel("")
        self.meta.setObjectName("dim")
        info.addWidget(self.title)
        info.addWidget(self.sub)
        info.addWidget(self.meta)
        self.source_lbl = QLabel("")
        self.source_lbl.setObjectName("dim")
        info.addWidget(self.source_lbl)
        head.addLayout(info, 1)
        self.key_badge = QLabel("—")
        self.key_badge.setStyleSheet(f"color: {theme.ACCENT}; font-size: 30px; font-weight: 800; padding: 0 8px;")
        self.key_badge.setToolTip("Tonalidad detectada")
        head.addWidget(self.key_badge)
        lay.addLayout(head)

        self.stack = QStackedWidget()
        self.view = LyricView()
        self.plain = QTextBrowser()
        self.plain.setStyleSheet(f"QTextBrowser {{ background: {theme.BG}; border: none; color: {theme.TEXT}; }}")
        self.msg = QLabel("")
        self.msg.setAlignment(Qt.AlignCenter)
        self.msg.setObjectName("dim")
        mf = QFont(self.msg.font())
        mf.setPointSizeF(16)
        self.msg.setFont(mf)
        self.stack.addWidget(self.view)
        self.stack.addWidget(self.plain)
        self.stack.addWidget(self.msg)
        lay.addWidget(self.stack, 1)

        foot = QHBoxLayout()
        b = QPushButton("−0.5 s")
        b.setToolTip("La letra va adelantada: atrasarla")
        b.clicked.connect(lambda: self._nudge(-0.5))
        foot.addWidget(b)
        self.off_lbl = QLabel("±0.0 s")
        self.off_lbl.setObjectName("dim")
        foot.addWidget(self.off_lbl)
        b = QPushButton("+0.5 s")
        b.setToolTip("La letra va atrasada: adelantarla")
        b.clicked.connect(lambda: self._nudge(0.5))
        foot.addWidget(b)
        foot.addSpacing(16)
        b = QPushButton("A−")
        b.clicked.connect(lambda: self._font(-3))
        foot.addWidget(b)
        b = QPushButton("A＋")
        b.clicked.connect(lambda: self._font(3))
        foot.addWidget(b)
        foot.addStretch(1)
        self.autoscroll = QCheckBox("Auto-scroll (letra sin tiempos)")
        self.autoscroll.setChecked(True)
        foot.addWidget(self.autoscroll)
        b = QPushButton("🔎 Buscar…")
        b.clicked.connect(self._manual)
        foot.addWidget(b)
        b = QPushButton("⟳ Otra fuente")
        b.setToolTip("Buscar la letra en otra fuente (Musixmatch, NetEase, Megalobiz, Genius…)")
        b.clicked.connect(self._other_source)
        foot.addWidget(b)
        b = QPushButton("📁")
        b.setToolTip("Carpeta de letras propias: pon ahí 'Artista - Canción.lrc' o .txt y tienen prioridad")
        b.clicked.connect(self._open_local)
        foot.addWidget(b)
        self.ontop = QCheckBox("Siempre visible")
        self.ontop.toggled.connect(self._ontop)
        foot.addWidget(self.ontop)
        self.dock_btn = QPushButton("⇱ Despegar")
        self.dock_btn.setToolTip("Sacar la letra a una ventana aparte (para ponerla en el otro monitor)")
        self.dock_btn.clicked.connect(self._dock_clicked)
        foot.addWidget(self.dock_btn)
        b = QPushButton("⛶")
        b.setToolTip("Pantalla completa (F11)")
        b.clicked.connect(self._fullscreen)
        foot.addWidget(b)
        lay.addLayout(foot)
        self._font(0)
        self.show_message("Pon una canción en Spotify, YouTube, etc.")

    # ------------------------------------------------------------------
    def keyPressEvent(self, e):
        if e.key() == Qt.Key_F11:
            self._fullscreen()
        elif e.key() == Qt.Key_Escape and self.isFullScreen():
            self.showNormal()
        else:
            super().keyPressEvent(e)

    def _fullscreen(self):
        if self.docked:
            self.detach_requested.emit()
            if self.docked:
                return
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def _ontop(self, on):
        if self.docked:
            return   # solo aplica a la ventana flotante
        self.setWindowFlag(Qt.WindowStaysOnTopHint, on)
        self.show()

    def _dock_clicked(self):
        (self.detach_requested if self.docked else self.dock_requested).emit()

    def set_docked(self, docked: bool):
        self.docked = docked
        self.dock_btn.setText("⇱ Despegar" if docked else "⇲ Acoplar")
        self.dock_btn.setToolTip("Sacar la letra a una ventana aparte (para ponerla en el otro monitor)" if docked
                                 else "Volver a meter la letra en la pestaña de la ventana principal")
        self.ontop.setEnabled(not docked)

    def closeEvent(self, e):
        # cerrar la ventana flotante = devolverla a su pestaña (no se pierde)
        if not self.docked and not getattr(self, "_really_close", False):
            e.ignore()
            self.dock_requested.emit()
            return
        super().closeEvent(e)

    def _font(self, d):
        self.view.font_size = max(14, min(90, self.view.font_size + d))
        f = QFont(self.plain.font())
        f.setPointSizeF(self.view.font_size * 0.75)
        self.plain.setFont(f)
        self.view.update()

    def _manual(self):
        t = self.track
        dlg = ManualSearch(t.artist if t else "", t.title if t else "", self)
        if dlg.exec():
            self.manual_search.emit(dlg.artist.text().strip(), dlg.title.text().strip())

    # -- offsets por canción ----------------------------------------------
    @staticmethod
    def _load_offsets():
        try:
            return json.loads(OFFSETS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _track_id(self):
        return "|".join(self.track.key) if self.track else ""

    def _nudge(self, d):
        self.offset = round(self.offset + d, 2)
        self.off_lbl.setText(f"{self.offset:+.1f} s")
        if self.track:
            self._offsets[self._track_id()] = self.offset
            try:
                OFFSETS_FILE.parent.mkdir(parents=True, exist_ok=True)
                OFFSETS_FILE.write_text(json.dumps(self._offsets), encoding="utf-8")
            except Exception:
                pass

    # ------------------------------------------------------------------
    def show_message(self, text):
        self.msg.setText(text)
        self.stack.setCurrentWidget(self.msg)

    def set_track(self, track):
        self.track = track
        if track is None:
            self.title.setText("Nada sonando")
            self.sub.setText("")
            return
        self.title.setText(track.title or "Sin título")
        self.sub.setText(" · ".join(x for x in (track.artist, track.album) if x))
        self.offset = float(self._offsets.get(self._track_id(), 0.0))
        self.off_lbl.setText(f"{self.offset:+.1f} s")
        if track.thumbnail:
            pm = QPixmap()
            if pm.loadFromData(track.thumbnail):
                self.cover.setPixmap(pm.scaled(76, 76, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))
        else:
            self.cover.setText("♪")
        self.lyrics = None
        self.show_message("Buscando letra…")

    def _other_source(self):
        self.show_message("Buscando en otra fuente…")
        self.next_source.emit()

    def _open_local(self):
        LOCAL_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(LOCAL_DIR))  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    def set_lyrics(self, lyrics):
        self.lyrics = lyrics
        src = getattr(lyrics, "source", "") if lyrics is not None else ""
        if lyrics is not None and lyrics.found:
            kind = "sincronizada" if lyrics.synced else "sin tiempos"
            self.source_lbl.setText(f"Letra: {src} ({kind})")
        else:
            self.source_lbl.setText("")
        if lyrics is None or not lyrics.found:
            self.show_message("No encontré la letra 😕\nPrueba con ⟳ Otra fuente o 🔎 Buscar…")
        elif lyrics.instrumental and not (lyrics.synced or lyrics.plain):
            self.show_message("♪ Instrumental ♪")
        elif lyrics.synced:
            self.view.set_lines(lyrics.synced)
            self.stack.setCurrentWidget(self.view)
        else:
            body = "<div style='text-align:center; line-height:150%'>" + "<br>".join(
                (_html.escape(ln) or "&nbsp;") for ln in lyrics.plain.splitlines()) + "</div>"
            self.plain.setHtml(body)
            self.stack.setCurrentWidget(self.plain)

    def tick(self, track, key_text: str):
        self.key_badge.setText(key_text)
        if track is None:
            return
        pos = track.current_position()
        dur = track.duration
        self.meta.setText(f"{_fmt_time(pos)} / {_fmt_time(dur)}" + ("" if track.playing else "  ⏸") +
                          (f"   ·   {track.app.split('!')[0].split('.')[-1]}" if track.app else ""))
        if pos is None:
            return
        t = pos + self.offset
        if self.stack.currentWidget() is self.view:
            self.view.set_time(t)
        elif self.stack.currentWidget() is self.plain and self.autoscroll.isChecked() and dur:
            sb = self.plain.verticalScrollBar()
            # letra sin tiempos: avanzar proporcional (con margen para intro/outro)
            frac = min(1.0, max(0.0, (t - dur * 0.06) / (dur * 0.85)))
            sb.setValue(int(frac * sb.maximum()))
