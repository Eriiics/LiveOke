"""Ventana principal: barra superior, mezclador y editor de cadena. El audio lo procesa VocalEngine."""
from __future__ import annotations

import json
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QMainWindow, QMessageBox, QPushButton,
    QScrollArea, QSizePolicy, QSplitter, QToolBar, QVBoxLayout, QWidget,
)

from .. import lyrics as lyr
from ..music import Key
from ..nowplaying import NowPlayingWatcher
from ..remote import CONFIG_DIR, OUTPUTS, EngineError, RemoteEngine, find_vst3
from .chain_editor import ChainEditor
from .lyrics_window import LyricsWindow
from .settings_dialog import SettingsDialog
from .strips import AddBusWidget, OutputWidget, StripWidget

BUS_PRESETS = {
    "Reverb": (["reverb"], {}),
    "Delay": (["delay"], {}),
    "Reverb + sidechain (se abre en los silencios)": (["ducker", "reverb"], {0: {"depth_db": 12, "source": "Mic"}}),
    "Slapback": (["delay"], {0: {"time_ms": 110, "feedback": 0.1}}),
    "Delay ping-pong": (["delay"], {0: {"pingpong": True, "time_ms": 300}}),
    "Chorus / doble": (["chorus"], {}),
    "Vacío": ([], {}),
}


class Bridge(QObject):
    """Pasa eventos de hilos de fondo al hilo de la interfaz."""
    track_changed = Signal(object)
    lyrics_ready = Signal(object, object)   # (track_key, Lyrics)
    engine_started = Signal(bool)


class MainWindow(QMainWindow):
    def __init__(self, engine: RemoteEngine | None = None, autostart: bool = True):
        super().__init__()
        self.setWindowTitle("VocalChain — cadena vocal y karaoke")
        self.resize(1280, 860)
        self.engine = engine or RemoteEngine()
        self.engine.state_changed.connect(self._state_changed)
        self.selected_id = None
        self.strip_widgets: list[StripWidget] = []
        self.output_widgets: list[OutputWidget] = []
        self._last_key = None
        self._err_count = 0
        self._starting = False
        self._lyric_sources_tried: set[str] = set()

        self.bridge = Bridge()
        self.bridge.track_changed.connect(self._on_track_changed)
        self.bridge.lyrics_ready.connect(self._on_lyrics)
        self.bridge.engine_started.connect(self._engine_started)
        self.watcher = NowPlayingWatcher(on_change=self.bridge.track_changed.emit)
        self.lyrics_win = LyricsWindow()
        self.lyrics_win.manual_search.connect(self._manual_search)
        self.lyrics_win.refetch.connect(lambda: self._fetch_lyrics(self.watcher.track, use_cache=False))
        self.lyrics_win.next_source.connect(self._next_lyrics_source)

        self._build_toolbar()
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(10, 10, 10, 10)
        split = QSplitter(Qt.Vertical)
        lay.addWidget(split)

        self.mixer_area = QScrollArea()
        self.mixer_area.setWidgetResizable(True)
        self.mixer_host = QWidget()
        self.mixer_lay = QHBoxLayout(self.mixer_host)
        self.mixer_lay.setContentsMargins(0, 0, 0, 0)
        self.mixer_lay.setSpacing(8)
        self.mixer_area.setWidget(self.mixer_host)
        split.addWidget(self.mixer_area)

        self.chain = ChainEditor(lambda: self.engine)
        self.chain.chain_changed.connect(self._chain_changed)
        split.addWidget(self.chain)
        split.setSizes([560, 300])

        self.rebuild_mixer()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(33)
        self.slow = QTimer(self)
        self.slow.timeout.connect(self._slow_tick)
        self.slow.start(400)

        self.watcher.start()
        if self.watcher.error:
            self.statusBar().showMessage(self.watcher.error + " — usa 🔎 Buscar en la ventana de letras.", 10000)
        if autostart:
            QTimer.singleShot(150, self._start_audio)

    # ------------------------------------------------------------------ barra
    def _build_toolbar(self):
        tb = QToolBar()
        tb.setMovable(False)
        self.addToolBar(tb)
        self.play = QPushButton("⏻ Motor")
        self.play.setObjectName("play")
        self.play.setCheckable(True)
        self.play.setToolTip("Encender / apagar el motor de audio")
        self.play.clicked.connect(self._toggle_audio)
        tb.addWidget(self.play)
        b = QPushButton("⚙ Audio")
        b.clicked.connect(self._settings)
        tb.addWidget(b)
        b = QPushButton("💾 Guardar preset")
        b.clicked.connect(self._save_preset)
        tb.addWidget(b)
        b = QPushButton("📂 Cargar preset")
        b.clicked.connect(self._load_preset)
        tb.addWidget(b)
        b = QPushButton("🎤 Letra")
        b.setToolTip("Abrir la ventana de letras (Ctrl+L)")
        b.clicked.connect(self._show_lyrics)
        tb.addWidget(b)
        a = QAction(self)
        a.setShortcut(QKeySequence("Ctrl+L"))
        a.triggered.connect(self._show_lyrics)
        self.addAction(a)

        tb.addSeparator()
        tb.addWidget(QLabel("  Tonalidad "))
        self.key_lbl = QLabel("—")
        self.key_lbl.setObjectName("key")
        self.key_lbl.setMinimumWidth(90)
        tb.addWidget(self.key_lbl)
        self.key_combo = QComboBox()
        self.key_combo.addItem("Auto (detectar)", None)
        for mode in ("major", "minor"):
            for r in range(12):
                k = Key(r, mode)
                self.key_combo.addItem(k.name, (r, mode))
        mk = self.engine.manual_key
        if mk:
            idx = self.key_combo.findData((mk.root, mk.mode))
            self.key_combo.setCurrentIndex(max(0, idx))
        self.key_combo.setToolTip("Auto = la detecta del audio del PC. Elige una para fijarla.")
        self.key_combo.currentIndexChanged.connect(self._key_selected)
        tb.addWidget(self.key_combo)
        b = QPushButton("↺")
        b.setToolTip("Reiniciar detección de tonalidad")
        b.clicked.connect(self._reset_key)
        tb.addWidget(b)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        self.song_lbl = QLabel("")
        self.song_lbl.setObjectName("dim")
        tb.addWidget(self.song_lbl)
        self.cpu_lbl = QLabel("")
        self.cpu_lbl.setObjectName("dim")
        tb.addWidget(self.cpu_lbl)

    # ------------------------------------------------------------------ mezclador
    def rebuild_mixer(self):
        while self.mixer_lay.count():
            it = self.mixer_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self.strip_widgets = []
        self.output_widgets = []
        m = self.engine.mixer
        if not m.strips:
            msg = QLabel("Iniciando el motor de audio…" if self._starting else
                         "Motor apagado. Pulsa ⏻ Motor para encenderlo.")
            msg.setObjectName("dim")
            self.mixer_lay.addWidget(msg)
            self.mixer_lay.addStretch(1)
            return
        buses = m.buses
        for s in m.inputs + buses:
            w = StripWidget(s, buses, self.engine)
            w.selected.connect(self._select)
            w.remove_requested.connect(self._remove_bus)
            w.renamed.connect(self._rename_strip)
            w.set_selected(s.id == self.selected_id)
            self.mixer_lay.addWidget(w)
            self.strip_widgets.append(w)
        add = AddBusWidget()
        add.add_requested.connect(self._add_bus)
        self.mixer_lay.addWidget(add)
        self.mixer_lay.addStretch(1)
        a = self.engine.audio
        labels = {"monitor": a.get("name") or a.get("device") or "sin interfaz",
                  "stream": a.get("stream_device") or "sin dispositivo"}
        for o in OUTPUTS:
            w = OutputWidget(o, m.outputs[o], labels[o])
            self.mixer_lay.addWidget(w)
            self.output_widgets.append(w)

    def _state_changed(self):
        self.rebuild_mixer()
        sel = self.engine.mixer.strip(self.selected_id) if self.selected_id else None
        if sel is None:
            sel = self.engine.mixer.by_name("Mic") or (self.engine.mixer.strips[0] if self.engine.mixer.strips else None)
        if sel is not None:
            self._select(sel)
        else:
            self.chain.set_strip(None)

    def _select(self, strip):
        self.selected_id = strip.id
        for w in self.strip_widgets:
            w.set_selected(w.strip.id == strip.id)
        self.chain.set_strip(strip)

    def _chain_changed(self, strip):
        for w in self.strip_widgets:
            w.refresh_fx()

    def _rename_strip(self, strip):
        try:
            self.engine.client.call("set_strip", strip=strip.id, name=strip.name)
        except EngineError as e:
            QMessageBox.warning(self, "Motor", str(e))

    def _add_bus(self):
        names = list(BUS_PRESETS)
        choice, ok = QInputDialog.getItem(self, "Nuevo bus", "Tipo de bus:", names, 0, False)
        if not ok:
            return
        base = choice.split(" (")[0].split(" /")[0]
        name, n = base, 2
        while self.engine.mixer.by_name(name):
            name = f"{base} {n}"
            n += 1
        fx_types, params = BUS_PRESETS[choice]
        try:
            bus = self.engine.client.call("add_strip", name=name, kind="bus", fx=fx_types)
            for i, p in params.items():
                self.engine.client.call("set_fx", strip=bus["id"], slot=bus["chain"][i]["uid"], params=p)
            self.engine.refresh_state()
        except EngineError as e:
            QMessageBox.warning(self, "No pude crear el bus", str(e))
            return
        nb = self.engine.mixer.strip(bus["id"])
        if nb:
            self._select(nb)

    def _remove_bus(self, strip):
        if QMessageBox.question(self, "Quitar bus", f"¿Quitar el bus «{strip.name}»?") == QMessageBox.Yes:
            try:
                self.engine.client.call("remove_strip", strip=strip.id)
                if self.selected_id == strip.id:
                    self.selected_id = None
                self.engine.refresh_state()
            except EngineError as e:
                QMessageBox.warning(self, "Motor", str(e))

    # ------------------------------------------------------------------ motor
    def _start_audio(self):
        if self._starting:
            return
        self._starting = True
        self.play.setChecked(True)
        self.rebuild_mixer()
        self.statusBar().showMessage("Iniciando el motor de audio (si cargas plugins con licencia puede tardar unos segundos)…")
        threading.Thread(target=lambda: self.bridge.engine_started.emit(self.engine.start()), daemon=True).start()

    def _engine_started(self, ok: bool):
        self._starting = False
        self.play.setChecked(ok)
        self.statusBar().clearMessage()
        self._state_changed()
        self._show_errors()
        if ok:
            self._first_run_setup()
            self._last_key = "force"

    def _toggle_audio(self, on):
        if on:
            self._start_audio()
        else:
            self.engine.stop()
            self.engine.mixer.strips = []
            self.rebuild_mixer()
            self.chain.set_strip(None)

    def _first_run_setup(self):
        """La primera vez: agrega Auto-Tune Pro al micrófono y Auto-Key al canal PC."""
        s = self.engine.settings
        if s.get("first_run_done"):
            return
        s["first_run_done"] = True
        self.engine.save_settings()
        vsts = find_vst3()
        tune = next((p for p in vsts if "auto-tune" in p.stem.lower() or "autotune" in p.stem.lower()), None)
        key = next((p for p in vsts if "auto-key" in p.stem.lower()), None)
        mic = self.engine.mixer.by_name("Mic")
        pc = self.engine.mixer.by_name("PC")
        added = []
        if tune and mic and not any(fx.looks_like_autotune for fx in mic.chain):
            if QMessageBox.question(self, "Auto-Tune", f"Encontré {tune.stem}. ¿Lo agrego a la cadena del micrófono?\n"
                                    "(la app le pondrá la tonalidad de la canción automáticamente)") == QMessageBox.Yes:
                try:
                    self.engine.client.call("add_plugin", timeout=120, strip=mic.id, path=str(tune))
                    added.append(tune.stem)
                except EngineError as e:
                    QMessageBox.warning(self, "Auto-Tune", f"No pude cargar {tune.stem}: {e}")
        if key and pc and s.get("use_autokey", True) and not any(fx.is_autokey for fx in pc.chain):
            try:
                self.engine.client.call("add_plugin", timeout=120, strip=pc.id, path=str(key))
                added.append(f"{key.stem} (en el canal PC)")
            except EngineError as e:
                self.engine.errors.append(f"No pude cargar Auto-Key: {e}")
        if added:
            self.engine.refresh_state()
            self.statusBar().showMessage("Agregado: " + ", ".join(added), 10000)

    def _settings(self):
        if not self.engine.client.connected:
            QMessageBox.information(self, "Audio", "Primero enciende el motor (⏻ Motor).")
            return
        dlg = SettingsDialog(self.engine, self)
        if dlg.exec():
            err = dlg.apply_to(self.engine)
            try:
                self.engine.audio = self.engine.client.call("audio_devices", timeout=20).get("current", {})
                self.engine.refresh_state()
            except EngineError as e:
                err = str(e)
            if err:
                QMessageBox.warning(self, "Audio", err)
            self.engine.analyzer.start("CABLE Input" if self.engine.settings.get("windows_to_cable", True) else "")

    def _show_errors(self):
        errs = self.engine.errors
        if len(errs) > self._err_count:
            self.statusBar().showMessage("⚠ " + " | ".join(errs[self._err_count:]), 15000)
            self._err_count = len(errs)
        elif not errs:
            self._err_count = 0
            notes = self.engine.notes
            if notes and getattr(self, "_notes_shown", None) is not notes:
                self._notes_shown = notes
                self.statusBar().showMessage("ℹ " + " | ".join(notes), 12000)

    # ------------------------------------------------------------------ tonalidad
    def _key_selected(self, idx):
        data = self.key_combo.itemData(idx)
        self.engine.manual_key = Key(*data) if data else None
        self.engine.save_settings()

    def _reset_key(self):
        self.engine.key_detector.reset()
        self.engine.detected_key = None

    def _apply_key(self, key):
        for s in self.engine.mixer.strips:
            for fx in s.chain:
                try:
                    fx.on_key(key)
                except Exception as e:  # noqa: BLE001
                    fx.status = f"no pude ajustar tonalidad: {e}"

    def key_text(self):
        k = self.engine.key
        return k.short if k else "—"

    # ------------------------------------------------------------------ canción / letra
    def _on_track_changed(self, track):
        if track is None:
            return
        self.song_lbl.setText(f"♪ {track.artist} — {track.title}   ")
        if not self.engine.manual_key:
            self._reset_key()
        self._lyric_sources_tried = set()
        self.lyrics_win.set_track(track)
        self._fetch_lyrics(track)

    def _fetch_lyrics(self, track, use_cache=True, query: tuple[str, str] | None = None, exclude=None):
        if track is None:
            return
        self.lyrics_win.set_track(track)
        artist, title = query or (track.artist, track.title)
        album = "" if query else track.album
        excl = set(exclude or ())

        def work(t=track):
            try:
                res = lyr.fetch(artist, title, album, t.duration, use_cache=use_cache, exclude=excl)
            except Exception as e:  # noqa: BLE001
                res = None
                self.engine.errors.append(f"Letras: {e}")
            self.bridge.lyrics_ready.emit(t.key, res)

        threading.Thread(target=work, daemon=True).start()

    def _on_lyrics(self, key, res):
        cur = self.lyrics_win.track
        if cur is not None and cur.key == key:
            if res is not None and res.source:
                self._lyric_sources_tried.add(res.source)
            self.lyrics_win.set_lyrics(res)

    def _next_lyrics_source(self):
        t = self.watcher.track
        if t is None:
            return
        cur = self.lyrics_win.lyrics
        if cur is not None and cur.source:
            self._lyric_sources_tried.add(cur.source)
        self._fetch_lyrics(t, use_cache=False, exclude=self._lyric_sources_tried)

    def _manual_search(self, artist, title):
        if self.watcher.track is None:
            self.watcher.set_manual(artist, title)
        else:
            self._lyric_sources_tried = set()
            self._fetch_lyrics(self.watcher.track, use_cache=False, query=(artist, title))

    def _show_lyrics(self):
        self.lyrics_win.show()
        self.lyrics_win.raise_()
        self.lyrics_win.activateWindow()

    # ------------------------------------------------------------------ timers
    def _tick(self):
        blocked = self.engine.mixer.blocked
        for w in self.strip_widgets:
            w.tick(blocked)
        for w in self.output_widgets:
            w.tick()
        if self.lyrics_win.isVisible():
            self.lyrics_win.tick(self.watcher.track, self.key_text())

    def _slow_tick(self):
        k = self.engine.key
        kid = (k.root, k.mode) if k else None
        if kid != self._last_key and self.engine.client.connected:
            self._last_key = kid
            self._apply_key(k)
        if k is None:
            self.key_lbl.setText("…" if self.engine.running else "—")
            self.key_lbl.setToolTip("Escuchando el audio del PC…")
        else:
            src = "fijada" if self.engine.manual_key else f"detectada {max(0, k.confidence) * 100:.0f}%"
            self.key_lbl.setText(k.name)
            self.key_lbl.setToolTip(f"{k.name_es} ({src}) — relativa: {k.relative().name}")
        e = self.engine
        if e.running and e.stats:
            st = e.stats
            lat = f" · {e.latency_ms:.1f} ms" if e.latency_ms else ""
            cuts = f" · ⚠ cortes {e.total_xruns}" if e.total_xruns else ""
            pc = f" · PC +{st.get('pc_ms', 0):.0f} ms" if st.get("pc_active") else " · PC sin señal"
            self.cpu_lbl.setText(f"{st.get('type', '')} {e.sr / 1000:g} kHz · buffer {e.block}{lat}{pc} · "
                                 f"CPU {e.cpu * 100:.0f}%{cuts}  ")
            self.cpu_lbl.setToolTip("Latencia = ida y vuelta de tu voz (entrada + salida de la interfaz).\n"
                                    "PC = retraso extra de la música por VB-Cable.\n"
                                    "Cortes: si suben, sube el buffer en ⚙ Audio.")
        elif self._starting:
            self.cpu_lbl.setText("iniciando motor…  ")
        else:
            self.cpu_lbl.setText("motor apagado  ")
        self.play.setChecked(e.running or self._starting)
        self._show_errors()

    # ------------------------------------------------------------------ presets
    def _save_preset(self):
        d = CONFIG_DIR / "presets"
        d.mkdir(parents=True, exist_ok=True)
        path, _ = QFileDialog.getSaveFileName(self, "Guardar preset", str(d / "mi_cadena.json"), "Preset (*.json)")
        if not path:
            return
        try:
            data = self.engine.client.call("get_session", timeout=30)
        except EngineError as e:
            QMessageBox.warning(self, "Preset", str(e))
            return
        data.pop("audio", None)  # el preset no cambia tus dispositivos
        data["manual_key"] = [self.engine.manual_key.root, self.engine.manual_key.mode] if self.engine.manual_key else None
        Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        self.statusBar().showMessage(f"Preset guardado: {path}", 5000)

    def _load_preset(self):
        d = CONFIG_DIR / "presets"
        path, _ = QFileDialog.getOpenFileName(self, "Cargar preset", str(d), "Preset (*.json)")
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if "mixer" in data and "strips" not in data:
                raise ValueError("Es un preset de la versión anterior (sin motor); no es compatible.")
            self.engine.client.call("load_session", timeout=120, data=data)
            mk = data.get("manual_key")
            self.engine.manual_key = Key(mk[0], mk[1]) if mk else None
            self.selected_id = None
            self.engine.refresh_state()
            self._last_key = "force"
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Preset", f"No pude cargar el preset: {e}")

    # ------------------------------------------------------------------
    def closeEvent(self, e):
        try:
            self.engine.save()
        except Exception as ex:  # noqa: BLE001
            print("No pude guardar:", ex)
        self.watcher.stop()
        self.engine.stop()
        self.lyrics_win.close()
        super().closeEvent(e)
