"""The four-step wizard: Region -> Resolution -> Create -> Done."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Qt, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QButtonGroup, QFileDialog, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QRadioButton, QSpinBox, QSplitter, QStackedWidget,
    QVBoxLayout, QWidget,
)

from .. import APP_NAME, __version__, regions
from ..dem import ATTRIBUTION, RESOLUTIONS
from ..installer import InstallTarget, pack_name
from ..pipeline import MESH_DETAILS, BuildRequest, BuildResult, Events, Pipeline, mesh_detail
from ..settings import Settings
from ..xplane import find_xplane12_installs, is_xplane12, read_autoortho_config
from .map_widget import TileMap

STEPS = ("Region", "Resolution", "Create", "Done")
CUSTOM_KEY = "__custom__"

# Rough per-tile build time in minutes on a mid-range CPU, used for estimates.
_MINUTES = {("1", "balanced"): 3, ("1", "high"): 4, ("1", "maximum"): 6,
            ("3", "balanced"): 2, ("3", "high"): 3, ("3", "maximum"): 3}
_PACK_MB = {("1", "balanced"): 35, ("1", "high"): 45, ("1", "maximum"): 60,
            ("3", "balanced"): 30, ("3", "high"): 35, ("3", "maximum"): 40}


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
        self.thread: Optional[BuildThread] = None
        self.result: Optional[BuildResult] = None
        self.xplane: Optional[Path] = self._detect_xplane()

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
        subtitle = QLabel("High-resolution terrain for X-Plane 12 from Sonny's LiDAR DTMs")
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
        layout.addWidget(_hint("Pick a preset or paint your own selection on the map: left-drag "
                               "selects 1°×1° tiles, right-drag removes them, mouse wheel zooms, "
                               "middle-drag pans. Highlighted squares are covered by Sonny's DTM."))
        splitter = QSplitter(Qt.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 8, 0)
        self.region_list = QListWidget()
        self.region_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.region_list.setWordWrap(True)
        for region in regions.PRESETS:
            item = QListWidgetItem(f"{region.name}  ({len(region.tiles())} tiles)")
            item.setData(Qt.UserRole, region.key)
            item.setToolTip(region.description)
            self.region_list.addItem(item)
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
        all_btn = QPushButton("Show Europe")
        all_btn.clicked.connect(lambda: self.map.show_all())
        buttons.addWidget(clear_btn)
        buttons.addWidget(all_btn)
        left_layout.addLayout(buttons)
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
        splitter.addWidget(right)
        splitter.setSizes([300, 860])
        layout.addWidget(splitter, 1)
        self._suppress_custom = False
        return page

    def _preset_chosen(self, item: Optional[QListWidgetItem], _prev=None) -> None:
        if item is None:
            return
        key = item.data(Qt.UserRole)
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

    def _name_is_preset(self) -> bool:
        return self.name_edit.text() in {r.name for r in regions.PRESETS}

    def _map_changed(self) -> None:
        if not self._suppress_custom:
            current = self.region_list.currentItem()
            if current is not None and current.data(Qt.UserRole) != CUSTOM_KEY:
                region = regions.preset(current.data(Qt.UserRole))
                if sorted(region.tiles()) != self.map.selected():
                    self.region_list.blockSignals(True)
                    self.region_list.setCurrentRow(self.region_list.count() - 1)
                    self.region_list.blockSignals(False)
                    self.region_desc.setText("Your own tile selection.")
        self._update_status()

    # ------------------------------------------------------------------
    # Page 2: resolution
    # ------------------------------------------------------------------
    def _build_quality_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(_title("2  Choose the DEM resolution"))
        layout.addWidget(_hint("Sonny's models are LiDAR based and far more accurate than the "
                               "SRTM/Viewfinder data used by X-Plane's default mesh and the stock "
                               "AutoOrtho packs."))
        self.res_group = QButtonGroup(self)
        self.res_cards: list[OptionCard] = []
        row = QHBoxLayout()
        for res in RESOLUTIONS:
            card = OptionCard(res.label, res.description, self.res_group, res.key)
            self.res_cards.append(card)
            row.addWidget(card)
        layout.addLayout(row)

        layout.addSpacing(10)
        layout.addWidget(QLabel("Mesh detail"))
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
        grid.addWidget(QLabel("Tiles built in parallel"), 0, 0)
        self.parallel_spin = QSpinBox()
        self.parallel_spin.setRange(1, max(1, min(8, (os.cpu_count() or 2) // 2)))
        self.parallel_spin.valueChanged.connect(lambda *_: self._update_status())
        grid.addWidget(self.parallel_spin, 0, 1)
        grid.addWidget(_hint("Each parallel tile needs about 3-4 GB of RAM."), 0, 2)
        grid.addWidget(QLabel("Own Sonny DTM folder"), 1, 0)
        folder_row = QHBoxLayout()
        self.dem_folder_edit = QLineEdit()
        self.dem_folder_edit.setPlaceholderText("empty = download automatically")
        folder_row.addWidget(self.dem_folder_edit, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._choose_dem_folder)
        folder_row.addWidget(browse)
        grid.addLayout(folder_row, 1, 1, 1, 2)
        grid.addWidget(_hint("Use this if you downloaded newer country releases (1\" .hgt or .zip) "
                             "from sonny.4lima.de yourself; missing tiles are still downloaded."), 2, 0, 1, 3)
        grid.setColumnStretch(2, 1)
        layout.addWidget(advanced)
        layout.addStretch(1)
        return page

    def _choose_dem_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Folder with Sonny .hgt / .zip files")
        if folder:
            self.dem_folder_edit.setText(folder)

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
        target_layout.addWidget(self.target_hint)
        layout.addLayout(top)
        layout.addWidget(target_box)

        controls = QHBoxLayout()
        self.create_btn = QPushButton("Create && Install")
        self.create_btn.setObjectName("primary")
        self.create_btn.setMinimumHeight(40)
        self.create_btn.clicked.connect(self._start_build)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel_build)
        controls.addWidget(self.create_btn)
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
        self._refresh_summary()

    def _target(self) -> Optional[InstallTarget]:
        if self.xplane is None:
            return None
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
        minutes = _MINUTES[(res.key, detail.key)] * len(tiles) / max(1, self.parallel_spin.value())
        download_mb = 14 * len(tiles)
        disk_mb = _PACK_MB[(res.key, detail.key)] * len(tiles)
        self.summary_label.setText(
            f"<b>{name}</b> &nbsp;→ scenery pack <code>{pack_name(slug)}</code><br>"
            f"{len(tiles)} tile(s): {regions.bounds(tiles)} &nbsp;·&nbsp; "
            f"DEM: {res.label} &nbsp;·&nbsp; Mesh: {detail.label}<br>"
            f"Download ≈ {download_mb} MB &nbsp;·&nbsp; Disk ≈ {disk_mb} MB &nbsp;·&nbsp; "
            f"Build time ≈ {max(1, round(minutes))} min")
        self.build_map.set_selected(tiles)

        self.xplane_label.setText(f"X-Plane 12: <b>{self.xplane}</b>" if self.xplane
                                  else "<b style='color:#ff8a65'>X-Plane 12 not found</b> - click Change…")
        ao = read_autoortho_config()
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

    # -- build control --------------------------------------------------
    def _start_build(self) -> None:
        target = self._target()
        if target is None or self.xplane is None:
            return
        tiles = self.map.selected()
        name = self.name_edit.text().strip() or "My region"
        self._save_choices()
        request = BuildRequest(
            tiles=tiles, region_name=name, slug=regions.slugify(name),
            resolution_key=self._res_key(), mesh_key=self._mesh_key(), xplane=self.xplane,
            target=target,
            local_dem_source=Path(self.dem_folder_edit.text()) if self.dem_folder_edit.text().strip() else None,
            parallel=self.parallel_spin.value())
        existing = target.pack_dir(request.pack)
        if existing.exists():
            answer = QMessageBox.question(
                self, APP_NAME, f"'{request.pack}' already exists. Rebuilt tiles will replace "
                "the old ones and new tiles are added. Continue?")
            if answer != QMessageBox.Yes:
                return
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
        self.progress.setValue(0)
        self.create_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.back_btn.setEnabled(False)
        self._append_log(f"Building {len(tiles)} tile(s) into {request.pack}")
        self._append_log(ATTRIBUTION)
        self.thread.start()

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
    def _on_progress(self, done: float, total: float) -> None:
        if total:
            self.progress.setValue(int(1000 * done / total))

    @Slot(object)
    def _on_finished(self, result: BuildResult) -> None:
        self.result = result
        self.cancel_btn.setEnabled(False)
        self.back_btn.setEnabled(True)
        self.thread = None
        if result.cancelled:
            self.stage_label.setText("Cancelled.")
            self._refresh_summary()
            return
        if not result.ok_tiles:
            self.stage_label.setText("Build failed - see log.")
            for tile, message in result.failed_tiles.items():
                self._append_log(f"FAILED {regions.tile_name(*tile)}: {message}")
            self._refresh_summary()
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
        again = QPushButton("Build another region")
        again.setObjectName("primary")
        again.clicked.connect(lambda: self._go(0))
        buttons.addWidget(open_btn)
        buttons.addStretch(1)
        buttons.addWidget(again)
        layout.addLayout(buttons)
        layout.addStretch(1)
        return page

    def _show_done(self, result: BuildResult) -> None:
        ao_mode = self.target_ao.isChecked()
        failed = ""
        if result.failed_tiles:
            failed = ("<p style='color:#ff8a65'><b>Some tiles failed:</b><br>" +
                      "<br>".join(f"{regions.tile_name(*t)}: {m}" for t, m in result.failed_tiles.items())
                      + "</p>")
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
            f"<p class='hint'>{ATTRIBUTION}. Mesh built with Ortho4XP (GPL v3). "
            "Vector data © OpenStreetMap contributors.</p>")
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
        self.parallel_spin.setValue(max(1, s.parallel))
        self.dem_folder_edit.setText(s.local_dem_folder)
        (self.target_cs if s.install_target == "custom_scenery" else self.target_ao).setChecked(True)
        keys = [self.region_list.item(i).data(Qt.UserRole) for i in range(self.region_list.count())]
        row = keys.index(s.last_region) if s.last_region in keys else 0
        self.region_list.setCurrentRow(row)

    def _save_choices(self) -> None:
        s = self.settings
        s.xplane_path = str(self.xplane or "")
        s.resolution = self._res_key()
        s.mesh_detail = self._mesh_key()
        s.parallel = self.parallel_spin.value()
        s.local_dem_folder = self.dem_folder_edit.text().strip()
        s.install_target = "autoortho" if self.target_ao.isChecked() else "custom_scenery"
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
        if not hasattr(self, "res_cards"):
            return
        res = self._res_key()
        detail = self._mesh_key()
        minutes = _MINUTES[(res, detail)] * len(tiles) / max(1, self.parallel_spin.value())
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
