"""CI helper: pre-seed an empty OSM cache for one tile (no Overpass needed).

usage: python packaging/ci_seed_osm.py <XP12DEM_DATA> <lat> <lon>
"""

import bz2
import math
import sys
from pathlib import Path

EMPTY = b"<?xml version='1.0' encoding='UTF-8'?>\n<osm version='0.6' generator='ci'>\n</osm>\n"


def main(data_root: str, lat: int, lon: int) -> None:
    name = f"{lat:+03d}{lon:+04d}"
    group = f"{math.floor(lat / 10) * 10:+03d}{math.floor(lon / 10) * 10:+04d}"
    folder = Path(data_root) / "ortho4xp" / "OSM_data" / group / name
    folder.mkdir(parents=True, exist_ok=True)
    for layer in ("airports", "big_roads", "small_roads", "coastline", "water"):
        (folder / f"{name}_{layer}.osm.bz2").write_bytes(bz2.compress(EMPTY))
    print(f"seeded {folder}")


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]))
