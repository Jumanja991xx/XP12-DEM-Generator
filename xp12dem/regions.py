"""Region presets and 1x1 degree tile helpers."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from functools import lru_cache

from .paths import resources_dir

Tile = tuple[int, int]  # (lat, lon) of the south-west corner


def tile_name(lat: int, lon: int) -> str:
    """Ortho4XP / X-Plane style name, e.g. ``+47+011``."""
    return f"{lat:+03d}{lon:+04d}"


def hgt_name(lat: int, lon: int) -> str:
    """SRTM style name, e.g. ``N47E011``."""
    return f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}{'E' if lon >= 0 else 'W'}{abs(lon):03d}"


def parse_tile(text: str) -> Tile:
    """Accept ``+47+011``, ``N47E011`` or ``47,11``."""
    text = text.strip()
    m = re.fullmatch(r"([+-]\d{1,2})([+-]\d{1,3})", text)
    if m:
        return int(m[1]), int(m[2])
    m = re.fullmatch(r"([NS])(\d{1,2})([EW])(\d{1,3})", text, re.IGNORECASE)
    if m:
        lat = int(m[2]) * (1 if m[1].upper() == "N" else -1)
        lon = int(m[4]) * (1 if m[3].upper() == "E" else -1)
        return lat, lon
    m = re.fullmatch(r"(-?\d{1,2})\s*[,;\s]\s*(-?\d{1,3})", text)
    if m:
        return int(m[1]), int(m[2])
    raise ValueError(f"Not a tile name: {text!r}")


def dsf_relpath(lat: int, lon: int) -> str:
    """Path of a tile's DSF inside a scenery pack."""
    lat10 = math.floor(lat / 10) * 10
    lon10 = math.floor(lon / 10) * 10
    return f"Earth nav data/{lat10:+03d}{lon10:+04d}/{tile_name(lat, lon)}.dsf"


@lru_cache(maxsize=1)
def available_tiles() -> frozenset[Tile]:
    """Tiles covered by Sonny's European 1" DTM mirror."""
    data = json.loads((resources_dir() / "sonny_tiles.json").read_text(encoding="utf-8"))
    return frozenset((int(lat), int(lon)) for lat, lon in data["tiles"])


@dataclass(frozen=True)
class Region:
    key: str
    name: str
    description: str
    boxes: tuple[tuple[int, int, int, int], ...]  # (lat_min, lat_max, lon_min, lon_max) inclusive
    extra: tuple[Tile, ...] = field(default=())

    def tiles(self) -> list[Tile]:
        found = set(self.extra)
        for lat_min, lat_max, lon_min, lon_max in self.boxes:
            for lat in range(lat_min, lat_max + 1):
                for lon in range(lon_min, lon_max + 1):
                    found.add((lat, lon))
        avail = available_tiles()
        return sorted(t for t in found if t in avail)


# Each box lists the south-west corners of the 1x1 degree tiles it contains.
PRESETS: tuple[Region, ...] = (
    Region("alps", "Alps (complete)",
           "The whole Alpine arc from Nice to Vienna.",
           ((43, 47, 5, 15), (47, 47, 16, 16))),
    Region("swiss_alps", "Swiss Alps",
           "Valais, Bernese Oberland, Graubünden and Ticino.",
           ((45, 46, 6, 10),)),
    Region("tyrol", "Tyrol & Salzburg",
           "North/East Tyrol, Zillertal, Hohe Tauern and Salzburg mountains.",
           ((46, 47, 10, 13),)),
    Region("bavarian_alps", "Bavarian Alps & Allgäu",
           "Zugspitze, Karwendel, Berchtesgaden and the Allgäu.",
           ((47, 47, 9, 13),)),
    Region("dolomites", "Dolomites & South Tyrol",
           "Dolomites, Ortler/Ortles and Südtirol.",
           ((46, 46, 10, 12),)),
    Region("french_alps", "French Alps",
           "Mont Blanc, Savoie, Dauphiné and the Southern Alps.",
           ((44, 45, 5, 7), (43, 43, 6, 7))),
    Region("julian_alps", "Julian Alps & Carinthia",
           "Slovenia's Julian Alps, Karawanks and Carinthia.",
           ((45, 46, 13, 15),)),
    Region("pyrenees", "Pyrenees",
           "From the Basque coast to Catalonia.",
           ((42, 42, -2, 2), (43, 43, -2, 0))),
    Region("tatra", "Tatra & Carpathians (Slovakia/Poland)",
           "High and Low Tatras.",
           ((48, 49, 18, 21),)),
    Region("norway_fjords", "Norwegian Fjords",
           "Sognefjord, Hardanger and Geirangerfjord.",
           ((59, 62, 5, 8),)),
    Region("scottish_highlands", "Scottish Highlands",
           "Glencoe, Ben Nevis, Skye and the Cairngorms.",
           ((56, 57, -7, -3),)),
    Region("black_forest", "Black Forest & Vosges",
           "South-west Germany and Alsace low mountains.",
           ((47, 48, 6, 8),)),
)


def preset(key: str) -> Region:
    for region in PRESETS:
        if region.key == key:
            return region
    raise KeyError(key)


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug or "custom"


def bounds(tiles: list[Tile]) -> str:
    if not tiles:
        return "-"
    lats = [t[0] for t in tiles]
    lons = [t[1] for t in tiles]

    def fmt(value: int, pos: str, neg: str) -> str:
        return f"{abs(value)}°{pos if value >= 0 else neg}"

    return (f"{fmt(min(lats), 'N', 'S')}–{fmt(max(lats) + 1, 'N', 'S')}, "
            f"{fmt(min(lons), 'E', 'W')}–{fmt(max(lons) + 1, 'E', 'W')}")
