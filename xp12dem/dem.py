"""Download and prepare Sonny's LiDAR DTM tiles.

Sonny publishes his European terrain models as 1x1 degree SRTM ``.hgt`` files
(CC BY 4.0, https://sonny.4lima.de). The original site serves whole countries
through Google Drive, which cannot be fetched per tile, so by default we use
the per-tile mirror of the 1" "Europe" collection at
https://github.com/mg4gh/hgtdata. Users who already downloaded newer releases
from Sonny's site can point the app at that folder instead.
"""

from __future__ import annotations

import io
import os
import shutil
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import requests

from .regions import hgt_name

MIRROR_URL = "https://raw.githubusercontent.com/mg4gh/hgtdata/main/docs/{name}.zip"
ATTRIBUTION = "Elevation data: Sonny's LiDAR Digital Terrain Models of Europe (https://sonny.4lima.de), CC BY 4.0"

SIZE_1S = 3601 * 3601 * 2  # 25,934,402 bytes
SIZE_3S = 1201 * 1201 * 2  # 2,884,802 bytes


@dataclass(frozen=True)
class Resolution:
    key: str
    label: str
    arcsec: int
    description: str


RESOLUTIONS: tuple[Resolution, ...] = (
    Resolution("1", '1 arc-second (≈ 30 m)', 1,
               "Sonny's native resolution. Sharp ridges and narrow valleys; "
               "largest files and longest build time."),
    Resolution("3", '3 arc-seconds (≈ 90 m)', 3,
               "Resampled from the 1\" data. Smooth, light-weight terrain; "
               "about 3x faster to build."),
)


def resolution(key: str) -> Resolution:
    for res in RESOLUTIONS:
        if res.key == key:
            return res
    raise KeyError(key)


class Cancelled(Exception):
    pass


ProgressFn = Callable[[int, int], None]  # (bytes_done, bytes_total)


SOURCE_LABELS = {
    "local": "your own DEM files",
    "usgs": "USGS 3DEP 1\"",
    "sonny": "Sonny's LiDAR DTM",
    "fabdem": "FABDEM V1-2",
}


def source_label(source: str, lat: int, lon: int) -> str:
    """Human readable source name; Sonny is only LiDAR where the coverage map says so."""
    if source == "sonny":
        from .dem_sources import FABDEM, tile_info

        info = tile_info((lat, lon))
        if info is not None and info.quality == FABDEM:
            return "Sonny's DTM (SRTM/Viewfinder based in this area)"
    return SOURCE_LABELS[source]


class DemStore:
    """Local cache of DEM tiles, filled from the best available source per tile.

    Order of preference: the user's own files, then the source the coverage
    map recommends (USGS 3DEP in the USA, Sonny where he has LiDAR or national
    models, FABDEM elsewhere), then the remaining sources as fallback.
    """

    def __init__(self, cache_dir: Path, local_source: Optional[Path] = None,
                 session: Optional[requests.Session] = None):
        self.cache_dir = Path(cache_dir)
        self.local_source = Path(local_source) if local_source else None
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "XP12-DEM-Generator")
        self.warnings: list[str] = []

    # -- paths -----------------------------------------------------------
    def path(self, lat: int, lon: int, arcsec: int = 1, source: str = "sonny") -> Path:
        return self.cache_dir / f"{arcsec}s" / source / f"{hgt_name(lat, lon)}.hgt"

    def _legacy_path(self, lat: int, lon: int, arcsec: int) -> Path:
        # caches written by version 0.1 (Sonny only) had no source folder
        return self.cache_dir / f"{arcsec}s" / f"{hgt_name(lat, lon)}.hgt"

    def has(self, lat: int, lon: int, arcsec: int = 1, source: str = "sonny") -> bool:
        return (_valid_hgt(self.path(lat, lon, arcsec, source), arcsec)
                or (source == "sonny" and _valid_hgt(self._legacy_path(lat, lon, arcsec), arcsec)))

    def is_cached(self, lat: int, lon: int, arcsec: int) -> bool:
        """True if ``ensure`` will not need to download anything."""
        order = self.candidates(lat, lon)
        if not order:
            return False
        source = order[0]  # ensure() only falls back to later sources after a failure
        return source == "local" or self.has(lat, lon, arcsec, source) or self.has(lat, lon, 1, source)

    def candidates(self, lat: int, lon: int) -> list[str]:
        from .dem_sources import FABDEM, LIDAR, NATIONAL, USGS, find_fabdem_archives, \
            find_local_files, tile_info

        order: list[str] = []
        if self.local_source and [p for p in find_local_files(self.local_source, lat, lon)
                                  if "fabdem" not in p.name.lower()]:
            order.append("local")
        info = tile_info((lat, lon))
        if info is None:
            order += ["sonny", "fabdem"]
        elif info.quality == USGS:
            order += ["usgs", "fabdem"]
        elif info.quality in (LIDAR, NATIONAL):
            order += ["sonny", "fabdem"]
        else:  # FABDEM is the best free source; Sonny (satellite based) as fallback
            order += ["fabdem"] + (["sonny"] if info.has_sonny else [])
        return order

    # -- public API ------------------------------------------------------
    def ensure(self, lat: int, lon: int, arcsec: int,
               progress: Optional[ProgressFn] = None,
               cancel: Optional[threading.Event] = None) -> Path:
        """Return a valid local ``.hgt`` for the tile, downloading if needed."""
        return self.ensure_with_source(lat, lon, arcsec, progress, cancel)[0]

    def ensure_with_source(self, lat: int, lon: int, arcsec: int,
                           progress: Optional[ProgressFn] = None,
                           cancel: Optional[threading.Event] = None) -> tuple[Path, str]:
        if arcsec not in (1, 3):
            raise ValueError(f"Unsupported resolution: {arcsec}")
        errors: list[str] = []
        order = self.candidates(lat, lon)
        for index, source in enumerate(order):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            target = self.path(lat, lon, arcsec, source)
            if _valid_hgt(target, arcsec):
                return target, source
            base = self.path(lat, lon, 1, source)
            legacy = self._legacy_path(lat, lon, 1)
            try:
                if source == "sonny" and not _valid_hgt(base, 1) and _valid_hgt(legacy, 1):
                    base.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(legacy, base)
                if not _valid_hgt(base, 1):
                    self._fetch(source, lat, lon, base, progress, cancel)
                if arcsec == 3:
                    downsample_to_3s(base, target)
                if index > 0 and errors:
                    self.warnings.append(f"{hgt_name(lat, lon)}: used {source_label(source, lat, lon)} "
                                         f"because {'; '.join(errors)}")
                return target, source
            except Cancelled:
                raise
            except Exception as exc:
                errors.append(f"{SOURCE_LABELS[source]}: {exc}")
        raise RuntimeError(f"No elevation data for {hgt_name(lat, lon)} - " + " | ".join(errors))

    # -- sources ---------------------------------------------------------
    def _fetch(self, source: str, lat: int, lon: int, dest: Path,
               progress: Optional[ProgressFn], cancel: Optional[threading.Event]) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if source == "sonny":
            self._fetch_sonny(lat, lon, dest, progress, cancel)
        elif source == "usgs":
            self._fetch_usgs(lat, lon, dest, progress, cancel)
        elif source == "fabdem":
            self._fetch_fabdem(lat, lon, dest, progress, cancel)
        elif source == "local":
            self._fetch_local(lat, lon, dest)
        else:
            raise ValueError(source)

    def _fetch_sonny(self, lat: int, lon: int, dest: Path,
                     progress: Optional[ProgressFn], cancel: Optional[threading.Event]) -> None:
        if self.local_source and self._import_local_sonny(lat, lon, dest):
            return
        name = hgt_name(lat, lon)
        url = MIRROR_URL.format(name=name)
        payload = self._download_with_retries(url, progress, cancel, "tile outside Sonny's coverage")
        _extract_hgt(payload, name, dest, arcsec=1)

    def _fetch_usgs(self, lat: int, lon: int, dest: Path,
                    progress: Optional[ProgressFn], cancel: Optional[threading.Event]) -> None:
        from .dem_sources import raster_to_grid, read_geotiff, usgs_url, valid_fraction, write_hgt

        payload = self._download_with_retries(usgs_url(lat, lon), progress, cancel,
                                              "no USGS 3DEP 1\" tile here")
        grid = raster_to_grid([read_geotiff(io.BytesIO(payload))], lat, lon)
        if valid_fraction(grid) < 0.2:
            raise ValueError("USGS tile does not cover this area")
        write_hgt(grid, dest)

    def _fetch_fabdem(self, lat: int, lon: int, dest: Path,
                      progress: Optional[ProgressFn], cancel: Optional[threading.Event]) -> None:
        from .dem_sources import (FABDEM_BASE, HTTPRangeFile, NeedsManualDownload,
                                  fabdem_manual_message, fabdem_member, fabdem_zip_name,
                                  find_fabdem_archives, find_local_files, raster_to_grid,
                                  read_geotiff, read_zip_member_tif, valid_fraction, write_hgt)

        member = fabdem_member(lat, lon)
        raster = None
        if self.local_source:  # extracted tile or the 10x10 degree archive
            for tif in find_local_files(self.local_source, lat, lon):
                if "fabdem" in tif.name.lower() and tif.suffix.lower() in (".tif", ".tiff"):
                    raster = read_geotiff(tif)
                    break
            if raster is None:
                for archive in find_fabdem_archives(self.local_source, lat, lon):
                    raster = read_zip_member_tif(archive, member)
                    if raster is not None:
                        break
        if raster is None:
            try:
                remote = HTTPRangeFile(FABDEM_BASE + fabdem_zip_name(lat, lon), self.session,
                                       progress, cancel)
                raster = read_zip_member_tif(remote, member)
            except NeedsManualDownload:
                raise NeedsManualDownload(fabdem_manual_message(lat, lon))
            except InterruptedError:
                raise Cancelled()
            except (requests.RequestException, OSError, zipfile.BadZipFile) as exc:
                raise NeedsManualDownload(fabdem_manual_message(lat, lon)) from exc
            if raster is None:
                raise FileNotFoundError(f"{member} is not part of FABDEM (sea?)")
        grid = raster_to_grid([raster], lat, lon)
        if valid_fraction(grid) < 0.2:
            raise ValueError("FABDEM tile does not cover this area")
        write_hgt(grid, dest)

    def _fetch_local(self, lat: int, lon: int, dest: Path) -> None:
        """Any .hgt/.tif/.zip in the user's folder whose name matches the tile."""
        from .dem_sources import GeoRaster, find_local_files, raster_to_grid, read_geotiff, \
            valid_fraction, write_hgt

        assert self.local_source is not None
        rasters = []
        for path in find_local_files(self.local_source, lat, lon):
            suffix = path.suffix.lower()
            try:
                if suffix == ".hgt" and path.stat().st_size == SIZE_1S:
                    shutil.copyfile(path, dest)
                    return
                if suffix == ".zip":
                    if self._import_local_sonny(lat, lon, dest):
                        return
                    continue
                if suffix == ".hgt":
                    size = int(round((path.stat().st_size / 2) ** 0.5))
                    data = np.fromfile(path, ">i2").reshape(size, size).astype(np.float32)
                    data[data <= -32000] = np.nan
                    step = 1.0 / (size - 1)
                    rasters.append(GeoRaster(data, lon, lat + 1, step, step))
                elif suffix in (".tif", ".tiff") and "fabdem" not in path.name.lower():
                    rasters.append(read_geotiff(path))
            except Exception:
                continue
        if not rasters:
            raise FileNotFoundError("no readable file for this tile in your DEM folder")
        grid = raster_to_grid(rasters, lat, lon)
        if valid_fraction(grid) < 0.2:
            raise ValueError("your files do not cover this tile")
        write_hgt(grid, dest)

    # -- helpers ---------------------------------------------------------
    def _download_with_retries(self, url: str, progress: Optional[ProgressFn],
                               cancel: Optional[threading.Event], missing_hint: str) -> bytes:
        last_error: Optional[Exception] = None
        for attempt in range(4):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            try:
                return self._download(url, progress, cancel, missing_hint)
            except (Cancelled, FileNotFoundError):
                raise
            except Exception as exc:  # network hiccup, truncated download...
                last_error = exc
                time.sleep(2 ** attempt)
        raise RuntimeError(f"download failed: {last_error}")

    def _download(self, url: str, progress: Optional[ProgressFn],
                  cancel: Optional[threading.Event], missing_hint: str = "not found") -> bytes:
        with self.session.get(url, stream=True, timeout=60) as response:
            if response.status_code in (403, 404):
                raise FileNotFoundError(missing_hint)
            response.raise_for_status()
            total = int(response.headers.get("Content-Length") or 0)
            buf = io.BytesIO()
            for chunk in response.iter_content(chunk_size=1 << 16):
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                buf.write(chunk)
                if progress:
                    progress(buf.tell(), total)
            return buf.getvalue()

    def _import_local_sonny(self, lat: int, lon: int, dest: Path) -> bool:
        """Use a 1" .hgt (or .zip containing one) from the user's own folder."""
        name = hgt_name(lat, lon)
        if not self.local_source or not self.local_source.is_dir():
            return False
        candidates = [p for p in self.local_source.rglob("*")
                      if p.is_file() and p.stem.upper() == name and p.suffix.lower() in (".hgt", ".zip")]
        for candidate in sorted(candidates, key=lambda p: p.suffix.lower() != ".hgt"):
            try:
                if candidate.suffix.lower() == ".hgt":
                    if candidate.stat().st_size != SIZE_1S:
                        continue
                    tmp = dest.with_suffix(".part")
                    tmp.write_bytes(candidate.read_bytes())
                    os.replace(tmp, dest)
                else:
                    _extract_hgt(candidate.read_bytes(), name, dest, arcsec=1)
                return True
            except Exception:
                continue
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
