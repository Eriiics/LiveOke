"""Pestaña Grabaciones: grabar lo que escuchas (voz + base) a MP3 y ver las tomas guardadas.

Modo Canción: una carpeta por toma con el MP3, info.txt/info.json (instrumental, link de YouTube,
tonalidad, fecha, duración, preset) y letra.txt para tu letra.
Modo Sesión: un MP3 continuo; cada cambio de canción queda como marca con su tiempo.
El motor graba el mismo audio que sale por el monitor (WAV temporal); al parar se convierte a MP3.
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QRadioButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .. import recording as recmod
from ..remote import EngineError
from . import theme


class _Bridge(QObject):
    search_done = Signal(object, str)        # (resultado | None, error)
    download_done = Signal(object, str)      # (ruta | None, error)
    download_progress = Signal(float)
    rec_done = Signal(object)                # Recording


class RecordingsPanel(QWidget):
    recording_changed = Signal(bool)

    def __init__(self, engine, context, parent=None):
        """context() -> dict(track=..., key=str, preset=str, chain=[str]) con lo que suena ahora."""
        super().__init__(parent)
        self.engine = engine
        self.context = context
        self.rec: recmod.Recording | None = None
        self.last_folder: Path | None = None
        self._busy_dl = False
        self._auto_name = ""
        self.b = _Bridge()
        self.b.search_done.connect(self._search_done)
        self.b.download_done.connect(self._download_done)
        self.b.download_progress.connect(lambda f: self.status.setText(f"Descargando instrumental… {f * 100:.0f}%"))
        self.b.rec_done.connect(self._rec_done)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(10)

        # ---------------------------------------------------------------- grabar
        box = QFrame()
        box.setObjectName("strip")
        g = QGridLayout(box)
        g.setContentsMargins(12, 10, 12, 10)
        g.setHorizontalSpacing(8)
        title = QLabel("Grabar lo que escuchas")
        title.setObjectName("h1")
        g.addWidget(title, 0, 0, 1, 2)

        mode_row = QHBoxLayout()
        self.mode_song = QRadioButton("🎵 Canción (una carpeta por toma)")
        self.mode_session = QRadioButton("📼 Sesión (un archivo continuo con marcas)")
        self.mode_song.setChecked(engine.settings.get("rec_mode", "song") != "session")
        self.mode_session.setChecked(engine.settings.get("rec_mode") == "session")
        grp = QButtonGroup(self)
        grp.addButton(self.mode_song)
        grp.addButton(self.mode_session)
        self.mode_song.toggled.connect(self._mode_changed)
        mode_row.addWidget(self.mode_song)
        mode_row.addWidget(self.mode_session)
        mode_row.addStretch(1)
        g.addLayout(mode_row, 0, 2, 1, 3)

        g.addWidget(QLabel("Instrumental"), 1, 0)
        self.name = QLineEdit()
        self.name.setPlaceholderText("Nombre de la base (se llena con lo que suena)")
        g.addWidget(self.name, 1, 1, 1, 3)
        b = QPushButton("↺ Lo que suena")
        b.setToolTip("Usar el título de lo que está sonando en el PC")
        b.clicked.connect(lambda: self._fill_from_track(force=True))
        g.addWidget(b, 1, 4)

        g.addWidget(QLabel("YouTube"), 2, 0)
        self.url = QLineEdit()
        self.url.setPlaceholderText("Pega el link de la base, o pulsa Buscar")
        g.addWidget(self.url, 2, 1, 1, 2)
        self.search_btn = QPushButton("🔎 Buscar")
        self.search_btn.setToolTip("Buscar la instrumental en YouTube por su nombre (te pregunta antes de usarla)")
        self.search_btn.clicked.connect(self._search)
        g.addWidget(self.search_btn, 2, 3)
        self.dl_btn = QPushButton("⬇ Descargar")
        self.dl_btn.setToolTip("Descargar la instrumental (audio) a la carpeta de la grabación")
        self.dl_btn.clicked.connect(self._download_current)
        g.addWidget(self.dl_btn, 2, 4)
        self.channel = ""

        ctl = QHBoxLayout()
        self.rec_btn = QPushButton("●  GRABAR")
        self.rec_btn.setObjectName("play")
        self.rec_btn.setCheckable(True)
        self.rec_btn.setMinimumHeight(40)
        self.rec_btn.clicked.connect(self.toggle)
        ctl.addWidget(self.rec_btn)
        self.timer_lbl = QLabel("00:00")
        self.timer_lbl.setStyleSheet(f"font-size: 26px; font-weight: 800; color: {theme.DIM}; padding: 0 12px;")
        ctl.addWidget(self.timer_lbl)
        self.mark_btn = QPushButton("＋ Marca")
        self.mark_btn.setToolTip("Marcar este momento (en modo sesión también se marca solo al cambiar de canción)")
        self.mark_btn.clicked.connect(lambda: self._marker(manual=True))
        self.mark_btn.setEnabled(False)
        ctl.addWidget(self.mark_btn)
        ctl.addStretch(1)
        self.status = QLabel("")
        self.status.setObjectName("dim")
        self.status.setWordWrap(True)
        ctl.addWidget(self.status, 2)
        g.addLayout(ctl, 3, 0, 1, 5)
        g.setColumnStretch(1, 1)
        lay.addWidget(box)

        # ---------------------------------------------------------------- lista
        head = QHBoxLayout()
        t = QLabel("Grabaciones")
        t.setObjectName("h1")
        head.addWidget(t)
        self.root_lbl = QLabel(str(recmod.RECORD_ROOT))
        self.root_lbl.setObjectName("dim")
        head.addWidget(self.root_lbl, 1)
        for text, tip, fn in (("📂 Abrir carpeta", "Abrir la carpeta de la toma elegida", self._open_selected),
                              ("▶ Reproducir", "Abrir el MP3 con tu reproductor", self._play_selected),
                              ("⬇ Instrumental", "Descargar la instrumental de la toma elegida", self._download_selected),
                              ("📁 Todas", "Abrir la carpeta Música\\VocalChain", self._open_root),
                              ("⟳", "Actualizar lista", self.refresh)):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            head.addWidget(b)
        lay.addLayout(head)
        self.list = QTreeWidget()
        self.list.setHeaderLabels(["Toma", "Fecha", "Duración", "Tono", "Tipo", "YouTube"])
        self.list.setRootIsDecorated(False)
        self.list.setAlternatingRowColors(False)
        self.list.itemDoubleClicked.connect(lambda *_: self._open_selected())
        self.list.setColumnWidth(0, 360)
        self.list.setColumnWidth(1, 140)
        lay.addWidget(self.list, 1)

        self._tick_timer = QTimer(self)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start(250)
        self.refresh()

    # ------------------------------------------------------------------ estado
    @property
    def recording(self) -> bool:
        return self.rec is not None

    @property
    def mode(self) -> str:
        return "session" if self.mode_session.isChecked() else "song"

    def _mode_changed(self):
        self.engine.settings["rec_mode"] = self.mode
        self.engine.save_settings()

    def _fill_from_track(self, force=False):
        t = self.context().get("track")
        if t is None:
            return
        title = recmod.clean_title_for_search(f"{t.artist} - {t.title}" if t.artist else t.title)
        cur = self.name.text().strip()
        if force or not cur or cur == self._auto_name:
            self.name.setText(title)
            self._auto_name = title
            if not force and self.url.text().strip() and not self.recording:
                self.url.clear()     # el link era de la base anterior
                self.channel = ""

    def on_track_changed(self, track):
        """Lo llama la ventana principal al cambiar la canción que suena."""
        if self.recording and self.mode == "session":
            self._fill_from_track(force=True)
            self.url.clear()
            self.channel = ""
            self._marker()
        elif not self.recording:
            self._fill_from_track()

    # ------------------------------------------------------------------ grabar
    def toggle(self):
        if self.recording:
            self.stop()
        else:
            self.start()

    def start(self):
        self.rec_btn.setChecked(False)
        if not self.engine.client.connected:
            QMessageBox.information(self, "Grabar", "Primero enciende el motor (⏻ Motor).")
            return
        ctx = self.context()
        url = recmod.normalize_youtube_url(self.url.text()) or self.url.text().strip()
        try:
            rec = recmod.Recording.start(self.mode, self.name.text().strip(), url, self.channel,
                                         ctx.get("key", ""), None, ctx.get("preset", ""), ctx.get("chain", []))
        except OSError as e:
            QMessageBox.warning(self, "Grabar", f"No pude crear la carpeta en {recmod.RECORD_ROOT}:\n{e}")
            return
        try:
            self.engine.client.call("rec_start", timeout=10, path=str(rec.wav_tmp))
        except EngineError as e:
            shutil.rmtree(rec.folder, ignore_errors=True)   # carpeta recién creada por nosotros, vacía
            QMessageBox.warning(self, "Grabar", f"El motor no pudo empezar a grabar: {e}")
            return
        self.rec = rec
        self.last_folder = rec.folder
        self.rec_btn.setChecked(True)
        self.rec_btn.setText("■  PARAR")
        self.mark_btn.setEnabled(True)
        self.mode_song.setEnabled(False)
        self.mode_session.setEnabled(False)
        self.timer_lbl.setStyleSheet(f"font-size: 26px; font-weight: 800; color: {theme.DANGER}; padding: 0 12px;")
        self.status.setText(f"Grabando en {rec.folder.name}")
        self.recording_changed.emit(True)

    def stop(self):
        rec = self.rec
        if rec is None:
            return
        res = {}
        try:
            res = self.engine.client.call("rec_stop", timeout=30) or {}
        except EngineError as e:
            self.status.setText(f"⚠ El motor no respondió al parar ({e}); convierto lo que se haya guardado.")
        self.rec = None
        self.rec_btn.setChecked(False)
        self.rec_btn.setText("●  GRABAR")
        self.mark_btn.setEnabled(False)
        self.mode_song.setEnabled(True)
        self.mode_session.setEnabled(True)
        self.timer_lbl.setStyleSheet(f"font-size: 26px; font-weight: 800; color: {theme.DIM}; padding: 0 12px;")
        url = recmod.normalize_youtube_url(self.url.text()) or self.url.text().strip()
        if rec.info.mode == "song" and (url != rec.info.youtube_url or self.name.text().strip() != rec.info.instrumental):
            rec.update(youtube_url=url, instrumental=self.name.text().strip(), channel=self.channel)
        dropped = int(res.get("dropped", 0) or 0)
        self.status.setText("Convirtiendo a MP3…" + (f"  (⚠ se perdieron {dropped} muestras)" if dropped else ""))
        rec.finish(res.get("seconds"), res.get("markers"), on_done=self.b.rec_done.emit)
        self.recording_changed.emit(False)

    def _rec_done(self, rec):
        if rec.error:
            self.status.setText("⚠ " + rec.error)
        else:
            self.status.setText(f"✔ Guardado: {rec.folder.name}  ({recmod.fmt_time(rec.info.duration_s)})")
        self.refresh(select=rec.folder)

    def _marker(self, manual=False):
        if not self.recording:
            return
        name = self.name.text().strip() or ("Marca" if manual else "Canción")
        try:
            t = float((self.engine.client.call("rec_marker", name=name) or {}).get("t", 0.0))
        except EngineError:
            t = float(self.engine.rec.get("t", 0.0) or 0.0)
        url = recmod.normalize_youtube_url(self.url.text()) or ""
        self.rec.add_marker(t, name, url, self.context().get("key", ""))
        self.status.setText(f"Marca {recmod.fmt_time(t)} · {name}")

    def _tick(self):
        if not self.recording:
            return
        r = self.engine.rec
        t = float(r.get("t", 0.0) or 0.0)
        self.timer_lbl.setText(recmod.fmt_time(t))
        if not r.get("on", True) and self.engine.client.connected:
            self.status.setText("⚠ El motor dejó de grabar")
        elif int(r.get("dropped", 0) or 0):
            self.status.setText(f"⚠ Se están perdiendo muestras ({r.get('dropped')}): el disco no da abasto")
        if not self.engine.client.connected:
            self.status.setText("⚠ Se cerró el motor: la grabación se guardó hasta ese momento")
            self.stop()

    # ------------------------------------------------------------------ YouTube
    def _search(self):
        q = self.name.text().strip()
        if not q:
            self._fill_from_track(force=True)
            q = self.name.text().strip()
        if not q:
            QMessageBox.information(self, "YouTube", "Escribe el nombre de la instrumental primero.")
            return
        self.search_btn.setEnabled(False)
        self.status.setText("Buscando en YouTube…")

        def work():
            try:
                self.b.search_done.emit(recmod.yt_search(q), "")
            except ImportError:
                self.b.search_done.emit(None, "Falta yt-dlp: ejecuta  pip install yt-dlp")
            except Exception as e:  # noqa: BLE001
                self.b.search_done.emit(None, str(e))
        threading.Thread(target=work, daemon=True).start()

    def _search_done(self, res, err):
        self.search_btn.setEnabled(True)
        if err or not res:
            self.status.setText("⚠ " + (err or "No encontré nada en YouTube con ese nombre"))
            return
        dur = res.get("duration")
        dtxt = f" · {int(dur) // 60}:{int(dur) % 60:02d}" if dur else ""
        if QMessageBox.question(self, "¿Es esta la instrumental?",
                                f"{res['title']}\n{res.get('channel', '')}{dtxt}\n\n{res['url']}") == QMessageBox.Yes:
            self.url.setText(res["url"])
            self.channel = res.get("channel", "")
            if self.rec and self.rec.info.mode == "song":
                self.rec.update(youtube_url=res["url"], channel=self.channel)
            self.status.setText("Link guardado")
        else:
            self.status.setText("Pega el link a mano si no era esa")

    def _download_current(self):
        url = recmod.normalize_youtube_url(self.url.text())
        if not url:
            QMessageBox.information(self, "Descargar", "Pega un link de YouTube válido (o usa 🔎 Buscar).")
            return
        folder = self.rec.folder if self.rec else (self.last_folder or recmod.RECORD_ROOT / "Instrumentales")
        self._download(url, folder, self.name.text().strip() or None)

    def _download_selected(self):
        sel = self._selected()
        if not sel:
            QMessageBox.information(self, "Descargar", "Elige una grabación de la lista.")
            return
        folder, info = sel
        url = info.youtube_url or next((m.youtube_url for m in info.markers if m.youtube_url), "")
        if not url:
            url = recmod.normalize_youtube_url(self.url.text()) or ""
            if not url:
                QMessageBox.information(self, "Descargar", "Esa grabación no tiene link de YouTube.\n"
                                        "Pega el link arriba y vuelve a pulsar ⬇ Instrumental.")
                return
            info.youtube_url = url
            recmod.write_info(folder, info)
        self._download(url, folder, info.instrumental or None)

    def _download(self, url, folder, name):
        if self._busy_dl:
            return
        self._busy_dl = True
        self.dl_btn.setEnabled(False)
        self.status.setText("Descargando instrumental…")

        def work():
            try:
                p = recmod.yt_download_audio(url, Path(folder), name, progress=self.b.download_progress.emit)
                self.b.download_done.emit(p, "")
            except ImportError:
                self.b.download_done.emit(None, "Falta yt-dlp: ejecuta  pip install yt-dlp")
            except Exception as e:  # noqa: BLE001
                self.b.download_done.emit(None, str(e))
        threading.Thread(target=work, daemon=True).start()

    def _download_done(self, path, err):
        self._busy_dl = False
        self.dl_btn.setEnabled(True)
        if err:
            self.status.setText(f"⚠ No pude descargar: {err}")
            return
        extra = "" if recmod.find_ffmpeg() else " (sin ffmpeg: queda en el formato original)"
        self.status.setText(f"✔ Instrumental descargada: {Path(path).name}{extra}")

    # ------------------------------------------------------------------ lista
    def refresh(self, select: Path | None = None):
        self.list.clear()
        try:
            items = recmod.list_recordings()
        except OSError:
            items = []
        for folder, info in items:
            date = info.date.replace("T", " ")[:16]
            kind = "Sesión" + (f" ({len(info.markers)})" if info.markers else "") if info.mode == "session" else "Canción"
            it = QTreeWidgetItem([folder.name, date, recmod.fmt_time(info.duration_s), info.key or "",
                                  kind, info.youtube_url or ""])
            it.setData(0, Qt.UserRole, str(folder))
            if not (folder / info.audio_file).exists():
                it.setToolTip(0, "Todavía convirtiendo o sin audio")
            self.list.addTopLevelItem(it)
            if select and Path(select) == folder:
                self.list.setCurrentItem(it)

    def _selected(self):
        it = self.list.currentItem()
        if it is None:
            return None
        folder = Path(it.data(0, Qt.UserRole))
        info = recmod.read_info(folder)
        return (folder, info) if info else None

    def _open_selected(self):
        sel = self._selected()
        if sel:
            self._open(sel[0])

    def _play_selected(self):
        sel = self._selected()
        if not sel:
            return
        folder, info = sel
        f = folder / info.audio_file
        if not f.exists():
            QMessageBox.information(self, "Reproducir", "Todavía no está el MP3 (¿se está convirtiendo?).")
            return
        self._open(f)

    def _open_root(self):
        recmod.RECORD_ROOT.mkdir(parents=True, exist_ok=True)
        self._open(recmod.RECORD_ROOT)

    def _open(self, path: Path):
        try:
            recmod.open_folder(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Abrir", str(e))

    def shutdown(self):
        """Al cerrar la app: parar y convertir (bloqueando para no dejar el WAV a medias)."""
        if self.rec is None:
            return
        rec = self.rec
        res = {}
        try:
            res = self.engine.client.call("rec_stop", timeout=15) or {}
        except EngineError:
            pass
        self.rec = None
        rec.finish(res.get("seconds"), res.get("markers"), blocking=True)
