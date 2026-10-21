"""QApplication setup and styling."""

from __future__ import annotations

import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from .. import APP_NAME
from ..paths import resources_dir
from .main_window import MainWindow

STYLE = """
QWidget { background: #141b22; color: #e6edf3; font-size: 13px; }
QLabel, QRadioButton, QCheckBox { background: transparent; }
QLabel:disabled, QRadioButton:disabled { color: #4f5d6b; }
QFrame#card:disabled { border-color: #222d38; background: #172029; }
QFrame#header { background: #0e1419; border-bottom: 1px solid #263240; }
QFrame#footer { background: #0e1419; border-top: 1px solid #263240; }
QLabel#appTitle { font-size: 20px; font-weight: 600; background: transparent; }
QLabel#appSubtitle { color: #8b9bab; background: transparent; }
QLabel#step { padding: 6px 12px; margin-left: 6px; border-radius: 14px; color: #8b9bab;
              background: #1b2530; }
QLabel#step[state="active"] { background: #f0a030; color: #141b22; font-weight: 600; }
QLabel#step[state="done"] { background: #24543a; color: #d6f5e0; }
QLabel#pageTitle { font-size: 18px; font-weight: 600; margin: 4px 0 2px 0; }
QLabel#hint { color: #8b9bab; }
QLabel#stage { color: #f0b860; font-weight: 600; padding-left: 10px; }
QFrame#card { background: #1b2530; border: 1px solid #2c3a49; border-radius: 10px; padding: 8px; }
QFrame#card:hover { border-color: #f0a030; }
QRadioButton#cardTitle { font-size: 14px; font-weight: 600; }
QRadioButton::indicator { width: 12px; height: 12px; border-radius: 8px;
              border: 2px solid #5c6b7a; background: #0e1419; }
QRadioButton::indicator:checked { border: 2px solid #f0a030;
              background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
              stop:0 #f0a030, stop:0.55 #f0a030, stop:0.65 #0e1419); }
QRadioButton::indicator:disabled { border-color: #34465a; }
QPushButton { background: #24303d; border: 1px solid #34465a; border-radius: 6px;
              padding: 7px 16px; }
QPushButton:hover { border-color: #f0a030; }
QPushButton:disabled { color: #5c6b7a; border-color: #263240; }
QPushButton#primary { background: #f0a030; color: #141b22; font-weight: 600; border: none; }
QPushButton#primary:hover { background: #ffb84d; }
QPushButton#primary:disabled { background: #5a4a30; color: #9a8a70; }
QLineEdit, QSpinBox, QPlainTextEdit, QListWidget { background: #0e1419; border: 1px solid #2c3a49;
              border-radius: 6px; padding: 5px; selection-background-color: #f0a030;
              selection-color: #141b22; }
QListWidget::item { padding: 6px; }
QListWidget::item:selected { background: #3a2f1a; color: #ffd28a; }
QGroupBox { border: 1px solid #2c3a49; border-radius: 8px; margin-top: 14px; padding: 10px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #8b9bab; }
QProgressBar { border: 1px solid #2c3a49; border-radius: 6px; background: #0e1419; height: 16px;
               text-align: center; }
QProgressBar::chunk { background: #f0a030; border-radius: 5px; }
QSplitter::handle { background: #141b22; }
QToolTip { background: #24303d; color: #e6edf3; border: 1px solid #34465a; }
"""


def run() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    icon = resources_dir() / "icon.png"
    if icon.is_file():
        app.setWindowIcon(QIcon(str(icon)))
    window = MainWindow()
    window.show()
    return app.exec()
