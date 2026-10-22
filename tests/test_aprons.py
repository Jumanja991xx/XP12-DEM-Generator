import sys

import numpy as np
import pytest

from xp12dem import aprons, paths


def test_plane_fit_limits_slope():
    x, y = np.meshgrid(np.arange(0, 200, 10.0), np.arange(0, 100, 10.0))
    x, y = x.ravel(), y.ravel()
    z = 2000 + 0.18 * x  # Courchevel-like 18 % slope
    a, b, c = aprons.fit_plane(x, y, z)
    assert abs(np.hypot(b, c) - aprons.MAX_SLOPE) < 1e-9
    assert abs((a + b * 100 + c * 50) - np.mean(z)) < 1.0  # stays at the mean terrain height
    a, b, c = aprons.fit_plane(x, y, 1500 + 0.005 * y)  # gentle slopes are kept
    assert abs(c - 0.005) < 1e-9 and abs(b) < 1e-9


@pytest.fixture
def o4x_modules():
    root = paths.ortho4xp_root()
    sys.path.insert(0, str(root / "src"))
    from xp12dem.o4x_worker import _install_tkinter_stub

    _install_tkinter_stub()
    import O4_Vector_Utils as VECT
    yield VECT
    sys.path.remove(str(root / "src"))


def test_level_aprons_on_steep_airfield(o4x_modules):
    from shapely import geometry

    VECT = o4x_modules

    class Dem:
        def alt_vec(self, way):  # terrain climbs 18 % towards the east
            return 2000 + 0.18 * way[:, 0] * 111_320 * np.cos(np.radians(45.5))

    class Tile:
        lat, lon, dem = 45, 6, Dem()

    runway = geometry.Polygon([(0.630, 0.395), (0.637, 0.395), (0.637, 0.3954), (0.630, 0.3954)])
    apron = geometry.Polygon([(0.631, 0.3960), (0.634, 0.3960), (0.634, 0.3975), (0.631, 0.3975)])
    airports = {"LFLJ": {"apron": (geometry.MultiPolygon([apron]), []),
                         "runway": (geometry.MultiPolygon([runway]), [], []),
                         "taxiway": (geometry.MultiPolygon(), []),
                         "hangar": geometry.MultiPolygon()}}
    vmap = VECT.Vector_Map()
    assert aprons.level_aprons(Tile(), airports, vmap, [], VECT) == 1
    assert len(vmap.seeds["APRON"]) == 1
    ids = sorted(vmap.nodes_dico)
    nodes = np.array([vmap.nodes_dico[i] for i in ids], dtype=float)
    z = np.array([float(np.ravel(vmap.data_nodes[i])[0]) for i in ids])
    x_m = nodes[:, 0] * 111_320 * np.cos(np.radians(45.5))
    slope = np.polyfit(x_m, z, 1)[0]
    assert slope <= aprons.MAX_SLOPE + 1e-6  # instead of 18 %
