"""Punto de entrada:  python -m vocalchain   (la versión instalada lo abre con pythonw, sin consola)"""
import sys
from pathlib import Path

LOG_FILE = Path.home() / ".vocalchain" / "app.log"


def _redirect_output_if_windowless():
    """Con pythonw no hay consola (stdout/stderr = None): los errores van a ~/.vocalchain/app.log."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_000_000:
            LOG_FILE.write_text("", encoding="utf-8")
        f = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
        import time
        f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} inicio =====\n")
        sys.stdout = sys.stdout or f
        sys.stderr = sys.stderr or f
    except OSError:
        pass


def _windows_app_id():
    """Que la barra de tareas muestre el icono de VocalChain (no el de Python)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Chacho.VocalChain")
    except Exception:  # noqa: BLE001
        pass


def main():
    _redirect_output_if_windowless()
    _windows_app_id()
    # El hilo de audio necesita el GIL a tiempo: cambiar de hilo más seguido evita cortes
    sys.setswitchinterval(0.0005)
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from .ui import theme
    from .ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("VocalChain")
    icon = Path(__file__).resolve().parent / "ui" / "icon.png"
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    theme.apply(app)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        import traceback
        traceback.print_exc()
        sys.exit(1)
