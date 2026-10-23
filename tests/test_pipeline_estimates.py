import numpy as np

from xp12dem import pipeline


def _hgt(tmp_path, name, relief_m):
    size = 1201
    y, x = np.mgrid[0:size, 0:size]
    z = 500 + relief_m * (np.sin(x / 12) * np.cos(y / 15) + 1) / 2
    path = tmp_path / name
    z.astype(">i2").tofile(path)
    return path


def test_automatic_mesh_detail_follows_the_terrain(tmp_path):
    flat = pipeline.auto_detail(_hgt(tmp_path, "flat.hgt", 20), 47)[0]
    hills = pipeline.auto_detail(_hgt(tmp_path, "hills.hgt", 500), 47)[0]
    alps = pipeline.auto_detail(_hgt(tmp_path, "alps.hgt", 1500), 47)[0]
    assert (flat.key, hills.key, alps.key) == ("balanced", "high", "maximum")
    assert pipeline.mesh_detail("auto").key == "auto"
    assert [d.key for d in pipeline.MESH_CHOICES] == ["auto", "high", "maximum"]
