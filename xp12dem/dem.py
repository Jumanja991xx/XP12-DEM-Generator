"""Elevation data per 1x1 degree tile, from the best free source available.

The order of sources per tile comes from ``resources/dem_coverage.json``
(see :mod:`xp12dem.dem_sources`); the sources themselves live in
:mod:`xp12dem.sources`. The best source is used where it has data, the next
ones fill its gaps (sea, survey edges) with a soft blend. The result is
cached as an SRTM-style ``.hgt`` per tile and resolution, with a small JSON
file next to it that records which sources were used.
"""

from __future__ import annotations

import io
import json
import os
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Union

import numpy as np
import requests

from . import raster as R
from .raster import Cancelled  # noqa: F401  (re-exported)
from .regions import hgt_name

MIRROR_URL = "https://raw.githubusercontent.com/mg4gh/hgtdata/main/docs/{name}.zip"
ATTRIBUTION = "Elevation data: Sonny's LiDAR Digital Terrain Models of Europe (https://sonny.4lima.de), CC BY 4.0"

SIZE_1S = 3601 * 3601 * 2  # 25,934,402 bytes
SIZE_3S = 1201 * 1201 * 2  # 2,884,802 bytes
SIZE_13S = 10801 * 10801 * 2
FEATHER_M = 300.0  # blend width where a weaker source fills gaps of a better one


@dataclass(frozen=True)
class Resolution:
    key: str
    label: str
    arcsec: float
    description: str

    @property
    def posts(self) -> int:
        return int(round(3600 / self.arcsec)) + 1

    @property
    def folder(self) -> str:
        return {"1": "1s", "3": "3s", "13": "13s"}[self.key]


RESOLUTIONS: tuple[Resolution, ...] = (
    Resolution("1", '1 arc-second (≈ 30 m)', 1,
               "Recommended. Sharp ridges and narrow valleys; the native resolution of "
               "Sonny's LiDAR DTM and most national models."),
    Resolution("13", '1/3 arc-second (≈ 10 m)', 1 / 3,
               "Even finer where 10 m data exists (USA, New Zealand, Canada LiDAR, Japan, "
               "Arctic, your own files). Other tiles use 1\". Big files, slower builds."),
    Resolution("3", '3 arc-seconds (≈ 90 m)', 3,
               "Resampled from the 1\" data. Smooth, light-weight terrain; "
               "about 3x faster to build."),
)


def resolution(key: Union[str, int, float]) -> Resolution:
    key = {1: "1", 3: "3"}.get(key, key) if not isinstance(key, str) else key
    for res in RESOLUTIONS:
        if res.key == key:
            return res
    raise KeyError(key)


ProgressFn = Callable[[int, int], None]  # (bytes_done, bytes_total)


SOURCE_LABELS = {
    "local": "your own DEM files",
    "usgs": "USGS 3DEP",
    "sonny": "Sonny's LiDAR DTM",
    "fabdem": "FABDEM V1-2",
    "glo30": "Copernicus GLO-30",
    "linz": "LINZ LiDAR 1 m (New Zealand)",
    "hrdem": "NRCan HRDEM LiDAR (Canada)",
    "mrdem": "NRCan MRDEM 30 m (Canada)",
    "arcticdem": "ArcticDEM 10 m",
    "gsi": "GSI 5-10 m (Japan)",
}


def source_label(source: str, lat: int, lon: int) -> str:
    """Human readable source name; Sonny is only LiDAR where the coverage map says so."""
    if source == "sonny":
        from .dem_sources import FABDEM, tile_info

        info = tile_info((lat, lon))
        if info is not None and info.quality == FABDEM:
            return "Sonny's DTM (SRTM/Viewfinder based in this area)"
    return SOURCE_LABELS.get(source, source)


class DemStore:
    """Cache of merged elevation tiles, filled from the best sources per tile."""

    def __init__(self, cache_dir: Path, local_source: Optional[Path] = None,
                 session: Optional[requests.Session] = None, log: Callable[[str], None] = lambda t: None):
        self.cache_dir = Path(cache_dir)
        self.local_source = Path(local_source) if local_source else None
        self.session = session or requests.Session()
        try:
            self.session.headers.setdefault("User-Agent", "XP12-DEM-Generator")
        except AttributeError:
            pass
        self.warnings: list[str] = []
        self.log = log

    # -- paths -----------------------------------------------------------
    def path(self, lat: int, lon: int, res: Union[str, int, float] = "1", source: Optional[str] = None) -> Path:
        res = resolution(res)
        if source:  # cache layout of version 0.1 (one folder per source)
            return self.cache_dir / res.folder / source / f"{hgt_name(lat, lon)}.hgt"
        return self.cache_dir / res.folder / f"{hgt_name(lat, lon)}.hgt"

    def meta(self, lat: int, lon: int, res: Union[str, int, float] = "1") -> dict:
        meta_file = self.path(lat, lon, res).with_suffix(".json")
        try:
            return json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def has(self, lat: int, lon: int, res: Union[str, int, float] = "1", source: Optional[str] = None) -> bool:
        res = resolution(res)
        path = self.path(lat, lon, res.key, source)
        if not _valid_size(path, res):
            return False
        return source is not None or path.with_suffix(".json").is_file()

    def _context(self, progress, cancel):
        from .sources import Context

        return Context(self.session, self.cache_dir, self.local_source, cancel, progress, self.log)

    def candidates(self, lat: int, lon: int) -> list[str]:
        """Sources for a tile, best first."""
        from .dem_sources import tile_info
        from .sources import SOURCES

        info = tile_info((lat, lon))
        chain = list(info.sources) if info else ["sonny", "fabdem", "glo30"]
        if self.local_source and SOURCES["local"].files_for(self._context(None, None), lat, lon):
            chain.insert(0, "local")
        return chain

    def fine_available(self, lat: int, lon: int) -> bool:
        """Is 1/3" worth it here (a source with <= ~12 m posts)?"""
        from .dem_sources import tile_info
        from .sources import SOURCES

        if self.local_source:
            native = SOURCES["local"].native_m_for(self._context(None, None), lat, lon)
            if native is not None and native <= 12:
                return True
        info = tile_info((lat, lon))
        return bool(info and info.fine)

    def is_cached(self, lat: int, lon: int, res: Union[str, int, float] = "1") -> bool:
        """True if ``ensure`` will not need to download anything."""
        res = resolution(res)
        if res.key == "13" and not self.fine_available(lat, lon):
            res = resolution("1")
        if self.has(lat, lon, res.key) or (res.key == "3" and self.has(lat, lon, "1")):
            return True
        return self._legacy(lat, lon) is not None

    # -- public API ------------------------------------------------------
    def ensure(self, lat: int, lon: int, res: Union[str, int, float] = "1",
               progress: Optional[ProgressFn] = None,
               cancel: Optional[threading.Event] = None) -> Path:
        """Return a valid local ``.hgt`` for the tile, downloading if needed."""
        return self.ensure_with_source(lat, lon, res, progress, cancel)[0]

    def ensure_with_source(self, lat: int, lon: int, res: Union[str, int, float] = "1",
                           progress: Optional[ProgressFn] = None,
                           cancel: Optional[threading.Event] = None) -> tuple[Path, str]:
        """(path, main source key). ``describe`` gives the human readable sources."""
        res = resolution(res)
        if res.key == "13" and not self.fine_available(lat, lon):
            self.warnings.append(f"{hgt_name(lat, lon)}: no 10 m data here - using 1\" "
                                 "(the best available)")
            res = resolution("1")
        if res.key == "3":
            base, source = self.ensure_with_source(lat, lon, "1", progress, cancel)
            target = self.path(lat, lon, "3")
            if not self.has(lat, lon, "3") or self.meta(lat, lon, "3") != self.meta(lat, lon, "1"):
                downsample_to_3s(base, target)
                target.with_suffix(".json").write_text(json.dumps(self.meta(lat, lon, "1")), encoding="utf-8")
            return target, source
        target = self.path(lat, lon, res.key)
        if self.has(lat, lon, res.key):
            return target, self.meta(lat, lon, res.key).get("main", "?")
        if res.key == "1":
            adopted = self._adopt_legacy(lat, lon)
            if adopted:
                return target, adopted
        return target, self._build(lat, lon, res, target, progress, cancel)

    def describe(self, lat: int, lon: int, res: Union[str, int, float] = "1") -> str:
        """Human readable sources of a cached tile, e.g. 'LINZ LiDAR 1 m + FABDEM V1-2 (gaps)'."""
        res = resolution(res)
        meta = self.meta(lat, lon, res.key)
        if not meta and res.key == "13":
            meta = self.meta(lat, lon, "1")
        used = meta.get("sources", [])
        if not used:
            return source_label(meta.get("main", "sonny"), lat, lon)
        text = source_label(used[0]["key"], lat, lon)
        extra = [u for u in used[1:] if u.get("share", 0) >= 0.01]
        if extra:
            text += " + " + ", ".join(source_label(u["key"], lat, lon) for u in extra) + " (gaps)"
        return text

    # -- building --------------------------------------------------------
    def _build(self, lat: int, lon: int, res: Resolution, target: Path,
               progress: Optional[ProgressFn], cancel: Optional[threading.Event]) -> str:
        from .sources import SOURCES, NotAvailable

        ctx = self._context(progress, cancel)
        n = res.posts
        name = hgt_name(lat, lon)
        grid: Optional[np.ndarray] = None
        used: list[dict] = []
        errors: list[str] = []
        feather = max(1, int(round(FEATHER_M / (R.metres_per_degree_lat() / (n - 1)))))
        chain = self.candidates(lat, lon)
        for index, key in enumerate(chain):
            ctx.check()
            if grid is not None and not np.isnan(grid).any():
                break
            try:
                part = SOURCES[key].fetch(ctx, lat, lon, n)
            except Cancelled:
                raise
            except NotAvailable as exc:
                if index == 0 and key != "local":
                    errors.append(f"{source_label(key, lat, lon)}: {exc}")
                continue
            except Exception as exc:  # network trouble, server changes...
                errors.append(f"{source_label(key, lat, lon)}: {exc}")
                continue
            before = 0 if grid is None else int(np.count_nonzero(~np.isnan(grid)))
            if grid is None:
                grid = part.astype(np.float32, copy=True)
            else:
                R.feather_fill(grid, part, feather)
            added = int(np.count_nonzero(~np.isnan(grid))) - before
            if added > 0:
                used.append({"key": key, "share": round(added / grid.size, 4)})
                if errors and len(used) == 1:
                    self.warnings.append(f"{name}: used {source_label(key, lat, lon)} because "
                                         + "; ".join(errors))
        if grid is None or not used:
            raise RuntimeError(f"No elevation data for {name} - " + (" | ".join(errors) or "no source"))
        missing = np.isnan(grid)
        if missing.any():  # only open sea is left: sea level
            grid[missing] = 0.0
        R.write_hgt(grid, target)
        main = used[0]["key"]
        target.with_suffix(".json").write_text(
            json.dumps({"main": main, "sources": used, "posts": n}), encoding="utf-8")
        return main

    # -- caches of version 0.1 --------------------------------------------
    def _legacy(self, lat: int, lon: int) -> Optional[tuple[Path, str]]:
        res = resolution("1")
        plain = self.path(lat, lon, "1")
        if _valid_size(plain, res) and not plain.with_suffix(".json").is_file():
            return plain, "sonny"  # version 0.1: Sonny tiles directly in 1s/
        for key in self.candidates(lat, lon):
            old = self.path(lat, lon, "1", key)
            if _valid_size(old, res):
                return old, key
        return None

    def _adopt_legacy(self, lat: int, lon: int) -> Optional[str]:
        found = self._legacy(lat, lon)
        if found is None:
            return None
        old, key = found
        target = self.path(lat, lon, "1")
        if old != target:
            os.replace(old, target)
        target.with_suffix(".json").write_text(
            json.dumps({"main": key, "sources": [{"key": key, "share": 1.0}], "posts": 3601}),
            encoding="utf-8")
        return key


def _valid_size(path: Path, res: "Resolution") -> bool:
    try:
        return path.stat().st_size == res.posts * res.posts * 2
    except OSError:
        return False


def _valid_hgt(path: Path, arcsec: int) -> bool:
    expected = SIZE_1S if arcsec == 1 else SIZE_3S
    try:
        return path.stat().st_size == expected
    except OSError:
        return False


def _extract_hgt(payload: bytes, name: str, dest: Path, arcsec: int) -> None:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = [m for m in archive.namelist() if Path(m).stem.upper() == name and m.lower().endswith(".hgt")]
        if not members:
            raise ValueError(f"{name}.hgt missing from archive")
        data = archive.read(members[0])
    expected = SIZE_1S if arcsec == 1 else SIZE_3S
    if len(data) != expected:
        raise ValueError(f"{name}.hgt has unexpected size {len(data)}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    tmp.write_bytes(data)
    os.replace(tmp, dest)


def downsample_to_3s(source: Path, dest: Path) -> None:
    """Resample a 1" tile (3601²) to 3" (1201²).

    Every third post of the 1" grid lies exactly on the 3" grid, so we take a
    3x3 mean centred on each 3" post (edges included) - this keeps the
    tile borders identical between neighbours and avoids aliasing.
    """
    grid = np.fromfile(source, dtype=">i2").reshape(3601, 3601).astype(np.float32)
    nodata = grid == -32768
    if nodata.any():
        grid[nodata] = np.nan
    padded = np.pad(grid, 1, mode="edge")
    acc = np.zeros((1201, 1201), dtype=np.float64)
    cnt = np.zeros((1201, 1201), dtype=np.float64)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            window = padded[1 + dy:3602 + dy:3, 1 + dx:3602 + dx:3]
            valid = ~np.isnan(window)
            acc[valid] += window[valid]
            cnt += valid
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(cnt > 0, np.round(acc / np.maximum(cnt, 1)), -32768)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    out.astype(">i2").tofile(tmp)
    os.replace(tmp, dest)
