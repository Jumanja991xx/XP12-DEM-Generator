"""Elevation sources besides Sonny's DTM, and per-tile source selection.

* USGS 3DEP 1 arc-second (USA, public domain) - downloaded per 1x1 degree
  tile from the USGS S3 bucket, no API key.
* FABDEM V1-2 (University of Bristol, Copernicus GLO-30 with forests and
  buildings removed, ~30 m, non-commercial licence) - the official archive
  ships 10x10 degree zip files; we read only the needed tile out of the
  remote zip with HTTP range requests, or from a zip/tif the user
  downloaded into their own DEM folder.
* Any georeferenced GeoTIFF / SRTM .hgt the user puts into the DEM folder.

All sources are normalised to the same 1" grid (3601 x 3601 posts, SRTM
.hgt layout) so the rest of the app does not care where heights came from.
"""

from __future__ import annotations

import io
import json
import math
import re
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable, Optional

import numpy as np

from .paths import resources_dir
from .regions import Tile, hgt_name

# ---------------------------------------------------------------------------
# Quality classes and per-tile source order (resources/dem_coverage.json)
# ---------------------------------------------------------------------------
LIDAR, NATIONAL, USGS, FABDEM = "L", "N", "U", "F"

QUALITY_TEXT = {
    LIDAR: "LiDAR",
    NATIONAL: "National 5-50 m terrain model",
    USGS: "USGS 3DEP (LiDAR / IfSAR based)",
    FABDEM: "FABDEM (satellite, forests and buildings removed)",
}
RATING = {LIDAR: "best", USGS: "best", NATIONAL: "good", FABDEM: "basic"}

# Default order when a tile has no explicit list.
DEFAULT_CHAIN = ("fabdem", "glo30")


@dataclass(frozen=True)
class TileInfo:
    quality: str  # one of L, N, U, F
    has_sonny: bool
    country: str
    sources: tuple = DEFAULT_CHAIN  # best first; later ones fill gaps
    fine: bool = False  # a source with <= 10 m posts covers this tile (1/3" worth it)

    @property
    def primary(self) -> str:
        return self.sources[0] if self.sources else "fabdem"

    def text(self) -> str:
        from .sources import SOURCES

        src = SOURCES.get(self.primary)
        name = src.label if src else self.primary
        if self.primary == "sonny" and self.quality == FABDEM:
            name = "Sonny's DTM (satellite based here)"
        return name


@lru_cache(maxsize=1)
def coverage() -> dict[Tile, TileInfo]:
    data = json.loads((resources_dir() / "dem_coverage.json").read_text(encoding="utf-8"))
    out = {}
    for row in data["tiles"]:
        lat, lon, code, country = row[:4]
        sources = tuple(row[4].split(",")) if len(row) > 4 and row[4] else None
        fine = bool(row[5]) if len(row) > 5 else False
        if sources is None:  # version 1 file
            if code[0] == USGS:
                sources = ("usgs", "fabdem", "glo30")
            elif code[0] in (LIDAR, NATIONAL):
                sources = ("sonny", "fabdem", "glo30")
            elif code.endswith("s"):
                sources = ("fabdem", "sonny", "glo30")
            else:
                sources = DEFAULT_CHAIN
        out[(int(lat), int(lon))] = TileInfo(code[0], code.endswith("s"), country, sources, fine)
    return out


def tile_info(tile: Tile) -> Optional[TileInfo]:
    return coverage().get(tile)


# ---------------------------------------------------------------------------
# USGS 3DEP
# ---------------------------------------------------------------------------
USGS_URL = ("https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/1/TIFF/current/"
            "{name}/USGS_1_{name}.tif")


def usgs_name(lat: int, lon: int) -> str:
    """3DEP tiles are named after their NORTH-west corner, e.g. n40w106."""
    north = lat + 1
    return (f"{'n' if north >= 0 else 's'}{abs(north):02d}"
            f"{'w' if lon < 0 else 'e'}{abs(lon):03d}")


def usgs_url(lat: int, lon: int) -> str:
    name = usgs_name(lat, lon)
    return USGS_URL.format(name=name)


# ---------------------------------------------------------------------------
# FABDEM
# ---------------------------------------------------------------------------
FABDEM_BASE = "https://data.bris.ac.uk/datasets/s5hqmjcdj8yo2ibzi9b4ew3sn/"
FABDEM_PAGE = "https://data.bris.ac.uk/data/dataset/s5hqmjcdj8yo2ibzi9b4ew3sn"
FABDEM_LICENCE = ("FABDEM V1-2 © University of Bristol, based on Copernicus GLO-30; "
                  "non-commercial licence - free for private use")


def _ll(lat: int, lon: int) -> str:
    return (f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}"
            f"{'E' if lon >= 0 else 'W'}{abs(lon):03d}")


def fabdem_zip_name(lat: int, lon: int) -> str:
    """10x10 degree archive holding a tile, e.g. N40E010-N50E020_FABDEM_V1-2.zip."""
    lat0, lon0 = math.floor(lat / 10) * 10, math.floor(lon / 10) * 10
    return f"{_ll(lat0, lon0)}-{_ll(lat0 + 10, lon0 + 10)}_FABDEM_V1-2.zip"


def fabdem_member(lat: int, lon: int) -> str:
    return f"{hgt_name(lat, lon)}_FABDEM_V1-2.tif"


class NeedsManualDownload(RuntimeError):
    """Raised when a source cannot be fetched automatically; message explains what to do."""


class HTTPRangeFile(io.RawIOBase):
    """Read-only, seekable view of a remote file using HTTP range requests.

    Lets :class:`zipfile.ZipFile` and ``tifffile`` read the directory and a few
    parts of a multi-gigabyte file without downloading all of it. Requests
    start small (64 KB, enough for most headers) and double in size while a
    reader keeps going sequentially, up to 8 MB.
    """

    BLOCK = 64 * 1024  # first request size
    MAX_BLOCK = 8 << 20

    def __init__(self, url: str, session, progress: Optional[Callable[[int, int], None]] = None,
                 cancel=None):
        super().__init__()
        self.url, self.session, self.progress, self.cancel = url, session, progress, cancel
        self.pos = 0
        self.fetched = 0
        self._chunks: list[tuple[int, bytes]] = []
        self._next = self.BLOCK
        response = self._get(0, self.BLOCK - 1)
        if response.status_code == 206:
            match = re.search(r"/(\d+)", response.headers.get("Content-Range", ""))
            if not match:
                raise NeedsManualDownload("server did not report the file size")
            self.size = int(match.group(1))
            self._store(0, response.content)
        elif response.status_code == 200:  # no range support: fine for small files only
            body = response.content
            if len(body) > 64 << 20:
                raise NeedsManualDownload("server does not support partial downloads")
            self.size = len(body)
            self._store(0, body)
        else:
            raise NeedsManualDownload(f"HTTP {response.status_code}")

    def _get(self, start: int, end: int):
        if self.cancel is not None and self.cancel.is_set():
            raise InterruptedError("cancelled")
        return self.session.get(self.url, headers={"Range": f"bytes={start}-{end}"}, timeout=60)

    def _store(self, start: int, body: bytes) -> None:
        self.fetched += len(body)
        if self.progress:
            self.progress(self.fetched, 0)
        self._chunks.append((start, body))
        if len(self._chunks) > 48:  # keep memory bounded
            self._chunks.pop(0)

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self.pos = offset
        elif whence == io.SEEK_CUR:
            self.pos += offset
        else:
            self.pos = self.size + offset
        return self.pos

    def _chunk_at(self, pos: int, want: int) -> tuple[int, bytes]:
        for start, body in reversed(self._chunks):
            if start <= pos < start + len(body):
                return start, body
        size = max(want, self._next)
        self._next = min(self.MAX_BLOCK, self._next * 2)
        end = min(self.size, pos + size) - 1
        response = self._get(pos, end)
        if response.status_code == 206:
            body = response.content
        elif response.status_code == 200:
            body = response.content[pos:end + 1]
        else:
            raise NeedsManualDownload(f"range request answered HTTP {response.status_code}")
        if not body:
            raise OSError("empty response")
        self._store(pos, body)
        return pos, body

    def readinto(self, buffer) -> int:
        buffer = memoryview(buffer).cast("B")  # numpy arrays report items, not bytes
        if self.pos >= self.size:
            return 0
        want = min(len(buffer), self.size - self.pos)
        done = 0
        while done < want:
            start, body = self._chunk_at(self.pos + done, want - done)
            offset = self.pos + done - start
            piece = body[offset: offset + want - done]
            buffer[done: done + len(piece)] = piece
            done += len(piece)
        self.pos += done
        return done


def fabdem_manual_message(lat: int, lon: int, folder_hint: str = "") -> str:
    zip_name = fabdem_zip_name(lat, lon)
    return (f"FABDEM tile {fabdem_member(lat, lon)} could not be downloaded automatically. "
            f"Download '{zip_name}' from {FABDEM_PAGE} (or directly {FABDEM_BASE}{zip_name}) "
            "and put the zip - or the extracted .tif files - into the 'Own DEM folder' "
            f"(step 2, Advanced){folder_hint}, then build the region again.")


# ---------------------------------------------------------------------------
# GeoTIFF -> tile grid (thin wrappers around xp12dem.raster)
# ---------------------------------------------------------------------------
from .raster import Raster as GeoRaster, valid_fraction, write_hgt  # noqa: E402,F401


def read_geotiff(source) -> GeoRaster:
    """Read a whole single-band GeoTIFF (any EPSG projection)."""
    from .raster import TiffSource

    with TiffSource(source) as src:
        epsg, west, north, width, height, w, h = src.georef()
        raster = src.read_window((west, north - height, west + width, north), 0.0)
    if raster is None:
        raise ValueError("empty GeoTIFF")
    return raster


def raster_to_grid(rasters: Iterable[GeoRaster], lat: int, lon: int, n: int = 3601) -> np.ndarray:
    from .raster import resample_to_tile

    return resample_to_tile(list(rasters), lat, lon, n)


# ---------------------------------------------------------------------------
# Finding user-provided files
# ---------------------------------------------------------------------------
_TIF_NAME = re.compile(r"(?i)([ns])(\d{1,2})_?(?:00_)?([ew])(\d{1,3})")


def tile_patterns(lat: int, lon: int) -> list[str]:
    """Lower-case name fragments that identify a tile in common DEM products."""
    sw = hgt_name(lat, lon).lower()  # n47e018 (SRTM, FABDEM, ALOS)
    cop = (f"{'n' if lat >= 0 else 's'}{abs(lat):02d}_00_"
           f"{'e' if lon >= 0 else 'w'}{abs(lon):03d}_00")  # Copernicus
    return [sw, cop, usgs_name(lat, lon)]


def find_local_files(folder: Path, lat: int, lon: int) -> list[Path]:
    """.hgt / .tif / .zip files in ``folder`` whose name points at the tile."""
    if not folder or not folder.is_dir():
        return []
    patterns = tile_patterns(lat, lon)
    found = []
    for path in folder.rglob("*"):
        if not path.is_file():
            continue
        name = path.name.lower()
        if path.suffix.lower() in (".hgt", ".tif", ".tiff", ".zip") and any(p in name for p in patterns):
            found.append(path)
    return found


def find_fabdem_archives(folder: Path, lat: int, lon: int) -> list[Path]:
    if not folder or not folder.is_dir():
        return []
    wanted = fabdem_zip_name(lat, lon).lower()
    return [p for p in folder.rglob("*.zip") if p.name.lower() == wanted]


def read_zip_member_tif(archive, member_suffix: str) -> Optional[GeoRaster]:
    with zipfile.ZipFile(archive) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(member_suffix.lower())]
        if not names:
            return None
        return read_geotiff(io.BytesIO(zf.read(names[0])))
