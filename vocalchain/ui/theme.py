"""Tema oscuro tipo consola de estudio."""
from PySide6.QtGui import QColor, QFont, QPalette

BG = "#111215"
PANEL = "#181a1f"
STRIP = "#1e2127"
STRIP_BUS = "#1b2230"
STRIP_OUT = "#241e1a"
BORDER = "#2b2f37"
TEXT = "#e8e6e3"
DIM = "#8b9099"
ACCENT = "#ffb020"      # ámbar
ACCENT_2 = "#4fd1c5"    # turquesa
DANGER = "#ff5d5d"
OK = "#46d17a"

QSS = f"""
* {{ color: {TEXT}; }}
QMainWindow, QDialog, QWidget#root {{ background: {BG}; }}
QToolBar {{ background: {PANEL}; border: none; border-bottom: 1px solid {BORDER}; spacing: 6px; padding: 6px; }}
QToolButton, QPushButton {{
    background: #23262d; border: 1px solid {BORDER}; border-radius: 6px; padding: 5px 10px;
}}
QToolButton:hover, QPushButton:hover {{ border-color: {ACCENT}; }}
QToolButton:checked, QPushButton:checked {{ background: {ACCENT}; color: #1a1205; border-color: {ACCENT}; font-weight: 600; }}
QPushButton#small {{ padding: 2px 6px; border-radius: 4px; font-size: 11px; min-width: 0; }}
QPushButton#route:checked {{ background: {ACCENT_2}; border-color: {ACCENT_2}; color: #062320; }}
QPushButton#mute:checked {{ background: {DANGER}; border-color: {DANGER}; color: #2a0505; }}
QPushButton#solo:checked {{ background: #f2e05a; border-color: #f2e05a; color: #2a2505; }}
QPushButton#play:checked {{ background: {OK}; border-color: {OK}; color: #052a12; }}
QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox {{
    background: #23262d; border: 1px solid {BORDER}; border-radius: 5px; padding: 4px 6px;
}}
QComboBox QAbstractItemView {{ background: {PANEL}; selection-background-color: {ACCENT}; selection-color: #1a1205; }}
QListWidget {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 6px; padding: 4px; }}
QListWidget::item {{ padding: 7px 6px; border-radius: 5px; margin: 1px 0; }}
QListWidget::item:selected {{ background: #2d313a; border-left: 3px solid {ACCENT}; color: {TEXT}; }}
QScrollArea {{ border: none; background: transparent; }}
QSplitter::handle {{ background: {BORDER}; }}
QSlider::groove:horizontal {{ height: 4px; background: #2d313a; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{ width: 14px; margin: -6px 0; border-radius: 7px; background: {TEXT}; }}
QSlider::groove:vertical {{ width: 6px; background: #0c0d10; border-radius: 3px; }}
QSlider::handle:vertical {{
    height: 26px; margin: 0 -9px; border-radius: 4px;
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #d9d9d9, stop:0.48 #9a9a9a, stop:0.5 #222, stop:0.52 #9a9a9a, stop:1 #cfcfcf);
}}
QLabel#h1 {{ font-size: 15px; font-weight: 700; }}
QLabel#dim {{ color: {DIM}; }}
QLabel#key {{ font-size: 15px; font-weight: 700; color: {ACCENT}; }}
QLabel#warn {{ color: {DANGER}; font-size: 11px; }}
QFrame#strip {{ background: {STRIP}; border: 1px solid {BORDER}; border-radius: 10px; }}
QFrame#strip[kind="bus"] {{ background: {STRIP_BUS}; }}
QFrame#strip[kind="out"] {{ background: {STRIP_OUT}; }}
QFrame#strip[selected="true"] {{ border: 2px solid {ACCENT}; }}
QFrame#panel {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 10px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid {BORDER}; background: #23262d; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QStatusBar {{ background: {PANEL}; color: {DIM}; }}
QToolTip {{ background: {PANEL}; color: {TEXT}; border: 1px solid {BORDER}; }}
QMenu {{ background: {PANEL}; border: 1px solid {BORDER}; }}
QMenu::item:selected {{ background: {ACCENT}; color: #1a1205; }}
QTabWidget::pane {{ border: none; border-top: 1px solid {BORDER}; background: {BG}; }}
QTabBar {{ background: {PANEL}; }}
QTabBar::tab {{ background: {PANEL}; color: {DIM}; padding: 8px 18px; border: none; border-bottom: 2px solid transparent;
    font-weight: 600; }}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {ACCENT}; background: {BG}; }}
QTabBar::tab:hover {{ color: {TEXT}; }}
QTreeWidget {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 6px; alternate-background-color: #1c1f25; }}
QTreeWidget::item {{ padding: 4px 2px; }}
QTreeWidget::item:selected {{ background: #2d313a; color: {TEXT}; }}
QHeaderView::section {{ background: #1c1f25; color: {DIM}; border: none; border-bottom: 1px solid {BORDER}; padding: 5px 6px; }}
QRadioButton::indicator {{ width: 14px; height: 14px; border-radius: 7px; border: 1px solid {BORDER}; background: #23262d; }}
QRadioButton::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
"""


def apply(app):
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(BG))
    pal.setColor(QPalette.Base, QColor(PANEL))
    pal.setColor(QPalette.Text, QColor(TEXT))
    pal.setColor(QPalette.WindowText, QColor(TEXT))
    pal.setColor(QPalette.Button, QColor("#23262d"))
    pal.setColor(QPalette.ButtonText, QColor(TEXT))
    pal.setColor(QPalette.Highlight, QColor(ACCENT))
    app.setPalette(pal)
    f = QFont("Segoe UI Variable Text")
    f.setStyleHint(QFont.SansSerif)
    f.setPointSizeF(9.5)
    app.setFont(f)
    app.setStyleSheet(QSS)
