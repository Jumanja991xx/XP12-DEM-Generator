# PyInstaller spec - builds one folder containing two executables that share
# their libraries:
#   XP12DEMGenerator(.exe)  windowed GUI
#   xp12dem-worker(.exe)    console worker that runs Ortho4XP for one tile
#
#   pyinstaller --noconfirm packaging/xp12dem.spec
# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).parent
O4X = ROOT / "third_party" / "Ortho4XP"
WIN = sys.platform.startswith("win")
UTILS = "win" if WIN else ("mac" if sys.platform == "darwin" else "lin")

datas = [
    (str(ROOT / "xp12dem" / "resources"), "xp12dem/resources"),
    (str(O4X / "src"), "third_party/Ortho4XP/src"),
    (str(O4X / "Providers"), "third_party/Ortho4XP/Providers"),
    (str(O4X / "Filters"), "third_party/Ortho4XP/Filters"),
    (str(O4X / "Licence"), "third_party/Ortho4XP/Licence"),
    (str(O4X / "VENDORED.md"), "third_party/Ortho4XP"),
    (str(O4X / "Utils" / "water_transition.png"), "third_party/Ortho4XP/Utils"),
    (str(O4X / "Utils" / "world_tiles.png"), "third_party/Ortho4XP/Utils"),
    (str(O4X / "Utils" / UTILS), f"third_party/Ortho4XP/Utils/{UTILS}"),
    (str(ROOT / "LICENSE"), "."),
    (str(ROOT / "THIRD_PARTY_NOTICES.md"), "."),
]
binaries = []

# Libraries used by the (dynamically imported) Ortho4XP sources.
hiddenimports = [
    "array", "bz2", "hashlib", "io", "itertools", "pickle", "queue", "random",
    "struct", "threading", "zipfile", "subprocess",
    "numpy", "requests", "skfmm",
    "PIL.Image", "PIL.ImageDraw", "PIL.ImageFilter", "PIL.ImageEnhance", "PIL.ImageOps",
]
for package in ("shapely", "pyproj", "rtree"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden
hiddenimports += collect_submodules("skfmm")

excludes = ["tkinter", "_tkinter", "matplotlib", "IPython", "pytest", "cryptography",
            "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtPdf",
            "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtMultimedia",
            "PySide6.Qt3DCore", "PySide6.QtCharts", "PySide6.QtDataVisualization"]
icon = str(ROOT / "xp12dem" / "resources" / "icon.ico")

gui_a = Analysis([str(ROOT / "packaging" / "gui_entry.py")], pathex=[str(ROOT)],
                 binaries=binaries, datas=datas, hiddenimports=hiddenimports,
                 excludes=excludes, noarchive=False)
worker_a = Analysis([str(ROOT / "packaging" / "worker_entry.py")], pathex=[str(ROOT)],
                    binaries=binaries, datas=[], hiddenimports=hiddenimports,
                    excludes=excludes + ["PySide6"], noarchive=False)

gui_pyz = PYZ(gui_a.pure)
worker_pyz = PYZ(worker_a.pure)

gui_exe = EXE(gui_pyz, gui_a.scripts, [], exclude_binaries=True,
              name="XP12DEMGenerator", console=False, icon=icon,
              upx=False)
worker_exe = EXE(worker_pyz, worker_a.scripts, [], exclude_binaries=True,
                 name="xp12dem-worker", console=True, icon=icon, upx=False)

coll = COLLECT(gui_exe, gui_a.binaries, gui_a.datas,
               worker_exe, worker_a.binaries, worker_a.datas,
               upx=False, name="XP12DEMGenerator")
