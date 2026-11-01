"""ClearScan tree art: species x season billboard atlases.

Every season atlas has the same layout (same crown shapes in the same cells), so the
.for files only differ in their TEXTURE line and X-Plane can swap them by season.

Layout: 4096 x 2048 RGBA, cells of 512 px, 8 columns x 4 rows; SPECIES[i] uses the
cells VARIANTS*i .. VARIANTS*i+VARIANTS-1 (row-major from the top).
"""

import math

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

C = 512
COLS, ROWS = 8, 4
VARIANTS = 3
SEASONS = ("spr", "sum", "fal", "win")

# crown: (width/height of the crown ellipse, crown top, crown bottom as fraction of cell height)
# colours: base RGB per season; fall may list several mixed hues; None in winter = bare
SPECIES = {
    "linde": dict(kind="broad", w=0.84, top=0.03, bot=0.70, clump=(0.024, 0.05), n=1000,
                  bark=(78, 66, 54), col={"spr": [(104, 140, 52)], "sum": [(62, 94, 38)],
                                          "fal": [(170, 150, 50), (150, 128, 44), (118, 112, 46)], "win": None}),
    "ahorn": dict(kind="broad", w=0.90, top=0.04, bot=0.66, clump=(0.026, 0.055), n=950,
                  bark=(84, 74, 62), col={"spr": [(112, 146, 50)], "sum": [(58, 92, 36)],
                                          "fal": [(214, 150, 40), (196, 92, 34), (176, 52, 30)], "win": None}),
    "eiche": dict(kind="broad", w=0.94, top=0.05, bot=0.66, clump=(0.03, 0.065), n=820, lobes=True,
                  bark=(70, 62, 52), col={"spr": [(118, 140, 56)], "sum": [(56, 82, 36)],
                                          "fal": [(150, 110, 48), (126, 92, 44), (104, 96, 44)],
                                          "win": [(122, 90, 56)]}),       # oaks keep dry leaves
    "buche": dict(kind="broad", w=0.86, top=0.03, bot=0.72, clump=(0.024, 0.05), n=1000,
                  bark=(122, 120, 114), col={"spr": [(140, 168, 60)], "sum": [(64, 98, 38)],
                                             "fal": [(186, 110, 38), (164, 86, 34), (136, 70, 32)], "win": None}),
    "birke": dict(kind="broad", w=0.52, top=0.02, bot=0.62, clump=(0.018, 0.036), n=900, hang=True,
                  bark=(214, 210, 200), col={"spr": [(134, 170, 66)], "sum": [(88, 124, 48)],
                                             "fal": [(222, 186, 56), (204, 164, 48)], "win": None}),
    "pappel": dict(kind="broad", w=0.32, top=0.01, bot=0.86, clump=(0.02, 0.04), n=900,
                   bark=(92, 88, 78), col={"spr": [(110, 146, 58)], "sum": [(70, 104, 44)],
                                           "fal": [(206, 176, 52), (182, 152, 46)], "win": None}),
    "fichte": dict(kind="spruce", w=0.56, bark=(70, 54, 42),
                   col={s: [(34, 58, 38)] for s in SEASONS}),
    "kiefer": dict(kind="pine", w=0.70, bark=(150, 88, 58),
                   col={s: [(52, 72, 40)] for s in SEASONS}),
    "laerche": dict(kind="spruce", w=0.50, bark=(96, 70, 52), soft=True,
                    col={"spr": [(126, 168, 70)], "sum": [(84, 122, 52)],
                         "fal": [(212, 168, 56), (196, 146, 50)], "win": None}),
}
NAMES = list(SPECIES)


def _branches(draw, rng, x, y, ang, length, width, depth, env, bark):
    """Recursive bare branch skeleton, clipped to the crown envelope."""
    if depth == 0 or length < 4 or width < 0.6:
        return
    x2, y2 = x + math.cos(ang) * length, y - math.sin(ang) * length
    if not env(x2, y2):
        f = 0.55
        x2, y2 = x + math.cos(ang) * length * f, y - math.sin(ang) * length * f
    draw.line([(x, y), (x2, y2)], fill=bark + (255,), width=max(1, int(round(width))))
    n = 2 if depth > 3 else rng.integers(2, 4)
    for _ in range(n):
        da = rng.uniform(0.25, 0.75) * rng.choice([-1, 1])
        _branches(draw, rng, x2, y2, ang + da, length * rng.uniform(0.62, 0.8), width * 0.68,
                  depth - 1, env, bark)


def _broadleaf(sp, variant, season):
    rng = np.random.default_rng(1000 * NAMES.index(sp["name"]) + variant)
    img = Image.new("RGBA", (C, C), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    w = sp["w"] * rng.uniform(0.9, 1.05)
    cx = C * 0.5 + rng.uniform(-0.02, 0.02) * C
    top, bot = sp["top"] * C, sp["bot"] * C
    cy, rx, ry = (top + bot) / 2, w * C / 2, (bot - top) / 2

    def env(x, y):
        return ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 < 1.0

    bark = sp["bark"]
    # trunk + skeleton (visible in winter, peeks through in the other seasons)
    tw = C * (0.03 if sp["name"] != "pappel" else 0.025)
    d.polygon([(C / 2 - tw, C), (C / 2 + tw, C), (C / 2 + tw * 0.5, bot - ry * 0.3), (C / 2 - tw * 0.5, bot - ry * 0.3)],
              fill=bark + (255,))
    if sp["name"] == "birke":                        # dark bark marks
        for _ in range(14):
            yy = rng.uniform(bot - ry * 0.3, C)
            d.line([(C / 2 - tw, yy), (C / 2 + tw * rng.uniform(-0.2, 1), yy)], fill=(40, 38, 36, 255), width=3)
    sk = Image.new("RGBA", (C, C), (0, 0, 0, 0))
    sd = ImageDraw.Draw(sk)
    bark = (112, 104, 96) if sp["name"] == "birke" else bark          # birch twigs are dark, only the trunk is white
    up = math.pi / 2
    spread = 0.25 if sp["name"] == "pappel" else 0.9
    for k in range(8 if sp["name"] != "pappel" else 9):
        a = up + rng.uniform(-spread, spread)
        y0 = bot - ry * rng.uniform(0.2, 0.6)
        _branches(sd, rng, C / 2, y0, a, ry * rng.uniform(0.45, 0.65), tw * 0.9, 7, env, bark)
    sd.line([(C / 2, bot - ry * 0.3), (C / 2, top + ry * 0.25)], fill=bark + (255,), width=int(tw * 0.9))
    yy0, xx0 = np.mgrid[0:C, 0:C]
    inside = (((xx0 - cx) / (rx * 0.96)) ** 2 + ((yy0 - cy) / (ry * 0.96)) ** 2) < 1
    ska = np.asarray(sk).copy()
    ska[~inside & (yy0 < bot - ry * 0.3), 3] = 0          # keine Äste außerhalb der Krone
    img.alpha_composite(Image.fromarray(ska, "RGBA"))
    cols = sp["col"][season]
    if cols is None:
        # bare: thin twig haze for depth
        haze = Image.new("RGBA", (C, C), (0, 0, 0, 0))
        hd = ImageDraw.Draw(haze)
        for _ in range(7000):
            a, rr = rng.uniform(0, 2 * np.pi), np.sqrt(rng.uniform(0, 1))
            px, py = cx + math.cos(a) * rr * rx, cy + math.sin(a) * rr * ry
            ang = rng.uniform(0, 2 * np.pi)
            L = rng.uniform(4, 14)
            hd.line([(px, py), (px + math.cos(ang) * L, py + math.sin(ang) * L)], fill=bark + (215,),
                    width=2 if rr < 0.7 else 1)
        img.alpha_composite(haze)
        return np.asarray(img, np.float32)
    arr = np.asarray(img, np.float32).copy()
    yy, xx = np.mgrid[0:C, 0:C]
    lo, hi = sp["clump"]
    n = sp["n"]
    dense = 1.0
    if season == "win":                               # oak: sparse dry leaves
        n, dense = int(n * 0.22), 0.0
    if season == "spr":
        n = int(n * 0.85)
    base_core = np.array(cols[0], np.float32) * 0.42
    if dense:
        core = (((xx - cx) / (rx * 0.75)) ** 2 + ((yy - cy) / (ry * 0.75)) ** 2) < 1
        if sp.get("lobes"):
            ang = np.arctan2(yy - cy, xx - cx)
            core &= (((xx - cx) / (rx * 0.75)) ** 2 + ((yy - cy) / (ry * 0.75)) ** 2) < 0.8 + 0.2 * np.sin(5 * ang + variant)
        arr[core, :3] = base_core
        arr[core, 3] = 255
    for _ in range(n):
        a, rr = rng.uniform(0, 2 * np.pi), np.sqrt(rng.uniform(0, 1)) ** 0.8
        if sp.get("lobes"):
            rr *= 0.85 + 0.15 * math.sin(5 * a + variant)
        px, py = cx + math.cos(a) * rr * rx, cy + math.sin(a) * rr * ry
        if sp.get("hang"):
            py += rng.uniform(0, 0.08) * C * rr
        rad = rng.uniform(lo, hi) * C
        x0, x1 = int(max(px - rad, 0)), int(min(px + rad + 1, C))
        y0, y1 = int(max(py - rad, 0)), int(min(py + rad + 1, C))
        if x1 <= x0 or y1 <= y0:
            continue
        sx, sy = xx[y0:y1, x0:x1], yy[y0:y1, x0:x1]
        dd = np.hypot(sx - px, sy - py) / rad
        m = dd < 1
        light = (1.05 - 0.35 * dd + 0.25 * (-(sy - py) - (sx - px) * 0.5) / rad) \
            * (1.15 - 0.45 * (py - (cy - ry)) / (2 * ry))
        base = np.array(cols[rng.integers(0, len(cols))], np.float32) * rng.uniform(0.85, 1.12)
        col = base * np.clip(light, 0.35, 1.45)[..., None]
        reg = arr[y0:y1, x0:x1]
        reg[m, :3] = col[m]
        reg[m, 3] = 255
    arr[..., :3] *= (0.88 + 0.24 * rng.random((C, C, 1)))
    return arr


def _conifer(sp, variant, season):
    rng = np.random.default_rng(5000 + 1000 * NAMES.index(sp["name"]) + variant)
    img = np.zeros((C, C, 4), np.float32)
    bark = sp["bark"]
    cols = sp["col"][season]
    st = Image.new("RGBA", (C, C), (0, 0, 0, 0))
    sd = ImageDraw.Draw(st)
    if sp["kind"] == "pine":
        # Kiefer: tall bare reddish trunk, flat irregular crown on top
        sd.polygon([(C * 0.48, C), (C * 0.52, C), (C * 0.51, C * 0.3), (C * 0.49, C * 0.3)], fill=bark + (255,))
        for _ in range(6):
            y0 = rng.uniform(0.22, 0.42) * C
            x1 = C * 0.5 + rng.choice([-1, 1]) * rng.uniform(0.1, 0.3) * C
            sd.line([(C * 0.5, y0), (x1, y0 - rng.uniform(0.02, 0.08) * C)], fill=bark + (255,), width=5)
        img = np.asarray(st, np.float32).copy()
        yy, xx = np.mgrid[0:C, 0:C]
        base = np.array(cols[0], np.float32)
        for _ in range(26):
            px = C * 0.5 + rng.normal(0, 0.14) * C
            py = C * rng.uniform(0.08, 0.36)
            rx, ry = rng.uniform(0.06, 0.13) * C, rng.uniform(0.03, 0.06) * C
            m = ((xx - px) / rx) ** 2 + ((yy - py) / ry) ** 2 < 1
            light = np.clip(1.25 - 0.9 * (yy - (py - ry)) / (2 * ry), 0.45, 1.3)
            img[m, :3] = (base * rng.uniform(0.85, 1.1))[None] * light[m][:, None]
            img[m, 3] = 255
        img[..., :3] *= (0.85 + 0.3 * rng.random((C, C, 1)))
        return img
    # Fichte / Lärche: stacked tiers
    sd.rectangle([C * 0.485, C * 0.5, C * 0.515, C], fill=bark + (255,))
    w = sp["w"] * rng.uniform(0.9, 1.08)
    tiers = 16 if not sp.get("soft") else 13
    img = np.asarray(st, np.float32).copy()
    if cols is None:                                  # Lärche im Winter: kahles Gerüst
        sk = Image.fromarray(img.astype(np.uint8), "RGBA")
        d = ImageDraw.Draw(sk)
        d.line([(C / 2, C * 0.85), (C / 2, C * 0.03)], fill=bark + (255,), width=6)
        for k in range(22):
            t0 = k / 22
            y = C * (0.05 + 0.78 * t0)
            half = C * w / 2 * (0.15 + 0.85 * t0)
            for s in (-1, 1):
                d.line([(C / 2, y), (C / 2 + s * half, y + C * 0.04)], fill=bark + (220,), width=2)
        return np.asarray(sk, np.float32)
    for k in range(tiers):
        t0 = k / tiers
        top = C * (0.02 + 0.78 * t0 * 0.95)
        bottom = top + C * (0.15 if not sp.get("soft") else 0.13)
        half = C * w / 2 * (0.12 + 0.88 * (t0 + 0.06))
        layer = Image.new("L", (C, C), 0)
        pts = [(C / 2, top)] + sorted([(C / 2 + s * half * rng.uniform(0.78, 1.05), bottom - rng.uniform(0, 0.05) * C)
                                       for s in np.linspace(-1, 1, 17)], key=lambda p: p[0])
        ImageDraw.Draw(layer).polygon(pts, fill=255)
        m = np.asarray(layer.filter(ImageFilter.MaxFilter(3))) > 0
        yy, xx = np.nonzero(m)
        light = 1.15 - 0.6 * (yy - top) / (bottom - top) - 0.2 * (xx - C / 2) / max(half, 1)
        base = np.array(cols[rng.integers(0, len(cols))], np.float32)
        img[yy, xx, :3] = base * np.clip(light, 0.4, 1.3)[:, None] * rng.uniform(0.9, 1.1)
        img[yy, xx, 3] = 255
    img[..., :3] *= (0.86 + 0.28 * rng.random((C, C, 1)))
    if sp.get("soft"):                                # Lärche: lockerer, durchscheinender
        holes = rng.random((C, C)) < 0.18
        img[holes & (img[..., 3] > 0), 3] = 0
    return img


def cell_of(species, variant):
    i = NAMES.index(species) * VARIANTS + variant
    return i % COLS, i // COLS          # column, row from the top


def season_atlas(season):
    atlas = np.zeros((ROWS * C, COLS * C, 4), np.float32)
    for name in NAMES:
        sp = dict(SPECIES[name], name=name)
        for v in range(VARIANTS):
            cell = _broadleaf(sp, v, season) if sp["kind"] == "broad" else _conifer(sp, v, season)
            c, r = cell_of(name, v)
            atlas[r * C:(r + 1) * C, c * C:(c + 1) * C] = cell
    a = np.clip(atlas, 0, 255).astype(np.uint8)
    # bleed colour into transparent pixels so mipmaps don't get dark fringes
    rgb = Image.fromarray(a[..., :3])
    mask = a[..., 3] > 0
    bled = np.asarray(rgb.filter(ImageFilter.MaxFilter(5)))
    a[..., :3] = np.where(mask[..., None], a[..., :3], bled)
    return a


def for_text(species, lo_h, hi_h, texture):
    """.for file for one species and height class (all variants)."""
    lines = ["A", "800", "FOREST", "", f"TEXTURE {texture}", f"SCALE_X {COLS * C}", f"SCALE_Y {ROWS * C}",
             "SPACING 6 6", "RANDOM 3 3", "SKIP_SURFACE water", "",
             "#    s     t     w     y  offset freq min_h max_h quads type name"]
    freqs = [34, 33, 33]
    for v in range(VARIANTS):
        c, r = cell_of(species, v)
        t = (ROWS - 1 - r) * C                                     # t counts from the bottom
        lines.append(f"TREE {c * C} {t} {C} {C} {C // 2} {freqs[v]} {lo_h} {hi_h} 2 0 {species}{v}")
    return "\n".join(lines) + "\n"
