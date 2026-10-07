"""Punto de entrada:  python -m vocalchain"""
import sys


def main():
    # El hilo de audio necesita el GIL a tiempo: cambiar de hilo más seguido evita cortes
    sys.setswitchinterval(0.0005)
    from PySide6.QtWidgets import QApplication

    from .ui import theme
    from .ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("VocalChain")
    theme.apply(app)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
