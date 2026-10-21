import io
import zipfile

import numpy as np
import pytest

from xp12dem import dem


def _write_1s(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    values.astype(">i2").tofile(path)


def test_downsample_keeps_shared_posts_and_borders(tmp_path):
    rows = np.arange(3601, dtype=np.int32)[:, None]
    grid = np.broadcast_to(rows, (3601, 3601)).astype(np.int16)  # linear ramp
    src = tmp_path / "N47E011.hgt"
    _write_1s(src, grid)
    dst = tmp_path / "3s" / "N47E011.hgt"
    dem.downsample_to_3s(src, dst)
    out = np.fromfile(dst, ">i2").reshape(1201, 1201)
    assert dst.stat().st_size == dem.SIZE_3S
    # a 3x3 mean of a linear ramp equals the centre value (except clamped edges)
    assert out[600, 600] == grid[1800, 1800]
    assert out[1, 5] == grid[3, 15]
    assert out[0, 0] == round((0 + 0 + 1) / 3)


def test_downsample_ignores_partial_nodata_and_keeps_full_holes(tmp_path):
    grid = np.full((3601, 3601), 100, dtype=np.int16)
    grid[3001:3003, 3001:3003] = -32768  # partial: every 3x3 window keeps data
    grid[600:612, 600:612] = -32768  # contains complete windows -> stays nodata
    src = tmp_path / "a.hgt"
    _write_1s(src, grid)
    dst = tmp_path / "b.hgt"
    dem.downsample_to_3s(src, dst)
    out = np.fromfile(dst, ">i2").reshape(1201, 1201)
    assert out[1000:1002, 1000:1002].tolist() == [[100, 100], [100, 100]]
    assert out[201, 201] == -32768
    assert set(np.unique(out)) == {100, -32768}


def _zip_with(name, payload):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(f"{name}.hgt", payload)
    return buf.getvalue()


def test_extract_validates_size(tmp_path):
    with pytest.raises(ValueError):
        dem._extract_hgt(_zip_with("N47E011", b"\0" * 10), "N47E011", tmp_path / "x.hgt", 1)
    ok = _zip_with("N47E011", b"\0" * dem.SIZE_1S)
    dem._extract_hgt(ok, "N47E011", tmp_path / "x.hgt", 1)
    assert (tmp_path / "x.hgt").stat().st_size == dem.SIZE_1S


class _FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status
        self.headers = {"Content-Length": str(len(payload))}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def iter_content(self, chunk_size):
        for i in range(0, len(self.payload), chunk_size):
            yield self.payload[i:i + chunk_size]


class _FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.headers = {}
        self.urls = []

    def get(self, url, stream, timeout):
        self.urls.append(url)
        return _FakeResponse(self.payload)


def test_store_downloads_once_and_builds_3s(tmp_path):
    session = _FakeSession(_zip_with("N47E011", b"\0\1" * (dem.SIZE_1S // 2)))
    store = dem.DemStore(tmp_path, session=session)
    seen = []
    path = store.ensure(47, 11, 3, progress=lambda d, t: seen.append((d, t)))
    assert path == tmp_path / "3s" / "N47E011.hgt"
    assert store.has(47, 11, 1) and store.has(47, 11, 3)
    assert session.urls == [dem.MIRROR_URL.format(name="N47E011")]
    assert seen and seen[-1][0] == seen[-1][1]
    store.ensure(47, 11, 1)
    assert len(session.urls) == 1  # cached


def test_store_prefers_local_folder(tmp_path):
    local = tmp_path / "sonny" / "Austria"
    local.mkdir(parents=True)
    (local / "N47E011.hgt").write_bytes(b"\0" * dem.SIZE_1S)
    session = _FakeSession(b"")
    store = dem.DemStore(tmp_path / "cache", local_source=tmp_path / "sonny", session=session)
    store.ensure(47, 11, 1)
    assert session.urls == []
