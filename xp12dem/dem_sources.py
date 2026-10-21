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
# Quality classes (from resources/dem_coverage.json)
# ---------------------------------------------------------------------------
LIDAR, NATIONAL, USGS, FABDEM = "L", "N", "U", "F"

QUALITY_TEXT = {
    LIDAR: "LiDAR (Sonny's DTM)",
    NATIONAL: "National 10-50 m terrain model (Sonny's DTM)",
    USGS: "USGS 3DEP (LiDAR / IfSAR based)",
    FABDEM: "FABDEM (satellite, forests and buildings removed)",
}


@dataclass(frozen=True)
class TileInfo:
    quality: str  # one of L, N, U, F
    has_sonny: bool
    country: str


@lru_cache(maxsize=1)
def coverage() -> dict[Tile, TileInfo]:
    data = json.loads((resources_dir() / "dem_coverage.json").read_text(encoding="utf-8"))
    out = {}
    for lat, lon, code, country in data["tiles"]:
        out[(int(lat), int(lon))] = TileInfo(code[0], code.endswith("s"), country)
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

    Lets :class:`zipfile.ZipFile` read the central directory and a single
    member of a multi-gigabyte archive without downloading all of it.
    """

    BLOCK = 1 << 20

    def __init__(self, url: str, session, progress: Optional[Callable[[int, int], None]] = None,
                 cancel=None):
        super().__init__()
        self.url, self.session, self.progress, self.cancel = url, session, progress, cancel
        head = session.head(url, timeout=30, allow_redirects=True)
        if head.status_code != 200:
            raise NeedsManualDownload(f"HTTP {head.status_code}")
        self.size = int(head.headers.get("Content-Length") or 0)
        if not self.size or head.headers.get("Accept-Ranges", "").lower() != "bytes":
            raise NeedsManualDownload("server does not support partial downloads")
        self.pos = 0
        self._blocks: dict[int, bytes] = {}
        self.fetched = 0

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

    def _block(self, index: int) -> bytes:
        if index not in self._blocks:
            if self.cancel is not None and self.cancel.is_set():
                raise InterruptedError("cancelled")
            start = index * self.BLOCK
            end = min(self.size, start + self.BLOCK) - 1
            response = self.session.get(self.url, headers={"Range": f"bytes={start}-{end}"}, timeout=60)
            if response.status_code != 206:
                raise NeedsManualDownload(f"range request answered HTTP {response.status_code}")
            if len(self._blocks) > 64:  # keep memory bounded (sequential member reads)
                self._blocks.pop(next(iter(self._blocks)))
            self._blocks[index] = response.content
            self.fetched += len(response.content)
            if self.progress:
                self.progress(self.fetched, 0)
        return self._blocks[index]

    def readinto(self, buffer) -> int:
        if self.pos >= self.size:
            return 0
        want = min(len(buffer), self.size - self.pos)
        out = bytearray()
        while len(out) < want:
            index, offset = divmod(self.pos + len(out), self.BLOCK)
            out += self._block(index)[offset: offset + want - len(out)]
        buffer[: len(out)] = out
        self.pos += len(out)
        return len(out)


def fabdem_manual_message(lat: int, lon: int, folder_hint: str = "") -> str:
    zip_name = fabdem_zip_name(lat, lon)
    return (f"FABDEM tile {fabdem_member(lat, lon)} could not be downloaded automatically. "
            f"Download '{zip_name}' from {FABDEM_PAGE} (or directly {FABDEM_BASE}{zip_name}) "
            "and put the zip - or the extracted .tif files - into the 'Own DEM folder' "
            f"(step 2, Advanced){folder_hint}, then build the region again.")


# ---------------------------------------------------------------------------
# GeoTIFF -> 1" grid
# ---------------------------------------------------------------------------
@dataclass
class GeoRaster:
    data: np.ndarray  # float32, NaN = nodata
    x0: float  # longitude of the CENTRE of pixel column 0
    y0: float  # latitude of the CENTRE of pixel row 0
    dx: float
    dy: float  # positive; rows go southwards


def read_geotiff(source) -> GeoRaster:
    """Read a geographic (EPSG:4326/4269) single-band GeoTIFF."""
    import tifffile

    with tifffile.TiffFile(source) as tif:
        page = tif.pages[0]
        tags = {tag.name: tag.value for tag in page.tags.values()}
        scale = tags.get("ModelPixelScaleTag")
        tie = tags.get("ModelTiepointTag")
        if not scale or not tie:
            raise ValueError("GeoTIFF has no georeference")
        geo = tif.geotiff_metadata or {}
        model = geo.get("GTModelTypeGeoKey")
        if model is not None and int(model) != 2:  # 2 = geographic lat/lon
            raise ValueError("only geographic (lat/lon) GeoTIFFs are supported")
        pixel_is_point = int(geo.get("GTRasterTypeGeoKey", 1)) == 2
        data = page.asarray().astype(np.float32)
        if data.ndim == 3:
            data = data[..., 0]
        nodata = tags.get("GDAL_NODATA")
        if nodata not in (None, ""):
            try:
                data[data == float(str(nodata).strip("\x00 "))] = np.nan
            except ValueError:
                pass
        data[(data < -1000) | (data > 9000)] = np.nan
    dx, dy = float(scale[0]), float(scale[1])
    i, j, _, x, y, _ = (float(v) for v in tie[:6])
    x0 = x - i * dx
    y0 = y + j * dy
    if not pixel_is_point:  # tie point is the corner of the pixel
        x0 += dx / 2
        y0 -= dy / 2
    return GeoRaster(data, x0, y0, dx, dy)


def raster_to_grid(rasters: Iterable[GeoRaster], lat: int, lon: int, n: int = 3601) -> np.ndarray:
    """Bilinear resampling onto the n x n posts of a tile (north row first)."""
    grid = np.full((n, n), np.nan, dtype=np.float32)
    lons = lon + np.arange(n) / (n - 1)
    lats = lat + 1 - np.arange(n) / (n - 1)
    for r in rasters:
        h, w = r.data.shape
        col = (lons - r.x0) / r.dx
        row = (r.y0 - lats) / r.dy
        eps = 1e-6  # tile edges sit exactly half a pixel outside the outer pixel centres
        inside_c = (col >= -0.5 - eps) & (col <= w - 0.5 + eps)
        inside_r = (row >= -0.5 - eps) & (row <= h - 0.5 + eps)
        if not inside_c.any() or not inside_r.any():
            continue
        c = np.clip(col, 0, w - 1)
        rr = np.clip(row, 0, h - 1)
        c0 = np.minimum(np.floor(c).astype(np.int64), max(w - 2, 0))
        r0 = np.minimum(np.floor(rr).astype(np.int64), max(h - 2, 0))
        fc = (c - c0)[None, :]
        fr = (rr - r0)[:, None]
        d = r.data
        top = d[np.ix_(r0, c0)] * (1 - fc) + d[np.ix_(r0, c0 + 1)] * fc
        bottom = d[np.ix_(r0 + 1, c0)] * (1 - fc) + d[np.ix_(r0 + 1, c0 + 1)] * fc
        values = top * (1 - fr) + bottom * fr
        mask = inside_r[:, None] & inside_c[None, :] & np.isnan(grid)
        grid[mask] = values[mask]
    return grid


def write_hgt(grid: np.ndarray, dest: Path) -> None:
    out = np.where(np.isnan(grid), -32768, np.round(grid)).astype(">i2")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    out.tofile(tmp)
    tmp.replace(dest)


def valid_fraction(grid: np.ndarray) -> float:
    return float(np.count_nonzero(~np.isnan(grid))) / grid.size


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
