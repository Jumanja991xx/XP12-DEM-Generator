import numpy as np
from PIL import Image

from xp12dem import imagery as I
from xp12dem import imagery
import io


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


class _Flat(imagery.Layer):
    """Synthetic layer: smooth terrain colour, optionally with a darker 'flight block'."""

    def __init__(self, key, factor_left=1.0, factor_right=1.0, balance=True):
        self.key, self.fl, self.fr, self.balance = key, factor_left, factor_right, balance

    @property
    def lut(self):
        return None

    def lowres(self, tex, ctx):
        px = imagery.LOW_PX
        x = (np.arange(px) + tex.col / imagery.TEX_TILES * px)[None, :].repeat(px, 0)
        base = 90 + 20 * np.sin(x / 50.0)
        factor = np.where(x < 2 * px, self.fl, self.fr)  # step at a fixed world position
        rgb = np.clip(base[..., None] * factor[..., None] * np.array([1.0, 1.1, 0.8]), 0, 255)
        return np.dstack([rgb, np.full((px, px), 255.0)]).astype(np.uint8)


def test_balancer_removes_a_flight_step():
    ref = _Flat("ref", balance=False)
    src = _Flat("src", 0.7, 1.0)  # left part of the world 30 % darker
    bal = imagery.Balancer(ref)
    px = imagery.LOW_PX
    tex = imagery.TextureId(1600, 2 * imagery.TEX_TILES - 16 * 0 + 16, 16)  # step at its left edge region
    for col in (16, 32, 48):  # blocks left of, across and right of the step
        t = imagery.TextureId(1600, col, 16)
        gain = bal.gain(src, t, None, px)
        low = src.lowres(t, None)[..., :3].astype(np.float32)
        fixed = (low + 10) * gain - 10
        target = ref.lowres(t, None)[..., :3].astype(np.float32)
        inner = np.s_[20:-20, 20:-20]
        assert np.abs(fixed[inner] - target[inner]).mean() < 6, col
    assert bal.gain(ref, tex, None, px) is None  # the reference itself is never changed


def test_wms_layer_stitches_parts_for_small_size_limits():
    class Ctx:
        pool = imagery.ThreadPoolExecutor(4)
        calls = []

        def get(self, url, params, key):
            self.calls.append(params["WIDTH"])
            img = Image.new("RGBA", (params["WIDTH"], params["HEIGHT"]), (10, 20, 30, 255))
            buf = io.BytesIO()
            img.save(buf, "PNG")
            return buf.getvalue()

    layer = imagery.SouthTyrolOrtho()
    ctx = Ctx()
    out = layer._get_rgba(ctx, imagery.TextureId(1600, 1600, 16), 4096, "image/png")
    assert out.shape == (4096, 4096, 4) and (out[..., 2] == 30).all()
    assert max(ctx.calls) <= layer.max_px and len(ctx.calls) == 16  # 4 x 4 parts of 1024 px
