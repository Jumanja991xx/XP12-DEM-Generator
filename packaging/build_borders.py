"""Build xp12dem/resources/borders.json (country/state outlines used to keep each
photo source inside its own area) from OpenStreetMap boundary relations.

Usage:  python packaging/build_borders.py <dir with relation_<id>.xml files>
        (download: https://api.openstreetmap.org/api/0.6/relation/<id>/full)
"""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from shapely.geometry import LineString, MultiPolygon, Polygon, mapping
from shapely.ops import linemerge, polygonize, unary_union

RELATIONS = {"austria": 16239, "tirol": 52343}
TOLERANCE = 0.00008  # ~6-9 m


def outline(path: Path, rel_id: int):
    root = ET.parse(path).getroot()
    nodes = {n.get("id"): (float(n.get("lon")), float(n.get("lat"))) for n in root.iter("node")}
    ways = {w.get("id"): [nodes[nd.get("ref")] for nd in w.iter("nd") if nd.get("ref") in nodes]
            for w in root.iter("way")}
    rel = next(r for r in root.iter("relation") if r.get("id") == str(rel_id))
    lines = [LineString(ways[m.get("ref")]) for m in rel.iter("member")
             if m.get("type") == "way" and m.get("role") in ("outer", "") and len(ways.get(m.get("ref"), [])) > 1]
    polys = list(polygonize(linemerge(lines)))
    geom = unary_union(polys)
    return geom.simplify(TOLERANCE, preserve_topology=True)


def main(folder: Path) -> None:
    out = {}
    for name, rel_id in RELATIONS.items():
        geom = outline(folder / f"rel_{rel_id}.xml", rel_id)
        if isinstance(geom, Polygon):
            geom = MultiPolygon([geom])
        out[name] = {"osm_relation": rel_id, "geometry": mapping(geom)}
        print(name, geom.geom_type, len(geom.geoms), "area", round(geom.area, 3),
              "points", sum(len(p.exterior.coords) for p in geom.geoms))
    target = Path(__file__).resolve().parents[1] / "xp12dem" / "resources" / "borders.json"
    data = {"about": "Outlines from OpenStreetMap (ODbL), simplified ~8 m. Built by packaging/build_borders.py.",
            "areas": out}
    target.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    print(target, target.stat().st_size // 1024, "KB")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
