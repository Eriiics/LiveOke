"""Editor de la cadena de efectos del canal/bus seleccionado (todo se aplica en el motor)."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMenu, QMessageBox, QPushButton, QScrollArea, QSplitter, QVBoxLayout, QWidget,
)

from ..remote import EngineError, find_vst3
from .widgets import ParamControl, RawParamControl

VST3_DEFAULT_DIR = r"C:\Program Files\Common Files\VST3"


class ChainList(QListWidget):
    """Lista que avisa cuando el usuario reordena arrastrando."""
    reordered = Signal(int, int)   # (desde, hasta)

    def startDrag(self, actions):
        self._from = self.currentRow()
        super().startDrag(actions)

    def dropEvent(self, e):
        super().dropEvent(e)
        self.reordered.emit(getattr(self, "_from", -1), self.currentRow())


class _Async(QObject):
    done = Signal(object, object)


class ChainEditor(QFrame):
    chain_changed = Signal(object)   # Strip

    def __init__(self, engine_getter, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        self.engine = engine_getter
        self.strip = None
        self._param_widgets = []
        self._vst_cache: list[Path] | None = None
        self._async = _Async()
        self._async.done.connect(self._plugin_added)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        split = QSplitter(Qt.Horizontal)
        lay.addWidget(split)

        # --- izquierda: lista de efectos
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.header = QLabel("Selecciona un canal")
        self.header.setObjectName("h1")
        ll.addWidget(self.header)
        hint = QLabel("Arrastra para reordenar · casilla = activar/bypass")
        hint.setObjectName("dim")
        ll.addWidget(hint)
        self.list = ChainList()
        self.list.setDragDropMode(QListWidget.InternalMove)
        self.list.setDefaultDropAction(Qt.MoveAction)
        self.list.reordered.connect(self._reordered)
        self.list.currentRowChanged.connect(self._show_params)
        self.list.itemChanged.connect(self._item_toggled)
        self.list.itemDoubleClicked.connect(lambda it: self._open_editor())
        ll.addWidget(self.list, 1)
        btns = QHBoxLayout()
        self.add_btn = QPushButton("＋ Añadir")
        self.add_btn.clicked.connect(self._add_menu)
        self.rm_btn = QPushButton("Quitar")
        self.rm_btn.clicked.connect(self._remove)
        up = QPushButton("▲")
        up.clicked.connect(lambda: self._move(-1))
        dn = QPushButton("▼")
        dn.clicked.connect(lambda: self._move(1))
        for b in (self.add_btn, self.rm_btn, up, dn):
            btns.addWidget(b)
        ll.addLayout(btns)
        split.addWidget(left)

        # --- derecha: parámetros
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(8, 0, 0, 0)
        top = QHBoxLayout()
        self.fx_title = QLabel("")
        self.fx_title.setObjectName("h1")
        top.addWidget(self.fx_title, 1)
        self.editor_btn = QPushButton("🎛 Abrir interfaz del plugin")
        self.editor_btn.clicked.connect(self._open_editor)
        self.editor_btn.hide()
        top.addWidget(self.editor_btn)
        rl.addLayout(top)
        self.status = QLabel("")
        self.status.setObjectName("dim")
        self.status.setWordWrap(True)
        rl.addWidget(self.status)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filtrar parámetros del plugin…")
        self.filter.textChanged.connect(self._filter_params)
        self.filter.hide()
        rl.addWidget(self.filter)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.params_host = QWidget()
        self.params_lay = QVBoxLayout(self.params_host)
        self.params_lay.addStretch(1)
        self.scroll.setWidget(self.params_host)
        rl.addWidget(self.scroll, 1)
        split.addWidget(right)
        split.setSizes([300, 620])

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(200)
        self.set_strip(None)

    # ------------------------------------------------------------------
    def _client(self):
        return self.engine().client

    def _fail(self, title: str, e: Exception | str):
        QMessageBox.warning(self, title, str(e))

    def set_strip(self, strip):
        self.strip = strip
        self.add_btn.setEnabled(strip is not None)
        self.rm_btn.setEnabled(strip is not None)
        self.header.setText(f"Cadena: {strip.name}" if strip else "Selecciona un canal (botón ⛓ Cadena)")
        self._rebuild_list()

    def _reload(self, select: int | None = None):
        """Vuelve a leer el estado del motor y redibuja."""
        try:
            self.engine().refresh_state()
        except EngineError as e:
            self._fail("Motor", e)
        if self.strip is not None:
            self.strip = self.engine().mixer.strip(self.strip.id)
        self._rebuild_list(select)
        self.chain_changed.emit(self.strip)

    def _rebuild_list(self, select: int | None = None):
        self.list.blockSignals(True)
        self.list.clear()
        if self.strip:
            for fx in self.strip.chain:
                it = QListWidgetItem(("⚠ " if fx.failed else "") + fx.title)
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsDragEnabled)
                it.setCheckState(Qt.Checked if fx.enabled else Qt.Unchecked)
                it.setData(Qt.UserRole, fx.uid)
                if fx.failed:
                    it.setToolTip(fx.error or "El plugin no cargó")
                self.list.addItem(it)
        self.list.blockSignals(False)
        if self.list.count():
            self.list.setCurrentRow(min(select if select is not None else 0, self.list.count() - 1))
        else:
            self._show_params(-1)

    def current_fx(self):
        it = self.list.currentItem()
        if it is None or self.strip is None:
            return None
        uid = it.data(Qt.UserRole)
        return next((fx for fx in self.strip.chain if fx.uid == uid), None)

    def _item_toggled(self, it):
        uid = it.data(Qt.UserRole)
        fx = next((f for f in self.strip.chain if f.uid == uid), None) if self.strip else None
        if fx is not None:
            try:
                fx.enabled = it.checkState() == Qt.Checked
            except EngineError as e:
                self._fail("Motor", e)

    def _reordered(self, frm, to):
        if self.strip is None or frm < 0:
            return
        # el orden visible ya cambió: mandar el movimiento del elemento arrastrado
        uid = self.list.item(to).data(Qt.UserRole) if 0 <= to < self.list.count() else None
        if uid is None:
            return
        try:
            self._client().call("move_fx", strip=self.strip.id, slot=uid, index=to)
        except EngineError as e:
            self._fail("Motor", e)
        self._reload(to)

    def _move(self, d):
        fx = self.current_fx()
        if fx is None:
            return
        r = self.list.currentRow()
        nr = r + d
        if 0 <= nr < self.list.count():
            try:
                self._client().call("move_fx", strip=self.strip.id, slot=fx.uid, index=nr)
            except EngineError as e:
                self._fail("Motor", e)
            self._reload(nr)

    def _remove(self):
        fx = self.current_fx()
        if fx is None:
            return
        r = self.list.currentRow()
        try:
            self._client().call("remove_fx", strip=self.strip.id, slot=fx.uid)
        except EngineError as e:
            self._fail("Motor", e)
        self._reload(r)

    # ------------------------------------------------------------------
    def _add_menu(self):
        if not self.strip:
            return
        m = QMenu(self)
        cats: dict[str, list] = {}
        for t, info in self.engine().builtin_specs.items():
            cats.setdefault(info["category"], []).append((t, info["label"]))
        for cat in sorted(cats):
            sub = m.addMenu(cat)
            for t, label in cats[cat]:
                a = QAction(label, sub)
                a.triggered.connect(lambda _=False, t=t: self._add_builtin(t))
                sub.addAction(a)
        m.addSeparator()
        vst = m.addMenu("Plugins VST3 instalados")
        if self._vst_cache is None:
            self._vst_cache = find_vst3()
        if not self._vst_cache:
            na = vst.addAction("(no encontré plugins en Common Files\\VST3)")
            na.setEnabled(False)
        # los de Antares primero
        for p in sorted(self._vst_cache, key=lambda p: (0 if "antares" in str(p).lower() else 1, p.stem.lower())):
            a = QAction(p.stem, vst)
            a.triggered.connect(lambda _=False, p=p: self._add_vst(str(p)))
            vst.addAction(a)
        vst.addSeparator()
        rs = vst.addAction("Volver a escanear")
        rs.triggered.connect(lambda: setattr(self, "_vst_cache", None))
        m.addAction("Buscar archivo .vst3…").triggered.connect(self._browse_vst)
        m.exec(self.add_btn.mapToGlobal(self.add_btn.rect().bottomLeft()))

    def _insert_index(self):
        r = self.list.currentRow()
        return r + 1 if r >= 0 else len(self.strip.chain)

    def _add_builtin(self, t):
        try:
            self._client().call("add_fx", strip=self.strip.id, type=t, index=self._insert_index())
        except EngineError as e:
            self._fail("No pude agregar el efecto", e)
        self._reload(self._insert_index())

    def _browse_vst(self):
        path, _ = QFileDialog.getOpenFileName(self, "Plugin VST3", VST3_DEFAULT_DIR, "VST3 (*.vst3)")
        if path:
            self._add_vst(path)

    def _add_vst(self, path: str):
        name = ""
        try:
            names = self._client().call("plugin_types", path=path, timeout=30)
            if names and len(names) > 1:
                name, ok = QInputDialog.getItem(self, "Plugin", "Este archivo trae varios plugins:", names, 0, False)
                if not ok:
                    return
        except EngineError:
            pass
        QApplication.setOverrideCursor(Qt.WaitCursor)
        self.status.setText(f"Cargando {Path(path).stem}… (algunos plugins tardan, ej. verificación de licencia)")
        idx = self._insert_index()
        strip_id = self.strip.id
        self._client().call_async("add_plugin", lambda r, e: self._async.done.emit((idx, r), e),
                                  timeout=120, strip=strip_id, path=path, name=name, index=idx)

    def _plugin_added(self, res, err):
        QApplication.restoreOverrideCursor()
        if err:
            self.status.setText("")
            self._fail("No pude cargar el plugin", err)
            return
        idx, data = res
        self._reload(idx)
        fx = self.current_fx()
        if fx is not None and fx.looks_like_autotune and self.engine().key is not None:
            fx.on_key(self.engine().key)

    # ------------------------------------------------------------------
    def _clear_params(self):
        for w in self._param_widgets:
            w.setParent(None)
            w.deleteLater()
        self._param_widgets = []

    def _add_param_widget(self, w):
        self.params_lay.insertWidget(self.params_lay.count() - 1, w)
        self._param_widgets.append(w)

    def _show_params(self, row):
        self._clear_params()
        fx = self.current_fx()
        plugin_ok = fx is not None and fx.is_plugin and not fx.failed
        self.editor_btn.setVisible(plugin_ok)
        self.filter.setVisible(plugin_ok)
        self.filter.clear()
        if fx is None:
            self.fx_title.setText("")
            self.status.setText("")
            return
        self.fx_title.setText(fx.title)
        if fx.failed:
            self.status.setText(f"⚠ Este plugin no cargó: {fx.error}\nSe conserva su preset; si lo reinstalas, vuelve a cargar al reiniciar.")
            return
        eng = self.engine()
        sources = [s.name for s in eng.mixer.strips if s.kind == "input"]
        for p in fx.specs():
            w = ParamControl(p, fx.values.get(p.id, p.default), sources)
            w.changed.connect(lambda pid, v, fx=fx: self._param_changed(fx, pid, v))
            self._add_param_widget(w)
        if plugin_ok:
            if fx.looks_like_autotune:
                note = QLabel("🎯 Autotune detectado: la app le pone la tonalidad de la canción (Key/Scale).")
                note.setObjectName("dim")
                self._add_param_widget(note)
            if fx.is_autokey:
                note = QLabel("🔑 Auto-Key: la app lee la tonalidad que detecta y la usa para el autotune.")
                note.setObjectName("dim")
                self._add_param_widget(note)
            for cp in fx.raw_params(refresh=True)[:400]:
                self._add_param_widget(RawParamControl(cp))

    def _param_changed(self, fx, pid, v):
        try:
            fx.set(pid, v)
        except EngineError as e:
            self._fail("Motor", e)
        if pid == "auto_key" and v:
            fx.on_key(self.engine().key)
        if pid == "source":
            self._reload(self.list.currentRow())

    def _filter_params(self, text):
        t = text.lower()
        for w in self._param_widgets:
            if isinstance(w, RawParamControl):
                w.setVisible(t in w.p.name.lower())

    def _open_editor(self):
        fx = self.current_fx()
        if fx is not None and fx.is_plugin and not fx.failed:
            try:
                fx.show_editor()   # la ventana la abre el motor; no bloquea
            except EngineError as e:
                self._fail("Interfaz del plugin", e)

    def refresh_titles(self):
        if self.strip is None:
            return
        for i in range(self.list.count()):
            it = self.list.item(i)
            fx = next((f for f in self.strip.chain if f.uid == it.data(Qt.UserRole)), None)
            if fx is None:
                continue
            self.list.blockSignals(True)
            it.setText(("⚠ " if fx.failed else "") + fx.title)
            it.setCheckState(Qt.Checked if fx.enabled else Qt.Unchecked)
            self.list.blockSignals(False)

    def _tick(self):
        fx = self.current_fx()
        if fx is not None and not fx.failed:
            self.status.setText(fx.status or "")
