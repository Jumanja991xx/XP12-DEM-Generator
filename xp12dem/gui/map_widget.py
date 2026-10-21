"""Interactive Europe map for picking 1x1 degree tiles."""

from __future__ import annotations

import json
import math
from typing import Iterable

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QGraphicsItem, QGraphicsScene, QGraphicsSimpleTextItem, QGraphicsView

from ..paths import resources_dir
from ..regions import Tile, available_tiles, tile_name

# Equirectangular projection, x stretched by cos(52°) so Europe looks right.
_KX = math.cos(math.radians(52)) * 20.0
_KY = 20.0

COLOR_SEA = QColor("#1b2a38")
COLOR_LAND = QColor("#2f4a3a")
COLOR_COAST = QColor("#5d7f69")
COLOR_AVAILABLE = QColor(255, 255, 255, 28)
COLOR_GRID = QColor(255, 255, 255, 45)
COLOR_SELECTED = QColor(255, 170, 40, 150)
COLOR_SELECTED_EDGE = QColor(255, 200, 90)
COLOR_CITY = QColor(235, 240, 245, 210)

# A few reference points to find your way around the map.
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

COLOR_STATE = {
    "downloading": QColor(80, 160, 255, 160),
    "dem_ready": QColor(80, 160, 255, 160),
    "building": QColor(170, 120, 255, 180),
    "built": QColor(60, 200, 110, 190),
    "failed": QColor(230, 70, 70, 200),
}


def to_scene(lon: float, lat: float) -> QPointF:
    return QPointF(lon * _KX, -lat * _KY)


def tile_rect(lat: int, lon: int) -> QRectF:
    top_left = to_scene(lon, lat + 1)
    bottom_right = to_scene(lon + 1, lat)
    return QRectF(top_left, bottom_right)


def scene_to_tile(point: QPointF) -> Tile:
    return math.floor(-point.y() / _KY), math.floor(point.x() / _KX)


class TileMap(QGraphicsView):
    """Left click/drag selects tiles, right click/drag deselects, wheel zooms."""

    selectionChanged = Signal()
    hovered = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRenderHint(QPainter.Antialiasing)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
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
        self._tile_items: dict[Tile, object] = {}
        self._paint_mode: bool | None = None
        self._editable = True
        self._pan_start = None
        self._build_static()

    # -- construction ---------------------------------------------------
    def _build_static(self) -> None:
        data = json.loads((resources_dir() / "europe_land.json").read_text(encoding="utf-8"))
        land = QPainterPath()
        for ring in data["polygons"]:
            land.addPolygon(QPolygonF([to_scene(x, y) for x, y in ring]))
            land.closeSubpath()
        land_item = self._scene.addPath(land, QPen(COLOR_COAST, 0), QBrush(COLOR_LAND))
        land_item.setZValue(0)
        pen = QPen(COLOR_GRID, 0)
        for lat, lon in self._available:
            item = self._scene.addRect(tile_rect(lat, lon), pen, QBrush(COLOR_AVAILABLE))
            item.setZValue(1)
        font = QFont()
        font.setPointSize(8)
        for name, lat, lon in CITIES:
            dot = self._scene.addEllipse(-2, -2, 4, 4, QPen(Qt.NoPen), QBrush(COLOR_CITY))
            dot.setPos(to_scene(lon, lat))
            dot.setFlag(QGraphicsItem.ItemIgnoresTransformations)
            dot.setZValue(3)
            label = QGraphicsSimpleTextItem(name)
            label.setFont(font)
            label.setBrush(QBrush(COLOR_CITY))
            label.setPos(to_scene(lon, lat))
            label.setFlag(QGraphicsItem.ItemIgnoresTransformations)
            label.setZValue(3)
            self._scene.addItem(label)
        self._scene.setSceneRect(QRectF(to_scene(-26, 73), to_scene(46, 33)))

    # -- public API -----------------------------------------------------
    def selected(self) -> list[Tile]:
        return sorted(self._selected)

    def set_selected(self, tiles: Iterable[Tile], focus: bool = True) -> None:
        self._selected = {t for t in tiles if t in self._available}
        self._refresh()
        if focus and self._selected:
            self.focus_tiles(self._selected)
        self.selectionChanged.emit()

    def set_tile_state(self, tile: Tile, state: str | None) -> None:
        if state is None:
            self._states.pop(tile, None)
        else:
            self._states[tile] = "building" if state.startswith("step:") else state
        self._refresh()

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
        margin = max(rect.width(), rect.height()) * 0.8 + 40
        self.fitInView(rect.adjusted(-margin, -margin, margin, margin), Qt.KeepAspectRatio)

    def show_all(self) -> None:
        self.fitInView(QRectF(to_scene(-12, 66), to_scene(32, 36)), Qt.KeepAspectRatio)

    # -- drawing --------------------------------------------------------
    def _refresh(self) -> None:
        wanted = set(self._selected) | set(self._states)
        for tile in list(self._tile_items):
            if tile not in wanted:
                self._scene.removeItem(self._tile_items.pop(tile))
        for tile in wanted:
            state = self._states.get(tile)
            fill = COLOR_STATE.get(state, COLOR_SELECTED) if state else COLOR_SELECTED
            item = self._tile_items.get(tile)
            if item is None:
                item = self._scene.addRect(tile_rect(*tile), QPen(COLOR_SELECTED_EDGE, 0), QBrush(fill))
                item.setZValue(2)
                self._tile_items[tile] = item
            else:
                item.setBrush(QBrush(fill))

    # -- interaction ----------------------------------------------------
    def wheelEvent(self, event):  # noqa: N802 - Qt API
        factor = 1.2 if event.angleDelta().y() > 0 else 1 / 1.2
        self.scale(factor, factor)

    def _apply_paint(self, event) -> None:
        tile = scene_to_tile(self.mapToScene(event.position().toPoint()))
        if tile not in self._available or self._paint_mode is None:
            return
        changed = False
        if self._paint_mode and tile not in self._selected:
            self._selected.add(tile)
            changed = True
        elif not self._paint_mode and tile in self._selected:
            self._selected.discard(tile)
            changed = True
        if changed:
            self._refresh()
            self.selectionChanged.emit()

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MiddleButton:
            self._pan_start = event.position()
            return
        if not self._editable:
            return
        if event.button() == Qt.LeftButton:
            tile = scene_to_tile(self.mapToScene(event.position().toPoint()))
            # clicking a selected tile toggles it off, otherwise paint on
            self._paint_mode = tile not in self._selected
        elif event.button() == Qt.RightButton:
            self._paint_mode = False
        self._apply_paint(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._pan_start is not None:
            delta = event.position() - self._pan_start
            self._pan_start = event.position()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - int(delta.x()))
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - int(delta.y()))
            return
        tile = scene_to_tile(self.mapToScene(event.position().toPoint()))
        if tile in self._available:
            mark = "selected" if tile in self._selected else "available"
            self.hovered.emit(f"Tile {tile_name(*tile)} — {mark}")
        else:
            self.hovered.emit("No Sonny DTM coverage here")
        if self._paint_mode is not None:
            self._apply_paint(event)

    def mouseReleaseEvent(self, event):  # noqa: N802
        self._paint_mode = None
        self._pan_start = None
