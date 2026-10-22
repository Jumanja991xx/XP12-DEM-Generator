"""Remaining-time estimate for a build.

The estimate is made in seconds, not in "progress units": every tile has an
expected download time (zero when the data is already cached) and an
expected build time. Build times measured on this PC are remembered between
runs, so from the second build on the estimate matches the user's hardware.
The progress bar then shows elapsed / (elapsed + remaining), which keeps the
percentage and the remaining time consistent with each other.
"""

from __future__ import annotations

import json
import statistics
import threading
import time
from pathlib import Path
from typing import Optional

from .regions import Tile

# First guesses (seconds) until real durations are known.
DEFAULT_BUILD = {("improve", "1"): 110, ("improve", "3"): 90, ("improve", "13"): 240,
                 ("rebuild", "1"): 200, ("rebuild", "3"): 150, ("rebuild", "13"): 480}
DEFAULT_DOWNLOAD = 40.0   # one uncached elevation tile (national mosaics take a while)
CACHED_DOWNLOAD = 0.5     # tile already on disk (3" may still need resampling)
FINAL_BASE, FINAL_PER_TILE = 4.0, 1.0  # assembling + installing the pack
HISTORY = 12              # remembered durations per kind of build


def build_key(method: str, resolution_key: str, mesh_key: str) -> str:
    return f"{method}-{resolution_key}" + ("" if method == "improve" else f"-{mesh_key}")


class TimingHistory:
    """Per-PC build durations, stored as JSON next to the settings."""

    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            self.data: dict[str, list[float]] = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            self.data = {}

    def typical(self, key: str) -> Optional[float]:
        values = self.data.get(key) or []
        return float(statistics.median(values)) if values else None

    def add(self, key: str, seconds: float) -> None:
        values = self.data.setdefault(key, [])
        values.append(round(seconds, 1))
        del values[:-HISTORY]

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        except OSError:
            pass


class Estimator:
    def __init__(self, tiles: list[Tile], cached: set[Tile], workers: int,
                 expected_build: float, clock=time.monotonic):
        self.clock = clock
        self.t0 = clock()
        self.workers = max(1, workers)
        self.expected_build = expected_build
        self.download_measured: list[float] = []
        self.build_measured: list[float] = []
        self.to_download: dict[Tile, bool] = {t: t in cached for t in tiles}  # tile -> cached
        self.downloading: Optional[tuple[Tile, float]] = None
        self.pending_build: set[Tile] = set(tiles)
        self.running: dict[Tile, float] = {}
        self.final_left = FINAL_BASE + FINAL_PER_TILE * len(tiles)
        self.lock = threading.Lock()

    # -- events ------------------------------------------------------------
    def download_started(self, tile: Tile) -> None:
        with self.lock:
            self.downloading = (tile, self.clock())

    def download_finished(self, tile: Tile, ok: bool = True) -> None:
        with self.lock:
            if self.downloading and self.downloading[0] == tile:
                seconds = self.clock() - self.downloading[1]
                if not self.to_download.get(tile, True) and seconds > 2:
                    self.download_measured.append(seconds)
            self.downloading = None
            self.to_download.pop(tile, None)
            if not ok:
                self.pending_build.discard(tile)

    def skip(self, tile: Tile) -> None:
        """Tile will not be built (failed before its build started)."""
        with self.lock:
            self.to_download.pop(tile, None)
            self.pending_build.discard(tile)

    def build_started(self, tile: Tile) -> None:
        with self.lock:
            self.pending_build.discard(tile)
            self.running[tile] = self.clock()

    def build_finished(self, tile: Tile, ok: bool) -> Optional[float]:
        with self.lock:
            begun = self.running.pop(tile, None)
            self.pending_build.discard(tile)
            if begun is None:
                return None
            seconds = self.clock() - begun
            if ok:
                self.build_measured.append(seconds)
            return seconds

    def final_done(self) -> None:
        with self.lock:
            self.final_left = 0.0

    # -- estimate ------------------------------------------------------------
    def per_tile_build(self) -> float:
        if self.build_measured:
            measured = statistics.mean(self.build_measured)
            # trust this run more with every finished tile
            weight = min(1.0, len(self.build_measured) / 2)
            return weight * measured + (1 - weight) * self.expected_build
        return self.expected_build

    def remaining(self) -> float:
        with self.lock:
            now = self.clock()
            per_download = (statistics.mean(self.download_measured)
                            if self.download_measured else DEFAULT_DOWNLOAD)
            download = 0.0
            for tile, cached in self.to_download.items():
                expected = CACHED_DOWNLOAD if cached else per_download
                if self.downloading and self.downloading[0] == tile:
                    expected = max(expected - (now - self.downloading[1]), expected * 0.1)
                download += expected
            per_build = self.per_tile_build()
            # a tile taking longer than usual: keep a growing, honest estimate instead of
            # a fixed "few seconds left"
            running = sum(max(per_build - (now - begun), 0.0) + per_build * 0.04
                          + max(0.0, (now - begun) - per_build) * 0.25
                          for begun in self.running.values())
            work = running + len(self.pending_build) * per_build
            slots = min(self.workers, max(1, len(self.running) + len(self.pending_build)))
            return download + work / slots + self.final_left

    def elapsed(self) -> float:
        return self.clock() - self.t0
