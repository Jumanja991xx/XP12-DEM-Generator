"""CI helper: report whether the Overpass API is reachable from this machine."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xp12dem import overpass  # noqa: E402

overpass.MAX_ROUNDS = 1
overpass.MAX_SECONDS = 240
answer = overpass.fetch('node["aeroway"="aerodrome"]', (47, 11, 48, 12))
if answer:
    print(f"Overpass OK, {len(answer)} bytes")
else:
    sys.exit(f"Overpass unreachable from this runner: {overpass.failed_queries}")
