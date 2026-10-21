"""Generic elevation raster handling.

* :class:`TiffSource` reads (Cloud Optimized) GeoTIFFs from disk or over
  HTTP. For remote files only the header and the tiles inside the wanted
  window are downloaded, from the overview level that matches the wanted
  resolution - a 1x1 degree window of an 80 GB national mosaic costs a few MB.
* :func:`resample_to_tile` puts rasters in any projection (EPSG code) onto
  the n x n posts of a 1x1 degree tile (SRTM .hgt layout, north row first).
* :func:`feather_fill` fills the gaps of a better source with a weaker one
  and blends the two over a few hundred metres, so no step is visible where
  e.g. a LiDAR survey ends.
"""

from __future__ import annotations

import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence, Union

import numpy as np

GEOGRAPHIC = 4326
NODATA_HGT = -32768
PLAUSIBLE = (-1000.0, 9000.0)  # metres; everything else is treated as nodata

Progress = Optional[Callable[[int, int], None]]


class Cancelled(Exception):
    pass


@dataclass
class Raster:
    data: np.ndarray  # float32, NaN = nodata, row 0 = top (max y)
    x0: float  # x of the CENTRE of pixel column 0 (CRS units)
    y0: float  # y of the CENTRE of pixel row 0
    dx: float
    dy: float  # positive; rows go towards smaller y
    crs: int = GEOGRAPHIC


# ---------------------------------------------------------------------------
# Projections
# ---------------------------------------------------------------------------
@lru_cache(maxsize=32)
def _to_crs(epsg: int):
    from pyproj import Transformer

    return Transformer.from_crs(GEOGRAPHIC, epsg, always_xy=True)


@lru_cache(maxsize=32)
def _from_crs(epsg: int):
    from pyproj import Transformer

    return Transformer.from_crs(epsg, GEOGRAPHIC, always_xy=True)


def lonlat_to(epsg: int, lon, lat):
    if epsg == GEOGRAPHIC:
        return np.asarray(lon, dtype=np.float64), np.asarray(lat, dtype=np.float64)
    return _to_crs(epsg).transform(lon, lat)


def to_lonlat(epsg: int, x, y):
    if epsg == GEOGRAPHIC:
        return np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64)
    return _from_crs(epsg).transform(x, y)


def tile_bbox_in(epsg: int, lat: int, lon: int, margin_deg: float = 0.01) -> tuple[float, float, float, float]:
    """Bounding box of a 1x1 degree tile (plus margin) in another CRS."""
    t = np.linspace(0, 1, 21)
    w, e, s, n = lon - margin_deg, lon + 1 + margin_deg, lat - margin_deg, lat + 1 + margin_deg
    lons = np.concatenate([w + (e - w) * t, np.full_like(t, e), e - (e - w) * t, np.full_like(t, w)])
    lats = np.concatenate([np.full_like(t, s), s + (n - s) * t, np.full_like(t, n), n - (n - s) * t])
    x, y = lonlat_to(epsg, lons, lats)
    return float(np.min(x)), float(np.min(y)), float(np.max(x)), float(np.max(y))


def metres_per_degree_lat() -> float:
    return 111_320.0


# ---------------------------------------------------------------------------
# GeoTIFF / COG reading
# ---------------------------------------------------------------------------
def _epsg(geo: dict) -> int:
    model = geo.get("GTModelTypeGeoKey")
    model = int(model) if model is not None else 2
    if model == 2:  # geographic: WGS84, NAD83, ETRS89... all close enough at DEM scale
        return GEOGRAPHIC
    code = geo.get("ProjectedCSTypeGeoKey")
    try:
        code = int(code)
    except (TypeError, ValueError):
        code = 0
    if not code or code == 32767:
        raise ValueError("GeoTIFF uses a user-defined projection (no EPSG code)")
    return code


class TiffSource:
    """A GeoTIFF on disk or behind a URL (HTTP range requests)."""

    def __init__(self, src, session=None, cancel: Optional[threading.Event] = None,
                 progress: Progress = None):
        self.src = src
        self.session = session
        self.cancel = cancel
        self.progress = progress
        self.remote = isinstance(src, str) and src.startswith(("http://", "https://"))
        self._owned = not hasattr(src, "read")  # a file object passed in stays open
        self.downloaded = 0
        self._lock = threading.Lock()
        self._fh = None
        self._tif = None

    # -- context ---------------------------------------------------------
    def __enter__(self) -> "TiffSource":
        import tifffile

        if self.remote:
            from .dem_sources import HTTPRangeFile

            self._fh = HTTPRangeFile(self.src, self.session, cancel=self.cancel)
        elif not self._owned:
            self._fh = self.src
        else:
            self._fh = open(self.src, "rb")
        self._tif = tifffile.TiffFile(self._fh)
        if self.remote:
            self.downloaded += self._fh.fetched
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self._tif is not None:
                self._tif.close()
        finally:
            if self._fh is not None and self._owned:
                self._fh.close()

    # -- georeference ----------------------------------------------------
    def georef(self):
        """(epsg, west, north, width_units, height_units, base_w, base_h) of page 0 (corner based)."""
        page = self._tif.pages[0]
        tags = {tag.name: tag.value for tag in page.tags.values()}
        scale, tie = tags.get("ModelPixelScaleTag"), tags.get("ModelTiepointTag")
        if not scale or not tie:
            transform = tags.get("ModelTransformationTag")
            if not transform:
                raise ValueError("GeoTIFF has no georeference")
            m = list(transform)
            if abs(m[1]) > 1e-12 or abs(m[4]) > 1e-12:
                raise ValueError("rotated GeoTIFFs are not supported")
            scale, tie = (m[0], -m[5], 0.0), (0.0, 0.0, 0.0, m[3], m[7], 0.0)
        geo = self._tif.geotiff_metadata or {}
        epsg = _epsg(geo)
        point = int(geo.get("GTRasterTypeGeoKey", 1)) == 2
        sx, sy = float(scale[0]), float(scale[1])
        i, j, _, x, y, _ = (float(v) for v in tie[:6])
        west, north = x - i * sx, y + j * sy
        if point:  # tie point is a pixel centre
            west, north = west - sx / 2, north + sy / 2
        h, w = page.shape[:2]
        return epsg, west, north, w * sx, h * sy, w, h

    def nodata(self) -> Optional[float]:
        page = self._tif.pages[0]
        for tag in page.tags.values():
            if tag.name == "GDAL_NODATA":
                try:
                    return float(str(tag.value).strip("\x00 "))
                except ValueError:
                    return None
        return None

    def bounds_lonlat(self) -> tuple[float, float, float, float]:
        epsg, west, north, width, height, _, _ = self.georef()
        t = np.linspace(0, 1, 11)
        xs = np.concatenate([west + width * t, np.full_like(t, west + width), west + width * t, np.full_like(t, west)])
        ys = np.concatenate([np.full_like(t, north), north - height * t, np.full_like(t, north - height), north - height * t])
        lon, lat = to_lonlat(epsg, xs, ys)
        return float(np.min(lon)), float(np.min(lat)), float(np.max(lon)), float(np.max(lat))

    def _levels(self):
        """Full-resolution page and its reduced-resolution overviews (no masks), fine -> coarse."""
        base = self._tif.pages[0]
        pages = [page for page in self._tif.pages
                 if not getattr(page, "subfiletype", 0) & 4 and page.dtype == base.dtype]
        return sorted(pages or [base], key=lambda page: -page.shape[1])

    # -- reading ---------------------------------------------------------
    def read_window(self, bbox: Sequence[float], resolution: float) -> Optional[Raster]:
        """Pixels inside ``bbox`` (xmin, ymin, xmax, ymax in the file's CRS) from the
        coarsest level whose pixels are not larger than ``resolution`` (CRS units)."""
        epsg, west, north, width, height, base_w, base_h = self.georef()
        levels = self._levels()
        chosen = levels[0]
        for page in levels:  # sorted fine -> coarse
            if width / page.shape[1] <= resolution * 1.01:
                chosen = page
        h, w = chosen.shape[:2]
        dx, dy = width / w, height / h
        xmin, ymin, xmax, ymax = bbox
        c0 = max(0, int(math.floor((xmin - west) / dx)) - 1)
        c1 = min(w, int(math.ceil((xmax - west) / dx)) + 1)
        r0 = max(0, int(math.floor((north - ymax) / dy)) - 1)
        r1 = min(h, int(math.ceil((north - ymin) / dy)) + 1)
        if c0 >= c1 or r0 >= r1:
            return None
        data = self._read_pixels(chosen, r0, r1, c0, c1)
        nodata = self.nodata()
        if nodata is not None:
            data[data == np.float32(nodata)] = np.nan
        data[(data < PLAUSIBLE[0]) | (data > PLAUSIBLE[1])] = np.nan
        return Raster(data, west + (c0 + 0.5) * dx, north - (r0 + 0.5) * dy, dx, dy, epsg)

    def _read_pixels(self, page, r0: int, r1: int, c0: int, c1: int) -> np.ndarray:
        out = np.full((r1 - r0, c1 - c0), np.nan, dtype=np.float32)
        if not page.is_tiled:
            full = page.asarray()
            if full.ndim == 3:
                full = full[..., 0]
            out[:] = full[r0:r1, c0:c1]
            return out
        th, tw = page.tilelength, page.tilewidth
        h, w = page.shape[:2]
        ntx = (w + tw - 1) // tw
        wanted = [(ty, tx) for ty in range(r0 // th, (r1 - 1) // th + 1)
                  for tx in range(c0 // tw, (c1 - 1) // tw + 1)]
        offsets, counts = page.dataoffsets, page.databytecounts
        jobs = [(ty, tx, ty * ntx + tx) for ty, tx in wanted]
        jobs = [(ty, tx, i, offsets[i], counts[i]) for ty, tx, i in jobs if counts[i] > 0]
        blobs = self._fetch_blobs([(o, c) for *_, o, c in jobs])
        for (ty, tx, index, _, _), blob in zip(jobs, blobs):
            segment, _, shape = page.decode(blob, index)
            tile = np.asarray(segment).reshape(shape)  # (depth, length, width, samples)
            while tile.ndim > 2:
                tile = tile[0] if tile.shape[0] == 1 else tile[..., 0]
            tile = tile.astype(np.float32, copy=False)
            y0, x0 = ty * th, tx * tw
            ys, ye = max(y0, r0), min(y0 + tile.shape[0], r1, h)
            xs, xe = max(x0, c0), min(x0 + tile.shape[1], c1, w)
            if ys < ye and xs < xe:
                out[ys - r0:ye - r0, xs - c0:xe - c0] = tile[ys - y0:ye - y0, xs - x0:xe - x0]
        return out

    def _fetch_blobs(self, ranges: list[tuple[int, int]]) -> list[bytes]:
        if not self.remote:
            blobs = []
            for offset, count in ranges:
                self._fh.seek(offset)
                blobs.append(self._fh.read(count))
            return blobs
        # coalesce neighbouring tiles into one request
        order = sorted(range(len(ranges)), key=lambda i: ranges[i][0])
        groups: list[list[int]] = []
        for i in order:
            if groups:
                last = ranges[groups[-1][-1]]
                start = ranges[groups[-1][0]][0]
                if ranges[i][0] - (last[0] + last[1]) < 64 * 1024 and ranges[i][0] + ranges[i][1] - start < 8 << 20:
                    groups[-1].append(i)
                    continue
            groups.append([i])
        result: list[Optional[bytes]] = [None] * len(ranges)

        def get(group: list[int]) -> None:
            if self.cancel is not None and self.cancel.is_set():
                raise Cancelled()
            start = ranges[group[0]][0]
            end = max(ranges[i][0] + ranges[i][1] for i in group) - 1
            body = _ranged_get(self.session, self.src, start, end)
            with self._lock:
                self.downloaded += len(body)
                if self.progress:
                    self.progress(self.downloaded, 0)
            for i in group:
                o, c = ranges[i]
                result[i] = body[o - start:o - start + c]

        if len(groups) == 1:
            get(groups[0])
        else:
            with ThreadPoolExecutor(max_workers=min(8, len(groups))) as pool:
                for future in [pool.submit(get, g) for g in groups]:
                    future.result()
        return [b or b"" for b in result]


def _ranged_get(session, url: str, start: int, end: int, attempts: int = 4) -> bytes:
    import time

    last: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            response = session.get(url, headers={"Range": f"bytes={start}-{end}"}, timeout=60)
            if response.status_code == 206:
                return response.content
            if response.status_code == 200:  # server ignored the range
                return response.content[start:end + 1]
            raise OSError(f"HTTP {response.status_code}")
        except Cancelled:
            raise
        except Exception as exc:  # network hiccup
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise OSError(f"download failed: {last}")


# ---------------------------------------------------------------------------
# Resampling onto a tile
# ---------------------------------------------------------------------------
def tile_axes(lat: int, lon: int, n: int) -> tuple[np.ndarray, np.ndarray]:
    lons = lon + np.arange(n) / (n - 1)
    lats = lat + 1 - np.arange(n) / (n - 1)
    return lons, lats


def _sample(r: Raster, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Bilinear sample (NaN aware: falls back to the nearest valid pixel)."""
    h, w = r.data.shape
    col = (x - r.x0) / r.dx
    row = (r.y0 - y) / r.dy
    inside = (col >= -1.0) & (col <= w) & (row >= -1.0) & (row <= h)  # allow 1 px extrapolation
    out = np.full(x.shape, np.nan, dtype=np.float32)
    if not inside.any() or h < 1 or w < 1:
        return out
    c = np.clip(col[inside], 0, w - 1)
    rr = np.clip(row[inside], 0, h - 1)
    if w == 1 or h == 1:
        out[inside] = r.data[np.round(rr).astype(int), np.round(c).astype(int)]
        return out
    c0 = np.minimum(np.floor(c).astype(np.int64), w - 2)
    r0 = np.minimum(np.floor(rr).astype(np.int64), h - 2)
    fc, fr = c - c0, rr - r0
    d = r.data
    v = (d[r0, c0] * (1 - fc) * (1 - fr) + d[r0, c0 + 1] * fc * (1 - fr)
         + d[r0 + 1, c0] * (1 - fc) * fr + d[r0 + 1, c0 + 1] * fc * fr)
    bad = np.isnan(v)
    if bad.any():
        near = d[np.round(rr[bad]).astype(np.int64), np.round(c[bad]).astype(np.int64)]
        v[bad] = near
    out[inside] = v
    return out


def resample_to_tile(rasters: Iterable[Raster], lat: int, lon: int, n: int,
                     grid: Optional[np.ndarray] = None) -> np.ndarray:
    """Fill the NaN posts of ``grid`` (n x n, north row first) from ``rasters``
    in the given order. Rasters may use any CRS."""
    if grid is None:
        grid = np.full((n, n), np.nan, dtype=np.float32)
    lons, lats = tile_axes(lat, lon, n)
    rasters = [r for r in rasters if r is not None and r.data.size]
    if not rasters:
        return grid
    by_crs: dict[int, list[Raster]] = {}
    for r in rasters:
        by_crs.setdefault(r.crs, []).append(r)
    order = [r.crs for r in rasters]
    crs_order = list(dict.fromkeys(order))
    coarse = min(n, 121)
    ci = np.linspace(0, n - 1, coarse)
    for epsg in crs_order:
        if epsg != GEOGRAPHIC:
            clon, clat = np.meshgrid(lon + ci / (n - 1), lat + 1 - ci / (n - 1))
            cx, cy = lonlat_to(epsg, clon, clat)
        step = 1024
        for row0 in range(0, n, step):
            rows = np.arange(row0, min(n, row0 + step))
            if epsg == GEOGRAPHIC:
                x = np.broadcast_to(lons[None, :], (len(rows), n))
                y = np.broadcast_to(lats[rows][:, None], (len(rows), n))
            else:  # projection is smooth: interpolate a coarse transformed grid
                x = _interp2(cx, ci, rows, n)
                y = _interp2(cy, ci, rows, n)
            block = grid[row0:row0 + len(rows)]
            for r in by_crs[epsg]:
                todo = np.isnan(block)
                if not todo.any():
                    break
                values = _sample(r, x[todo], y[todo])
                block[todo] = values
    return grid


def _interp2(coarse: np.ndarray, ci: np.ndarray, rows: np.ndarray, n: int) -> np.ndarray:
    """Bilinear interpolation of a coarse (m x m) array onto rows x n posts."""
    cols = np.arange(n, dtype=np.float64)
    # interpolate along columns for every coarse row, then along rows
    across = np.empty((coarse.shape[0], n))
    for i in range(coarse.shape[0]):
        across[i] = np.interp(cols, ci, coarse[i])
    pos = np.interp(rows.astype(np.float64), ci, np.arange(len(ci), dtype=np.float64))
    i0 = np.minimum(np.floor(pos).astype(np.int64), len(ci) - 2)
    f = (pos - i0)[:, None]
    return across[i0] * (1 - f) + across[i0 + 1] * f


# ---------------------------------------------------------------------------
# Combining sources
# ---------------------------------------------------------------------------
def feather_fill(primary: np.ndarray, secondary: np.ndarray, width_px: int) -> np.ndarray:
    """Fill NaN posts of ``primary`` from ``secondary`` and blend the two over
    ``width_px`` posts next to the filled gaps (in place, returns primary)."""
    gap = np.isnan(primary) & ~np.isnan(secondary)
    if not gap.any():
        return primary
    primary[gap] = secondary[gap]
    if width_px <= 0:
        return primary
    # distance (in posts, up to width_px) from the filled gaps
    dist = np.full(primary.shape, width_px, dtype=np.int16)
    frontier = gap.copy()
    reached = gap.copy()
    for step in range(1, width_px):
        grown = frontier.copy()
        grown[1:] |= frontier[:-1]
        grown[:-1] |= frontier[1:]
        grown[:, 1:] |= frontier[:, :-1]
        grown[:, :-1] |= frontier[:, 1:]
        new = grown & ~reached
        if not new.any():
            break
        dist[new] = step
        reached |= new
        frontier = new
    dist[gap] = 0
    blend = reached & ~gap & ~np.isnan(secondary)
    if blend.any():
        w = (dist[blend].astype(np.float32) / width_px)
        w = w * w * (3 - 2 * w)  # smoothstep
        primary[blend] = primary[blend] * w + secondary[blend] * (1 - w)
    return primary


def valid_fraction(grid: np.ndarray) -> float:
    return float(np.count_nonzero(~np.isnan(grid))) / grid.size


def upsample(grid: np.ndarray, n: int) -> np.ndarray:
    """Bilinear resize of a tile grid (posts at the same tile corners)."""
    m = grid.shape[0]
    if m == n:
        return grid
    src = Raster(grid, 0.0, 1.0, 1.0 / (m - 1), 1.0 / (m - 1))
    lons, lats = tile_axes(0, 0, n)
    out = np.empty((n, n), dtype=np.float32)
    for row0 in range(0, n, 1024):
        rows = slice(row0, min(n, row0 + 1024))
        x = np.broadcast_to(lons[None, :], (lats[rows].size, n))
        y = np.broadcast_to(lats[rows][:, None], (lats[rows].size, n))
        out[rows] = _sample(src, x, y)
    return out


def write_hgt(grid: np.ndarray, dest: Path) -> None:
    out = np.where(np.isnan(grid), NODATA_HGT, np.round(grid)).astype(">i2")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    out.tofile(tmp)
    os.replace(tmp, dest)


def read_hgt(path: Path) -> np.ndarray:
    size = int(round(math.sqrt(path.stat().st_size / 2)))
    grid = np.fromfile(path, ">i2").reshape(size, size).astype(np.float32)
    grid[grid <= -32000] = np.nan
    return grid
