"""Rebuild xp12dem/resources/dem_coverage.json (best elevation sources per tile).

Starts from the existing file (Sonny / USGS / FABDEM classes and countries)
and adds the national and regional sources, using their published coverage:

* New Zealand  - LINZ LiDAR 1 m surveys (STAC catalogue) + national 8 m DEM
* Canada       - NRCan HRDEM LiDAR mosaic coverage + MRDEM 30 m
* Arctic       - ArcticDEM 10 m mosaic tiles (bare areas only: GRL, ISL, >= 67 N)
* Japan        - GSI 5-10 m elevation tiles
* Australia    - Geoscience Australia 1" DEM-H as fallback behind FABDEM

Usage:  python packaging/build_coverage.py <download cache dir>
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import requests
from shapely import wkb
from shapely.geometry import box
from shapely.ops import transform, unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xp12dem import raster as R  # noqa: E402
from xp12dem.sources import ArcticSource, Context, LinzSource  # noqa: E402

COVERAGE = ROOT / "xp12dem" / "resources" / "dem_coverage.json"
HRDEM_BASE = "https://canelevation-dem.s3.ca-central-1.amazonaws.com/"


def tile_poly(epsg: int, lat: int, lon: int):
    """Tile outline (densified) in a projected CRS."""
    import numpy as np

    t = np.linspace(0, 1, 9)
    lons = np.concatenate([lon + t, np.full_like(t, lon + 1), lon + 1 - t, np.full_like(t, lon)])
    lats = np.concatenate([np.full_like(t, lat), lat + t, np.full_like(t, lat + 1), lat + 1 - t])
    x, y = R.lonlat_to(epsg, lons, lats)
    from shapely.geometry import Polygon

    return Polygon(list(zip(x, y)))


def gpkg_geometry(path: Path, table: str):
    con = sqlite3.connect(path)
    blob = con.execute(f'select geometry from "{table}"').fetchone()[0]
    flags = blob[3]
    envelope = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}[(flags >> 1) & 7]
    return wkb.loads(bytes(blob[8 + envelope:]))


def main(cache: Path) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = "XP12-DEM-Generator coverage builder"
    ctx = Context(session, cache)
    data = json.loads(COVERAGE.read_text(encoding="utf-8"))
    rows = data["tiles"]

    # -- New Zealand ----------------------------------------------------------
    linz = LinzSource()
    collections = linz.collections(ctx)

    def sheet_rect(name: str):
        sheet, _, sub = name.partition("_10000_")
        row = linz.LETTERS.index(sheet[0]) * 24 + linz.LETTERS.index(sheet[1])
        col = int(sheet[2:4])
        left, top = linz.X0 + col * linz.SW, linz.Y0 - row * linz.SH
        if not sub:
            return box(left, top - linz.SH, left + linz.SW, top)
        r, c = int(sub[:2]) - 1, int(sub[2:]) - 1
        return box(left + c * 4800, top - (r + 1) * 7200, left + (c + 1) * 4800, top - r * 7200)

    lidar_nz = unary_union([sheet_rect(i) for c in collections for i in c["items"] if "_10000_" in i])
    catalog = json.loads(ctx.cached_text("linz_dem8.json", linz.BASE + "new-zealand/new-zealand-contour/dem_8m/2193/collection.json"))
    base_nz = unary_union([sheet_rect(l["href"][2:-5]) for l in catalog["links"] if l.get("rel") == "item"])
    print("NZ LiDAR km2", round(lidar_nz.area / 1e6), "8 m km2", round(base_nz.area / 1e6))

    # -- Canada: valid-data masks from the coarsest overview of every mosaic block
    import re

    import numpy as np

    listing = session.get(HRDEM_BASE + "?list-type=2&prefix=hrdem-mosaic-2m/&max-keys=1000", timeout=60).text
    blocks = re.findall(r"<Key>(hrdem-mosaic-2m/[^<]*-mosaic-2m-dtm\.tif)</Key>", listing)
    masks = []
    for key in blocks:
        with R.TiffSource(HRDEM_BASE + key, session) as src:
            epsg, west, north, width, height, w, h = src.georef()
            r = src.read_window((west, north - height, west + width, north), width / 400)
        masks.append(r)
    print("HRDEM blocks", len(masks), "LiDAR km2",
          round(sum(np.count_nonzero(~np.isnan(m.data)) * m.dx * m.dy for m in masks) / 1e6))

    def hrdem_share(lat: int, lon: int) -> float:
        lons, lats = np.meshgrid(lon + (np.arange(24) + 0.5) / 24, lat + (np.arange(24) + 0.5) / 24)
        x, y = R.lonlat_to(3979, lons.ravel(), lats.ravel())
        hit = np.zeros(x.size, dtype=bool)
        for m in masks:
            col = np.floor((x - m.x0) / m.dx + 0.5).astype(int)
            row = np.floor((m.y0 - y) / m.dy + 0.5).astype(int)
            ok = (col >= 0) & (col < m.data.shape[1]) & (row >= 0) & (row < m.data.shape[0])
            hit[ok] |= ~np.isnan(m.data[row[ok], col[ok]])
        return float(hit.mean())

    # -- ArcticDEM ------------------------------------------------------------
    arctic = unary_union([box(x0, y0, x1, y1) for _, x0, y0, x1, y1 in ArcticSource().tiles(ctx)])

    stats: dict[str, int] = {}
    out = []
    for row in rows:
        lat, lon, code, country = row[:4]
        q = code[0]
        has_sonny = code.endswith("s")
        if q == "U":
            chain, fine = ["usgs", "fabdem", "glo30"], True
        elif q in ("L", "N"):
            chain, fine = ["sonny", "fabdem", "glo30"], False
        elif has_sonny:
            chain, fine = ["fabdem", "sonny", "glo30"], False
        else:
            chain, fine = ["fabdem", "glo30"], False

        if country == "NZL" or (-48 <= lat <= -34 and 165 <= lon <= 179):
            poly = tile_poly(2193, lat, lon)
            base_share = poly.intersection(base_nz).area
            if base_share > 0:
                share = poly.intersection(lidar_nz).area / base_share
                q = "L" if share >= 0.3 else "N"
                chain, fine = ["linz", "fabdem", "glo30"], share >= 0.3
        elif country == "CAN" and q == "F":
            share = hrdem_share(lat, lon)
            if share >= 0.02:
                q = "L" if share >= 0.4 else "N"
                chain, fine = ["hrdem", "mrdem", "fabdem", "glo30"], share >= 0.4
            else:
                q, chain, fine = "N", ["mrdem", "fabdem", "glo30"], False
            if lat >= 60:
                chain.insert(chain.index("fabdem"), "arcticdem")
        elif country == "JPN":
            q, chain, fine = "N", ["gsi", "fabdem", "glo30"], True
        elif country == "AUS":
            chain = ["fabdem", "ga", "glo30"]
        if q == "F" and lat >= 55 and (country in ("GRL", "ISL", "SJM") or lat >= 67):
            if tile_poly(3413, lat, lon).intersects(arctic):
                q, chain, fine = "N", ["arcticdem", "fabdem", "glo30"], True
        if q == "U" and lat >= 55 and tile_poly(3413, lat, lon).intersects(arctic):
            chain.insert(1, "arcticdem")  # Alaska: fills 3DEP gaps
        out.append([lat, lon, q + ("s" if has_sonny else ""), country, ",".join(chain), 1 if fine else 0])
        stats[chain[0] + "/" + q] = stats.get(chain[0] + "/" + q, 0) + 1

    data = {
        "about": ("Best DEM sources per 1x1 degree tile: [lat, lon, quality, country, sources, fine]. "
                  "Quality L=LiDAR, N=national 5-50 m model, U=USGS 3DEP, F=FABDEM / satellite; "
                  "suffix s = a Sonny tile exists. Sources are tried in order, later ones fill gaps. "
                  "fine = 1 if a source with <= 10 m posts covers most of the tile. "
                  "Country = Natural Earth ADM0_A3. Built by packaging/build_coverage.py."),
        "version": 2,
        "tiles": out,
    }
    COVERAGE.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    for key in sorted(stats):
        print(f"{key:14s} {stats[key]}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "build" / "coverage-cache")
