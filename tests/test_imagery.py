import numpy as np
from PIL import Image

from xp12dem import imagery as I


def test_texture_names_and_bbox():
    t = I.TextureId.parse("45872_69632_OF17.dds")
    assert t == I.TextureId(45872, 69632, 17, "OF") and t.name == "45872_69632_OF17.dds"
    lon0, lat0, lon1, lat1 = t.lonlat_bbox()
    assert 11.24 < lon0 < 11.26 and 47.39 < lat0 < 47.41 and lat1 > lat0 and lon1 > lon0
    assert t.covering(16) == I.TextureId(22928, 34816, 16, "OF")
    assert I.TextureId.parse("22784_34864_BI16.dds").code == "BI"


def test_dds_with_mipmaps_is_readable(tmp_path):
    rgb = (np.random.default_rng(1).random((I.TEX_PX, I.TEX_PX, 3)) * 255).astype(np.uint8)
    path = tmp_path / "t.dds"
    I.write_dds(rgb, path)
    data = path.read_bytes()
    assert data[:4] == b"DDS " and int.from_bytes(data[28:32], "little") == 11  # 4096 -> 4 px
    assert len(data) == 128 + sum(max(1, (I.TEX_PX >> i) // 4) ** 2 * 8 for i in range(11))
    assert Image.open(path).size == (I.TEX_PX, I.TEX_PX)


def test_composite_prefers_first_source_and_fills_gaps():
    a = np.zeros((I.TEX_PX, I.TEX_PX, 4), np.uint8)
    a[:, : I.TEX_PX // 2] = (200, 0, 0, 255)          # left half: source A
    b = np.zeros_like(a)
    b[:, I.TEX_PX // 2:] = (0, 0, 200, 255)            # right half: source B (no overlap)
    rgb, missing = I.composite([(I.Layer(), a), (I.Layer(), b)])
    assert missing == 0
    assert tuple(rgb[100, 100]) == (200, 0, 0) and tuple(rgb[100, -100]) == (0, 0, 200)
    rgb, missing = I.composite([(I.Layer(), a)])
    assert 0.49 < missing < 0.51 and tuple(rgb[100, -100]) == (200, 0, 0)  # gap: average colour


def test_weaker_source_is_fitted_to_the_better_one_where_both_overlap():
    rng = np.random.default_rng(3)
    ground = rng.integers(40, 200, (I.TEX_PX, I.TEX_PX, 3)).astype(np.uint8)
    a = np.dstack([ground, np.full(ground.shape[:2], 255, np.uint8)])
    a[:, I.TEX_PX // 2:, 3] = 0                        # A ends in the middle
    darker = (ground * 0.6).astype(np.uint8)            # B: same ground, other flight
    b = np.dstack([darker, np.full(ground.shape[:2], 255, np.uint8)])
    rgb, _ = I.composite([(I.Layer(), a), (I.Layer(), b)])
    right = rgb[:, -500:].astype(float).mean()
    assert abs(right - ground[:, -500:].astype(float).mean()) < 4  # B brought up to A's level
