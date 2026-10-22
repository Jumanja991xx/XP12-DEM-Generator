"""Level aprons in new Ortho4XP meshes.

Ortho4XP shapes runways and taxiways from fitted height profiles, but leaves
aprons on the (airport-blurred) elevation raster. On a sloped airfield -
Courchevel's runway climbs 18 % - the blur drags the runway's slope into the
parking areas, and aircraft stand visibly tilted. Real aprons are built almost
level, so each apron gets a plane fitted to the terrain under it, with its
gradient limited to MAX_SLOPE. Runways keep their real profile.
"""

from __future__ import annotations

import math
from typing import Callable

import numpy as np

MAX_SLOPE = 0.015     # 1.5 %: steeper than ICAO allows for aprons, flat enough to stand level
SAMPLE_M = 8.0        # terrain samples every ~8 m inside an apron
EDGE_M = 15.0         # boundary vertices every ~15 m
MIN_AREA_M2 = 400.0   # ignore slivers left after cutting out runways and taxiways


def fit_plane(x: np.ndarray, y: np.ndarray, z: np.ndarray,
              max_slope: float = MAX_SLOPE) -> tuple[float, float, float]:
    """Least-squares plane ``z = a + b*x + c*y`` (x, y in metres) whose
    gradient is limited to ``max_slope``."""
    a_mat = np.column_stack([np.ones_like(x), x, y])
    (a, b, c), *_ = np.linalg.lstsq(a_mat, z, rcond=None)
    grad = math.hypot(b, c)
    if grad > max_slope:
        b, c = b * max_slope / grad, c * max_slope / grad
        a = float(np.mean(z - b * x - c * y))
    return float(a), float(b), float(c)


def level_aprons(tile, dico_airports, vector_map, patches_list, VECT,
                 log: Callable[[str], None] = lambda text: None) -> int:
    """Encode every apron as a gently sloped plane. Returns the number of aprons."""
    import shapely
    from shapely import ops

    lat_m = 111_320.0
    lon_m = lat_m * math.cos(math.radians(tile.lat + 0.5))
    count = 0
    for name, apt in dico_airports.items():
        if name in patches_list:
            continue
        try:
            aprons = apt["apron"][0]
        except (KeyError, IndexError, TypeError):
            continue
        if aprons is None or aprons.is_empty:
            continue
        # runways and taxiways are already shaped by Ortho4XP, hangars are levelled by it
        taken = ops.unary_union([VECT.improved_buffer(apt["runway"][0], 5, 0, 0),
                                 apt["taxiway"][0], apt["hangar"]])
        pieces = VECT.ensure_MultiPolygon(VECT.cut_to_tile(aprons.difference(taken)))
        for piece in pieces.geoms:
            if not piece.is_valid or piece.area * lat_m * lon_m < MIN_AREA_M2:
                continue
            xmin, ymin, xmax, ymax = piece.bounds
            nx = max(2, int((xmax - xmin) * lon_m / SAMPLE_M) + 1)
            ny = max(2, int((ymax - ymin) * lat_m / SAMPLE_M) + 1)
            gx, gy = np.meshgrid(np.linspace(xmin, xmax, nx), np.linspace(ymin, ymax, ny))
            gx, gy = gx.ravel(), gy.ravel()
            inside = shapely.contains_xy(piece, gx, gy)
            if inside.sum() < 3:
                continue
            pts = np.column_stack([gx[inside], gy[inside]])
            z = np.asarray(tile.dem.alt_vec(pts), dtype=float).ravel()
            a, b, c = fit_plane(pts[:, 0] * lon_m, pts[:, 1] * lat_m, z)

            def plane(way: np.ndarray) -> np.ndarray:
                return (a + b * way[:, 0] * lon_m + c * way[:, 1] * lat_m).reshape(-1, 1)

            for ring in [piece.exterior, *piece.interiors]:
                way = np.round(VECT.refine_way(np.array(ring.coords), EDGE_M), 7)
                vector_map.insert_way(np.hstack([way, plane(way)]), "APRON", check=True)
            seed = np.array(piece.representative_point().coords[0])
            vector_map.seeds.setdefault("APRON", []).append(seed)
            count += 1
    if count:
        log(f"Levelled {count} apron area(s) (max. {MAX_SLOPE:.1%} slope)")
    return count


def install(apt_module, vect_module, log: Callable[[str], None]) -> None:
    """Run :func:`level_aprons` right after Ortho4XP has encoded runways and taxiways."""
    original = apt_module.encode_runways_taxiways_and_aprons

    def encode(tile, airport_layer, dico_airports, vector_map, patches_list):
        area = original(tile, airport_layer, dico_airports, vector_map, patches_list)
        try:
            level_aprons(tile, dico_airports, vector_map, patches_list, vect_module, log)
        except Exception as exc:  # never fail a tile over this refinement
            log(f"Aprons left as they are ({type(exc).__name__}: {exc})")
        return area

    apt_module.encode_runways_taxiways_and_aprons = encode
