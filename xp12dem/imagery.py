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
* Austria  - basemap.at orthophoto (up to ~30 cm), XYZ tiles, for the rest of Austria
* Switzerland - swisstopo SWISSIMAGE, WMTS

Where none of them has data, XPME's photo of the same place can fill in.
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
from PIL import Image, ImageFilter

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
# Photo layers
# ---------------------------------------------------------------------------
class Layer:
    """One photo source. ``fetch`` returns RGBA (alpha 0 = no data) at TEX_PX."""

    key = ""
    label = ""
    credit = ""
    bounds: tuple[float, float, float, float] = (-180, -85, 180, 85)  # lon/lat, cheap pre-check
    gain = np.ones(3, np.float32)   # colour correction towards the common look
    bias = np.zeros(3, np.float32)

    def covers(self, tex: TextureId) -> bool:
        x0, y0, x1, y1 = tex.lonlat_bbox()
        bx0, by0, bx1, by1 = self.bounds
        return x0 < bx1 and x1 > bx0 and y0 < by1 and y1 > by0

    def fetch(self, tex: TextureId, ctx: "Fetcher") -> Optional[np.ndarray]:
        raise NotImplementedError


class WmsLayer(Layer):
    """WMS that renders EPSG:3857 directly: one request per texture block."""

    url = ""
    layer = ""
    version = "1.1.1"

    def _get(self, ctx, tex, px, fmt):
        x0, y0, x1, y1 = tex.merc_bbox()
        crs = "SRS" if self.version == "1.1.1" else "CRS"
        params = {"SERVICE": "WMS", "VERSION": self.version, "REQUEST": "GetMap",
                  "LAYERS": self.layer, "STYLES": "", crs: "EPSG:3857",
                  "BBOX": f"{x0},{y0},{x1},{y1}", "WIDTH": px, "HEIGHT": px,
                  "FORMAT": fmt, "TRANSPARENT": "TRUE"}
        return ctx.get(self.url, params, f"{self.key}/{tex.zl}/{tex.row}_{tex.col}_{px}.{fmt[6:]}")

    def fetch(self, tex, ctx):
        # a small transparent PNG tells where the service has data; only then the full photo
        mask = self._get(ctx, tex, 256, "image/png")
        if mask is None:
            return None
        alpha = np.asarray(Image.open(io.BytesIO(mask)).convert("RGBA"))[..., 3]
        if not alpha.any():
            return None
        if alpha.min() == 255:  # fully covered: compact JPEG
            data = self._get(ctx, tex, TEX_PX, "image/jpeg")
            if data is None:
                return None
            rgb = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))
            return np.dstack([rgb, np.full(rgb.shape[:2], 255, np.uint8)])
        data = self._get(ctx, tex, TEX_PX, "image/png")  # at a border: exact outline
        return None if data is None else np.asarray(Image.open(io.BytesIO(data)).convert("RGBA"))


class BavariaDOP(WmsLayer):
    key = "by_dop40"
    label = "Bavaria DOP40 (40 cm)"
    credit = "Bayerische Vermessungsverwaltung, www.geodaten.bayern.de, CC BY 4.0"
    bounds = (8.95, 47.25, 13.85, 50.57)
    url = "https://geoservices.bayern.de/od/wms/dop/v1/dop40"
    layer = "by_dop40c"


class TirolOrtho(WmsLayer):
    key = "tirol"
    label = "Land Tirol orthophoto (20 cm)"
    credit = "Land Tirol, data.tirol.gv.at, CC BY 4.0"
    bounds = (10.09, 46.65, 12.97, 47.75)
    url = "https://gis.tirol.gv.at/arcgis/services/Service_Public/orthofoto/MapServer/WMSServer"
    layer = "Image_Aktuell_RGB"
    version = "1.3.0"


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
            x, y = col0 + dx, row0 + dy
            data = ctx.get(self.tile_url(z, x, y), None, f"{self.key}/{z}/{y}/{x}.jpg")
            if data is None:
                return
            try:
                rgb = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))
            except OSError:
                return
            a = np.full(rgb.shape[:2], 255, np.uint8)
            white = rgb.min(axis=-1) >= 254
            if white.mean() > 0.3:  # "no data" is painted pure white
                a[white] = 0
            canvas[dy * 256:(dy + 1) * 256, dx * 256:(dx + 1) * 256] = np.dstack([rgb, a])

        list(ctx.pool.map(one, jobs))
        if not canvas[..., 3].any():
            return None
        if canvas.shape[0] != TEX_PX:
            canvas = np.asarray(Image.fromarray(canvas, "RGBA").resize((TEX_PX, TEX_PX), Image.BICUBIC))
        return canvas


class BasemapAT(XyzLayer):
    key = "basemap_at"
    label = "basemap.at orthophoto (Austria)"
    credit = "basemap.at, CC BY 4.0"
    bounds = (9.45, 46.35, 17.20, 49.05)
    template = "https://mapsneu.wien.gv.at/basemap/bmaporthofoto30cm/normal/google3857/{z}/{y}/{x}.jpeg"
    max_zl = 19


class SwissImage(XyzLayer):
    key = "swissimage"
    label = "swisstopo SWISSIMAGE (Switzerland)"
    credit = "swisstopo, CC BY 4.0"
    bounds = (5.90, 45.80, 10.55, 47.85)
    template = "https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.swissimage/default/current/3857/{z}/{x}/{y}.jpeg"
    max_zl = 19


class XpmeFill(Layer):
    """XPME's own photo of the same place (zoom 16), to fill gaps of the official layers."""

    key = "xpme"
    label = "XPME photos (gap filler)"
    credit = "via X-Plane Map Enhancement"

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
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)

    def close(self):
        self.pool.shutdown(wait=False)


# ---------------------------------------------------------------------------
# Compositing
# ---------------------------------------------------------------------------
FEATHER_PX = 48  # ~40 m at zoom 17: width of the soft seam between two sources


def correct(rgb: np.ndarray, layer: Layer) -> np.ndarray:
    if np.all(layer.gain == 1) and np.all(layer.bias == 0):
        return rgb.astype(np.float32)
    return rgb.astype(np.float32) * layer.gain + layer.bias


def composite(parts: list[tuple[Layer, np.ndarray]]) -> tuple[np.ndarray, float]:
    """Best source first. Returns RGB uint8 and the share of pixels without any data."""
    out = np.zeros((TEX_PX, TEX_PX, 3), np.float32)
    have = np.zeros((TEX_PX, TEX_PX), np.float32)  # accumulated weight (0..1)
    for layer, rgba in parts:
        alpha = rgba[..., 3]
        if not alpha.any():
            continue
        # soft edge: fade a source out over FEATHER_PX inside its own outline
        mask = Image.fromarray(alpha)
        if alpha.min() < 255:
            eroded = mask.filter(ImageFilter.MinFilter(9))
            soft = np.asarray(eroded.filter(ImageFilter.GaussianBlur(FEATHER_PX / 2)), np.float32) / 255
            weight = np.minimum(soft, alpha / 255.0)
        else:
            weight = np.ones(alpha.shape, np.float32)
        take = weight * (1 - have)
        out += correct(rgba[..., :3], layer) * take[..., None]
        have += take
        if have.min() > 0.999:
            break
    missing = float((have < 0.5).mean())
    if have.min() < 0.999:  # fill what nobody covers with the average colour
        fill = (out.sum((0, 1)) / max(have.sum(), 1)).astype(np.float32)
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
    layers: list[Layer] = [BavariaDOP(), TirolOrtho(), BasemapAT(), SwissImage()]
    if xpme_textures is not None:
        layers.append(XpmeFill(xpme_textures))
    return layers


def build_textures(textures: Iterable[TextureId], out_dir: Path, layers: list[Layer],
                   fetcher: Fetcher, log: Callable[[str], None] = print,
                   progress: Callable[[int, int], None] = lambda done, total: None) -> dict:
    """Write one DDS per texture block; returns statistics."""
    textures = list(textures)
    out_dir = Path(out_dir)
    stats = {"textures": len(textures), "written": 0, "skipped": 0, "missing_share": 0.0,
             "sources": {layer.key: 0 for layer in layers}}
    started = time.monotonic()
    for i, tex in enumerate(textures, 1):
        target = out_dir / tex.name
        if target.is_file() and target.stat().st_size > 1000:
            stats["skipped"] += 1
            progress(i, len(textures))
            continue
        parts = []
        coverage = np.zeros((TEX_PX, TEX_PX), bool)
        for layer in layers:
            if not layer.covers(tex):
                continue
            rgba = layer.fetch(tex, fetcher)
            if rgba is None:
                continue
            parts.append((layer, rgba))
            stats["sources"][layer.key] += 1
            coverage |= rgba[..., 3] > 0
            if coverage.all():
                break
        rgb, missing = composite(parts)
        stats["missing_share"] += missing
        write_dds(rgb, target)
        stats["written"] += 1
        progress(i, len(textures))
        if i % 25 == 0 or i == len(textures):
            rate = i / max(time.monotonic() - started, 1e-6)
            log(f"photos: {i} of {len(textures)} textures ({rate * 60:.0f}/min, "
                f"{fetcher.bytes / 1e6:.0f} MB downloaded)")
    if textures:
        stats["missing_share"] /= len(textures)
    return stats
