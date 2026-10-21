import io
import zipfile

import numpy as np
import pytest
import tifffile

from xp12dem import dem, dem_sources as DS


def _geotiff(data, west, north, step, pixel_is_area=True, nodata=None):
    """In-memory geographic GeoTIFF like USGS 3DEP / FABDEM tiles."""
    buf = io.BytesIO()
    extratags = [
        (33550, "d", 3, (step, step, 0.0)),
        (33922, "d", 6, (0.0, 0.0, 0.0, west, north, 0.0)),
        # GeoKeyDirectory: version 1.1.0, 3 keys: model=geographic, raster type, GCS=4326
        (34735, "H", 16, (1, 1, 0, 3, 1024, 0, 1, 2, 1025, 0, 1, 1 if pixel_is_area else 2,
                          2048, 0, 1, 4326)),
    ]
    if nodata is not None:
        extratags.append((42113, "s", 0, str(nodata)))
    tifffile.imwrite(buf, data.astype(np.float32), extratags=extratags)
    return buf.getvalue()


def _ramp(n, step, west, north):
    """Elevation = 100 * lon + 10 * lat at pixel centres (area convention)."""
    lons = west + (np.arange(n) + 0.5) * step
    lats = north - (np.arange(n) + 0.5) * step
    return 100.0 * lons[None, :] + 10.0 * lats[:, None]


def test_names():
    assert DS.usgs_name(39, -106) == "n40w106"
    assert DS.fabdem_zip_name(47, 18) == "N40E010-N50E020_FABDEM_V1-2.zip"
    assert DS.fabdem_zip_name(-3, -61) == "S10W070-N00W060_FABDEM_V1-2.zip"
    assert DS.fabdem_member(47, 18) == "N47E018_FABDEM_V1-2.tif"


def test_coverage_classes():
    assert DS.tile_info((47, 11)).quality == DS.LIDAR       # Innsbruck
    assert DS.tile_info((47, 18)).quality == DS.FABDEM      # Budapest hills
    assert DS.tile_info((47, 18)).has_sonny
    assert DS.tile_info((39, -106)).quality == DS.USGS      # Colorado
    assert DS.tile_info((59, 17)).quality == DS.NATIONAL    # Stockholm
    assert DS.tile_info((0, -150)) is None                  # Pacific


def test_geotiff_resampled_onto_posts():
    step = 1 / 120
    n = 120 + 2 * 6  # USGS style: 6 pixel overlap on each side
    west, north = 11 - 6 * step, 48 + 6 * step
    raster = DS.read_geotiff(io.BytesIO(_geotiff(_ramp(n, step, west, north), west, north, step)))
    grid = DS.raster_to_grid([raster], 47, 11, n=121)
    expected = 100.0 * (11 + np.arange(121) / 120)[None, :] + 10.0 * (48 - np.arange(121) / 120)[:, None]
    assert np.allclose(grid, expected, atol=0.05)


def test_nodata_becomes_nan():
    data = np.full((10, 10), 500.0)
    data[2, 3] = -9999
    raster = DS.read_geotiff(io.BytesIO(_geotiff(data, 0, 1, 0.1, nodata=-9999)))
    assert np.isnan(raster.data[2, 3]) and np.nanmax(raster.data) == 500.0


class _Resp:
    def __init__(self, status, content=b"", headers=None):
        self.status_code, self.content = status, content
        self.headers = headers or {"Content-Length": str(len(content))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def iter_content(self, chunk_size):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]


class _Server:
    """Fake HTTP: whole files and byte ranges."""

    def __init__(self, files, ranges=True):
        self.files, self.ranges, self.headers, self.calls = files, ranges, {}, []

    def head(self, url, timeout, allow_redirects=True):
        if url not in self.files:
            return _Resp(404)
        hdr = {"Content-Length": str(len(self.files[url]))}
        if self.ranges:
            hdr["Accept-Ranges"] = "bytes"
        return _Resp(200, b"", hdr)

    def get(self, url, stream=False, timeout=None, headers=None):
        self.calls.append((url, (headers or {}).get("Range")))
        if url not in self.files:
            return _Resp(404)
        body = self.files[url]
        if headers and "Range" in headers and self.ranges:
            a, b = headers["Range"].split("=")[1].split("-")
            part = body[int(a):int(b) + 1]
            return _Resp(206, part, {"Content-Length": str(len(part)),
                                     "Content-Range": f"bytes {a}-{int(a) + len(part) - 1}/{len(body)}"})
        return _Resp(200, body)


def _tile_tif(lat, lon, value):
    step = 1 / 60
    data = np.full((60, 60), float(value))
    return _geotiff(data, lon, lat + 1, step)


def test_usgs_tile_for_usa(tmp_path):
    server = _Server({DS.usgs_url(39, -106): _tile_tif(39, -106, 3000)})
    store = dem.DemStore(tmp_path, session=server)
    path, source = store.ensure_with_source(39, -106, 1)
    assert source == "usgs" and path.stat().st_size == dem.SIZE_1S
    grid = np.fromfile(path, ">i2")
    assert grid.min() == 3000 and grid.max() == 3000


def test_fabdem_member_read_with_range_requests(tmp_path):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("N47E018_FABDEM_V1-2.tif", _tile_tif(47, 18, 400))
        zf.writestr("N47E019_FABDEM_V1-2.tif", b"x" * 3_000_000)  # must not be downloaded
    url = DS.FABDEM_BASE + DS.fabdem_zip_name(47, 18)
    server = _Server({url: archive.getvalue()})
    store = dem.DemStore(tmp_path, session=server)
    path, source = store.ensure_with_source(47, 18, 3)
    assert source == "fabdem" and path.stat().st_size == dem.SIZE_3S
    fetched = sum(int(rng.split("-")[1]) - int(rng.split("=")[1].split("-")[0]) + 1
                  for url_, rng in server.calls if rng and url_ == url)
    assert fetched < 3_000_000  # only the needed part was read


def test_fabdem_falls_back_to_sonny_with_warning(tmp_path):
    sonny_zip = io.BytesIO()
    with zipfile.ZipFile(sonny_zip, "w") as zf:
        zf.writestr("N47E018.hgt", b"\0\1" * (dem.SIZE_1S // 2))
    server = _Server({dem.MIRROR_URL.format(name="N47E018"): sonny_zip.getvalue()}, ranges=False)
    store = dem.DemStore(tmp_path, session=server)
    _, source = store.ensure_with_source(47, 18, 1)
    assert source == "sonny"
    assert store.warnings and "FABDEM" in store.warnings[0] and "Download" in store.warnings[0]


def test_fabdem_from_local_archive(tmp_path):
    folder = tmp_path / "downloads"
    folder.mkdir()
    with zipfile.ZipFile(folder / DS.fabdem_zip_name(45, 15), "w") as zf:
        zf.writestr("N45E015_FABDEM_V1-2.tif", _tile_tif(45, 15, 250))
    store = dem.DemStore(tmp_path / "cache", local_source=folder, session=_Server({}))
    path, source = store.ensure_with_source(45, 15, 1)
    assert source == "fabdem" and np.fromfile(path, ">i2").max() == 250


def test_own_geotiff_wins(tmp_path):
    folder = tmp_path / "mine"
    folder.mkdir()
    (folder / "lidar_n40w106_1m.tif").write_bytes(_tile_tif(39, -106, 1234))
    store = dem.DemStore(tmp_path / "cache", local_source=folder, session=_Server({}))
    assert store.candidates(39, -106)[0] == "local"
    path, source = store.ensure_with_source(39, -106, 1)
    assert source == "local" and np.fromfile(path, ">i2").max() == 1234


def test_legacy_sonny_cache_is_reused(tmp_path):
    legacy = tmp_path / "1s" / "N47E011.hgt"  # version 0.1 layout (Sonny only)
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"\0" * dem.SIZE_1S)
    store = dem.DemStore(tmp_path, session=_Server({}))
    path, source = store.ensure_with_source(47, 11, 1)
    assert source == "sonny" and path == legacy and store.has(47, 11, 1)


def test_per_source_cache_of_version_01_is_adopted(tmp_path):
    old = tmp_path / "1s" / "usgs" / "N39W106.hgt"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"\0" * dem.SIZE_1S)
    store = dem.DemStore(tmp_path, session=_Server({}))
    assert store.is_cached(39, -106, 1)
    path, source = store.ensure_with_source(39, -106, 1)
    assert source == "usgs" and path == tmp_path / "1s" / "N39W106.hgt" and not old.exists()


def test_no_source_raises_with_reasons(tmp_path):
    store = dem.DemStore(tmp_path, session=_Server({}))
    with pytest.raises(RuntimeError) as err:
        store.ensure_with_source(39, -106, 1)
    assert "USGS" in str(err.value) and "FABDEM" in str(err.value)
