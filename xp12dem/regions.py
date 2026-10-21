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
    """Land tiles for which at least one elevation source exists (see dem_sources)."""
    from .dem_sources import coverage

    return frozenset(coverage())


@dataclass(frozen=True)
class Region:
    key: str
    name: str
    description: str
    boxes: tuple[tuple[int, int, int, int], ...]  # (lat_min, lat_max, lon_min, lon_max) inclusive
    extra: tuple[Tile, ...] = field(default=())
    area: str = "Europe"

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
    Region("budapest", "Budapest & Danube Bend",
           "Buda Hills, Pilis and Visegrád mountains (FABDEM).",
           ((47, 47, 18, 19),)),
    Region("croatia_coast", "Croatian Coast & Velebit",
           "Rijeka to Split, Velebit and the islands (FABDEM).",
           ((43, 45, 14, 16),)),
    Region("colorado", "Colorado Rockies",
           "Denver, Aspen, Vail and the high Rockies (USGS 3DEP).",
           ((38, 40, -108, -105),), area="USA"),
    Region("sierra_nevada", "Sierra Nevada & Yosemite",
           "Lake Tahoe, Yosemite, Mammoth and Mt. Whitney (USGS 3DEP).",
           ((36, 39, -121, -118),), area="USA"),
    Region("cascades", "Seattle & Cascades",
           "Mt. Rainier, Mt. Baker, the Olympics and Puget Sound (USGS 3DEP).",
           ((46, 48, -124, -121),), area="USA"),
    Region("grand_canyon", "Grand Canyon & Sedona",
           "Grand Canyon, Page/Lake Powell and the Mogollon Rim (USGS 3DEP).",
           ((34, 36, -113, -111),), area="USA"),
    Region("alaska_anchorage", "Alaska - Anchorage & Chugach",
           "Anchorage, Chugach Mountains and Prince William Sound (USGS 3DEP).",
           ((60, 61, -151, -146),), area="USA"),
    Region("hawaii", "Hawaii",
           "All main Hawaiian islands (USGS 3DEP).",
           ((18, 22, -161, -154),), area="USA"),
    Region("canadian_rockies", "Canadian Rockies - Banff & Calgary",
           "Calgary, Banff, Lake Louise, Kananaskis and the Columbia Icefield edge "
           "(NRCan LiDAR where surveyed, 30 m national model elsewhere).",
           ((50, 52, -118, -114),), area="Canada"),
    Region("bc_coast", "Vancouver, Whistler & Sea-to-Sky",
           "Vancouver, Squamish, Whistler and the Coast Mountains (NRCan).",
           ((49, 50, -124, -122),), area="Canada"),
    Region("nz_southern_alps", "Southern Alps - Queenstown & Milford",
           "Queenstown, Wanaka, Milford Sound, Mt Cook / Aoraki (LINZ LiDAR 1 m).",
           ((-46, -44, 166, 170),), area="New Zealand"),
    Region("nz_north_island", "Central North Island & Wellington",
           "Ruapehu, Tongariro, Taupo, Napier and Wellington (LINZ LiDAR 1 m).",
           ((-42, -38, 174, 176),), area="New Zealand"),
    Region("japan_fuji", "Mt Fuji, Tokyo & Japanese Alps",
           "Tokyo, Mt Fuji, Matsumoto and the Northern / Southern Alps (GSI 5-10 m).",
           ((35, 36, 137, 139),), area="Japan"),
    Region("iceland", "Iceland",
           "The whole island (Sonny's DTM, ArcticDEM where needed).",
           ((63, 66, -25, -13),), area="Arctic"),
    Region("greenland_west", "West Greenland - Kangerlussuaq & Nuuk",
           "Kangerlussuaq, Nuuk, Sisimiut and the ice-sheet edge (ArcticDEM 10 m).",
           ((63, 67, -54, -50),), area="Arctic"),
    Region("svalbard", "Svalbard",
           "Longyearbyen and Spitsbergen (Sonny's DTM / ArcticDEM).",
           ((77, 79, 10, 20),), area="Arctic"),
    Region("blue_mountains", "Sydney & Blue Mountains",
           "Sydney basin and the Blue Mountains (FABDEM; own LiDAR files from ELVIS possible).",
           ((-35, -33, 149, 151),), area="Australia"),
    Region("tasmania", "Tasmania",
           "Hobart, Launceston and Cradle Mountain (FABDEM).",
           ((-44, -41, 144, 148),), area="Australia"),
    Region("everest", "Himalaya - Everest & Lukla",
           "Lukla, Everest, Lhotse and the Khumbu (FABDEM).",
           ((27, 28, 86, 87),), area="World"),
    Region("patagonia", "Patagonia - Fitz Roy & El Calafate",
           "El Chaltén, Fitz Roy, Perito Moreno glacier (FABDEM).",
           ((-51, -49, -74, -72),), area="World"),
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
