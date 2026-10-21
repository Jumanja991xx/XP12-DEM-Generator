"""Informational CI check: can the elevation sources be reached from the runner?

Fetches one tile per source at a low resolution (121 x 121 posts) and prints
one line per source; the workflow turns the output into a notice annotation.
"""

import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xp12dem import raster as R  # noqa: E402
from xp12dem.sources import SOURCES, Context  # noqa: E402

PROBES = [
    ("gsi", 35, 138, "Mt Fuji ~3776 m"),
    ("ga", -34, 150, "Blue Mountains ~1100 m"),
    ("linz", -45, 168, "Remarkables ~2300 m"),
    ("hrdem", 51, -116, "Banff area"),
    ("mrdem", 51, -116, "Banff area"),
    ("arcticdem", 67, -51, "Kangerlussuaq"),
    ("glo30", 47, 11, "Innsbruck area"),
    ("usgs", 39, -106, "Colorado ~4300 m"),
    ("fabdem", 47, 18, "Budapest hills"),
]


def main() -> int:
    session = requests.Session()
    session.headers["User-Agent"] = "XP12-DEM-Generator CI probe"
    cache = Path(tempfile.mkdtemp())
    for key, lat, lon, what in PROBES:
        ctx = Context(session, cache)
        t = time.time()
        try:
            grid = SOURCES[key].fetch(ctx, lat, lon, 121)
            print(f"{key}: OK {what}: max {np.nanmax(grid):.0f} m, valid {R.valid_fraction(grid):.0%}, "
                  f"{ctx._downloaded / 1e6:.1f} MB, {time.time() - t:.0f} s")
        except Exception as exc:
            print(f"{key}: FAILED {type(exc).__name__}: {str(exc)[:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
