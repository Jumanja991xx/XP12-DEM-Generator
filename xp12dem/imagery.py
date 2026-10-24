"""Official aerial photos as X-Plane textures (Bavaria, Austria, Switzerland).

Ortho4XP lays its mesh out in texture blocks of 16 x 16 web-mercator tiles
(4096 x 4096 px) named ``<row>_<col>_<code><zl>.dds``. For every block the
new mesh references, this module fetches the photos of the official
open-data services, puts them together (best source first, soft seams at
the borders, colours matched between sources), and writes a BC1 (DXT1) DDS
with mipmaps - the format X-Plane loads fastest.

Sources (all CC BY 4.0, free of charge):

* Bavaria  - Bayerische Vermessungsverwaltung, DOP40 (40 cm), WMS
* Tyrol    - Land Tirol orthophoto (20 cm), WMS
* South Tyrol - Autonome Provinz Bozen orthophoto 2020 (20 cm), WMS
* Austria  - basemap.at orthophoto (up to ~30 cm), XYZ tiles, for the rest of Austria
* Switzerland - swisstopo SWISSIMAGE, WMTS

Where none of them has data, XPME's photo of the same place can fill in.

Every source is a mosaic of flights from different days and years, with
visible colour steps between them. Each source is therefore balanced against
one uniform reference - Sentinel-2 cloudless (EOX, one composite for all of
Europe) - at low resolution: only the large-scale colour follows the
reference, every detail stays the aerial photo's.
"""

from __future__ import annotations

import io
import math
import re
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

R = 20037508.342789244  # half the web-mercator extent (m)
TEX_TILES = 16          # web-mercator tiles per texture side
TEX_PX = 4096
CODE = "OF"             # provider code in texture names ("official")

Image.MAX_IMAGE_PIXELS = None


# ---------------------------------------------------------------------------
# Texture blocks
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TextureId:
    row: int  # top tile row at ``zl`` (multiple of 16)
    col: int  # left tile column at ``zl`` (multiple of 16)
    zl: int
    code: str = CODE

    _RE = re.compile(r"^(\d+)_(\d+)_([A-Za-z0-9]+?)(\d{2})\.dds$")

    @property
    def name(self) -> str:
        return f"{self.row}_{self.col}_{self.code}{self.zl}.dds"

    @classmethod
    def parse(cls, name: str) -> Optional["TextureId"]:
        m = cls._RE.match(name)
        return cls(int(m[1]), int(m[2]), int(m[4]), m[3]) if m else None

    def merc_bbox(self) -> tuple[float, float, float, float]:
        """(xmin, ymin, xmax, ymax) in EPSG:3857 metres."""
        n = 2 ** self.zl
        size = 2 * R / n
        x0 = -R + self.col * size
        y1 = R - self.row * size
        return x0, y1 - TEX_TILES * size, x0 + TEX_TILES * size, y1

    def lonlat_bbox(self) -> tuple[float, float, float, float]:
        x0, y0, x1, y1 = self.merc_bbox()
        return (x0 / R * 180, _merc_lat(y0), x1 / R * 180, _merc_lat(y1))

    def covering(self, zl: int) -> "TextureId":
        """The texture block at another (lower) zoom level that contains this one."""
        f = 2 ** (self.zl - zl)
        row, col = self.row // f, self.col // f
        return TextureId(row - row % TEX_TILES, col - col % TEX_TILES, zl, self.code)


def _merc_lat(y: float) -> float:
    return math.degrees(math.atan(math.sinh(y / R * math.pi)))


def textures_of(ter_dir: Path) -> list[TextureId]:
    """Texture blocks a built tile references (from its .ter files)."""
    found = set()
    for ter in Path(ter_dir).glob("*.ter"):
        for line in ter.read_text("utf-8", "replace").splitlines():
            if line.startswith("BASE_TEX"):
                tex = TextureId.parse(line.split()[-1].replace("\\", "/").rsplit("/", 1)[-1])
                if tex:
                    found.add(tex)
    return sorted(found, key=lambda t: (t.zl, t.row, t.col))


# ---------------------------------------------------------------------------
# Areas (keep each source inside its own country / state) and colour curves
# ---------------------------------------------------------------------------
_RESOURCES = Path(__file__).with_name("resources")
_areas: dict = {}
_curves: Optional[dict] = None


def area(name: str):
    """Shapely outline of a named area from resources/borders.json (lon/lat)."""
    if name not in _areas:
        import json

        from shapely.geometry import shape

        data = json.loads((_RESOURCES / "borders.json").read_text("utf-8"))
        _areas[name] = shape(data["areas"][name]["geometry"])
    return _areas[name]


def colour_curve(key: str) -> Optional[np.ndarray]:
    """3 x 256 tone curve mapping a source to the common look (None = unchanged)."""
    global _curves
    if _curves is None:
        import json

        try:
            _curves = json.loads((_RESOURCES / "photo_colours.json").read_text("utf-8"))["curves"]
        except (OSError, ValueError, KeyError):
            _curves = {}
    curve = _curves.get(key)
    return None if curve is None else np.asarray(curve, np.float32)


def area_mask(tex: TextureId, name: str, px: int = TEX_PX) -> np.ndarray:
    """uint8 mask (255 = inside) of an area over a texture block."""
    from shapely.geometry import box

    x0, y0, x1, y1 = tex.lonlat_bbox()
    clip = area(name).intersection(box(x0, y0, x1, y1))
    img = Image.new("L", (px, px), 0)
    if clip.is_empty:
        return np.asarray(img)
    mx0, my0, mx1, my1 = tex.merc_bbox()
    sx, sy = px / (mx1 - mx0), px / (my1 - my0)

    def pix(coords):
        out = []
        for lon, lat in coords:
            mx = lon / 180 * R
            my = math.log(math.tan((90 + lat) * math.pi / 360)) * R / math.pi
            out.append(((mx - mx0) * sx, (my1 - my) * sy))
        return out

    draw = ImageDraw.Draw(img)
    polys = getattr(clip, "geoms", [clip])
    for poly in polys:
        if poly.geom_type != "Polygon":
            continue
        draw.polygon(pix(poly.exterior.coords), fill=255)
        for hole in poly.interiors:
            draw.polygon(pix(hole.coords), fill=0)
    return np.asarray(img)


# ---------------------------------------------------------------------------
# Photo layers
# ---------------------------------------------------------------------------
class Layer:
    """One photo source. ``fetch`` returns RGBA (alpha 0 = no data) at TEX_PX."""

    key = ""
    label = ""
    credit = ""
    bounds: tuple[float, float, float, float] = (-180, -85, 180, 85)  # lon/lat, cheap pre-check
    inside: Optional[str] = None   # only use the source within this area ...
    outside: Optional[str] = None  # ... or never within this one
    balance = True                 # colour-balance against the reference (see Balancer)

    @property
    def lut(self) -> Optional[np.ndarray]:
        return colour_curve(self.key)

    def restrict(self, tex: TextureId, rgba: np.ndarray) -> np.ndarray:
        """Cut the source back to its own area (sources often reach a little beyond)."""
        if self.inside is None and self.outside is None:
            return rgba
        alpha = rgba[..., 3]
        px = rgba.shape[0]
        if self.inside is not None:
            alpha = np.minimum(alpha, area_mask(tex, self.inside, px))
        if self.outside is not None:
            alpha = np.minimum(alpha, 255 - area_mask(tex, self.outside, px))
        out = rgba.copy()
        out[..., 3] = alpha
        return out

    def covers(self, tex: TextureId) -> bool:
        x0, y0, x1, y1 = tex.lonlat_bbox()
        bx0, by0, bx1, by1 = self.bounds
        return x0 < bx1 and x1 > bx0 and y0 < by1 and y1 > by0

    def fetch(self, tex: TextureId, ctx: "Fetcher") -> Optional[np.ndarray]:
        raise NotImplementedError

    def lowres(self, tex: TextureId, ctx: "Fetcher") -> Optional[np.ndarray]:
        """The block at LOW_PX (RGBA), for the colour balance; None = no balancing."""
        return None


LOW_PX = 256  # low-resolution size of a texture block for colour balancing (~13 m at zoom 17)


def _decode(data: Optional[bytes], mode: str = "RGBA") -> Optional[np.ndarray]:
    if data is None:
        return None
    try:
        img = Image.open(io.BytesIO(data))
        if mode == "RGB":
            rgb = np.asarray(img.convert("RGB"))
            return np.dstack([rgb, np.full(rgb.shape[:2], 255, np.uint8)])
        return np.asarray(img.convert("RGBA"))
    except OSError:
        return None


class WmsLayer(Layer):
    """WMS that renders EPSG:3857 directly: one request per texture block (or a few,
    for services with a maximum image size)."""

    url = ""
    layer = ""
    version = "1.1.1"
    max_px = TEX_PX

    def _request(self, ctx, bbox, px, fmt, key):
        x0, y0, x1, y1 = bbox
        crs = "SRS" if self.version == "1.1.1" else "CRS"
        params = {"SERVICE": "WMS", "VERSION": self.version, "REQUEST": "GetMap",
                  "LAYERS": self.layer, "STYLES": "", crs: "EPSG:3857",
                  "BBOX": f"{x0},{y0},{x1},{y1}", "WIDTH": px, "HEIGHT": px,
                  "FORMAT": fmt, "TRANSPARENT": "TRUE"}
        return ctx.get(self.url, params, key)

    def _get_rgba(self, ctx, tex, px, fmt):
        """RGBA of a texture block, stitched from parts if the service limits the size."""
        mode = "RGB" if fmt == "image/jpeg" else "RGBA"
        ext = fmt[6:]
        if px <= self.max_px:
            key = f"{self.key}/{tex.zl}/{tex.row}_{tex.col}_{px}.{ext}"
            return _decode(self._request(ctx, tex.merc_bbox(), px, fmt, key), mode)
        n = -(-px // self.max_px)
        while px % n:
            n += 1
        part = px // n
        x0, y0, x1, y1 = tex.merc_bbox()
        w, h = (x1 - x0) / n, (y1 - y0) / n
        canvas = np.zeros((px, px, 4), np.uint8)
        jobs = [(i, j) for i in range(n) for j in range(n)]

        def one(job):
            i, j = job  # i: row from the top
            bbox = (x0 + j * w, y1 - (i + 1) * h, x0 + (j + 1) * w, y1 - i * h)
            key = f"{self.key}/{tex.zl}/{tex.row}_{tex.col}_{px}_{i}{j}.{ext}"
            img = _decode(self._request(ctx, bbox, part, fmt, key), mode)
            if img is not None:
                canvas[i * part:(i + 1) * part, j * part:(j + 1) * part] = img

        list(ctx.pool.map(one, jobs))
        return canvas

    def lowres(self, tex, ctx):
        img = self._get_rgba(ctx, tex, LOW_PX, "image/png")
        return None if img is None or not img[..., 3].any() else img

    def fetch(self, tex, ctx):
        # a small transparent PNG tells where the service has data; only then the full photo
        probe = self._get_rgba(ctx, tex, LOW_PX, "image/png")
        if probe is None:
            return None
        alpha = probe[..., 3]
        if not alpha.any():
            return None
        if alpha.min() == 255:  # fully covered: compact JPEG
            return self._get_rgba(ctx, tex, TEX_PX, "image/jpeg")
        return self._get_rgba(ctx, tex, TEX_PX, "image/png")  # at a border: exact outline


class BavariaDOP(WmsLayer):
    key = "by_dop40"
    label = "Bavaria DOP40 (40 cm)"
    credit = "Bayerische Vermessungsverwaltung, www.geodaten.bayern.de, CC BY 4.0"
    bounds = (8.95, 47.25, 13.85, 50.57)
    outside = "austria"  # the service shows a few km of Austria, from other flights
    url = "https://geoservices.bayern.de/od/wms/dop/v1/dop40"
    layer = "by_dop40c"


class TirolOrtho(WmsLayer):
    key = "tirol"
    label = "Land Tirol orthophoto 2016-2018 (20 cm)"
    credit = "Land Tirol, data.tirol.gv.at, CC BY 4.0"
    bounds = (10.09, 46.65, 12.97, 47.75)
    inside = "tirol"
    url = "https://gis.tirol.gv.at/arcgis/services/Service_Public/orthofoto/MapServer/WMSServer"
    # one complete flight campaign: "Image_Aktuell_RGB" mixes years with visible seams
    layer = "Image_2016_2018"
    version = "1.3.0"


class SouthTyrolOrtho(WmsLayer):
    key = "suedtirol"
    label = "Südtirol orthophoto 2020 (20 cm)"
    credit = "Autonome Provinz Bozen - Südtirol, geokatalog.buergernetz.bz.it, CC BY 4.0"
    bounds = (10.35, 46.16, 12.52, 47.14)
    inside = "suedtirol"  # the service reaches a few km into Tyrol and Trentino
    url = "https://geoservices.buergernetz.bz.it/mapproxy/wms"
    # one complete flight campaign (2023 was flown in autumn: yellow larches)
    layer = "p_bz-Orthoimagery:Aerial-2020-RGB"
    version = "1.3.0"
    max_px = 2000


class XyzLayer(Layer):
    """Web-mercator tile service, 256 px tiles; stitched into a texture block."""

    template = ""
    max_zl = 19
    yx = True  # template order {z}/{y}/{x}

    def tile_url(self, z, x, y):
        return self.template.format(z=z, x=x, y=y)

    def fetch(self, tex, ctx):
        z = min(tex.zl, self.max_zl)
        scale = 2 ** (tex.zl - z)       # texture zoom finer than the service: upsample
        span = TEX_TILES // scale
        row0, col0 = tex.row // scale, tex.col // scale
        jobs = [(dy, dx) for dy in range(span) for dx in range(span)]
        canvas = np.zeros((span * 256, span * 256, 4), np.uint8)

        def one(job):
            dy, dx = job
            img = self._tile(ctx, z, col0 + dx, row0 + dy)
            if img is not None:
                canvas[dy * 256:(dy + 1) * 256, dx * 256:(dx + 1) * 256] = img

        list(ctx.pool.map(one, jobs))
        if not canvas[..., 3].any():
            return None
        if canvas.shape[0] != TEX_PX:
            canvas = np.asarray(Image.fromarray(canvas, "RGBA").resize((TEX_PX, TEX_PX), Image.BICUBIC))
        return canvas

    def _tile(self, ctx, z, x, y) -> Optional[np.ndarray]:
        data = ctx.get(self.tile_url(z, x, y), None, f"{self.key}/{z}/{y}/{x}.jpg")
        img = _decode(data, "RGB")
        if img is None or img.shape[:2] != (256, 256):
            return None
        white = img[..., :3].min(axis=-1) >= 254
        if white.mean() > 0.3:  # "no data" is painted pure white
            img[white, 3] = 0
        return img

    def lowres(self, tex, ctx):
        # the whole block is one tile, four zoom levels up
        z = tex.zl - 4
        if z > self.max_zl:
            return None
        img = self._tile(ctx, z, tex.col // TEX_TILES, tex.row // TEX_TILES)
        return None if img is None or not img[..., 3].any() else img


class BasemapAT(XyzLayer):
    key = "basemap_at"
    label = "basemap.at orthophoto (Austria)"
    credit = "basemap.at, CC BY 4.0"
    bounds = (9.45, 46.35, 17.20, 49.05)
    inside = "austria"
    template = "https://mapsneu.wien.gv.at/basemap/bmaporthofoto30cm/normal/google3857/{z}/{y}/{x}.jpeg"
    max_zl = 19


class SwissImage(XyzLayer):
    key = "swissimage"
    label = "swisstopo SWISSIMAGE (Switzerland)"
    credit = "swisstopo, CC BY 4.0"
    bounds = (5.90, 45.80, 10.55, 47.85)
    template = "https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.swissimage/default/current/3857/{z}/{x}/{y}.jpeg"
    max_zl = 19


class Sentinel2Reference(XyzLayer):
    """Colour reference: one cloud-free satellite composite for all of Europe (10 m)."""

    key = "s2cloudless"
    label = "Sentinel-2 cloudless 2024 (colour reference)"
    credit = ("Sentinel-2 cloudless - https://s2maps.eu by EOX IT Services GmbH "
              "(Contains modified Copernicus Sentinel data 2024), CC BY-NC-SA 4.0")
    template = "https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2024_3857/default/g/{z}/{y}/{x}.jpg"
    max_zl = 15
    balance = False


class XpmeFill(Layer):
    """XPME's own photo of the same place (zoom 16), to fill gaps of the official layers."""

    key = "xpme"
    label = "XPME photos (gap filler)"
    credit = "via X-Plane Map Enhancement"
    balance = False  # already uniform; matched locally to its neighbours

    def __init__(self, textures_dir: Path, zl: int = 16, code: str = "BI"):
        self.dir, self.zl, self.code = Path(textures_dir), zl, code

    def fetch(self, tex, ctx):
        src = tex.covering(min(self.zl, tex.zl))
        src = TextureId(src.row, src.col, src.zl, self.code)
        path = self.dir / src.name
        try:
            img = Image.open(path).convert("RGB")
        except OSError:
            return None
        f = 2 ** (tex.zl - src.zl)
        part = img.size[0] // f
        x = (tex.col // TEX_TILES % f) * part
        y = (tex.row // TEX_TILES % f) * part
        crop = img.crop((x, y, x + part, y + part)).resize((TEX_PX, TEX_PX), Image.BICUBIC)
        rgb = np.asarray(crop)
        return np.dstack([rgb, np.full(rgb.shape[:2], 255, np.uint8)])


# ---------------------------------------------------------------------------
# Download helper with disk cache
# ---------------------------------------------------------------------------
class Fetcher:
    def __init__(self, cache_dir: Path, session=None, workers: int = 16,
                 cancel: Callable[[], bool] = lambda: False, max_per_second: float = 40.0):
        import requests

        self.cache = Path(cache_dir)
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "XP12-DEM-Generator (open-data orthophotos)")
        adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=workers)
        self.session.mount("https://", adapter)
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self.cancel = cancel
        self._lock = threading.Lock()
        self._next = 0.0
        self._interval = 1.0 / max_per_second
        self.bytes = 0
        self.requests = 0

    def _throttle(self):
        with self._lock:
            now = time.monotonic()
            wait = self._next - now
            self._next = max(now, self._next) + self._interval
        if wait > 0:
            time.sleep(wait)

    def get(self, url: str, params: Optional[dict], cache_key: str) -> Optional[bytes]:
        """Response body, cached on disk; None for 'no data here' (404/204) or after retries."""
        path = self.cache / cache_key
        if path.is_file():
            data = path.read_bytes()
            return data or None  # empty file = known to have no data
        if self.cancel():
            raise RuntimeError("cancelled")
        for attempt in range(4):
            self._throttle()
            try:
                r = self.session.get(url, params=params, timeout=90)
            except Exception:
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code in (404, 204):
                self._store(path, b"")
                return None
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                self.bytes += len(r.content)
                self.requests += 1
                self._store(path, r.content)
                return r.content
            time.sleep(2 * (attempt + 1))  # 429 / 5xx / XML error: back off and retry
        return None

    def _store(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # unique name: neighbouring blocks may fetch the same file at the same moment
        tmp = path.with_name(f"{path.name}.{threading.get_ident()}.part")
        tmp.write_bytes(data)
        try:
            tmp.replace(path)
        except OSError:  # another thread finished it first (Windows: target in use)
            tmp.unlink(missing_ok=True)

    def close(self):
        self.pool.shutdown(wait=False)


# ---------------------------------------------------------------------------
# Colour balance against the reference
# ---------------------------------------------------------------------------
_LOG_OFFSET = 10.0  # keeps dark pixels from dominating the log ratio
MAX_GAIN = 0.5      # at most x1.65 / x0.6 brighter or darker
MAX_TINT = 0.15     # at most ~16 % difference between the colour channels


class Balancer:
    """Per source and texture block: a smooth gain field that brings the source's
    large-scale colour to the reference's (mapped to the common look).

    Computed on the block plus its eight neighbours at LOW_PX, so neighbouring
    blocks get matching gains at their shared edge. The ratio source/reference is
    median filtered over ~400 m: hard steps between flight blocks stay steps in the
    gain - and so disappear from the photo - while fields, roofs or a lake that
    look different in the reference are ignored.
    """

    MEDIAN = 61   # px at LOW_PX (~800 m at zoom 17)
    SMOOTH = 2.0  # px, gaussian after the median

    def __init__(self, reference: Optional[Layer] = None):
        self.reference = reference or Sentinel2Reference()
        self._lock = threading.Lock()
        self._low: dict = {}

    def _low_of(self, layer: Layer, tex: TextureId, ctx: "Fetcher") -> Optional[np.ndarray]:
        key = (layer.key, tex.row, tex.col, tex.zl)
        with self._lock:
            if key in self._low:
                return self._low[key]
        img = layer.lowres(tex, ctx) if layer.covers(tex) else None
        if img is not None:
            img = layer.restrict(tex, img)
            rgb = correct(img[..., :3], layer)
            img = np.dstack([rgb, img[..., 3].astype(np.float32)])
        with self._lock:
            if len(self._low) > 4000:
                self._low.clear()
            self._low[key] = img
        return img

    def _mosaic(self, layer, tex, ctx) -> Optional[np.ndarray]:
        out = np.zeros((3 * LOW_PX, 3 * LOW_PX, 4), np.float32)
        found = False
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                n = TextureId(tex.row + dy * TEX_TILES, tex.col + dx * TEX_TILES, tex.zl, tex.code)
                img = self._low_of(layer, n, ctx)
                if img is None:
                    continue
                found = True
                y, x = (dy + 1) * LOW_PX, (dx + 1) * LOW_PX
                out[y:y + LOW_PX, x:x + LOW_PX] = img
        return out if found else None

    def gain(self, layer: Layer, tex: TextureId, ctx: "Fetcher", px: int = TEX_PX) -> Optional[np.ndarray]:
        """Gain (px x px x 3, multiply ``rgb + 10``) or None."""
        if not layer.balance:
            return None
        src = self._mosaic(layer, tex, ctx)
        ref = self._mosaic(self.reference, tex, ctx)
        if src is None or ref is None:
            return None
        s, r = src[..., :3], ref[..., :3]
        valid = ((src[..., 3] > 0) & (ref[..., 3] > 0) & (s.min(-1) > 6) & (s.max(-1) < 235)
                 & (r.min(-1) > 6) & (r.max(-1) < 240))
        centre = valid[LOW_PX:2 * LOW_PX, LOW_PX:2 * LOW_PX]
        if centre.mean() < 0.05:
            return None
        log_d = np.log(r + _LOG_OFFSET) - np.log(s + _LOG_OFFSET)
        weight = _blur(valid.astype(np.float32), 24)
        # the median only needs the block plus half a window around it
        m = 2 * self.MEDIAN
        crop = np.s_[LOW_PX - m:2 * LOW_PX + m, LOW_PX - m:2 * LOW_PX + m]
        smoothed = np.empty((LOW_PX + 2 * m, LOW_PX + 2 * m, 3), np.float32)
        for c in range(3):
            filled = np.where(valid, log_d[..., c], 0)
            filled = np.where(valid, filled, _blur(filled, 24) / np.maximum(weight, 1e-4))
            smoothed[..., c] = _median(filled[crop], self.MEDIAN, self.SMOOTH)
        level = np.clip(smoothed.mean(-1, keepdims=True), -MAX_GAIN, MAX_GAIN)
        tint = np.clip(smoothed - smoothed.mean(-1, keepdims=True), -MAX_TINT, MAX_TINT)
        field = np.exp(level + tint)[m:m + LOW_PX, m:m + LOW_PX]
        if px == LOW_PX:
            return field.astype(np.float32)
        out = np.empty((px, px, 3), np.float32)
        for c in range(3):
            img = Image.fromarray(field[..., c].astype(np.float32), "F")
            out[..., c] = np.asarray(img.resize((px, px), Image.BILINEAR))
        return out


def _box(a: np.ndarray, r: int, axis: int) -> np.ndarray:
    pad = [(0, 0), (0, 0)]
    pad[axis] = (r + 1, r)
    c = np.cumsum(np.pad(a, pad, mode="edge"), axis=axis, dtype=np.float64)
    n = a.shape[axis]
    hi = np.take(c, np.arange(2 * r + 1, 2 * r + 1 + n), axis=axis)
    lo = np.take(c, np.arange(0, n), axis=axis)
    return ((hi - lo) / (2 * r + 1)).astype(np.float32)


def _blur(a: np.ndarray, sigma: float) -> np.ndarray:
    """Gaussian-like blur (three box passes), edges extended."""
    r = max(1, int(round(math.sqrt(12 * sigma * sigma / 3 + 1) / 2)))
    for _ in range(3):
        a = _box(_box(a, r, 0), r, 1)
    return a


_Q = 160.0  # log-ratio steps per unit when median filtering in 8 bit


_COARSE = 4  # median grid step (px at LOW_PX); divides LOW_PX, so neighbours share the grid


def _median(a: np.ndarray, size: int, sigma: float) -> np.ndarray:
    """Median (edge preserving: steps stay steps) on a coarser grid, then a light blur."""
    h, w = a.shape
    k = _COARSE
    small = a[:h - h % k, :w - w % k].reshape(h // k, k, w // k, k).mean((1, 3))
    small = np.clip(np.round(128 + small * _Q), 0, 255).astype(np.uint8)
    med = Image.fromarray(small, "L").filter(ImageFilter.MedianFilter(max(3, size // k | 1)))
    med = (np.asarray(med, np.float32) - 128) / _Q
    big = np.kron(med, np.ones((k, k), np.float32))
    big = np.pad(big, ((0, h - big.shape[0]), (0, w - big.shape[1])), mode="edge")
    return _blur(big, sigma + k / 2)


# ---------------------------------------------------------------------------
# Compositing
# ---------------------------------------------------------------------------
FEATHER_PX = 96  # ~80 m at zoom 17: width of the soft seam between two sources


def correct(rgb: np.ndarray, layer: Layer) -> np.ndarray:
    lut = layer.lut
    if lut is None:
        return rgb.astype(np.float32)
    out = np.empty(rgb.shape, np.float32)
    for c in range(3):
        out[..., c] = lut[c][rgb[..., c]]
    return out


def _match_local(rgb: np.ndarray, alpha: np.ndarray, done: np.ndarray, have: np.ndarray) -> np.ndarray:
    """Fit a weaker source to what is already there where both have data: photos of
    different flights differ in light, a global curve cannot catch that."""
    both = (alpha > 0) & (have > 0.99)
    if both.sum() < 20000:
        return rgb
    src = rgb[both]
    dst = done[both] / have[both][:, None]
    s_mean, d_mean = src.mean(0), dst.mean(0)
    scale = np.clip(dst.std(0) / np.maximum(src.std(0), 1e-3), 0.75, 1.35)
    return (rgb - s_mean) * scale + d_mean


def composite(parts: list[tuple]) -> tuple[np.ndarray, float]:
    """Best source first. Returns RGB uint8 and the share of pixels without any data.

    Where a weaker source also has data, the better one fades out softly over
    FEATHER_PX; where it has none (e.g. exactly at a state border), the edge
    stays sharp so no gap opens - the colour curves make both sides match.
    """
    px = parts[0][1].shape[0] if parts else TEX_PX
    feather = FEATHER_PX * px / TEX_PX
    out = np.zeros((px, px, 3), np.float32)
    have = np.zeros((px, px), np.float32)  # accumulated weight (0..1)
    below = [None] * len(parts)  # union of the data of all weaker sources
    acc = np.zeros((px, px), bool)
    for i in range(len(parts) - 1, -1, -1):
        below[i] = acc.copy()
        acc |= parts[i][1][..., 3] > 0
    for part, lower in zip(parts, below):
        layer, rgba = part[0], part[1]
        gain = part[2] if len(part) > 2 else None
        alpha = rgba[..., 3]
        if not alpha.any():
            continue
        hard = alpha.astype(np.float32) / 255
        if alpha.min() < 255 and lower.any():
            eroded = Image.fromarray(alpha).filter(ImageFilter.MinFilter(9))
            soft = np.asarray(eroded.filter(ImageFilter.GaussianBlur(feather / 2)), np.float32) / 255
            weight = np.where(lower, np.minimum(soft, hard), hard)
        else:
            weight = hard
        take = weight * (1 - have)
        rgb = correct(rgba[..., :3], layer)
        if gain is not None:
            rgb = (rgb + _LOG_OFFSET) * gain - _LOG_OFFSET
        rgb = _match_local(rgb, alpha, out, have)
        out += rgb * take[..., None]
        have += take
        if have.min() > 0.999:
            break
    missing = float((have < 0.5).mean())
    if have.min() < 0.999:  # fill what nobody covers with the average colour
        fill = (out.sum((0, 1), dtype=np.float64) / max(float(have.sum(dtype=np.float64)), 1.0)).astype(np.float32)
        out += fill * (1 - have)[..., None]
    return np.clip(out, 0, 255).astype(np.uint8), missing


# ---------------------------------------------------------------------------
# DDS (BC1 / DXT1 with mipmaps)
# ---------------------------------------------------------------------------
def _bc1(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "DDS", pixel_format="DXT1")
    return buf.getvalue()[128:]  # strip Pillow's header


def write_dds(rgb: np.ndarray, path: Path) -> None:
    img = Image.fromarray(rgb, "RGB")
    w, h = img.size
    levels = []
    while True:
        levels.append(_bc1(img))
        if img.size[0] <= 4 or img.size[1] <= 4:
            break
        img = img.resize((max(4, img.size[0] // 2), max(4, img.size[1] // 2)), Image.BOX)
    flags = 0x1 | 0x2 | 0x4 | 0x1000 | 0x20000 | 0x80000  # caps|height|width|pixfmt|mipcount|linearsize
    pixfmt = struct.pack("<II4sIIIII", 32, 0x4, b"DXT1", 0, 0, 0, 0, 0)
    caps = struct.pack("<IIII", 0x1000 | 0x8 | 0x400000, 0, 0, 0)  # texture|complex|mipmap
    header = (b"DDS " + struct.pack("<IIIIIII", 124, flags, h, w, len(levels[0]), 0, len(levels))
              + b"\0" * 44 + pixfmt + caps + b"\0" * 4)
    assert len(header) == 128
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".dds.part")
    with open(tmp, "wb") as f:
        f.write(header)
        for level in levels:
            f.write(level)
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Build all textures of a tile
# ---------------------------------------------------------------------------
def official_layers(xpme_textures: Optional[Path] = None) -> list[Layer]:
    layers: list[Layer] = [BavariaDOP(), TirolOrtho(), SouthTyrolOrtho(), BasemapAT(), SwissImage()]
    if xpme_textures is not None:
        layers.append(XpmeFill(xpme_textures))
    return layers


def build_textures(textures: Iterable[TextureId], out_dir: Path, layers: list[Layer],
                   fetcher: Fetcher, log: Callable[[str], None] = print,
                   progress: Callable[[int, int], None] = lambda done, total: None,
                   parallel: int = 4, balancer: Optional[Balancer] = None) -> dict:
    """Write one DDS per texture block (several at once); returns statistics."""
    textures = list(textures)
    out_dir = Path(out_dir)
    stats = {"textures": len(textures), "written": 0, "skipped": 0, "missing_share": 0.0, "balanced": 0,
             "sources": {layer.key: 0 for layer in layers}}
    lock = threading.Lock()
    done = [0]
    started = time.monotonic()

    def one(tex: TextureId) -> None:
        target = out_dir / tex.name
        if target.is_file() and target.stat().st_size > 1000:
            with lock:
                stats["skipped"] += 1
        else:
            parts = []
            coverage = np.zeros((TEX_PX, TEX_PX), bool)
            used = []
            for layer in layers:
                if not layer.covers(tex):
                    continue
                rgba = layer.fetch(tex, fetcher)
                if rgba is None:
                    continue
                rgba = layer.restrict(tex, rgba)
                if not rgba[..., 3].any():
                    continue
                gain = None
                if balancer is not None:
                    try:
                        gain = balancer.gain(layer, tex, fetcher)
                    except RuntimeError:
                        raise
                    except Exception:  # the reference is a nicety - never lose a texture to it
                        gain = None
                parts.append((layer, rgba, gain))
                used.append(layer.key)
                coverage |= rgba[..., 3] > 0
                if coverage.all():
                    break
            rgb, missing = composite(parts)
            write_dds(rgb, target)
            with lock:
                stats["written"] += 1
                stats["balanced"] += any(part[2] is not None for part in parts)
                stats["missing_share"] += missing
                for key in used:
                    stats["sources"][key] += 1
        with lock:
            done[0] += 1
            n = done[0]
        progress(n, len(textures))
        if n % 25 == 0 or n == len(textures):
            rate = n / max(time.monotonic() - started, 1e-6)
            log(f"photos: {n} of {len(textures)} textures ({rate * 60:.0f}/min, "
                f"{fetcher.bytes / 1e6:.0f} MB downloaded)")

    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        for future in [pool.submit(one, tex) for tex in textures]:
            future.result()  # re-raise errors (e.g. cancelled)
    if stats["written"]:
        stats["missing_share"] /= stats["written"]
    return stats


def credits(stats: dict, layers: list[Layer], balancer: Optional[Balancer] = None) -> list[str]:
    out = [layer.credit for layer in layers if stats["sources"].get(layer.key)]
    if balancer is not None and stats.get("balanced"):
        out.append(balancer.reference.credit + " (colour reference)")
    return out
