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


class DemStore:
    """Local cache of DEM tiles at the resolutions we offer."""

    def __init__(self, cache_dir: Path, local_source: Optional[Path] = None,
                 session: Optional[requests.Session] = None):
        self.cache_dir = Path(cache_dir)
        self.local_source = Path(local_source) if local_source else None
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "XP12-DEM-Generator")

    # -- paths -----------------------------------------------------------
    def path(self, lat: int, lon: int, arcsec: int = 1) -> Path:
        return self.cache_dir / f"{arcsec}s" / f"{hgt_name(lat, lon)}.hgt"

    def has(self, lat: int, lon: int, arcsec: int = 1) -> bool:
        return _valid_hgt(self.path(lat, lon, arcsec), arcsec)

    # -- public API ------------------------------------------------------
    def ensure(self, lat: int, lon: int, arcsec: int,
               progress: Optional[ProgressFn] = None,
               cancel: Optional[threading.Event] = None) -> Path:
        """Return a valid local ``.hgt`` for the tile, downloading if needed."""
        target = self.path(lat, lon, arcsec)
        if _valid_hgt(target, arcsec):
            return target
        source = self.path(lat, lon, 1)
        if not _valid_hgt(source, 1):
            self._fetch_1s(lat, lon, source, progress, cancel)
        if arcsec == 1:
            return source
        if arcsec == 3:
            downsample_to_3s(source, target)
            return target
        raise ValueError(f"Unsupported resolution: {arcsec}")

    # -- internals -------------------------------------------------------
    def _fetch_1s(self, lat: int, lon: int, dest: Path,
                  progress: Optional[ProgressFn], cancel: Optional[threading.Event]) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if self.local_source and self._import_local(lat, lon, dest):
            return
        name = hgt_name(lat, lon)
        url = MIRROR_URL.format(name=name)
        last_error: Optional[Exception] = None
        for attempt in range(4):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            try:
                payload = self._download(url, progress, cancel)
                _extract_hgt(payload, name, dest, arcsec=1)
                return
            except Cancelled:
                raise
            except Exception as exc:  # network hiccup, truncated zip, ...
                last_error = exc
                time.sleep(2 ** attempt)
        raise RuntimeError(f"Could not download DEM tile {name}: {last_error}")

    def _download(self, url: str, progress: Optional[ProgressFn],
                  cancel: Optional[threading.Event]) -> bytes:
        with self.session.get(url, stream=True, timeout=60) as response:
            if response.status_code == 404:
                raise FileNotFoundError(f"{url} not found (tile outside Sonny's coverage?)")
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

    def _import_local(self, lat: int, lon: int, dest: Path) -> bool:
        """Use a 1" .hgt (or .zip containing one) from the user's own folder."""
        name = hgt_name(lat, lon)
        assert self.local_source is not None
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
