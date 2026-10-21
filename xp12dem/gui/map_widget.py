"""Interactive Europe map for picking 1x1 degree tiles.

The map uses the Web Mercator projection so that online map tiles line up:

* an online base map (OpenTopoMap - OpenStreetMap data with borders, places
  and contour lines; standard OpenStreetMap tiles as fallback; both free and
  without API key) is loaded on demand and cached on disk;
* a built-in offline layer (Natural Earth land, country borders and names
  plus a few cities) is always drawn underneath, so the map stays usable
  without internet access.
"""

from __future__ import annotations

import json
import math
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterable, Optional

import requests
from PySide6.QtCore import QObject, QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QGraphicsItem, QGraphicsPixmapItem, QGraphicsScene, QGraphicsSimpleTextItem, QGraphicsView,
    QLabel, QToolButton,
)

from .. import __version__
from ..paths import data_root, resources_dir
from ..dem_sources import FABDEM, LIDAR, NATIONAL, QUALITY_TEXT, USGS, coverage
from ..inventory import Inventory
from ..regions import Tile, available_tiles, tile_name

# Scene coordinates are Web Mercator pixels at zoom BASE_ZOOM.
BASE_ZOOM = 8
WORLD = 256 * 2 ** BASE_ZOOM
MIN_TILE_ZOOM, MAX_TILE_ZOOM = 2, 12

# Free tile services that need no API key. Both ask for an identifying
# User-Agent, attribution and local caching, which we do.
TILE_SOURCES = (
    ("opentopomap", "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png", "abc", 17),
    ("osm", "https://tile.openstreetmap.org/{z}/{x}/{y}.png", "", 19),
)
ATTRIBUTION = "© OpenStreetMap contributors, SRTM | style © OpenTopoMap (CC-BY-SA)"
CACHE_DAYS = 30
USER_AGENT = f"XP12-DEM-Generator/{__version__} (+https://github.com/Jumanja991xx/XP12-DEM-Generator)"

COLOR_SEA = QColor("#1b2a38")
COLOR_LAND = QColor("#2b3d33")
COLOR_COAST = QColor("#5d7f69")
COLOR_BORDER = QColor(230, 235, 240, 150)
COLOR_COUNTRY = QColor(200, 210, 220, 150)
COLOR_AVAILABLE = QColor(40, 130, 255, 22)
# Tile colours by best available elevation source: green = excellent,
# blue = good, grey = satellite data only.
COLOR_QUALITY = {
    LIDAR: QColor(30, 200, 80, 115),
    USGS: QColor(30, 200, 80, 115),
    NATIONAL: QColor(40, 120, 255, 105),
    FABDEM: QColor(120, 120, 125, 120),
}
RATING = {LIDAR: "BEST", USGS: "BEST", NATIONAL: "GOOD", FABDEM: "BASIC"}
COLOR_INSTALLED = QColor(255, 140, 0)
COLOR_CACHED = QColor(235, 245, 255, 230)
LEGEND = (
    (QColor(30, 200, 80), "■", "<b>Best</b> - LiDAR 1-5 m (Sonny / USGS 3DEP)"),
    (QColor(40, 120, 255), "■", "<b>Good</b> - national model 10-50 m"),
    (QColor(150, 150, 155), "■", "<b>Basic</b> - satellite 30 m (FABDEM)"),
    (COLOR_INSTALLED, "■", "<b>Already installed</b>"),
    (COLOR_CACHED, "⬚", "Elevation data downloaded"),
)
# pixels the mouse may move before a left click becomes a drag (pan)
CLICK_SLOP = 5
VIEWS = {
    "Europe": (-12, 66, 32, 36),
    "USA": (-126, 50, -66, 24),
    "Alaska & Hawaii": (-170, 68, -140, 18),
    "World": (-170, 75, 180, -50),
}
COLOR_GRID = QColor(70, 110, 160, 130)
COLOR_SELECTED = QColor(225, 40, 200, 130)
COLOR_SELECTED_EDGE = QColor(255, 255, 255)
COLOR_SELECTED_FRAME = QColor(235, 40, 210)
COLOR_CITY = QColor(235, 240, 245, 210)
COLOR_STATE = {
    "downloading": QColor(80, 160, 255, 150),
    "dem_ready": QColor(80, 160, 255, 150),
    "building": QColor(170, 120, 255, 170),
    "built": QColor(60, 200, 110, 170),
    "failed": QColor(230, 70, 70, 190),
}

# A few reference points for the offline map (the online map has its own labels).
CITIES = (
    ("Innsbruck", 47.27, 11.39), ("Munich", 48.14, 11.58), ("Zurich", 47.38, 8.54),
    ("Geneva", 46.20, 6.14), ("Vienna", 48.21, 16.37), ("Milan", 45.46, 9.19),
    ("Salzburg", 47.81, 13.04), ("Bolzano", 46.50, 11.35), ("Grenoble", 45.19, 5.72),
    ("Nice", 43.70, 7.27), ("Ljubljana", 46.06, 14.51), ("Bern", 46.95, 7.45),
    ("Lyon", 45.76, 4.84), ("Turin", 45.07, 7.69), ("Venice", 45.44, 12.33),
    ("Frankfurt", 50.11, 8.68), ("Berlin", 52.52, 13.40), ("Paris", 48.86, 2.35),
    ("London", 51.51, -0.13), ("Edinburgh", 55.95, -3.19), ("Oslo", 59.91, 10.75),
    ("Bergen", 60.39, 5.32), ("Prague", 50.08, 14.44), ("Kraków", 50.06, 19.94),
    ("Barcelona", 41.39, 2.17), ("Toulouse", 43.60, 1.44), ("Madrid", 40.42, -3.70),
    ("Rome", 41.90, 12.50), ("Stockholm", 59.33, 18.07), ("Copenhagen", 55.68, 12.57),
    ("Budapest", 47.50, 19.04), ("Zagreb", 45.81, 15.98), ("Reykjavík", 64.15, -21.94),
)


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------
def to_scene(lon: float, lat: float) -> QPointF:
    lat = max(min(lat, 85.0), -85.0)
    x = (lon + 180.0) / 360.0 * WORLD
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * WORLD
    return QPointF(x, y)


def from_scene(point: QPointF) -> tuple[float, float]:
    lon = point.x() / WORLD * 360.0 - 180.0
    n = math.pi - 2 * math.pi * point.y() / WORLD
    lat = math.degrees(math.atan(math.sinh(n)))
    return lon, lat


def tile_rect(lat: int, lon: int) -> QRectF:
    return QRectF(to_scene(lon, lat + 1), to_scene(lon + 1, lat))


def scene_to_tile(point: QPointF) -> Tile:
    lon, lat = from_scene(point)
    return math.floor(lat), math.floor(lon)


# ---------------------------------------------------------------------------
# Online base map
# ---------------------------------------------------------------------------
class TileFetcher(QObject):
    """Downloads base-map tiles in the background and caches them on disk.

    Tries OpenTopoMap first and falls back to the standard OpenStreetMap tiles
    per tile; only real PNG/JPEG images are accepted and cached.
    """

    loaded = Signal(int, int, int, bytes)

    def __init__(self, cache_dir: Path):
        super().__init__()
        self.cache_dir = cache_dir
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="maptiles")
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.pending: set[tuple[int, int, int]] = set()
        self.lock = threading.Lock()
        self.failures = 0
        self.offline = False

    def path(self, z: int, x: int, y: int) -> Path:
        return self.cache_dir / str(z) / str(x) / f"{y}.png"

    def request(self, z: int, x: int, y: int) -> None:
        key = (z, x, y)
        with self.lock:
            if key in self.pending:
                return
            self.pending.add(key)
        self.pool.submit(self._fetch, z, x, y)

    @staticmethod
    def _is_image(data: bytes) -> bool:
        return data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff"

    def _download(self, z: int, x: int, y: int) -> Optional[bytes]:
        for _name, url, subdomains, max_zoom in TILE_SOURCES:
            if z > max_zoom:
                continue
            sub = subdomains[(x + y) % len(subdomains)] if subdomains else ""
            try:
                response = self.session.get(url.format(s=sub, z=z, x=x, y=y), timeout=10)
            except requests.RequestException:
                continue
            if response.status_code == 200 and self._is_image(response.content):
                return response.content
        return None

    def _fetch(self, z: int, x: int, y: int) -> None:
        data: Optional[bytes] = None
        cached = self.path(z, x, y)
        try:
            fresh = cached.is_file() and (time.time() - cached.stat().st_mtime) < CACHE_DAYS * 86400
            if fresh:
                data = cached.read_bytes()
            elif not self.offline:
                data = self._download(z, x, y)
                if data:
                    cached.parent.mkdir(parents=True, exist_ok=True)
                    cached.write_bytes(data)
                    self.failures = 0
                else:
                    self.failures += 1
                    if cached.is_file():
                        data = cached.read_bytes()  # stale but better than nothing
        except OSError:
            self.failures += 1
        if self.failures >= 8:
            self.offline = True  # stop hammering when there is no connection
        with self.lock:
            self.pending.discard((z, x, y))
        if data and self._is_image(data):
            self.loaded.emit(z, x, y, data)

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)


# ---------------------------------------------------------------------------
# Map view
# ---------------------------------------------------------------------------
class TileMap(QGraphicsView):
    """Works like a normal web map: drag to move, wheel (or +/-) to zoom.
    A click toggles a tile; Shift+drag paints a selection, right-drag erases."""

    selectionChanged = Signal()
    hovered = Signal(str)

    def __init__(self, parent=None, online: bool = True):
        super().__init__(parent)
        self.setRenderHint(QPainter.Antialiasing)
        self.setRenderHint(QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setTransformationAnchor(QGraphicsView.NoAnchor)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setMouseTracking(True)
        self.setMinimumSize(420, 320)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setBackgroundBrush(COLOR_SEA)
        self._available = available_tiles()
        self._selected: set[Tile] = set()
        self._states: dict[Tile, str] = {}
        self._tile_items: dict[Tile, QGraphicsItem] = {}
        self._base_items: dict[tuple[int, int, int], QGraphicsPixmapItem] = {}
        self._paint_mode: Optional[bool] = None
        self._editable = True
        self._pan_start = None
        self._press_pos: Optional[QPointF] = None
        self._press_tile: Optional[Tile] = None
        self._inventory = Inventory()
        self._inventory_items: list[QGraphicsItem] = []
        self._build_static()

        self._fetcher: Optional[TileFetcher] = None
        if online:
            # tiles from the old CARTO source may be "API key" error images
            shutil.rmtree(data_root() / "maptiles" / "carto_dark", ignore_errors=True)
            self._fetcher = TileFetcher(data_root() / "maptiles" / "opentopomap")
            self._fetcher.loaded.connect(self._tile_loaded)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(120)
        self._refresh_timer.timeout.connect(self._update_base_map)
        self._attribution = QLabel(ATTRIBUTION if online else "Map: Natural Earth", self)
        self._attribution.setStyleSheet(
            "background: rgba(14,20,25,200); color: #c8d4e0; font-size: 10px; padding: 2px 6px;")
        self._attribution.adjustSize()
        self._legend = QLabel(self)
        self._legend.setTextFormat(Qt.RichText)
        self._legend.setText("<br>".join(
            f"<span style='color:{c.name()}'>{mark}</span> {text}" for c, mark, text in LEGEND))
        self._legend.setStyleSheet(
            "background: rgba(14,20,25,200); color: #dfe7ef; font-size: 10px; padding: 4px 7px;"
            "border-radius: 4px;")
        self._legend.adjustSize()
        self._legend.move(6, 6)
        self._zoom_buttons = []
        for text, factor in (("+", 1.6), ("−", 1 / 1.6)):
            button = QToolButton(self)
            button.setText(text)
            button.setFixedSize(30, 30)
            button.setCursor(Qt.PointingHandCursor)
            button.setStyleSheet(
                "QToolButton { background: rgba(14,20,25,215); color: #e8eef4; font-size: 17px;"
                " border: 1px solid #3a4a58; border-radius: 4px; }"
                "QToolButton:hover { background: rgba(40,60,80,235); }")
            button.clicked.connect(lambda _=False, f=factor: self.zoom_by(f))
            self._zoom_buttons.append(button)
        self.viewport().setCursor(Qt.OpenHandCursor)

    # -- construction ---------------------------------------------------
    def _build_static(self) -> None:
        land_data = json.loads((resources_dir() / "world_land.json").read_text(encoding="utf-8"))
        land = QPainterPath()
        for ring in land_data["polygons"]:
            land.addPolygon(QPolygonF([to_scene(x, y) for x, y in ring]))
            land.closeSubpath()
        item = self._scene.addPath(land, QPen(COLOR_COAST, 0), QBrush(COLOR_LAND))
        item.setZValue(0)

        borders = json.loads((resources_dir() / "world_borders.json").read_text(encoding="utf-8"))
        path = QPainterPath()
        for line in borders["lines"]:
            path.moveTo(to_scene(*line[0]))
            for x, y in line[1:]:
                path.lineTo(to_scene(x, y))
        pen = QPen(COLOR_BORDER, 1.2)
        pen.setCosmetic(True)
        pen.setStyle(Qt.DashLine)
        item = self._scene.addPath(path, pen)
        item.setZValue(0.5)

        country_font = QFont()
        country_font.setPointSize(9)
        country_font.setLetterSpacing(QFont.PercentageSpacing, 110)
        for name, lon, lat, rank in borders["labels"]:
            in_europe = -25 <= lon <= 45 and 34 <= lat <= 72
            if rank > (5 if in_europe else 3):
                continue
            label = QGraphicsSimpleTextItem(name.upper())
            label.setFont(country_font)
            label.setBrush(QBrush(COLOR_COUNTRY))
            label.setPos(to_scene(lon, lat))
            label.setFlag(QGraphicsItem.ItemIgnoresTransformations)
            label.setZValue(0.6)
            self._scene.addItem(label)

        city_font = QFont()
        city_font.setPointSize(8)
        for name, lat, lon in CITIES:
            dot = self._scene.addEllipse(-2, -2, 4, 4, QPen(Qt.NoPen), QBrush(COLOR_CITY))
            dot.setPos(to_scene(lon, lat))
            dot.setFlag(QGraphicsItem.ItemIgnoresTransformations)
            dot.setZValue(0.7)
            label = QGraphicsSimpleTextItem(name)
            label.setFont(city_font)
            label.setBrush(QBrush(COLOR_CITY))
            label.setPos(to_scene(lon, lat))
            label.setFlag(QGraphicsItem.ItemIgnoresTransformations)
            label.setZValue(0.7)
            self._scene.addItem(label)

        grid_pen = QPen(COLOR_GRID, 1)
        grid_pen.setCosmetic(True)
        info = coverage()
        areas: dict[Optional[str], QPainterPath] = {}
        for lat, lon in self._available:
            quality = info[(lat, lon)].quality if (lat, lon) in info else None
            areas.setdefault(quality, QPainterPath()).addRect(tile_rect(lat, lon))
        # one item per quality class: cheap to draw and to restyle
        self._quality_items = {}
        for quality, path in areas.items():
            item = self._scene.addPath(path, grid_pen, QBrush(COLOR_QUALITY.get(quality, COLOR_AVAILABLE)))
            item.setZValue(2)
            self._quality_items[quality] = item
        self._scene.setSceneRect(QRectF(to_scene(-180, 84), to_scene(180, -60)))

    # -- online base map ------------------------------------------------
    def _zoom_level(self) -> int:
        scale = self.transform().m11() or 1.0
        z = BASE_ZOOM + math.log2(scale)
        return int(max(MIN_TILE_ZOOM, min(MAX_TILE_ZOOM, math.ceil(z - 0.3))))

    def _update_base_map(self) -> None:
        if self._fetcher is None:
            return
        z = self._zoom_level()
        size = WORLD / 2 ** z  # scene size of one map tile at zoom z
        view = self.mapToScene(self.viewport().rect()).boundingRect()
        x0, x1 = int(view.left() // size), int(view.right() // size)
        y0, y1 = int(view.top() // size), int(view.bottom() // size)
        n = 2 ** z
        wanted = set()
        for x in range(max(0, x0), min(n - 1, x1) + 1):
            for y in range(max(0, y0), min(n - 1, y1) + 1):
                wanted.add((z, x, y))
                if (z, x, y) not in self._base_items:
                    self._fetcher.request(z, x, y)
        # forget tiles of other zoom levels far away to keep memory in check
        if len(self._base_items) > 400:
            for key in [k for k in self._base_items if k[0] != z and k not in wanted]:
                self._scene.removeItem(self._base_items.pop(key))

    def _tile_loaded(self, z: int, x: int, y: int, data: bytes) -> None:
        if (z, x, y) in self._base_items:
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            return
        item = QGraphicsPixmapItem(pixmap)
        item.setTransformationMode(Qt.SmoothTransformation)
        scale = WORLD / 2 ** z / pixmap.width()
        item.setScale(scale)
        item.setPos(x * WORLD / 2 ** z, y * WORLD / 2 ** z)
        item.setZValue(1 + z / 100.0)  # finer zoom levels on top
        self._scene.addItem(item)  # covers the offline layer (z < 1) where available
        self._base_items[(z, x, y)] = item

    def _schedule_base_map(self) -> None:
        if getattr(self, "_fetcher", None) is not None:
            self._refresh_timer.start()

    def resizeEvent(self, event):  # noqa: N802 - Qt API
        super().resizeEvent(event)
        if not hasattr(self, "_attribution"):
            return
        self._attribution.move(self.viewport().width() - self._attribution.width() - 4,
                               self.viewport().height() - self._attribution.height() - 4)
        for index, button in enumerate(self._zoom_buttons):
            button.move(self.viewport().width() - 36, 6 + index * 34)
        self._schedule_base_map()

    def scrollContentsBy(self, dx, dy):  # noqa: N802 - Qt API
        super().scrollContentsBy(dx, dy)
        self._schedule_base_map()

    def closeEvent(self, event):  # noqa: N802
        if self._fetcher is not None:
            self._fetcher.shutdown()
        super().closeEvent(event)

    # -- public API -----------------------------------------------------
    def selected(self) -> list[Tile]:
        return sorted(self._selected)

    def set_selected(self, tiles: Iterable[Tile], focus: bool = True) -> None:
        self._selected = {t for t in tiles if t in self._available}
        self._refresh()
        if focus and self._selected:
            self.focus_tiles(self._selected)
        self.selectionChanged.emit()

    def set_tile_state(self, tile: Tile, state: Optional[str]) -> None:
        if state is None:
            self._states.pop(tile, None)
        else:
            self._states[tile] = "building" if state.startswith("step:") else state
        self._refresh()

    def set_inventory(self, inventory: Inventory) -> None:
        """Mark tiles that are already installed or whose elevation data is downloaded."""
        self._inventory = inventory
        for item in self._inventory_items:
            self._scene.removeItem(item)
        self._inventory_items = []
        cached_pen = QPen(COLOR_CACHED, 1.3)
        cached_pen.setStyle(Qt.DotLine)
        cached_pen.setCosmetic(True)
        for tile in inventory.cached:
            if tile in inventory.installed:
                continue
            item = self._scene.addRect(tile_rect(*tile).adjusted(4, 4, -4, -4), cached_pen)
            item.setZValue(3.4)
            self._inventory_items.append(item)
        installed_pen = QPen(COLOR_INSTALLED, 2.5)
        installed_pen.setCosmetic(True)
        fill = QColor(COLOR_INSTALLED)
        fill.setAlpha(165)
        for tile in inventory.installed:
            item = self._scene.addRect(tile_rect(*tile).adjusted(1.5, 1.5, -1.5, -1.5), installed_pen,
                                       QBrush(fill))
            item.setZValue(3.2)  # below the selection outline
            self._inventory_items.append(item)
        self._refresh()

    def set_quality_view(self, strong: bool) -> None:
        """Strong colours make the data quality stand out over the base map."""
        for quality, item in self._quality_items.items():
            color = QColor(COLOR_QUALITY.get(quality, COLOR_AVAILABLE))
            if strong:
                color.setAlpha(215)
            item.setBrush(QBrush(color))

    def describe_tile(self, tile: Tile) -> str:
        info = coverage().get(tile)
        parts = [f"Tile {tile_name(*tile)}"]
        if tile in self._selected:
            parts.append("selected")
        parts.append(f"{RATING.get(info.quality, '')} {QUALITY_TEXT.get(info.quality, '')}" if info
                     else "no data")
        packs = self._inventory.installed.get(tile)
        if packs:
            parts.append("✔ installed in " + ", ".join(p.describe() for p in packs))
        else:
            parts.append("not installed yet")
        if tile in self._inventory.cached:
            parts.append("downloaded: " + ", ".join(self._inventory.cached[tile]))
        return " — ".join(parts)

    def zoom_by(self, factor: float, anchor: Optional[QPoint] = None) -> None:
        """Zoom keeping the map point under ``anchor`` (viewport pixels, default:
        centre) in place. Done by hand because Qt's AnchorUnderMouse relies on
        mouse-move bookkeeping that our own mouse handlers bypass."""
        scale = self.transform().m11() * factor
        if not 2 ** (1 - BASE_ZOOM) <= scale <= 2 ** (MAX_TILE_ZOOM - BASE_ZOOM + 1):
            return
        viewport = self.viewport().rect()
        anchor = anchor if anchor is not None else viewport.center()
        before = self.mapToScene(anchor)
        self.setTransformationAnchor(QGraphicsView.NoAnchor)
        self.scale(factor, factor)
        after = self.mapToScene(anchor)
        centre = self.mapToScene(viewport.center())
        self.centerOn(centre + (before - after))
        self._schedule_base_map()

    def clear_states(self) -> None:
        self._states.clear()
        self._refresh()

    def set_editable(self, editable: bool) -> None:
        self._editable = editable

    def focus_tiles(self, tiles: Iterable[Tile]) -> None:
        tiles = list(tiles)
        if not tiles:
            return
        rect = QRectF()
        for lat, lon in tiles:
            rect = rect.united(tile_rect(lat, lon))
        margin = max(rect.width(), rect.height()) * 0.8 + 20
        self.fitInView(rect.adjusted(-margin, -margin, margin, margin), Qt.KeepAspectRatio)
        self._schedule_base_map()

    def show_all(self) -> None:
        self.show_view("Europe")

    def show_view(self, name: str) -> None:
        west, north, east, south = VIEWS[name]
        self.fitInView(QRectF(to_scene(west, north), to_scene(east, south)), Qt.KeepAspectRatio)
        self._schedule_base_map()

    # -- drawing --------------------------------------------------------
    def _refresh(self) -> None:
        wanted = set(self._selected) | set(self._states)
        for tile in list(self._tile_items):
            if tile not in wanted:
                self._scene.removeItem(self._tile_items.pop(tile))
        edge = QPen(COLOR_SELECTED_EDGE, 1.5)
        edge.setCosmetic(True)
        # selected tiles that are already installed keep their orange fill and
        # get a thick selection frame instead
        frame = QPen(COLOR_SELECTED_FRAME, 4)
        frame.setCosmetic(True)
        for tile in wanted:
            state = self._states.get(tile)
            if state:
                pen, brush = edge, QBrush(COLOR_STATE.get(state, COLOR_SELECTED))
            elif tile in self._inventory.installed:
                pen, brush = frame, QBrush(Qt.NoBrush)
            else:
                pen, brush = edge, QBrush(COLOR_SELECTED)
            item = self._tile_items.get(tile)
            if item is None:
                item = self._scene.addRect(tile_rect(*tile).adjusted(1, 1, -1, -1), pen, brush)
                item.setZValue(3.3)
                self._tile_items[tile] = item
            else:
                item.setPen(pen)
                item.setBrush(brush)

    # -- interaction ----------------------------------------------------
    def wheelEvent(self, event):  # noqa: N802 - Qt API
        self.zoom_by(1.25 if event.angleDelta().y() > 0 else 1 / 1.25, event.position().toPoint())

    def mouseDoubleClickEvent(self, event):  # noqa: N802 - Qt API
        if event.button() == Qt.LeftButton:
            self.zoom_by(2.0, event.position())

    def _tile_at(self, event) -> Tile:
        return scene_to_tile(self.mapToScene(event.position().toPoint()))

    def _set_tile(self, tile: Tile, selected: bool) -> None:
        if tile not in self._available or (tile in self._selected) == selected:
            return
        (self._selected.add if selected else self._selected.discard)(tile)
        self._refresh()
        self.selectionChanged.emit()

    def _apply_paint(self, event) -> None:
        if self._paint_mode is not None:
            self._set_tile(self._tile_at(event), self._paint_mode)

    def _pan(self, event) -> None:
        delta = event.position() - self._pan_start
        self._pan_start = event.position()
        self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - int(delta.x()))
        self.verticalScrollBar().setValue(self.verticalScrollBar().value() - int(delta.y()))

    def mousePressEvent(self, event):  # noqa: N802
        button = event.button()
        if button == Qt.MiddleButton or (button == Qt.LeftButton and
                                         not (self._editable and event.modifiers() & Qt.ShiftModifier)):
            # a plain left press may become a click (toggle tile) or a drag (pan)
            self._pan_start = event.position()
            self._press_pos = event.position() if button == Qt.LeftButton else None
            self._press_tile = self._tile_at(event)
            return
        if not self._editable:
            return
        if button == Qt.LeftButton:  # Shift+drag paints
            self._paint_mode = self._tile_at(event) not in self._selected
        elif button == Qt.RightButton:
            self._paint_mode = False
        self._apply_paint(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._pan_start is not None:
            if self._press_pos is not None:
                if (event.position() - self._press_pos).manhattanLength() < CLICK_SLOP:
                    return
                self._press_pos = None  # it is a drag now
                self.viewport().setCursor(Qt.ClosedHandCursor)
            self._pan(event)
            return
        tile = self._tile_at(event)
        if tile in self._available:
            self.hovered.emit(self.describe_tile(tile))
        else:
            self.hovered.emit("No elevation data here (sea)")
        self._apply_paint(event)

    def mouseReleaseEvent(self, event):  # noqa: N802
        if self._press_pos is not None and self._editable and event.button() == Qt.LeftButton:
            tile = self._tile_at(event)
            if tile == self._press_tile:
                self._set_tile(tile, tile not in self._selected)
                self.hovered.emit(self.describe_tile(tile) if tile in self._available else "")
        self._paint_mode = None
        self._pan_start = None
        self._press_pos = None
        self.viewport().setCursor(Qt.OpenHandCursor)
