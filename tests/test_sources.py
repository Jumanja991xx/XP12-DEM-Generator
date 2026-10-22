import io
import json

import numpy as np
import pytest
import tifffile
from PIL import Image

from xp12dem import dem, raster as R, sources as S
from xp12dem.dem_sources import tile_info


def _projected_tiff(path, data, west, north, step, epsg, tiled=False):
    """GeoTIFF in a projected CRS (like swissALTI3D / NZTM / Canada LCC)."""
    extratags = [
        (33550, "d", 3, (step, step, 0.0)),
        (33922, "d", 6, (0.0, 0.0, 0.0, west, north, 0.0)),
        # model = projected, raster = area, projected CRS = epsg
        (34735, "H", 16, (1, 1, 0, 3, 1024, 0, 1, 1, 1025, 0, 1, 1, 3072, 0, 1, epsg)),
        (42113, "s", 0, "-9999"),
    ]
    kwargs = {"tile": (64, 64)} if tiled else {}
    tifffile.imwrite(path, data.astype(np.float32), extratags=extratags, **kwargs)


def test_local_projected_geotiff_is_reprojected(tmp_path):
    """A 'swissALTI3D'-like file in LV95 (EPSG:2056) is found by its extent."""
    folder = tmp_path / "own"
    folder.mkdir()
    # 20 km x 20 km around Zermatt at 20 m, height = 1000 + x_km * 10
    west, north, step, size = 2615000.0, 1105000.0, 20.0, 1000
    xs = west + (np.arange(size) + 0.5) * step
    data = np.repeat((1000.0 + (xs - west) / 100.0)[None, :], size, axis=0)
    data[:50, :50] = -9999  # nodata corner
    _projected_tiff(folder / "swissalti3d_2019_2615-1085_2_2056_5728.tif", data, west, north, step,
                    2056, tiled=True)
    ctx = S.Context(None, tmp_path / "cache", folder)
    local = S.SOURCES["local"]
    assert local.files_for(ctx, 45, 7) and not local.files_for(ctx, 47, 11)
    assert local.native_m_for(ctx, 45, 7) == pytest.approx(20.0)
    grid = local.fetch(ctx, 45, 7, 361)
    # Zermatt (45.98 N, 7.75 E) lies inside the file; LV95 x ~ 2624 km -> ~1090 m
    x, y = R.lonlat_to(2056, 7.75, 45.98)
    expected = 1000 + (x - west) / 100.0
    row, col = int(round((46 - 45.98) * 360)), int(round(0.75 * 360))
    assert grid[row, col] == pytest.approx(expected, abs=2.0)
    assert 0.001 < R.valid_fraction(grid) < 0.2  # only the file's footprint


def test_store_prefers_own_projected_files_and_fills_gaps(tmp_path):
    folder = tmp_path / "own"
    folder.mkdir()
    west, north, step, size = 2615000.0, 1105000.0, 50.0, 200
    _projected_tiff(folder / "dtm.tif", np.full((size, size), 2000.0), west, north, step, 2056)

    class Sonny(S.Source):
        key = "sonny"

        def fetch(self, ctx, lat, lon, n):
            return np.full((n, n), 500.0, dtype=np.float32)

    saved = S.SOURCES["sonny"]
    S.SOURCES["sonny"] = Sonny()
    try:
        store = dem.DemStore(tmp_path / "cache", local_source=folder, session=None)
        assert store.candidates(46, 7)[:2] == ["local", "sonny"]
        path, main = store.ensure_with_source(46, 7, "1")
        grid = R.read_hgt(path)
        assert main == "local" and grid.max() == 2000 and grid.min() == 500
        assert "your own DEM files + Sonny" in store.describe(46, 7)
        # blended edge: values between the two sources exist
        assert ((grid > 600) & (grid < 1900)).any()
    finally:
        S.SOURCES["sonny"] = saved


def test_feather_fill_blends_towards_gaps():
    primary = np.full((50, 50), 100.0, dtype=np.float32)
    primary[:, 25:] = np.nan
    secondary = np.full((50, 50), 0.0, dtype=np.float32)
    R.feather_fill(primary, secondary, 10)
    assert not np.isnan(primary).any()
    assert primary[0, 0] == 100.0 and primary[0, 30] == 0.0
    assert 0.0 < primary[0, 20] < 100.0  # ramp inside the better source
    assert np.all(np.diff(primary[0, :26]) <= 0)  # monotonic, no step


def test_linz_sheet_names():
    linz = S.LinzSource()
    # Queenstown survey sheet CB11_10000_0501 starts at NZTM 1252000 / 5017200
    assert linz.subsheet(1252100, 5017100) == "CB11_10000_0501"
    assert linz.subsheet(1276100, 5017100) == "CB12_10000_0501"
    assert linz.subsheet(1252100, 5009900) == "CC11_10000_0101"
    assert linz.sheet(1252100, 5017100) == "CB11"


def test_glo30_and_usgs_urls():
    assert S.Glo30Source().url(47, 11).endswith(
        "Copernicus_DSM_COG_10_N47_00_E011_00_DEM/Copernicus_DSM_COG_10_N47_00_E011_00_DEM.tif")
    assert S.Glo30Source().url(-45, -71).endswith("Copernicus_DSM_COG_10_S45_00_W071_00_DEM.tif")
    assert S.UsgsSource().url(39, -106, "13").endswith("13/TIFF/current/n40w106/USGS_13_n40w106.tif")


def test_coverage_chains():
    assert tile_info((-45, 168)).sources[0] == "linz"
    assert tile_info((51, -116)).sources[:2] == ("hrdem", "mrdem")
    assert tile_info((35, 138)).sources[0] == "gsi"
    assert tile_info((67, -51)).sources[0] == "arcticdem"
    assert tile_info((47, 11)).sources[0] == "sonny"
    assert tile_info((39, -106)).fine and not tile_info((47, 11)).fine
    assert all(info.sources[-1] == "glo30" for info in [tile_info((-45, 168)), tile_info((0, 10))])


class _PngServer:
    """Fake GSI tile server: elevation = 123.45 m except one sea tile (404)."""

    def __init__(self):
        self.headers, self.count = {}, 0

    def get(self, url, stream=False, timeout=None, headers=None):
        self.count += 1
        value = int(round(123.45 / 0.01))
        rgb = np.zeros((256, 256, 3), dtype=np.uint8)
        rgb[..., 0], rgb[..., 1], rgb[..., 2] = value >> 16, (value >> 8) & 255, value & 255
        rgb[:10, :10] = (128, 0, 0)  # 2**23 = no data
        buf = io.BytesIO()
        Image.fromarray(rgb).save(buf, "PNG")
        body = buf.getvalue()

        class Resp:
            status_code = 200
            headers = {"Content-Length": str(len(body))}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def raise_for_status(self):
                pass

            def iter_content(self, chunk_size):
                yield body

        return Resp()


def test_gsi_png_decoding(tmp_path):
    server = _PngServer()
    ctx = S.Context(server, tmp_path)
    grid = S.GsiSource().fetch(ctx, 35, 138, 121)
    assert server.count > 100  # zoom 13 tiles over the whole degree
    assert np.nanmax(grid) == pytest.approx(123.45, abs=0.01)


def test_arcticdem_heights_are_shifted_to_sea_level(tmp_path, monkeypatch):
    """Ellipsoidal ArcticDEM heights are calibrated against GLO-30 (geoid ~ +40 m)."""
    terrain = np.linspace(0, 800, 3601, dtype=np.float32)[None, :].repeat(3601, axis=0)

    def glo30(self, ctx, lat, lon, n):
        return R.upsample(terrain, n)

    monkeypatch.setattr(S.Glo30Source, "fetch", glo30)
    shifted = S.ArcticSource._to_sea_level(S.Context(None, tmp_path), terrain + 40.0, 67, -51)
    assert np.allclose(shifted, terrain, atol=0.5)


def test_sonny_half_arcsecond_zip_in_own_folder(tmp_path):
    """Sonny's 0.5" Austria tiles (7201 x 7201 .hgt, zipped) work without unpacking."""
    import zipfile as zf

    folder = tmp_path / "own"
    folder.mkdir()
    size = 7201
    data = np.full((size, size), 2000, dtype=">i2")
    data[:, size // 2:] = 2500
    with zf.ZipFile(folder / "N47E011.zip", "w") as archive:
        archive.writestr("N47E011.hgt", data.tobytes())
    store = dem.DemStore(tmp_path / "cache", local_source=folder, session=None)
    assert store.candidates(47, 11)[0] == "local"
    assert store.fine_available(47, 11)  # ~15 m posts: worth 1/3"
    path, main = store.ensure_with_source(47, 11, "13")
    grid = R.read_hgt(path)
    assert main == "local" and grid.shape == (10801, 10801)
    assert grid[100, 100] == 2000 and grid[100, -100] == 2500
