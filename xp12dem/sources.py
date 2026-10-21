"""Elevation data sources.

Every source turns "1x1 degree tile at n x n posts" into a height grid
(float32, NaN where it has no data). :class:`xp12dem.dem.DemStore` chains
them per tile - best first, weaker sources only fill the gaps - using the
order stored in ``resources/dem_coverage.json``.

All sources are free and need no account or API key.
"""

from __future__ import annotations

import io
import json
import math
import re
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from xml.etree import ElementTree

import numpy as np

from . import raster as R
from .raster import Cancelled
from .regions import hgt_name

INDEX_MAX_AGE = 30 * 86400  # refresh downloaded file indexes monthly


class NotAvailable(Exception):
    """The source has no data for this tile (not an error)."""


@dataclass
class Context:
    session: object
    cache_dir: Path  # DEM cache root (indexes live in cache_dir/index)
    local_folder: Optional[Path] = None
    cancel: Optional[threading.Event] = None
    progress: Optional[Callable[[int, int], None]] = None
    log: Callable[[str], None] = lambda text: None
    _downloaded: int = 0

    def check(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled()

    def add_bytes(self, count: int) -> None:
        self._downloaded += count
        if self.progress:
            self.progress(self._downloaded, 0)

    # -- small cached JSON/text indexes ---------------------------------
    def index_path(self, name: str) -> Path:
        return self.cache_dir / "index" / name

    def cached_text(self, name: str, url: str) -> str:
        path = self.index_path(name)
        if path.is_file() and time.time() - path.stat().st_mtime < INDEX_MAX_AGE:
            return path.read_text(encoding="utf-8")
        response = self.session.get(url, timeout=60)
        if response.status_code != 200:
            if path.is_file():
                return path.read_text(encoding="utf-8")
            raise OSError(f"HTTP {response.status_code} for {url}")
        text = response.content.decode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return text


def spacing_m(n: int) -> float:
    """Distance between posts (north-south) for an n x n tile."""
    return R.metres_per_degree_lat() / (n - 1)


def _read_remote(ctx: Context, url: str, _epsg_hint: Optional[int], lat: int, lon: int,
                 resolution_m: float) -> Optional[R.Raster]:
    """Window of a remote COG around the tile at about ``resolution_m``."""
    ctx.check()
    try:
        with R.TiffSource(url, ctx.session, cancel=ctx.cancel) as src:
            epsg = src.georef()[0]
            bbox = R.tile_bbox_in(epsg, lat, lon)
            res = resolution_m / R.metres_per_degree_lat() if epsg == R.GEOGRAPHIC else resolution_m
            result = src.read_window(bbox, res)
            ctx.add_bytes(src.downloaded)
            return result
    except Cancelled:
        raise
    except Exception as exc:
        from .dem_sources import NeedsManualDownload

        if isinstance(exc, NeedsManualDownload) and ("404" in str(exc) or "403" in str(exc)):
            return None  # no such file (S3 answers 403 for missing keys) = no data here
        raise


def _parallel(ctx: Context, jobs: list, worker, threads: int = 8) -> list:
    if not jobs:
        return []
    results = [None] * len(jobs)
    with ThreadPoolExecutor(max_workers=min(threads, len(jobs))) as pool:
        futures = {pool.submit(worker, job): i for i, job in enumerate(jobs)}
        for future, i in futures.items():
            results[i] = future.result()
    ctx.check()
    return results


class Source:
    key = ""
    label = ""
    licence = ""
    native_m = 30.0  # finest native post spacing (decides if 1/3" is worth it)

    def fetch(self, ctx: Context, lat: int, lon: int, n: int) -> np.ndarray:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Sonny's LiDAR DTM Europe (1")
# ---------------------------------------------------------------------------
class SonnySource(Source):
    key = "sonny"
    label = "Sonny's LiDAR DTM"
    licence = ("Elevation data: Sonny's LiDAR Digital Terrain Models of Europe "
               "(https://sonny.4lima.de), CC BY 4.0")
    native_m = 30.0
    URL = "https://raw.githubusercontent.com/mg4gh/hgtdata/main/docs/{name}.zip"

    def fetch(self, ctx, lat, lon, n):
        name = hgt_name(lat, lon)
        data = self._local(ctx, name)
        if data is None:
            response = _download(ctx, self.URL.format(name=name))
            if response is None:
                raise NotAvailable("tile outside Sonny's coverage")
            data = _hgt_from_zip(response, name)
        grid = np.frombuffer(data, ">i2").reshape(3601, 3601).astype(np.float32)
        grid[grid <= -32000] = np.nan
        return R.upsample(grid, n)

    @staticmethod
    def _local(ctx, name: str) -> Optional[bytes]:
        folder = ctx.local_folder
        if not folder or not folder.is_dir():
            return None
        for path in folder.rglob("*"):
            if path.is_file() and path.stem.upper() == name and path.suffix.lower() == ".zip":
                try:
                    return _hgt_from_zip(path.read_bytes(), name)
                except Exception:
                    continue
        return None


def _hgt_from_zip(payload: bytes, name: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = [m for m in archive.namelist()
                   if Path(m).stem.upper() == name and m.lower().endswith(".hgt")]
        if not members:
            raise ValueError(f"{name}.hgt missing from archive")
        data = archive.read(members[0])
    if len(data) != 3601 * 3601 * 2:
        raise ValueError(f"{name}.hgt has unexpected size {len(data)}")
    return data


def _download(ctx: Context, url: str, attempts: int = 4) -> Optional[bytes]:
    """Whole file, or None if the server says it does not exist."""
    last: Optional[Exception] = None
    for attempt in range(attempts):
        ctx.check()
        try:
            with ctx.session.get(url, stream=True, timeout=60) as response:
                if response.status_code in (403, 404):
                    return None
                response.raise_for_status()
                total = int(response.headers.get("Content-Length") or 0)
                buf = io.BytesIO()
                for chunk in response.iter_content(chunk_size=1 << 16):
                    ctx.check()
                    buf.write(chunk)
                    if ctx.progress:
                        ctx.progress(ctx._downloaded + buf.tell(), 0)
                if total and buf.tell() < total:
                    raise OSError("download incomplete")
                ctx._downloaded += buf.tell()
                return buf.getvalue()
        except Cancelled:
            raise
        except Exception as exc:
            last = exc
            time.sleep(2 ** attempt)
    raise OSError(f"download failed: {last}")


# ---------------------------------------------------------------------------
# USGS 3DEP (USA) - 1" and 1/3"
# ---------------------------------------------------------------------------
class UsgsSource(Source):
    key = "usgs"
    label = "USGS 3DEP"
    licence = "Elevation data: U.S. Geological Survey 3D Elevation Program (public domain)"
    native_m = 10.0
    URL = ("https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/{product}/TIFF/current/"
           "{name}/USGS_{product}_{name}.tif")

    @staticmethod
    def tile_name(lat: int, lon: int) -> str:
        north = lat + 1  # 3DEP tiles are named after their NORTH-west corner
        return (f"{'n' if north >= 0 else 's'}{abs(north):02d}"
                f"{'w' if lon < 0 else 'e'}{abs(lon):03d}")

    def url(self, lat: int, lon: int, product: str = "1") -> str:
        return self.URL.format(product=product, name=self.tile_name(lat, lon))

    def fetch(self, ctx, lat, lon, n):
        products = ["13", "1"] if n > 3601 else ["1"]
        for product in products:
            r = _read_remote(ctx, self.url(lat, lon, product), None, lat, lon, spacing_m(n) * 0.9)
            if r is not None:
                grid = R.resample_to_tile([r], lat, lon, n)
                if R.valid_fraction(grid) > 0.01:
                    return grid
        raise NotAvailable("no USGS 3DEP tile here")


# ---------------------------------------------------------------------------
# FABDEM (worldwide, bare earth, 30 m) - remote zip read with range requests
# ---------------------------------------------------------------------------
class FabdemSource(Source):
    key = "fabdem"
    label = "FABDEM V1-2"
    licence = ("Elevation data: FABDEM V1-2 (c) University of Bristol, based on Copernicus "
               "GLO-30 - non-commercial licence")
    native_m = 30.0

    def fetch(self, ctx, lat, lon, n):
        from .dem_sources import (FABDEM_BASE, HTTPRangeFile, NeedsManualDownload,
                                  fabdem_manual_message, fabdem_member, fabdem_zip_name,
                                  find_fabdem_archives, read_zip_member_tif)

        member = fabdem_member(lat, lon)
        raster = None
        if ctx.local_folder:
            for tif in ctx.local_folder.rglob("*.tif"):
                if tif.name.lower() == member.lower():
                    raster = _local_raster(tif)
                    break
            if raster is None:
                for archive in find_fabdem_archives(ctx.local_folder, lat, lon):
                    raster = read_zip_member_tif(archive, member)
                    if raster is not None:
                        break
        if raster is None:
            try:
                remote = HTTPRangeFile(FABDEM_BASE + fabdem_zip_name(lat, lon), ctx.session,
                                       ctx.progress, ctx.cancel)
                raster = read_zip_member_tif(remote, member)
                ctx.add_bytes(remote.fetched)
            except NeedsManualDownload:
                raise NeedsManualDownload(fabdem_manual_message(lat, lon))
            except InterruptedError:
                raise Cancelled()
            except Exception as exc:
                raise NeedsManualDownload(fabdem_manual_message(lat, lon)) from exc
            if raster is None:
                raise NotAvailable(f"{member} is not part of FABDEM (sea?)")
        return R.resample_to_tile([raster], lat, lon, n)


def _local_raster(path: Path) -> Optional[R.Raster]:
    with R.TiffSource(path) as src:
        epsg, west, north, width, height, w, h = src.georef()
        return src.read_window((west, north - height, west + width, north), 0.0)


# ---------------------------------------------------------------------------
# Copernicus GLO-30 (worldwide surface model, 30 m) - final gap filler
# ---------------------------------------------------------------------------
class Glo30Source(Source):
    key = "glo30"
    label = "Copernicus GLO-30"
    licence = ("Elevation data: Copernicus DEM GLO-30 (c) DLR e.V. 2010-2014 and (c) Airbus "
               "Defence and Space GmbH 2014-2018, provided under COPERNICUS by the European "
               "Union and ESA; all rights reserved")
    native_m = 30.0
    URL = ("https://copernicus-dem-30m.s3.amazonaws.com/Copernicus_DSM_COG_10_{ns}{lat:02d}_00_"
           "{ew}{lon:03d}_00_DEM/Copernicus_DSM_COG_10_{ns}{lat:02d}_00_{ew}{lon:03d}_00_DEM.tif")

    def url(self, lat: int, lon: int) -> str:
        return self.URL.format(ns="N" if lat >= 0 else "S", lat=abs(lat),
                               ew="E" if lon >= 0 else "W", lon=abs(lon))

    def fetch(self, ctx, lat, lon, n):
        rasters = []
        # neighbouring tiles supply the outermost row/column (GLO-30 tiles stop one post short)
        for dlat, dlon in ((0, 0), (0, 1), (-1, 0)):
            r = _read_remote(ctx, self.url(lat + dlat, lon + dlon), None, lat, lon, spacing_m(n) * 0.9)
            if r is not None:
                rasters.append(r)
        if not rasters:
            raise NotAvailable("no Copernicus tile here (sea)")
        return R.resample_to_tile(rasters, lat, lon, n)


# ---------------------------------------------------------------------------
# New Zealand - LINZ LiDAR 1 m + national 8 m DEM
# ---------------------------------------------------------------------------
class LinzSource(Source):
    key = "linz"
    label = "LINZ LiDAR 1 m (New Zealand)"
    licence = "Elevation data: Toitū Te Whenua Land Information New Zealand, CC BY 4.0"
    native_m = 1.0
    BASE = "https://nz-elevation.s3.ap-southeast-2.amazonaws.com/"
    EPSG = 2193
    LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # Topo50 sheet rows skip I and O
    X0, Y0, SW, SH = 988000.0, 6810000.0, 24000.0, 36000.0

    def sheet(self, x: float, y: float) -> str:
        col = int((x - self.X0) // self.SW)
        row = int((self.Y0 - y) // self.SH)
        return f"{self.LETTERS[row // 24]}{self.LETTERS[row % 24]}{col:02d}"

    def subsheet(self, x: float, y: float) -> str:
        col = int((x - self.X0) // self.SW)
        row = int((self.Y0 - y) // self.SH)
        left, top = self.X0 + col * self.SW, self.Y0 - row * self.SH
        r = int((top - y) // 7200) + 1
        c = int((x - left) // 4800) + 1
        return f"{self.sheet(x, y)}_10000_{r:02d}{c:02d}"

    def _sheets(self, bbox, sub: bool) -> set[str]:
        xmin, ymin, xmax, ymax = bbox
        step_x, step_y = (4800.0, 7200.0) if sub else (self.SW, self.SH)
        names = set()
        y = ymax
        while y > ymin - step_y:
            x = xmin
            while x < xmax + step_x:
                try:
                    names.add(self.subsheet(x, y) if sub else self.sheet(x, y))
                except IndexError:
                    pass
                x += step_x
            y -= step_y
        return names

    def collections(self, ctx: Context) -> list[dict]:
        """dem_1m surveys: [{"href", "title", "year", "items": [...]}], newest first."""
        path = ctx.index_path("linz_dem.json")
        if path.is_file() and time.time() - path.stat().st_mtime < INDEX_MAX_AGE:
            return json.loads(path.read_text(encoding="utf-8"))
        catalog = json.loads(ctx.cached_text("linz_catalog.json", self.BASE + "catalog.json"))
        children = [l for l in catalog["links"] if l.get("rel") == "child"
                    and "/dem_1m/" in l["href"]]

        def load(link):
            url = self.BASE + link["href"][2:]
            response = ctx.session.get(url, timeout=60)
            if response.status_code != 200:
                return None
            data = json.loads(response.content)
            years = re.findall(r"(\d{4})\)?\s*$", link.get("title", "")) or re.findall(r"\d{4}", link["href"])
            items = [l["href"][2:-5] for l in data.get("links", []) if l.get("rel") == "item"]
            return {"href": url.rsplit("/", 1)[0] + "/", "title": link.get("title", ""),
                    "year": int(years[-1]) if years else 0, "items": items,
                    "bbox": data.get("extent", {}).get("spatial", {}).get("bbox", [[]])[0]}

        found = [c for c in _parallel(ctx, children, load) if c]
        found.sort(key=lambda c: -c["year"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(found), encoding="utf-8")
        return found

    def fetch(self, ctx, lat, lon, n):
        bbox = R.tile_bbox_in(self.EPSG, lat, lon)
        want = spacing_m(n) * 0.9
        rasters: list[R.Raster] = []
        subs = self._sheets(bbox, True)
        jobs = []
        for coll in self.collections(ctx):
            b = coll.get("bbox") or []
            if len(b) == 4 and (b[2] < lon or b[0] > lon + 1 or b[3] < lat or b[1] > lat + 1):
                continue
            for item in coll["items"]:
                if item in subs:
                    jobs.append(coll["href"] + item + ".tiff")
        lidar = [r for r in _parallel(ctx, jobs, lambda url: _read_remote(ctx, url, self.EPSG, lat, lon, want),
                                      threads=16) if r is not None]
        rasters += lidar  # newest survey first
        sheets = self._sheets(bbox, False)
        base = self.BASE + "new-zealand/new-zealand-contour/dem_8m/2193/"
        rasters += [r for r in _parallel(ctx, sorted(sheets),
                                         lambda s: _read_remote(ctx, base + s + ".tiff", self.EPSG, lat, lon, want))
                    if r is not None]
        if not rasters:
            raise NotAvailable("outside New Zealand")
        grid = R.resample_to_tile(rasters, lat, lon, n)
        if R.valid_fraction(grid) < 0.001:
            raise NotAvailable("outside New Zealand")
        return grid


# ---------------------------------------------------------------------------
# Canada - NRCan HRDEM LiDAR mosaic (2 m) and MRDEM (30 m, nationwide)
# ---------------------------------------------------------------------------
class HrdemSource(Source):
    key = "hrdem"
    label = "NRCan HRDEM LiDAR (Canada)"
    licence = "Elevation data: Natural Resources Canada, Open Government Licence - Canada"
    native_m = 2.0
    BASE = "https://canelevation-dem.s3.ca-central-1.amazonaws.com/"
    EPSG = 3979

    def fetch(self, ctx, lat, lon, n):
        from shapely.geometry import box, shape

        index = json.loads(ctx.cached_text("hrdem_tiles.geojson", self.BASE + "mosaic_tile_index.geojson"))
        bbox = R.tile_bbox_in(self.EPSG, lat, lon)
        area = box(*bbox)
        ids = [f["properties"]["id"] for f in index["features"] if shape(f["geometry"]).intersects(area)]
        want = spacing_m(n) * 0.9
        rasters = [r for r in _parallel(
            ctx, ids, lambda i: _read_remote(ctx, f"{self.BASE}hrdem-mosaic-2m/{i}-mosaic-2m-dtm.tif",
                                             self.EPSG, lat, lon, want), threads=2) if r is not None]
        grid = R.resample_to_tile(rasters, lat, lon, n)
        if R.valid_fraction(grid) < 0.001:
            raise NotAvailable("no HRDEM LiDAR here")
        return grid


class MrdemSource(Source):
    key = "mrdem"
    label = "NRCan MRDEM 30 m (Canada)"
    licence = "Elevation data: Natural Resources Canada, Open Government Licence - Canada"
    native_m = 30.0
    URL = "https://canelevation-dem.s3.ca-central-1.amazonaws.com/mrdem-30/mrdem-30-dtm.tif"

    def fetch(self, ctx, lat, lon, n):
        r = _read_remote(ctx, self.URL, 3979, lat, lon, spacing_m(n) * 0.9)
        grid = R.resample_to_tile([r], lat, lon, n)
        if R.valid_fraction(grid) < 0.001:
            raise NotAvailable("outside Canada")
        return grid


# ---------------------------------------------------------------------------
# ArcticDEM (> 60 N) - 10 m surface model, ellipsoidal heights
# ---------------------------------------------------------------------------
class ArcticSource(Source):
    key = "arcticdem"
    label = "ArcticDEM 10 m"
    licence = ("Elevation data: ArcticDEM, Polar Geospatial Center (NSF-OPP awards 1043681, "
               "1559691, 1542736), CC BY 4.0")
    native_m = 10.0
    BASE = "https://pgc-opendata-dems.s3.us-west-2.amazonaws.com/"
    VRT = BASE + "arcticdem/mosaics/v4.1/10m_dem_tiles.vrt"
    EPSG = 3413

    def tiles(self, ctx) -> list[tuple[str, float, float, float, float]]:
        """(url, xmin, ymin, xmax, ymax) of every 10 m mosaic file."""
        text = ctx.cached_text("arcticdem_10m.vrt", self.VRT)
        root = ElementTree.fromstring(text)
        gt = [float(v) for v in root.findtext("GeoTransform").split(",")]
        out = []
        for src in root.iter("ComplexSource"):
            name = src.findtext("SourceFilename")
            rect = src.find("DstRect")
            if not name or rect is None:
                continue
            xo, yo = float(rect.get("xOff")), float(rect.get("yOff"))
            xs, ys = float(rect.get("xSize")), float(rect.get("ySize"))
            xmin = gt[0] + xo * gt[1]
            ymax = gt[3] + yo * gt[5]
            out.append((name.replace("/vsis3/pgc-opendata-dems/", self.BASE),
                        xmin, ymax + ys * gt[5], xmin + xs * gt[1], ymax))
        return out

    def fetch(self, ctx, lat, lon, n):
        xmin, ymin, xmax, ymax = R.tile_bbox_in(self.EPSG, lat, lon)
        urls = [t[0] for t in self.tiles(ctx)
                if t[1] < xmax and t[3] > xmin and t[2] < ymax and t[4] > ymin]
        if not urls:
            raise NotAvailable("outside ArcticDEM")
        want = spacing_m(n) * 0.9
        rasters = [r for r in _parallel(ctx, urls, lambda u: _read_remote(ctx, u, self.EPSG, lat, lon, want),
                                        threads=4) if r is not None]
        grid = R.resample_to_tile(rasters, lat, lon, n)
        if R.valid_fraction(grid) < 0.001:
            raise NotAvailable("no ArcticDEM data here")
        return self._to_sea_level(ctx, grid, lat, lon)

    @staticmethod
    def _to_sea_level(ctx, grid: np.ndarray, lat: int, lon: int) -> np.ndarray:
        """ArcticDEM heights refer to the WGS84 ellipsoid; X-Plane wants heights above
        sea level. The geoid offset (30-70 m up here) is measured against Copernicus
        GLO-30 (EGM2008 heights) in blocks, smoothed and subtracted."""
        n = grid.shape[0]
        reference = Glo30Source().fetch(ctx, lat, lon, 121)
        coarse = grid[:: max(1, (n - 1) // 120), :: max(1, (n - 1) // 120)][:121, :121]
        diff = coarse - reference
        blocks = 6
        size = 121 // blocks
        offsets = np.full((blocks, blocks), np.nan)
        for i in range(blocks):
            for j in range(blocks):
                cell = diff[i * size:(i + 1) * size, j * size:(j + 1) * size]
                cell = cell[np.isfinite(cell)]
                if cell.size > 20:
                    offsets[i, j] = float(np.median(cell))
        if not np.isfinite(offsets).any():
            raise NotAvailable("could not convert ArcticDEM heights to sea level here")
        offsets[~np.isfinite(offsets)] = np.nanmedian(offsets)
        return grid - R.upsample(offsets.astype(np.float32), n)


# ---------------------------------------------------------------------------
# Japan - GSI elevation tiles (5-10 m)
# ---------------------------------------------------------------------------
class GsiSource(Source):
    key = "gsi"
    label = "GSI 5-10 m (Japan)"
    licence = "Elevation data: Geospatial Information Authority of Japan (GSI) elevation tiles"
    native_m = 5.0
    URL = "https://cyberjapandata.gsi.go.jp/xyz/dem_png/{z}/{x}/{y}.png"
    EARTH = 20037508.342789244

    def fetch(self, ctx, lat, lon, n):
        from PIL import Image

        z = 14 if n > 3601 else 13
        tiles = 2 ** z

        def tx(lo):
            return int((lo + 180.0) / 360.0 * tiles)

        def ty(la):
            rad = math.radians(la)
            return int((1.0 - math.asinh(math.tan(rad)) / math.pi) / 2.0 * tiles)

        x0, x1 = tx(lon - 0.002), tx(lon + 1.002)
        y0, y1 = ty(lat + 1.002), ty(lat - 0.002)
        jobs = [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]
        mosaic = np.full(((y1 - y0 + 1) * 256, (x1 - x0 + 1) * 256), np.nan, dtype=np.float32)

        def load(job):
            x, y = job
            data = _download(ctx, self.URL.format(z=z, x=x, y=y), attempts=3)
            if data is None:
                return
            rgb = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"), dtype=np.int64)
            value = rgb[..., 0] * 65536 + rgb[..., 1] * 256 + rgb[..., 2]
            heights = np.where(value < 2 ** 23, value, value - 2 ** 24).astype(np.float32) * 0.01
            heights[value == 2 ** 23] = np.nan
            mosaic[(y - y0) * 256:(y - y0 + 1) * 256, (x - x0) * 256:(x - x0 + 1) * 256] = heights

        _parallel(ctx, jobs, load, threads=16)
        if not np.isfinite(mosaic).any():
            raise NotAvailable("outside Japan")
        res = 2 * self.EARTH / (256 * tiles)
        r = R.Raster(mosaic, -self.EARTH + (x0 * 256 + 0.5) * res,
                     self.EARTH - (y0 * 256 + 0.5) * res, res, res, 3857)
        return R.resample_to_tile([r], lat, lon, n)


# ---------------------------------------------------------------------------
# Australia - Geoscience Australia 1" DEM-H (WCS)
# ---------------------------------------------------------------------------
class GaSource(Source):
    key = "ga"
    label = "Geoscience Australia 1\" DEM-H"
    licence = "Elevation data: Geoscience Australia, CC BY 4.0"
    native_m = 30.0
    SERVICE = ("https://services.ga.gov.au/gis/services/DEM_SRTM_1Second_Hydro_Enforced/"
               "MapServer/WCSServer")

    def fetch(self, ctx, lat, lon, n):
        caps = ctx.cached_text("ga_wcs.xml", self.SERVICE + "?SERVICE=WCS&VERSION=1.0.0&REQUEST=GetCapabilities")
        names = re.findall(r"<(?:\w+:)?name>([^<]+)</(?:\w+:)?name>", caps)
        coverage = names[0] if names else "1"
        url = (f"{self.SERVICE}?SERVICE=WCS&VERSION=1.0.0&REQUEST=GetCoverage&COVERAGE={coverage}"
               f"&CRS=EPSG:4326&BBOX={lon - 0.01},{lat - 0.01},{lon + 1.01},{lat + 1.01}"
               f"&WIDTH=3673&HEIGHT=3673&FORMAT=GeoTIFF")
        data = _download(ctx, url, attempts=3)
        if not data or not data.startswith((b"II", b"MM")):
            raise NotAvailable("Geoscience Australia service returned no data")
        with R.TiffSource(io.BytesIO(data)) as src:
            epsg, west, north, width, height, w, h = src.georef()
            r = src.read_window((west, north - height, west + width, north), 0.0)
        grid = R.resample_to_tile([r], lat, lon, n)
        if R.valid_fraction(grid) < 0.01:
            raise NotAvailable("outside Australia")
        return grid


# ---------------------------------------------------------------------------
# The user's own files (any GeoTIFF in any projection, SRTM .hgt)
# ---------------------------------------------------------------------------
class LocalSource(Source):
    key = "local"
    label = "your own files"
    licence = "Elevation data: user supplied files"
    native_m = 30.0

    def index(self, ctx) -> list[dict]:
        """[{path, bounds: [w, s, e, n], res_m}] of usable files in the folder (cached)."""
        folder = ctx.local_folder
        if not folder or not folder.is_dir():
            return []
        cache_file = ctx.index_path("local_files.json")
        try:
            cache = json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
        out, changed = [], False
        for path in folder.rglob("*"):
            suffix = path.suffix.lower()
            if not path.is_file() or suffix not in (".tif", ".tiff", ".hgt"):
                continue
            if "fabdem" in path.name.lower():
                continue  # FABDEM files are used by the FABDEM source
            stat = path.stat()
            key = f"{path}|{stat.st_size}|{int(stat.st_mtime)}"
            entry = cache.get(key)
            if entry is None:
                entry = self._describe(path)
                cache[key] = entry
                changed = True
            if entry:
                out.append(dict(entry, path=str(path)))
        if changed:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(cache), encoding="utf-8")
        return out

    @staticmethod
    def _describe(path: Path) -> Optional[dict]:
        try:
            if path.suffix.lower() == ".hgt":
                m = re.match(r"(?i)([ns])(\d{2})([ew])(\d{3})", path.stem)
                if not m:
                    return None
                lat = int(m[2]) * (1 if m[1].upper() == "N" else -1)
                lon = int(m[4]) * (1 if m[3].upper() == "E" else -1)
                size = int(round(math.sqrt(path.stat().st_size / 2)))
                return {"bounds": [lon, lat, lon + 1, lat + 1],
                        "res_m": R.metres_per_degree_lat() / max(size - 1, 1)}
            with R.TiffSource(path) as src:
                epsg, west, north, width, height, w, h = src.georef()
                res = width / w * (R.metres_per_degree_lat() if epsg == R.GEOGRAPHIC else 1.0)
                return {"bounds": list(src.bounds_lonlat()), "res_m": res}
        except Exception:
            return None

    def files_for(self, ctx, lat: int, lon: int) -> list[dict]:
        return [e for e in self.index(ctx)
                if e["bounds"][0] < lon + 1 and e["bounds"][2] > lon
                and e["bounds"][1] < lat + 1 and e["bounds"][3] > lat]

    def fetch(self, ctx, lat, lon, n):
        files = sorted(self.files_for(ctx, lat, lon), key=lambda e: e["res_m"])  # finest first
        if not files:
            raise NotAvailable("no own files for this tile")
        want = spacing_m(n) * 0.9
        rasters = []
        for entry in files:
            ctx.check()
            path = Path(entry["path"])
            try:
                if path.suffix.lower() == ".hgt":
                    grid = R.read_hgt(path)
                    size = grid.shape[0]
                    w, s = entry["bounds"][0], entry["bounds"][1]
                    rasters.append(R.Raster(grid, w, s + 1, 1.0 / (size - 1), 1.0 / (size - 1)))
                else:
                    with R.TiffSource(path) as src:
                        epsg = src.georef()[0]
                        res = want / R.metres_per_degree_lat() if epsg == R.GEOGRAPHIC else want
                        r = src.read_window(R.tile_bbox_in(epsg, lat, lon), res)
                        if r is not None:
                            rasters.append(r)
            except Exception as exc:
                ctx.log(f"   skipped {path.name}: {exc}")
        grid = R.resample_to_tile(rasters, lat, lon, n)
        if R.valid_fraction(grid) < 0.001:
            raise NotAvailable("your files do not cover this tile")
        return grid

    def native_m_for(self, ctx, lat: int, lon: int) -> Optional[float]:
        files = self.files_for(ctx, lat, lon)
        return min(e["res_m"] for e in files) if files else None


SOURCES: dict[str, Source] = {s.key: s for s in (
    LocalSource(), SonnySource(), UsgsSource(), FabdemSource(), Glo30Source(), LinzSource(),
    HrdemSource(), MrdemSource(), ArcticSource(), GsiSource(), GaSource())}


def source(key: str) -> Source:
    return SOURCES[key]
