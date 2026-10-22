"""The four-step wizard: Region -> Resolution -> Create -> Done."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Qt, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QRadioButton, QSpinBox, QSplitter, QStackedWidget,
    QVBoxLayout, QWidget,
)

from .. import APP_NAME, __version__, paths, regions
from ..dem import ATTRIBUTION, RESOLUTIONS
from ..installer import InstallTarget, pack_name
from ..inventory import Inventory, scan
from ..pipeline import MESH_DETAILS, BuildRequest, BuildResult, Events, Pipeline, mesh_detail
from ..settings import Settings
from ..xplane import find_xplane12_installs, is_xplane12, read_autoortho_config
from .map_widget import TileMap

STEPS = ("Region", "Method", "Create", "Done")
METHODS = (
    ("improve", "Improve my current terrain (recommended)",
     "Works with XPME, AutoOrtho, Ortho4XP packs and X-Plane's default scenery. Keeps your "
     "textures, water and roads and replaces only the heights with LiDAR data."),
    ("rebuild", "Build a new, denser mesh (AutoOrtho only)",
     "Ortho4XP builds a completely new mesh from the LiDAR data. Sharpest result, but the "
     "photos must come from AutoOrtho."),
)
CUSTOM_KEY = "__custom__"

# Rough per-tile build time in minutes on a mid-range CPU, used for estimates.
_MINUTES = {("1", "balanced"): 3, ("1", "high"): 4, ("1", "maximum"): 6,
            ("3", "balanced"): 2, ("3", "high"): 3, ("3", "maximum"): 3,
            ("13", "balanced"): 7, ("13", "high"): 9, ("13", "maximum"): 12}
# "improve" mode: copy of the existing mesh, time dominated by DSF conversion.
_IMPROVE_MINUTES = {"1": 2, "3": 2, "13": 4}
_IMPROVE_MB = 35
_PACK_MB = {("1", "balanced"): 35, ("1", "high"): 45, ("1", "maximum"): 60,
            ("3", "balanced"): 30, ("3", "high"): 35, ("3", "maximum"): 40,
            ("13", "balanced"): 60, ("13", "high"): 80, ("13", "maximum"): 110}
# Typical download per tile (MB) - national LiDAR mosaics are read partially.
_DOWNLOAD_MB = {"1": 25, "3": 25, "13": 150}

DATA_OVERVIEW = """
<h3>Where the elevation data comes from</h3>
<p>Every 1°×1° tile automatically gets the best free data for its area. Weaker sources only
fill gaps (sea, edges of a survey) and are blended in softly. Nothing needs an account or key.</p>
<table cellspacing="0" cellpadding="5" border="1" style="border-collapse:collapse; border-color:#34465a">
<tr style="background:#24303d"><th>Area</th><th>Source</th><th>Quality</th><th>Finest</th></tr>
<tr><td>Europe</td><td>Sonny's LiDAR DTM (CC BY 4.0)</td><td><span style="color:#3ad070">Best</span> /
<span style="color:#5a9bff">Good</span></td><td>1″ (30 m)</td></tr>
<tr><td>USA incl. Alaska &amp; Hawaii</td><td>USGS 3DEP (public domain)</td>
<td><span style="color:#3ad070">Best</span></td><td>1/3″ (10 m)</td></tr>
<tr><td>New Zealand</td><td>LINZ LiDAR 1 m + national 8 m DEM (CC BY 4.0)</td>
<td><span style="color:#3ad070">Best</span></td><td>1/3″ (10 m)</td></tr>
<tr><td>Canada</td><td>NRCan HRDEM LiDAR (surveyed areas) + MRDEM 30 m (OGL Canada)</td>
<td><span style="color:#3ad070">Best</span> / <span style="color:#5a9bff">Good</span></td><td>1/3″ (10 m)</td></tr>
<tr><td>Japan</td><td>GSI elevation tiles 5–10 m</td><td><span style="color:#5a9bff">Good</span></td>
<td>1/3″ (10 m)</td></tr>
<tr><td>Greenland, high Arctic</td><td>ArcticDEM 10 m (CC BY 4.0), converted to heights above sea level</td>
<td><span style="color:#5a9bff">Good</span></td><td>1/3″ (10 m)</td></tr>
<tr><td>Everywhere else</td><td>FABDEM 30 m (forests and buildings removed; non-commercial licence),
Copernicus GLO-30 fills gaps</td><td><span style="color:#ff6b6b">Basic</span></td><td>1″ (30 m)</td></tr>
</table>
<h3>Even better data you can add yourself</h3>
<p>Download the files and put them into the <b>Own elevation files</b> folder (step 2, Advanced).
Any GeoTIFF in any projection works - the app finds the tiles by their coordinates and prefers
them over everything else.</p>
<ul>
<li><b>Switzerland</b> - swissALTI3D 0.5 / 2 m: swisstopo.admin.ch (Height model swissALTI3D)</li>
<li><b>Austria</b> - ALS DTM 1 m / 10 m: data.gv.at (Digitales Geländemodell Österreich)</li>
<li><b>Bavaria</b> - DGM1: geodaten.bayern.de (OpenData, Digitales Geländemodell 1 m)</li>
<li><b>South Tyrol</b> - DTM 0.5 / 2.5 m: geokatalog.buergernetz.bz.it</li>
<li><b>Australia</b> - LiDAR 1–5 m: elevation.fsdf.org.au (ELVIS)</li>
<li><b>Mexico</b> - CEM 15 m: inegi.org.mx (Continuo de Elevaciones Mexicano)</li>
</ul>
<p>Choose <b>1/3″ (≈ 10 m)</b> in step 2 to use the full detail of 1–10 m files.</p>
"""


def _duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds} s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {seconds:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"


def _title(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("pageTitle")
    return label


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("hint")
    label.setWordWrap(True)
    return label


class OptionCard(QFrame):
    """A big clickable radio button with a description underneath."""

    def __init__(self, title: str, description: str, group: QButtonGroup, key: str):
        super().__init__()
        self.setObjectName("card")
        self.key = key
        self.radio = QRadioButton(title)
        self.radio.setObjectName("cardTitle")
        group.addButton(self.radio)
        desc = _hint(description)
        layout = QVBoxLayout(self)
        layout.addWidget(self.radio)
        layout.addWidget(desc)

    def mousePressEvent(self, event):  # noqa: N802 - Qt API
        self.radio.setChecked(True)


# ---------------------------------------------------------------------------
# Background build
# ---------------------------------------------------------------------------
class _SignalEvents(QObject, Events):
    logged = Signal(str)
    staged = Signal(str)
    progressed = Signal(float, float)
    tileChanged = Signal(int, int, str)

    def __init__(self):
        QObject.__init__(self)

    def log(self, text: str) -> None:
        self.logged.emit(text)

    def stage(self, name: str) -> None:
        self.staged.emit(name)

    def progress(self, done: float, total: float) -> None:
        self.progressed.emit(done, total)

    def tile_state(self, tile, state: str) -> None:
        self.tileChanged.emit(tile[0], tile[1], state)


class BuildThread(QThread):
    finishedWithResult = Signal(object)

    def __init__(self, pipeline: Pipeline):
        super().__init__()
        self.pipeline = pipeline

    def run(self) -> None:
        try:
            result = self.pipeline.run()
        except Exception as exc:  # surfaced in the GUI
            result = BuildResult()
            result.failed_tiles[(0, 0)] = f"{type(exc).__name__}: {exc}"
        self.finishedWithResult.emit(result)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = Settings.load()
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1180, 760)
        self.pipeline: Optional[Pipeline] = None
        self._last_tiles: list = []
        self._build_t0 = time.monotonic()
        self._eta: Optional[float] = None
        self.thread: Optional[BuildThread] = None
        self.result: Optional[BuildResult] = None
        self.xplane: Optional[Path] = self._detect_xplane()
        self.inventory = Inventory()

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())
        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_region_page())
        self.stack.addWidget(self._build_quality_page())
        self.stack.addWidget(self._build_create_page())
        self.stack.addWidget(self._build_done_page())
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(24, 16, 24, 8)
        body_layout.addWidget(self.stack)
        root.addWidget(body, 1)
        root.addWidget(self._build_footer())
        self.setCentralWidget(central)

        self._restore_choices()
        self._refresh_inventory()
        self._go(0)

    # ------------------------------------------------------------------
    # Header / footer
    # ------------------------------------------------------------------
    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName("header")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(24, 14, 24, 14)
        title = QLabel(APP_NAME)
        title.setObjectName("appTitle")
        subtitle = QLabel("LiDAR terrain for X-Plane 12 - works with XPME, AutoOrtho and the default scenery")
        subtitle.setObjectName("appSubtitle")
        texts = QVBoxLayout()
        texts.addWidget(title)
        texts.addWidget(subtitle)
        layout.addLayout(texts)
        layout.addStretch(1)
        self.step_labels: list[QLabel] = []
        for index, name in enumerate(STEPS):
            label = QLabel(f"{index + 1}  {name}")
            label.setObjectName("step")
            self.step_labels.append(label)
            layout.addWidget(label)
        return header

    def _build_footer(self) -> QWidget:
        footer = QFrame()
        footer.setObjectName("footer")
        layout = QHBoxLayout(footer)
        layout.setContentsMargins(24, 10, 24, 14)
        self.delete_btn = QPushButton("Delete data…")
        self.delete_btn.setToolTip("Delete downloaded data, caches and - if you want - installed packs")
        self.delete_btn.clicked.connect(self._delete_data)
        layout.addWidget(self.delete_btn)
        self.status_label = _hint("")
        layout.addWidget(self.status_label, 1)
        self.back_btn = QPushButton("Back")
        self.next_btn = QPushButton("Next")
        self.next_btn.setObjectName("primary")
        self.back_btn.clicked.connect(lambda: self._go(self.stack.currentIndex() - 1))
        self.next_btn.clicked.connect(self._next)
        layout.addWidget(self.back_btn)
        layout.addWidget(self.next_btn)
        return footer

    def _go(self, index: int) -> None:
        index = max(0, min(index, len(STEPS) - 1))
        self.stack.setCurrentIndex(index)
        for i, label in enumerate(self.step_labels):
            label.setProperty("state", "active" if i == index else ("done" if i < index else "todo"))
            label.style().unpolish(label)
            label.style().polish(label)
        building = self.thread is not None and self.thread.isRunning()
        self.back_btn.setVisible(index in (1, 2))
        self.back_btn.setEnabled(not building)
        self.next_btn.setVisible(index in (0, 1))
        if index == 2:
            self._refresh_summary()
        self._update_status()

    def _next(self) -> None:
        index = self.stack.currentIndex()
        if index == 0 and not self.map.selected():
            QMessageBox.information(self, APP_NAME, "Please select at least one tile.")
            return
        self._go(index + 1)

    # ------------------------------------------------------------------
    # Page 1: region
    # ------------------------------------------------------------------
    def _build_region_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(_title("1  Choose a region"))
        layout.addWidget(_hint("Pick a preset or click 1°×1° tiles on the map. Drag to move the map, "
                               "mouse wheel or +/− to zoom, Shift+drag selects many tiles, right-drag "
                               "removes them. Green = LiDAR (best), blue = national model, red = satellite "
                               "only. <b style='color:#ff8c00'>Orange ✓</b> tiles are already installed; "
                               "dashed ones have their data downloaded but are not built yet."))
        splitter = QSplitter(Qt.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 8, 0)
        self.region_list = QListWidget()
        self.region_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.region_list.setWordWrap(True)
        last_area = None
        for region in regions.PRESETS:
            if region.area != last_area:
                header = QListWidgetItem(region.area.upper())
                header.setFlags(Qt.NoItemFlags)
                header.setData(Qt.UserRole, None)
                self.region_list.addItem(header)
                last_area = region.area
            item = QListWidgetItem(f"{region.name}  ({len(region.tiles())} tiles)")
            item.setData(Qt.UserRole, region.key)
            item.setToolTip(region.description)
            self.region_list.addItem(item)
        header = QListWidgetItem("OTHER")
        header.setFlags(Qt.NoItemFlags)
        header.setData(Qt.UserRole, None)
        self.region_list.addItem(header)
        custom = QListWidgetItem("Custom selection")
        custom.setData(Qt.UserRole, CUSTOM_KEY)
        self.region_list.addItem(custom)
        self.region_list.currentItemChanged.connect(self._preset_chosen)
        left_layout.addWidget(self.region_list, 1)
        self.region_desc = _hint("")
        left_layout.addWidget(self.region_desc)
        left_layout.addWidget(QLabel("Scenery pack name"))
        self.name_edit = QLineEdit()
        self.name_edit.textChanged.connect(self._update_status)
        left_layout.addWidget(self.name_edit)
        buttons = QHBoxLayout()
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(lambda: self.map.set_selected([], focus=False))
        buttons.addWidget(clear_btn)
        self.view_combo = QComboBox()
        self.view_combo.addItem("Go to…")
        from .map_widget import VIEWS

        for view in VIEWS:
            self.view_combo.addItem(view)
        self.view_combo.activated.connect(self._go_to_view)
        buttons.addWidget(self.view_combo, 1)
        left_layout.addLayout(buttons)
        about_data = QPushButton("Where does the data come from?")
        about_data.clicked.connect(self._show_data_overview)
        left_layout.addWidget(about_data)
        self.quality_btn = QPushButton("Highlight data quality")
        self.quality_btn.setCheckable(True)
        self.quality_btn.setToolTip("Strong colours: green = LiDAR, blue = national model, "
                                    "grey = satellite only")
        self.quality_btn.toggled.connect(lambda on: self.map.set_quality_view(on))
        left_layout.addWidget(self.quality_btn)
        splitter.addWidget(left)

        self.map = TileMap()
        self.map.selectionChanged.connect(self._map_changed)
        self.map.hovered.connect(lambda text: self.map_hover.setText(text))
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 0, 0, 0)
        right_layout.addWidget(self.map, 1)
        self.map_hover = _hint(" ")
        right_layout.addWidget(self.map_hover)
        have_row = QHBoxLayout()
        self.have_label = QLabel(" ")
        self.have_label.setTextFormat(Qt.RichText)
        self.have_label.setWordWrap(True)
        have_row.addWidget(self.have_label, 1)
        self.missing_btn = QPushButton("Select only missing tiles")
        self.missing_btn.setToolTip("Remove tiles that are already installed from the selection")
        self.missing_btn.clicked.connect(self._select_missing)
        have_row.addWidget(self.missing_btn)
        self.installed_btn = QPushButton("Show my installed tiles")
        self.installed_btn.clicked.connect(self._show_installed)
        have_row.addWidget(self.installed_btn)
        right_layout.addLayout(have_row)
        inactive_row = QHBoxLayout()
        self.inactive_label = QLabel("")
        self.inactive_label.setTextFormat(Qt.RichText)
        self.inactive_label.setWordWrap(True)
        inactive_row.addWidget(self.inactive_label, 1)
        self.activate_btn = QPushButton("Fix now")
        self.activate_btn.setObjectName("primary")
        self.activate_btn.setToolTip("Move the packs into Custom Scenery and list them in scenery_packs.ini, "
                                     "above the scenery they improve")
        self.activate_btn.clicked.connect(self._activate_inactive)
        inactive_row.addWidget(self.activate_btn)
        right_layout.addLayout(inactive_row)
        splitter.addWidget(right)
        splitter.setSizes([300, 860])
        layout.addWidget(splitter, 1)
        self._suppress_custom = False
        return page

    def _preset_chosen(self, item: Optional[QListWidgetItem], _prev=None) -> None:
        if item is None:
            return
        key = item.data(Qt.UserRole)
        if key is None:  # group header
            return
        if key == CUSTOM_KEY:
            self.region_desc.setText("Your own tile selection.")
            if not self.name_edit.text() or self._name_is_preset():
                self.name_edit.setText("My region")
            if self.settings.custom_tiles and not self.map.selected():
                self.map.set_selected([tuple(t) for t in self.settings.custom_tiles])
            return
        region = regions.preset(key)
        self.region_desc.setText(region.description)
        self._suppress_custom = True
        self.map.set_selected(region.tiles())
        self._suppress_custom = False
        self.name_edit.setText(region.name)

    def _go_to_view(self, index: int) -> None:
        if index > 0:
            self.map.show_view(self.view_combo.itemText(index))
        self.view_combo.setCurrentIndex(0)

    def _show_data_overview(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Elevation data")
        dialog.resize(760, 640)
        layout = QVBoxLayout(dialog)
        text = QLabel(DATA_OVERVIEW)
        text.setTextFormat(Qt.RichText)
        text.setWordWrap(True)
        text.setTextInteractionFlags(Qt.TextBrowserInteraction)
        layout.addWidget(text)
        layout.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    # -- what is already installed / downloaded ---------------------------
    def _refresh_inventory(self) -> None:
        try:
            self.inventory = scan(self.xplane, paths.dem_cache_dir(), self._extra_roots())
        except Exception as exc:  # never block the app because of an odd folder
            self._append_log(f"Could not read installed packs: {exc}")
            self.inventory = Inventory()
        self.map.set_inventory(self.inventory)
        self.build_map.set_inventory(self.inventory)
        self._update_have()

    def _update_have(self) -> None:
        if not hasattr(self, "have_label"):
            return
        tiles = self.map.selected()
        inv = self.inventory
        installed = [t for t in tiles if t in inv.installed]
        missing = inv.missing(tiles)
        downloaded = [t for t in missing if t in inv.cached]
        if not tiles:
            total = len(inv.installed)
            self.have_label.setText(
                f"You have <b>{total}</b> tile(s) installed in {len(inv.packs)} pack(s)." if total
                else "Nothing installed yet.")
        else:
            from ..dem_sources import FABDEM, LIDAR, NATIONAL, USGS, coverage

            packs = sorted({p.name for t in installed for p in inv.installed[t]})
            text = f"Selection: <b>{len(tiles)}</b> tile(s) · "
            if installed:
                text += (f"<span style='color:#ff9a1f'>✓ {len(installed)} already installed</span>"
                         f" ({', '.join(packs[:3])}{'…' if len(packs) > 3 else ''}) · ")
            text += f"<b>{len(missing)} not built yet</b>"
            if downloaded:
                text += f" (elevation data for {len(downloaded)} of them already downloaded)"
            # which data the selection gets - every tile uses its own best source
            counts: dict[str, int] = {}
            for tile in tiles:
                info = coverage().get(tile)
                rating = {LIDAR: "best", USGS: "best", NATIONAL: "good", FABDEM: "basic"}.get(
                    info.quality if info else "", "basic")
                counts[rating] = counts.get(rating, 0) + 1
            colours = {"best": "#3ad070", "good": "#5a9bff", "basic": "#ff6b6b"}
            names = {"best": "Best (LiDAR)", "good": "Good (national model)", "basic": "Basic (satellite)"}
            mix = ", ".join(f"<span style='color:{colours[r]}'>{counts[r]} × {names[r]}</span>"
                            for r in ("best", "good", "basic") if r in counts)
            text += f"<br>Elevation data: {mix}"
            if len(counts) > 1:
                text += " - every tile automatically gets its own best data"
                if not hasattr(self, "method_cards") or self._method_key() == "improve":
                    text += ", tile edges blend smoothly into each other"
            self.have_label.setText(text)
        self.missing_btn.setEnabled(bool(tiles) and bool(installed) and bool(missing))
        self.installed_btn.setEnabled(bool(inv.installed))
        broken = inv.needs_fix()
        self.inactive_label.setVisible(bool(broken))
        self.activate_btn.setVisible(bool(broken))
        if broken:
            lines = []
            for p in broken:
                why = []
                if not p.enabled:
                    why.append("not listed in scenery_packs.ini, so X-Plane ignores it")
                if p.misplaced:
                    why.append(f"stored in {p.path.parent} - XPME drops such entries when it "
                               "rewrites scenery_packs.ini")
                if p.broken:
                    names = ", ".join(regions.tile_name(*t) for t in sorted(p.broken)[:6])
                    more = f" … (+{len(p.broken) - 6})" if len(p.broken) > 6 else ""
                    why.append(f"{len(p.broken)} tile(s) miss terrain files ({names}{more}), so X-Plane "
                               "refuses the whole pack - Fix now takes them out (they show XPME's "
                               "terrain again); select them and build again")
                lines.append(f"<b>{p.name}</b> ({len(p.tiles)} tiles): " + "; ".join(why))
            self.inactive_label.setText(
                "<span style='color:#ff9a1f'><b>⚠ Needs a fix:</b> " + "<br>".join(lines)
                + "<br>Fix now repairs this: broken tiles are taken out, packs move into Custom "
                "Scenery (textures keep working) and are listed above the scenery they improve.</span>")

    def _activate_inactive(self) -> None:
        from ..installer import reactivate_pack, relocate_pack

        if self.xplane is None:
            return
        custom = self.xplane / "Custom Scenery"
        errors, moved = [], []
        # several copies of one name: merge all into Custom Scenery/<name>
        removed: list = []
        for pack in self.inventory.needs_fix():
            try:
                if pack.broken:
                    from ..inventory import remove_tiles

                    remove_tiles(pack, list(pack.broken))
                    removed += list(pack.broken)
                if not any(pack.path.glob("Earth nav data/*/*.dsf")):
                    continue  # nothing left in this copy
                target = custom / pack.name
                if pack.misplaced and pack.path.exists():
                    relocate_pack(pack.path, target)
                    moved.append(f"{pack.name}: {pack.path.parent} → Custom Scenery")
                reactivate_pack(target if pack.misplaced else pack.path, self.xplane, pack.improves)
            except OSError as exc:
                errors.append(f"{pack.name}: {exc}")
        self._refresh_inventory()
        if errors:
            QMessageBox.warning(self, APP_NAME, "Could not fix everything:\n" + "\n".join(errors))
        else:
            if removed:
                self.map.set_selected(sorted(set(removed)))
            QMessageBox.information(
                self, APP_NAME, "Done.\n\n" + ("\n".join(moved) + "\n\n" if moved else "")
                + (f"{len(set(removed))} incomplete tile(s) were taken out and are now selected - "
                   "start XPME, wait until it has finished loading, then build them again "
                   "(Next).\n\n" if removed else "")
                + "The packs are listed in scenery_packs.ini above the scenery they improve (backup: "
                "scenery_packs.ini.xp12dem.bak).")

    def _select_missing(self) -> None:
        self.map.set_selected(self.inventory.missing(self.map.selected()), focus=False)

    def _show_installed(self) -> None:
        self.map.focus_tiles(self.inventory.installed)

    def _delete_data(self) -> None:
        """Delete app data and optionally installed packs - asks twice."""
        from ..cleanup import delete_items, delete_packs, folder_size, human_size, local_items

        if self.thread is not None and self.thread.isRunning():
            QMessageBox.information(self, APP_NAME, "Please wait until the build has finished.")
            return
        self._refresh_inventory()
        dialog = QDialog(self)
        dialog.setWindowTitle("Delete data")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("<b>What should be deleted?</b>"))
        boxes = []
        for item in local_items():
            size = item.size
            box = QCheckBox(f"{item.label}  ({human_size(size)})")
            box.setChecked(item.default and size > 0)
            box.setEnabled(size > 0)
            boxes.append((box, item, size))
            layout.addWidget(box)
        packs = self.inventory.packs
        pack_sizes = sum(folder_size(p.path) for p in packs)
        packs_box = QCheckBox(f"Installed terrain packs of this app ({len(packs)} pack(s), "
                              f"{human_size(pack_sizes)}) - removed from X-Plane")
        packs_box.setEnabled(bool(packs))
        layout.addWidget(packs_box)
        if packs:
            layout.addWidget(_hint("  " + ", ".join(p.name for p in packs)))
        layout.addWidget(_hint("X-Plane, XPME, AutoOrtho and all other scenery are never touched."))
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        go = buttons.addButton("Delete…", QDialogButtonBox.DestructiveRole)
        go.clicked.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            return
        chosen = [(item, size) for box, item, size in boxes if box.isChecked()]
        remove_packs = packs_box.isChecked()
        if not chosen and not remove_packs:
            return
        total = sum(size for _, size in chosen) + (pack_sizes if remove_packs else 0)
        lines = [f"• {item.label}" for item, _ in chosen]
        if remove_packs:
            lines.append(f"• {len(packs)} installed pack(s): " + ", ".join(p.name for p in packs))
        confirm = QMessageBox(self)
        confirm.setIcon(QMessageBox.Warning)
        confirm.setWindowTitle(APP_NAME)
        confirm.setText(f"Really delete {human_size(total)}? This cannot be undone.\n\n" + "\n".join(lines))
        yes = confirm.addButton("Yes, delete permanently", QMessageBox.DestructiveRole)
        confirm.addButton(QMessageBox.Cancel)
        confirm.setDefaultButton(QMessageBox.Cancel)
        confirm.exec()
        if confirm.clickedButton() is not yes:
            return
        errors = delete_items(item for item, _ in chosen)
        if remove_packs:
            errors += delete_packs(packs, self.xplane)
        self._refresh_inventory()
        if errors:
            QMessageBox.warning(self, APP_NAME, "Some files could not be deleted (in use?):\n\n"
                                + "\n".join(errors[:10]))
        else:
            QMessageBox.information(self, APP_NAME, f"Deleted {human_size(total)}.")

    def _name_is_preset(self) -> bool:
        return self.name_edit.text() in {r.name for r in regions.PRESETS}

    def _map_changed(self) -> None:
        if not self._suppress_custom:
            current = self.region_list.currentItem()
            if current is not None and current.data(Qt.UserRole) not in (None, CUSTOM_KEY):
                region = regions.preset(current.data(Qt.UserRole))
                if sorted(region.tiles()) != self.map.selected():
                    self.region_list.blockSignals(True)
                    self.region_list.setCurrentRow(self.region_list.count() - 1)
                    self.region_list.blockSignals(False)
                    self.region_desc.setText("Your own tile selection.")
        self._update_have()
        self._update_status()

    # ------------------------------------------------------------------
    # Page 2: resolution
    # ------------------------------------------------------------------
    def _build_quality_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(_title("2  Choose method and DEM resolution"))
        self.method_group = QButtonGroup(self)
        self.method_cards: list[OptionCard] = []
        row = QHBoxLayout()
        for key, title, text in METHODS:
            card = OptionCard(title, text, self.method_group, key)
            self.method_cards.append(card)
            row.addWidget(card)
        layout.addLayout(row)
        self.method_group.buttonToggled.connect(lambda *_: self._method_changed())

        layout.addSpacing(6)
        layout.addWidget(QLabel("DEM resolution"))
        layout.addWidget(_hint("Every tile uses the best free data for its area (LiDAR or national "
                               "models where available) - far more accurate than the SRTM data in "
                               "X-Plane's default mesh, XPME and the stock AutoOrtho packs."))
        self.res_group = QButtonGroup(self)
        self.res_cards: list[OptionCard] = []
        row = QHBoxLayout()
        for res in RESOLUTIONS:
            card = OptionCard(res.label, res.description, self.res_group, res.key)
            self.res_cards.append(card)
            row.addWidget(card)
        layout.addLayout(row)
        self.fine_label = _hint("")
        layout.addWidget(self.fine_label)

        layout.addSpacing(6)
        self.mesh_label = QLabel("Mesh detail (new mesh only)")
        layout.addWidget(self.mesh_label)
        self.mesh_group = QButtonGroup(self)
        self.mesh_cards: list[OptionCard] = []
        row = QHBoxLayout()
        for detail in MESH_DETAILS:
            card = OptionCard(detail.label, detail.description, self.mesh_group, detail.key)
            self.mesh_cards.append(card)
            row.addWidget(card)
        layout.addLayout(row)
        self.res_group.buttonToggled.connect(lambda *_: self._update_status())
        self.mesh_group.buttonToggled.connect(lambda *_: self._update_status())

        advanced = QGroupBox("Advanced (optional)")
        grid = QGridLayout(advanced)
        grid.addWidget(QLabel("Tiles built at the same time"), 0, 0)
        self.parallel_combo = QComboBox()
        self.parallel_combo.addItem("Automatic (recommended)", 0)
        for n in range(1, 9):
            self.parallel_combo.addItem(f"{n} at a time", n)
        self.parallel_combo.currentIndexChanged.connect(lambda *_: self._parallel_changed())
        grid.addWidget(self.parallel_combo, 0, 1)
        self.parallel_hint = _hint("")
        grid.addWidget(self.parallel_hint, 0, 2)
        grid.addWidget(QLabel("Own elevation files"), 1, 0)
        folder_row = QHBoxLayout()
        self.dem_folder_edit = QLineEdit()
        self.dem_folder_edit.setPlaceholderText("empty = download automatically")
        folder_row.addWidget(self.dem_folder_edit, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._choose_dem_folder)
        folder_row.addWidget(browse)
        grid.addLayout(folder_row, 1, 1, 1, 2)
        grid.addWidget(_hint("Optional folder with your own elevation files: any GeoTIFF in any "
                             "projection (e.g. swissALTI3D, Austrian / Bavarian 1 m models, Australian "
                             "LiDAR), SRTM .hgt, Sonny .zip, FABDEM zips. Files here are used first; "
                             "everything else is downloaded automatically. See \"Where does the data "
                             "come from?\" on step 1 for download links."), 2, 0, 1, 3)
        grid.setColumnStretch(2, 1)
        layout.addWidget(advanced)
        layout.addStretch(1)
        return page

    def _choose_dem_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Folder with your own elevation files")
        if folder:
            self.dem_folder_edit.setText(folder)

    def _method_key(self) -> str:
        for card in self.method_cards:
            if card.radio.isChecked():
                return card.key
        return "improve"

    def _method_changed(self) -> None:
        rebuild = self._method_key() == "rebuild"
        for card in self.mesh_cards:
            card.setEnabled(rebuild)
        self.mesh_label.setEnabled(rebuild)
        if hasattr(self, "parallel_hint"):
            self._parallel_changed()  # the recommendation depends on the method
        self._update_status()

    def _res_key(self) -> str:
        for card in self.res_cards:
            if card.radio.isChecked():
                return card.key
        return "1"

    def _mesh_key(self) -> str:
        for card in self.mesh_cards:
            if card.radio.isChecked():
                return card.key
        return "high"

    # ------------------------------------------------------------------
    # Page 3: create
    # ------------------------------------------------------------------
    def _build_create_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(_title("3  Create and install"))

        top = QHBoxLayout()
        summary_box = QGroupBox("Summary")
        summary_layout = QVBoxLayout(summary_box)
        self.summary_label = QLabel()
        self.summary_label.setTextFormat(Qt.RichText)
        self.summary_label.setWordWrap(True)
        summary_layout.addWidget(self.summary_label)
        top.addWidget(summary_box, 1)

        target_box = QGroupBox("Install into")
        target_layout = QVBoxLayout(target_box)
        xp_row = QHBoxLayout()
        self.xplane_label = QLabel()
        self.xplane_label.setWordWrap(True)
        xp_row.addWidget(self.xplane_label, 1)
        change = QPushButton("Change…")
        change.clicked.connect(self._choose_xplane)
        xp_row.addWidget(change)
        target_layout.addLayout(xp_row)
        self.target_group = QButtonGroup(self)
        self.target_ao = QRadioButton()
        self.target_cs = QRadioButton("X-Plane 'Custom Scenery' folder directly")
        self.target_group.addButton(self.target_ao)
        self.target_group.addButton(self.target_cs)
        self.target_ao.toggled.connect(lambda *_: self._refresh_summary())
        target_layout.addWidget(self.target_ao)
        target_layout.addWidget(self.target_cs)
        self.target_hint = _hint("")
        self.target_hint.setTextFormat(Qt.RichText)
        self.target_hint.setMinimumHeight(56)
        target_layout.addWidget(self.target_hint)
        layout.addLayout(top)
        layout.addWidget(target_box)

        controls = QHBoxLayout()
        self.create_btn = QPushButton("Create && Install")
        self.create_btn.setObjectName("primary")
        self.create_btn.setMinimumHeight(40)
        self.create_btn.clicked.connect(self._start_build)
        self.retry_btn = QPushButton("Retry failed tiles")
        self.retry_btn.setObjectName("primary")
        self.retry_btn.setMinimumHeight(40)
        self.retry_btn.setVisible(False)
        self.retry_btn.clicked.connect(self._retry_failed)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel_build)
        controls.addWidget(self.create_btn)
        controls.addWidget(self.retry_btn)
        controls.addWidget(self.cancel_btn)
        self.stage_label = QLabel("Ready.")
        self.stage_label.setObjectName("stage")
        controls.addWidget(self.stage_label, 1)
        layout.addLayout(controls)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        lower = QSplitter(Qt.Horizontal)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont("Consolas", 9))
        lower.addWidget(self.log_view)
        self.build_map = TileMap()
        self.build_map.set_editable(False)
        lower.addWidget(self.build_map)
        lower.setSizes([620, 480])
        layout.addWidget(lower, 1)
        return page

    def _detect_xplane(self) -> Optional[Path]:
        if self.settings.xplane_path and is_xplane12(Path(self.settings.xplane_path)):
            return Path(self.settings.xplane_path)
        installs = find_xplane12_installs()
        if installs:
            return installs[0]
        ao = read_autoortho_config()
        if ao and ao.xplane_path and is_xplane12(ao.xplane_path):
            return ao.xplane_path
        return None

    def _choose_xplane(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select your X-Plane 12 folder",
                                                  str(self.xplane or Path.home()))
        if not folder:
            return
        path = Path(folder)
        if not is_xplane12(path):
            QMessageBox.warning(self, APP_NAME, "This folder does not look like an X-Plane 12 "
                                "installation (missing 'Custom Scenery' or "
                                "'Global Scenery/X-Plane 12 Global Scenery').")
            return
        self.xplane = path
        self._refresh_inventory()
        self._refresh_summary()

    def _target(self) -> Optional[InstallTarget]:
        if self.xplane is None:
            return None
        if self._method_key() == "improve":
            return InstallTarget.for_custom_scenery(self.xplane)
        if self.target_ao.isChecked():
            ao = read_autoortho_config()
            if ao and ao.ao_scenery_dir:
                return InstallTarget.for_autoortho(ao, self.xplane)
            return None
        return InstallTarget.for_custom_scenery(self.xplane)

    def _refresh_summary(self) -> None:
        tiles = self.map.selected()
        res = next(r for r in RESOLUTIONS if r.key == self._res_key())
        detail = mesh_detail(self._mesh_key())
        name = self.name_edit.text().strip() or "My region"
        slug = regions.slugify(name)
        method = self._method_key()
        minutes = self._minutes(len(tiles))
        download_mb = _DOWNLOAD_MB.get(res.key, 25) * len(tiles)
        per_tile = _IMPROVE_MB if method == "improve" else _PACK_MB[(res.key, detail.key)]
        disk_mb = per_tile * len(tiles)
        mesh_text = ("Mesh: your current terrain, LiDAR heights" if method == "improve"
                     else f"Mesh: new Ortho4XP mesh ({detail.label})")
        self.summary_label.setText(
            f"<b>{name}</b> &nbsp;→ scenery pack <code>{pack_name(slug, method)}</code><br>"
            f"{self._quality_summary(tiles)}<br>"
            f"{len(tiles)} tile(s): {regions.bounds(tiles)} &nbsp;·&nbsp; "
            f"DEM: {res.label} &nbsp;·&nbsp; {mesh_text}<br>"
            f"Download ≈ {download_mb} MB &nbsp;·&nbsp; Disk ≈ {disk_mb} MB &nbsp;·&nbsp; "
            f"Build time ≈ {max(1, round(minutes))} min")
        self.build_map.set_selected(tiles)

        self.xplane_label.setText(f"X-Plane 12: <b>{self.xplane}</b>" if self.xplane
                                  else "<b style='color:#ff8a65'>X-Plane 12 not found</b> - click Change…")
        ao = read_autoortho_config()
        improve = method == "improve"
        self.target_ao.setVisible(not improve)
        self.target_cs.setVisible(not improve)
        if improve:
            self.target_hint.setText(self._sources_preview(tiles))
            target_ok = self._target() is not None
            busy = self.thread is not None and self.thread.isRunning()
            self.create_btn.setEnabled(bool(tiles) and target_ok and not busy)
            return
        if ao and ao.ao_scenery_dir:
            self.target_ao.setText(f"AutoOrtho (recommended) - {ao.ao_scenery_dir}")
            self.target_ao.setEnabled(True)
        else:
            self.target_ao.setText("AutoOrtho (not found - start AutoOrtho once and set its paths)")
            self.target_ao.setEnabled(False)
            self.target_cs.setChecked(True)
        if self.target_ao.isChecked():
            self.target_hint.setText(
                "The pack is placed next to AutoOrtho's own packs and appears in Custom Scenery "
                "as soon as AutoOrtho is (re)started. It is listed above z_ao_* in "
                "scenery_packs.ini so its terrain replaces the stock AutoOrtho mesh.")
        else:
            self.target_hint.setText(
                "The pack is copied into Custom Scenery. The mesh references AutoOrtho-style "
                "photo textures, so without AutoOrtho these tiles will show missing textures.")
        target_ok = self._target() is not None
        busy = self.thread is not None and self.thread.isRunning()
        self.create_btn.setEnabled(bool(tiles) and target_ok and not busy)

    @staticmethod
    def _quality_summary(tiles) -> str:
        from ..dem_sources import FABDEM, RATING, coverage

        colours = {"best": "#3ad070", "good": "#5a9bff", "basic": "#ff6b6b"}
        counts: dict[tuple[str, str], int] = {}
        fabdem = False
        for tile in tiles:
            info = coverage().get(tile)
            if info:
                key = (RATING.get(info.quality, "basic"), info.text())
                counts[key] = counts.get(key, 0) + 1
                fabdem |= info.primary == "fabdem" or info.quality == FABDEM
        order = {"best": 0, "good": 1, "basic": 2}
        text = "Elevation data: " + ", ".join(
            f"<span style='color:{colours[r]}'>{n} × {name}</span>"
            for (r, name), n in sorted(counts.items(), key=lambda kv: (order[kv[0][0]], -kv[1])))
        if fabdem:
            text += (" &nbsp;<span style='color:#9fb0c0'>(FABDEM: non-commercial licence - "
                     "fine for private use)</span>")
        return text

    def _parallel(self) -> int:
        """Tiles to build at once - the automatic choice depends on CPU, RAM and method."""
        from ..system import recommended_parallel

        chosen = self.parallel_combo.currentData() if hasattr(self, "parallel_combo") else 0
        return chosen or recommended_parallel(self._method_key())

    def _parallel_changed(self) -> None:
        from ..system import total_ram_gb

        ram = total_ram_gb()
        machine = f"{os.cpu_count() or '?'} CPU threads" + (f", {ram:.0f} GB RAM" if ram else "")
        if not self.parallel_combo.currentData():
            self.parallel_hint.setText(f"{self._parallel()} at a time for this PC ({machine}). "
                                       "Each tile needs about 2-3 GB of RAM.")
        else:
            self.parallel_hint.setText(f"This PC: {machine}. Each tile needs about 2-3 GB of RAM.")
        self._update_status()

    def _minutes(self, count: int) -> float:
        from ..eta import TimingHistory, build_key

        measured = TimingHistory(paths.data_root() / "timings.json").typical(
            build_key(self._method_key(), self._res_key(), self._mesh_key()))
        if measured:  # real build times on this PC
            return measured / 60 * count / max(1, self._parallel())
        if self._method_key() == "improve":
            per_tile = _IMPROVE_MINUTES[self._res_key()]
        else:
            per_tile = _MINUTES[(self._res_key(), self._mesh_key())]
        return per_tile * count / max(1, self._parallel())

    def _sources_preview(self, tiles) -> str:
        """Which terrain each tile currently uses (shown before building)."""
        if self.xplane is None:
            return ""
        from ..reheight import find_mesh_source

        ao = read_autoortho_config()
        roots = [ao.ao_scenery_dir] if ao and ao.ao_scenery_dir else []
        name = self.name_edit.text().strip() or "My region"
        own = pack_name(regions.slugify(name), "improve")
        counts: dict[str, int] = {}
        problems: set[str] = set()
        for tile in tiles[:120]:
            try:
                src = find_mesh_source(tile, self.xplane, skip=[own], extra_roots=roots, strict=False)
            except Exception:  # unreadable DSF etc. - reported again during the build
                continue
            counts[src.label] = counts.get(src.label, 0) + 1
            problems.update(src.unreadable_above)
        parts = ", ".join(f"<b>{label}</b> ({n})" for label, n in sorted(counts.items(), key=lambda x: -x[1]))
        text = ("Installed to <code>Custom Scenery</code>, directly above the scenery it improves. "
                f"Your current terrain for these tiles: {parts or '-'}. "
                "Textures, water and roads stay those of that scenery.")
        if problems:
            text += ("<br><b style='color:#ff8a65'>Not readable: "
                     f"{', '.join(sorted(problems))}</b> - if this is XPME or AutoOrtho, start it "
                     "first so its terrain can be improved.")
        return text

    # -- build control --------------------------------------------------
    def _failed_to_retry(self) -> list:
        if not self.result or not self.result.failed_tiles:
            return []
        failed = [t for t in self.result.failed_tiles if t != (0, 0)]
        if (0, 0) in self.result.failed_tiles and self._last_tiles:
            return list(self._last_tiles)  # the whole run crashed
        return sorted(failed)

    def _retry_failed(self) -> None:
        tiles = self._failed_to_retry()
        if tiles:
            self._go(2)
            self._start_build(tiles, retry=True)

    def _start_build(self, tiles=None, retry: bool = False) -> None:
        target = self._target()
        if target is None or self.xplane is None:
            return
        tiles = list(tiles) if tiles else self.map.selected()
        self._last_tiles = list(tiles)
        name = self.name_edit.text().strip() or "My region"
        self._save_choices()
        request = BuildRequest(
            tiles=tiles, region_name=name, slug=regions.slugify(name),
            resolution_key=self._res_key(), mesh_key=self._mesh_key(), xplane=self.xplane,
            target=target,
            local_dem_source=Path(self.dem_folder_edit.text()) if self.dem_folder_edit.text().strip() else None,
            parallel=self._parallel(), method=self._method_key(),
            extra_roots=self._extra_roots())
        if not retry:
            self._refresh_inventory()
            tiles = self._confirm_existing(request.pack, tiles)
            if not tiles:
                return
            request.tiles = tiles
            self._last_tiles = list(tiles)
        events = _SignalEvents()
        events.logged.connect(self._append_log)
        events.staged.connect(self._on_stage)
        events.progressed.connect(self._on_progress)
        events.tileChanged.connect(lambda lat, lon, s: self.build_map.set_tile_state((lat, lon), s))
        self._events = events  # keep a reference alive
        self.pipeline = Pipeline(request, events)
        self.thread = BuildThread(self.pipeline)
        self.thread.finishedWithResult.connect(self._on_finished)
        self.log_view.clear()
        self.build_map.clear_states()
        self._reset_progress()
        self.create_btn.setEnabled(False)
        self.retry_btn.setVisible(False)
        self.cancel_btn.setEnabled(True)
        self.back_btn.setEnabled(False)
        if retry:
            self._append_log(f"Retrying {len(tiles)} tile(s): "
                             + ", ".join(regions.tile_name(*t) for t in tiles))
        self._append_log(f"Building {len(tiles)} tile(s) into {request.pack}")
        self._append_log(ATTRIBUTION)
        self.thread.start()

    def _confirm_existing(self, pack: str, tiles: list) -> list:
        """Explain what happens to tiles that are already in the pack. Returns
        the tiles to build (empty = cancelled)."""
        existing = self.inventory.pack(pack)
        if existing is None:
            return tiles
        again = [t for t in tiles if t in set(existing.tiles)]
        new = [t for t in tiles if t not in set(existing.tiles)]
        if not again:
            return tiles  # only adds tiles - nothing to ask

        def names(items: list) -> str:
            text = ", ".join(regions.tile_name(*t) for t in items[:8])
            return text + (f" … (+{len(items) - 8})" if len(items) > 8 else "")

        box = QMessageBox(self)
        box.setWindowTitle(APP_NAME)
        box.setIcon(QMessageBox.Question)
        box.setTextFormat(Qt.RichText)
        built = f", built {existing.built}" if existing.built else ""
        text = (f"Your pack <b>{pack}</b> already exists ({len(existing.tiles)} tile(s){built}).<br><br>"
                f"Of the {len(tiles)} selected tile(s):<br>"
                f"• <b>{len(again)}</b> are already in it: {names(again)}<br>")
        text += (f"• <b>{len(new)}</b> are new: {names(new)}<br>" if new else "")
        text += ("<br>Tiles in the pack that you did not select stay as they are.")
        box.setText(text)
        only_new = box.addButton(f"Build only the {len(new)} new tile(s)", QMessageBox.AcceptRole) if new else None
        rebuild = box.addButton(f"Build all {len(tiles)} (replace the {len(again)} existing)",
                                QMessageBox.DestructiveRole)
        box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(only_new or rebuild)
        box.exec()
        if only_new is not None and box.clickedButton() is only_new:
            return new
        if box.clickedButton() is rebuild:
            return tiles
        return []

    @staticmethod
    def _extra_roots() -> list[Path]:
        ao = read_autoortho_config()
        return [ao.ao_scenery_dir] if ao and ao.ao_scenery_dir else []

    def _cancel_build(self) -> None:
        if self.pipeline:
            self.stage_label.setText("Cancelling…")
            self.pipeline.cancel()

    @Slot(str)
    def _append_log(self, text: str) -> None:
        self.log_view.appendPlainText(text)

    @Slot(str)
    def _on_stage(self, text: str) -> None:
        self.stage_label.setText(text)
        self._append_log(f"── {text}")

    @Slot(float, float)
    def _on_progress(self, elapsed: float, total: float) -> None:
        """``elapsed`` seconds so far, ``total`` = elapsed + estimated remaining seconds."""
        if total <= 0:
            return
        value = max(self.progress.value(), int(1000 * min(elapsed / total, 1.0)))  # never go back
        self.progress.setValue(value)
        remaining = max(0.0, total - elapsed)
        now = time.monotonic()
        if self._eta is None:
            self._eta = remaining
        else:
            # count down by the time that passed, then move gently towards the new estimate
            expected = max(0.0, self._eta - (now - self._eta_at))
            self._eta = expected + 0.25 * (remaining - expected)
        self._eta_at = now
        if elapsed > 3:
            self.progress.setFormat(f"%p%  ·  ≈ {_duration(self._eta)} left")

    def _reset_progress(self) -> None:
        self._build_t0 = time.monotonic()
        self._eta = None
        self._eta_at = time.monotonic()
        self.progress.setValue(0)
        self.progress.setFormat("%p%")

    @Slot(object)
    def _on_finished(self, result: BuildResult) -> None:
        self.result = result
        self.progress.setFormat(f"%p%  ·  {_duration(result.seconds)} total")
        self.cancel_btn.setEnabled(False)
        self.back_btn.setEnabled(True)
        self.thread = None
        self._refresh_inventory()
        if result.cancelled:
            self.stage_label.setText("Cancelled.")
            self._refresh_summary()
            return
        if not result.ok_tiles:
            self.stage_label.setText("Build failed - fix the cause (see log) and click Retry.")
            for tile, message in result.failed_tiles.items():
                self._append_log(f"FAILED {regions.tile_name(*tile)}: {message}")
            self._refresh_summary()
            self.retry_btn.setVisible(bool(self._failed_to_retry()))
            return
        self.progress.setValue(1000)
        self.stage_label.setText("Finished.")
        self._show_done(result)

    # ------------------------------------------------------------------
    # Page 4: done
    # ------------------------------------------------------------------
    def _build_done_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(_title("4  Installed"))
        self.done_label = QLabel()
        self.done_label.setTextFormat(Qt.RichText)
        self.done_label.setWordWrap(True)
        self.done_label.setTextInteractionFlags(Qt.TextBrowserInteraction)
        layout.addWidget(self.done_label)
        buttons = QHBoxLayout()
        open_btn = QPushButton("Open pack folder")
        open_btn.clicked.connect(self._open_pack)
        self.done_retry_btn = QPushButton("Retry failed tiles")
        self.done_retry_btn.clicked.connect(self._retry_failed)
        again = QPushButton("Build another region")
        again.setObjectName("primary")
        again.clicked.connect(lambda: self._go(0))
        buttons.addWidget(open_btn)
        buttons.addWidget(self.done_retry_btn)
        buttons.addStretch(1)
        buttons.addWidget(again)
        layout.addLayout(buttons)
        layout.addStretch(1)
        return page

    def _show_done(self, result: BuildResult) -> None:
        self.done_retry_btn.setVisible(bool(self._failed_to_retry()))
        improve = self._method_key() == "improve"
        ao_mode = self.target_ao.isChecked() and not improve
        failed = ""
        if result.failed_tiles:
            from ..pipeline import short_error

            failed = ("<p style='color:#ff8a65'><b>Some tiles failed</b> - they simply keep their "
                      "previous terrain, nothing else is affected:<br>" +
                      "<br>".join(f"{regions.tile_name(*t)}: {short_error(m)}"
                                  for t, m in result.failed_tiles.items())
                      + "<br><span style='color:#9fb0c0'>Full details are in the log on the Create "
                      "page. Try <b>Retry failed tiles</b>, or 3\" resolution for these tiles.</span></p>")
        if improve:
            steps = ("<li>Keep the original scenery installed - XPME / AutoOrtho still deliver "
                     "the textures. The pack is listed in front of XPME's block in "
                     "scenery_packs.ini; if it ever shows up as <i>Needs a fix</i> on step 1, "
                     "click <b>Fix now</b>.</li>"
                     "<li>Rebuild this pack after XPME / AutoOrtho update their base packages "
                     "or after flattening an airport in XPME.</li>")
        else:
            steps = ("<li>Restart <b>AutoOrtho</b> so it mounts the new pack.</li>"
                     if ao_mode else "")
        self.done_label.setText(
            f"<p style='font-size:15px'>✅ <b>{len(result.ok_tiles)} tile(s)</b> built in "
            f"{result.seconds / 60:.1f} min and installed to:<br><code>{result.installed_to}</code></p>"
            f"{failed}"
            f"<p><b>Next steps</b></p><ol>{steps}"
            "<li>Start X-Plane 12 and fly - the new terrain is used automatically.</li>"
            "<li>scenery_packs.ini was updated; a backup is stored as "
            "<code>scenery_packs.ini.xp12dem.bak</code>.</li></ol>"
            f"<p class='hint'>{ATTRIBUTION}. "
            + ("Heights applied to your existing scenery with DSFTool."
               if improve else
               "Mesh built with Ortho4XP (GPL v3). Vector data © OpenStreetMap contributors.")
            + "</p>")
        self._go(3)

    def _open_pack(self) -> None:
        if self.result and self.result.installed_to:
            path = str(self.result.installed_to)
            if sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    # ------------------------------------------------------------------
    # Persistence & status
    # ------------------------------------------------------------------
    def _restore_choices(self) -> None:
        s = self.settings
        for card in self.res_cards:
            card.radio.setChecked(card.key == s.resolution)
        for card in self.mesh_cards:
            card.radio.setChecked(card.key == s.mesh_detail)
        index = self.parallel_combo.findData(max(0, min(8, s.parallel)))
        self.parallel_combo.setCurrentIndex(max(0, index))
        self.dem_folder_edit.setText(s.local_dem_folder)
        (self.target_cs if s.install_target == "custom_scenery" else self.target_ao).setChecked(True)
        for card in self.method_cards:
            card.radio.setChecked(card.key == s.method)
        self._method_changed()
        keys = [self.region_list.item(i).data(Qt.UserRole) for i in range(self.region_list.count())]
        row = keys.index(s.last_region) if s.last_region in keys else keys.index("tyrol")
        self.region_list.setCurrentRow(row)

    def _save_choices(self) -> None:
        s = self.settings
        s.xplane_path = str(self.xplane or "")
        s.resolution = self._res_key()
        s.mesh_detail = self._mesh_key()
        s.parallel = int(self.parallel_combo.currentData() or 0)
        s.local_dem_folder = self.dem_folder_edit.text().strip()
        s.install_target = "autoortho" if self.target_ao.isChecked() else "custom_scenery"
        s.method = self._method_key()
        current = self.region_list.currentItem()
        s.last_region = current.data(Qt.UserRole) if current else s.last_region
        if s.last_region == CUSTOM_KEY:
            s.custom_tiles = [list(t) for t in self.map.selected()]
        try:
            s.save()
        except OSError:
            pass

    def _update_status(self, *_args) -> None:
        tiles = self.map.selected() if hasattr(self, "map") else []
        if hasattr(self, "fine_label"):
            from ..dem_sources import coverage

            fine = sum(1 for t in tiles if (coverage().get(t) and coverage()[t].fine))
            own = (" (plus tiles covered by your own 1-10 m files)"
                   if hasattr(self, "dem_folder_edit") and self.dem_folder_edit.text().strip() else "")
            self.fine_label.setText(
                f"10 m data available for <b>{fine} of {len(tiles)}</b> selected tile(s){own}."
                if tiles else "")
        if not hasattr(self, "mesh_cards") or not hasattr(self, "parallel_combo"):
            return
        minutes = self._minutes(len(tiles))
        self.status_label.setText(
            f"{len(tiles)} tile(s) selected · {regions.bounds(tiles)} · "
            f"≈ {max(1, round(minutes)) if tiles else 0} min build time")

    def closeEvent(self, event):  # noqa: N802 - Qt API
        if self.thread is not None and self.thread.isRunning():
            answer = QMessageBox.question(self, APP_NAME, "A build is running. Cancel it and quit?")
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            if self.pipeline:
                self.pipeline.cancel()
            self.thread.wait(10000)
        self._save_choices()
        event.accept()
