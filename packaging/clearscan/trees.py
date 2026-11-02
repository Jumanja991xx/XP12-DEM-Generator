"""ClearScan tree art: species x season billboard atlases.

Every crown is built as a small 3D model (thousands of leaves / needle tufts in clusters
around a branch skeleton), lit by a sun from the upper left with sky light and self
shadowing, and projected into a 512 px billboard cell. Colours are muted, photo-like
values per species and season.

Every season atlas has the same layout (same crowns in the same cells), so the .for files
only differ in their TEXTURE line and X-Plane can swap them by season.

Layout: 4096 x 2048 RGBA, cells of 512 px, 8 columns x 4 rows; SPECIES[i] uses the cells
VARIANTS*i .. VARIANTS*i+VARIANTS-1 (row-major from the top). Variant 0/1/2 = darker /
medium / lighter crown, chosen per tree from the aerial photo.
"""

import math

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

C = 512
COLS, ROWS = 8, 4
VARIANTS = 3
SEASONS = ("spr", "sum", "fal", "win")
BRIGHT = (0.82, 1.0, 1.17)
SUN = np.array([-0.5, 0.72, 0.48]); SUN /= np.linalg.norm(SUN)

# Muted, photo-like colours (sRGB). fal: list of autumn hues mixed into the crown,
# green = how much of the crown is still green in autumn.
SPECIES = {
    "linde": dict(kind="broad", w=0.80, top=0.03, bot=0.76, clusters=60, leaf=(2, 4), bark=(74, 66, 58),
                  spr=(112, 132, 64), sum=(70, 90, 46), fal=[(150, 132, 62), (128, 116, 56), (164, 142, 70)],
                  green=0.35),
    "ahorn": dict(kind="broad", w=0.86, top=0.04, bot=0.74, clusters=55, leaf=(2, 5), bark=(80, 72, 64),
                  spr=(118, 136, 62), sum=(66, 88, 44), fal=[(178, 128, 52), (166, 92, 46), (150, 66, 42),
                                                             (176, 146, 64)], green=0.25),
    "eiche": dict(kind="broad", w=0.92, top=0.05, bot=0.72, clusters=34, leaf=(2, 4), bark=(66, 60, 54),
                  spr=(116, 128, 66), sum=(62, 80, 44), fal=[(128, 104, 58), (112, 92, 52), (140, 116, 64)],
                  green=0.45, lobes=True, winter_leaves=(116, 92, 64)),
    "buche": dict(kind="broad", w=0.84, top=0.03, bot=0.78, clusters=60, leaf=(2, 4), bark=(118, 116, 110),
                  spr=(132, 150, 70), sum=(72, 94, 48), fal=[(160, 104, 52), (142, 86, 46), (172, 120, 58)],
                  green=0.2),
    "birke": dict(kind="broad", w=0.50, top=0.02, bot=0.70, clusters=48, leaf=(1, 3), bark=(206, 202, 194),
                  spr=(128, 146, 72), sum=(88, 106, 56), fal=[(186, 160, 74), (172, 146, 66)], green=0.25,
                  hang=True, sparse=0.8),
    "pappel": dict(kind="broad", w=0.36, top=0.01, bot=0.86, clusters=90, leaf=(2, 3), bark=(90, 86, 78),
                   spr=(116, 134, 66), sum=(74, 94, 52), fal=[(176, 156, 72), (160, 140, 64)], green=0.3),
    "fichte": dict(kind="spruce", w=0.50, bark=(70, 56, 46), col=(40, 56, 42), droop=0.10, whorls=24),
    "kiefer": dict(kind="pine", w=0.62, bark=(146, 92, 64), col=(60, 74, 52)),
    "laerche": dict(kind="larch", w=0.46, bark=(92, 72, 56), col=(84, 108, 60), spr=(116, 140, 72),
                    fal=(176, 140, 64), droop=0.04, whorls=20),
}
NAMES = list(SPECIES)


# --------------------------------------------------------------------------- helpers
def _sprites(rng):
    """Leaf / needle sprite shapes: lists of (dx, dy, brightness) offsets."""
    out = {}
    for size in range(1, 6):
        shapes = []
        for k in range(6):
            a = rng.uniform(0, np.pi)
            ax, ay = size, max(1.0, size * rng.uniform(0.45, 0.75))
            pts = []
            R = int(math.ceil(size)) + 1
            for dy in range(-R, R + 1):
                for dx in range(-R, R + 1):
                    u = dx * math.cos(a) + dy * math.sin(a)
                    v = -dx * math.sin(a) + dy * math.cos(a)
                    q = (u / ax) ** 2 + (v / ay) ** 2
                    if q <= 1.0:
                        pts.append((dx, dy, 1.06 - 0.12 * q + 0.06 * (v / ay)))
            shapes.append(np.array(pts, np.float32))
        out[size] = shapes
    needles = []
    for k in range(8):
        a = k / 8 * np.pi
        L = 4
        pts = [(int(round(t * math.cos(a))), int(round(t * math.sin(a))), 1.0 - 0.04 * abs(t)) for t in range(-L, L + 1)]
        needles.append(np.array(pts, np.float32))
    out["needle"] = needles
    return out


SPRITES = _sprites(np.random.default_rng(42))


def _render(points, colours, sizes, rng, base_rgba=None, needle=False):
    """Z-buffer splatting. points (N,3) in tree space (x right, y up, z to viewer),
    cell coordinates: x in [-0.5, 0.5], y in [0, 1]."""
    img = np.zeros((C, C, 4), np.float32) if base_rgba is None else base_rgba.copy()
    zbuf = np.full((C, C), -1e9, np.float32)
    if base_rgba is not None:
        zbuf[img[..., 3] > 0] = -1e8
    px = ((points[:, 0] + 0.5) * C).astype(np.int32)
    py = ((1.0 - points[:, 1]) * C).astype(np.int32)
    all_i, all_z, all_c = [], [], []
    for s in np.unique(sizes):
        sel = np.nonzero(sizes == s)[0]
        shapes = SPRITES["needle"] if needle else SPRITES[int(s)]
        pick = rng.integers(0, len(shapes), len(sel))
        for k, sh in enumerate(shapes):
            idx = sel[pick == k]
            if not len(idx):
                continue
            X = px[idx, None] + sh[None, :, 0].astype(np.int32)
            Y = py[idx, None] + sh[None, :, 1].astype(np.int32)
            ok = (X >= 0) & (X < C) & (Y >= 0) & (Y < C)
            lin = (Y * C + X)[ok]
            all_i.append(lin)
            all_z.append(np.broadcast_to(points[idx, 2:3], X.shape)[ok])
            all_c.append((colours[idx, None, :] * sh[None, :, 2:3])[ok])
    if not all_i:
        return img
    lin, z, col = np.concatenate(all_i), np.concatenate(all_z), np.concatenate(all_c)
    order = np.lexsort((z, lin))
    lin, z, col = lin[order], z[order], col[order]
    last = np.r_[lin[1:] != lin[:-1], True]
    lin, z, col = lin[last], z[last], col[last]
    flat = img.reshape(-1, 4)
    zf = zbuf.reshape(-1)
    win = z > zf[lin]
    flat[lin[win], :3] = col[win]
    flat[lin[win], 3] = 255
    return img


def _light(n, depth, sky_up):
    """Lambert sun + sky light + self shadowing (depth 0 = crown surface, 1 = centre)."""
    diff = np.clip(n @ SUN, 0, 1)
    sky = 0.55 + 0.45 * np.clip(n[:, 1] * 0.5 + 0.5, 0, 1) * sky_up
    occl = np.clip(1.0 - 0.75 * depth ** 1.2, 0.18, 1.0)
    return (0.42 * sky + 0.78 * diff) * occl


def _branches2d(draw, rng, x, y, ang, length, width, depth, colour, alpha=255, env=None):
    """Recursive branches; thin twigs keep branching and end at the crown outline."""
    if depth == 0 or length < 2.5:
        return
    x2, y2 = x + math.cos(ang) * length, y - math.sin(ang) * length
    if env is not None and not env(x2, y2):
        length *= 0.5
        x2, y2 = x + math.cos(ang) * length, y - math.sin(ang) * length
        if not env(x2, y2):
            return
    c = np.clip(np.asarray(colour) * (0.85 + 0.03 * depth), 0, 255)
    draw.line([(x, y), (x2, y2)], fill=tuple(int(v) for v in c) + (alpha,), width=max(1, int(round(width))))
    n = 2 if depth > 5 else int(rng.integers(2, 4))
    for _ in range(n):
        da = rng.uniform(0.2, 0.6) * rng.choice([-1, 1])
        na = ang + da
        na = na * 0.92 + (math.pi / 2) * 0.08            # twigs bend upwards towards the light
        _branches2d(draw, rng, x2, y2, na, length * rng.uniform(0.68, 0.82), max(width * 0.68, 0.8),
                    depth - 1, colour, alpha, env)


def _skeleton(sp, rng, cx, cy, rx, ry, bare):
    """Trunk + branch skeleton as RGBA (drawn under the leaves)."""
    img = Image.new("RGBA", (C, C), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    bark = np.array(sp["bark"], np.float32)
    tw = C * (0.024 if sp["name"] != "pappel" else 0.02)
    trunk_top = cy + ry * (0.55 if bare else 0.3)
    d.polygon([(C / 2 - tw, C), (C / 2 + tw, C), (C / 2 + tw * 0.45, trunk_top), (C / 2 - tw * 0.45, trunk_top)],
              fill=tuple(int(c) for c in bark) + (255,))
    # trunk shading: lit left, shaded right
    arr = np.asarray(img, np.float32).copy()
    xs = np.arange(C)
    shade = np.clip(1.15 - 0.6 * (xs - (C / 2 - tw)) / (2 * tw), 0.55, 1.15)
    m = arr[..., 3] > 0
    arr[..., :3] *= np.where(m[..., None], shade[None, :, None], 1)
    if sp["name"] == "birke":
        for _ in range(16):
            yy = int(rng.uniform(trunk_top, C))
            arr[yy:yy + 3, int(C / 2 - tw):int(C / 2 + tw * rng.uniform(-0.3, 1))][..., :3] = (38, 36, 34)
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGBA")
    d = ImageDraw.Draw(img)
    twig = bark * (0.75 if sp["name"] != "birke" else 0.5) if sp["name"] != "birke" else np.array((96, 88, 82.0))
    n_main = (14 if bare else 8) if sp["name"] != "pappel" else (16 if bare else 10)
    spread = (0.35 if sp["name"] == "pappel" else 1.62) if bare else (0.3 if sp["name"] == "pappel" else 1.3)
    for k in range(n_main):
        a = math.pi / 2 + (k / max(n_main - 1, 1) - 0.5) * 2 * spread + rng.normal(0, 0.12)
        y0 = trunk_top - ry * rng.uniform(0.0, 1.0)
        env = (lambda X, Y: ((X - cx) / rx) ** 2 + ((Y - cy) / ry) ** 2 < 1.0)
        _branches2d(d, rng, C / 2, y0, a, ry * rng.uniform(0.24, 0.32) * (1.0 if bare else 0.9),
                    tw * (0.7 if bare else 0.55), 10 if bare else 3, twig, env=env)
    d.line([(C / 2, trunk_top), (C / 2, cy - ry * (0.7 if bare else 0.3))], fill=tuple(int(c) for c in twig) + (255,),
           width=int(tw * 0.8))
    a = np.asarray(img, np.float32).copy()
    # keep branches inside the crown envelope
    yy, xx = np.mgrid[0:C, 0:C]
    outside = (((xx - cx) / (rx * 1.02)) ** 2 + ((yy - cy) / (ry * 1.02)) ** 2) > 1
    a[outside & (yy < trunk_top - 2), 3] = 0
    a[a[..., 3] < 128, 3] = 0
    a[a[..., 3] >= 128, 3] = 255
    return a


# --------------------------------------------------------------------------- broadleaf
def _broadleaf(sp, variant, season):
    rng = np.random.default_rng(1000 * NAMES.index(sp["name"]) + variant)
    w = sp["w"] * rng.uniform(0.92, 1.05)
    top, bot = sp["top"], sp["bot"]
    cyn = 1.0 - (top + bot) / 2                     # crown centre height (0 bottom .. 1 top)
    rxn, ryn = w / 2, (bot - top) / 2
    cx, cy, rx, ry = C / 2, (top + bot) / 2 * C, rxn * C, ryn * C
    bare = season == "win" and "winter_leaves" not in sp
    base = _skeleton(sp, np.random.default_rng(rng.integers(1 << 30)), cx, cy, rx, ry,
                     bare=season == "win")
    if bare:
        return base
    # clusters inside the crown ellipsoid, biased to the shell
    ncl = sp["clusters"]
    u = rng.normal(size=(ncl * 3, 3))
    u /= np.linalg.norm(u, axis=1, keepdims=True)
    r = rng.uniform(0, 1, ncl * 3) ** 0.45
    cen = u * r[:, None]
    if sp.get("lobes"):
        cen *= (0.8 + 0.2 * rng.uniform(0, 1, len(cen)))[:, None]
    cen = cen[:ncl]
    cen_w = cen * [rxn, ryn, rxn] + [0, cyn, 0]
    crad = rng.uniform(0.22, 0.38, ncl) * min(rxn, ryn) * (1.35 if sp.get("lobes") else 1.0)
    per = int(42000 * (rxn * ryn / 0.13) * sp.get("sparse", 1.0) / ncl)
    per = int(np.clip(per, 250, 1800))
    if season == "spr":
        per = int(per * 0.7)
    if season == "win":                              # oak: some dry leaves remain
        per = int(per * 0.3)
    pts, nrm, dep, cl_id = [], [], [], []
    for k in range(ncl):
        v = rng.normal(size=(per, 3))
        v /= np.linalg.norm(v, axis=1, keepdims=True)
        rr = rng.uniform(0, 1, per) ** 0.35
        p = cen_w[k] + v * (rr * crad[k])[:, None]
        if sp.get("hang"):
            p[:, 1] -= (rr ** 2) * crad[k] * 0.5
        pts.append(p)
        crown_dir = (p - [0, cyn, 0]) / [rxn, ryn, rxn]
        nn = 0.65 * v + 0.35 * crown_dir / np.maximum(np.linalg.norm(crown_dir, axis=1, keepdims=True), 1e-6)
        nrm.append(nn / np.linalg.norm(nn, axis=1, keepdims=True))
        dep.append(np.clip(1 - np.linalg.norm(crown_dir, axis=1), 0, 1))
        cl_id.append(np.full(per, k))
    P, N, D, K = np.vstack(pts), np.vstack(nrm), np.concatenate(dep), np.concatenate(cl_id)
    inside = (((P[:, 0]) / rxn) ** 2 + ((P[:, 1] - cyn) / ryn) ** 2 + (P[:, 2] / rxn) ** 2) < 1.08
    P, N, D, K = P[inside], N[inside], D[inside], K[inside]
    # colour
    n = len(P)
    if season == "win":
        col = np.tile(np.array(sp["winter_leaves"], np.float32), (n, 1))
    elif season in ("spr", "sum"):
        col = np.tile(np.array(sp[season], np.float32), (n, 1))
    else:
        fal = np.array(sp["fal"], np.float32)
        green = np.array(sp["sum"], np.float32)
        hue = fal[rng.integers(0, len(fal), ncl)][K]
        # outer and upper parts turn first, interior stays greener
        t = np.clip(0.55 + 0.6 * (1 - D) + 0.25 * (P[:, 1] - cyn) / ryn - sp["green"] + rng.normal(0, 0.18, n), 0, 1)
        col = green * (1 - t[:, None]) + hue * t[:, None]
    cl_b = rng.uniform(0.9, 1.1, ncl)[K]                      # cluster brightness
    hue_j = 1 + rng.normal(0, 0.035, (n, 3))                  # leaf hue jitter
    lt = _light(N, D, 1.0)
    shade = 0.32 + 0.95 * lt
    col = col * shade[:, None] * cl_b[:, None] * hue_j * BRIGHT[variant]
    col = np.clip(col, 0, 255)
    sizes = rng.integers(sp["leaf"][0], sp["leaf"][1] + 1, n)
    return _render(P, col, sizes, rng, base_rgba=base)


# --------------------------------------------------------------------------- conifers
def _whorl_tree(sp, variant, season, rng):
    """Spruce / larch: whorls of drooping branches with needle tufts."""
    w = sp["w"] * rng.uniform(0.92, 1.06)
    bark = np.array(sp["bark"], np.float32)
    base = Image.new("RGBA", (C, C), (0, 0, 0, 0))
    d = ImageDraw.Draw(base)
    d.polygon([(C * 0.49, C), (C * 0.51, C), (C * 0.502, C * 0.03), (C * 0.498, C * 0.03)],
              fill=tuple(int(c) for c in bark * 0.8) + (255,))
    pts, nrm, dep = [], [], []
    nwh = sp["whorls"]
    bare = sp["kind"] == "larch" and season == "win"
    y_low, y_top = 0.12, 0.985
    for k in range(nwh):
        t = k / (nwh - 1)
        y = y_low + (y_top - y_low) * t
        R = w / 2 * (1 - t) ** 0.92 + 0.012
        nb = int(rng.integers(6, 9))
        for j in range(nb):
            th = rng.uniform(0, 2 * np.pi)
            L = R * rng.uniform(0.8, 1.05)
            s = np.linspace(0.08, 1.0, max(4, int(L * 70)))
            bx, bz = np.cos(th) * L * s, np.sin(th) * L * s
            by = y - sp["droop"] * (s ** 1.6) * (1.2 - t) + 0.02 * s
            if bare:
                pts.append(np.column_stack([bx, by, bz]))
                continue
            # needle tufts along the branch, denser at the tip, hanging below the branch
            m = len(s) * 22
            sel = rng.integers(0, len(s), m)
            off = rng.normal(0, 1, (m, 3)) * (0.012 + 0.02 * s[sel])[:, None]
            off[:, 1] -= np.abs(off[:, 1]) * 0.6
            p = np.column_stack([bx[sel], by[sel], bz[sel]]) + off
            pts.append(p)
            radial = np.column_stack([np.cos(th) * np.ones(m), 0.35 + 0.3 * s[sel], np.sin(th) * np.ones(m)])
            nrm.append(radial / np.linalg.norm(radial, axis=1, keepdims=True))
            dep.append(np.clip(1 - s[sel], 0, 1) * 0.8)
    if bare:
        P = np.vstack(pts)
        col = np.tile(bark * 0.85, (len(P), 1)) * rng.uniform(0.8, 1.2, (len(P), 1))
        img = _render(P, col, np.ones(len(P), int), rng, base_rgba=np.asarray(base, np.float32))
        return img
    P, N, D = np.vstack(pts), np.vstack(nrm), np.concatenate(dep)
    if sp["kind"] == "larch":
        colbase = np.array(sp.get(season, sp["col"]) if season in ("spr", "fal") else sp["col"], np.float32)
    else:
        colbase = np.array(sp["col"], np.float32)
    lt = _light(N, D, 1.0)
    col = colbase * (0.38 + 0.9 * lt)[:, None] * (1 + rng.normal(0, 0.05, (len(P), 3))) * BRIGHT[variant]
    if sp["kind"] == "larch":                         # larch: airier crown
        keep = rng.random(len(P)) < 0.55
        P, col = P[keep], col[keep]
    return _render(P, np.clip(col, 0, 255), rng.integers(1, 3, len(P)), rng,
                   base_rgba=np.asarray(base, np.float32))


def _pine(sp, variant, season, rng):
    """Scots pine: long bare reddish trunk, irregular flat needle clouds on top."""
    bark = np.array(sp["bark"], np.float32)
    base = Image.new("RGBA", (C, C), (0, 0, 0, 0))
    d = ImageDraw.Draw(base)
    d.polygon([(C * 0.485, C), (C * 0.515, C), (C * 0.506, C * 0.25), (C * 0.494, C * 0.25)],
              fill=tuple(int(c) for c in bark * 0.7) + (255,))
    d.polygon([(C * 0.49, C * 0.6), (C * 0.51, C * 0.6), (C * 0.505, C * 0.25), (C * 0.495, C * 0.25)],
              fill=tuple(int(c) for c in bark) + (255,))
    for _ in range(7):
        y0 = rng.uniform(0.2, 0.45) * C
        x1 = C * 0.5 + rng.choice([-1, 1]) * rng.uniform(0.08, 0.26) * C
        d.line([(C * 0.5, y0), (x1, y0 - rng.uniform(0.03, 0.09) * C)], fill=tuple(int(c) for c in bark * 0.8) + (255,),
               width=4)
    w = sp["w"] * rng.uniform(0.9, 1.08)
    pts, nrm, dep = [], [], []
    ncl = int(rng.integers(14, 20))
    for k in range(ncl):
        c = np.array([rng.normal(0, w * 0.2), rng.uniform(0.6, 0.93), rng.normal(0, w * 0.2)])
        rad = np.array([rng.uniform(0.05, 0.11), rng.uniform(0.03, 0.055), rng.uniform(0.05, 0.11)])
        m = 2600
        v = rng.normal(size=(m, 3))
        v /= np.linalg.norm(v, axis=1, keepdims=True)
        rr = rng.uniform(0, 1, m) ** 0.4
        p = c + v * rad * rr[:, None]
        pts.append(p)
        nn = v + [0, 0.6, 0]
        nrm.append(nn / np.linalg.norm(nn, axis=1, keepdims=True))
        dep.append(1 - rr)
    P, N, D = np.vstack(pts), np.vstack(nrm), np.concatenate(dep)
    col = np.array(sp["col"], np.float32) * (0.36 + 0.92 * _light(N, D, 1.0))[:, None] \
        * (1 + rng.normal(0, 0.05, (len(P), 3))) * BRIGHT[variant]
    return _render(P, np.clip(col, 0, 255), rng.integers(1, 3, len(P)), rng,
                   base_rgba=np.asarray(base, np.float32))


def tree_cell(name, variant, season):
    sp = dict(SPECIES[name], name=name)
    rng = np.random.default_rng(5000 + 1000 * NAMES.index(name) + variant)
    if sp["kind"] == "broad":
        return _broadleaf(sp, variant, season)
    if sp["kind"] == "pine":
        return _pine(sp, variant, season, rng)
    return _whorl_tree(sp, variant, season, rng)


def cell_of(species, variant):
    i = NAMES.index(species) * VARIANTS + variant
    return i % COLS, i // COLS          # column, row from the top


def season_atlas(season):
    atlas = np.zeros((ROWS * C, COLS * C, 4), np.float32)
    for name in NAMES:
        for v in range(VARIANTS):
            c, r = cell_of(name, v)
            atlas[r * C:(r + 1) * C, c * C:(c + 1) * C] = tree_cell(name, v, season)
    a = np.clip(atlas, 0, 255).astype(np.uint8)
    # bleed colour into transparent pixels so mipmaps don't get dark fringes
    mask = a[..., 3] > 0
    bled = np.asarray(Image.fromarray(a[..., :3]).filter(ImageFilter.MaxFilter(5)))
    a[..., :3] = np.where(mask[..., None], a[..., :3], bled)
    return a


def for_text(species, variant, lo_h, hi_h, texture):
    """.for file for one species, brightness variant and height class."""
    c, r = cell_of(species, variant)
    t = (ROWS - 1 - r) * C                                         # t counts from the bottom
    lines = ["A", "800", "FOREST", "", f"TEXTURE {texture}", f"SCALE_X {COLS * C}", f"SCALE_Y {ROWS * C}",
             "SPACING 6 6", "RANDOM 3 3", "SKIP_SURFACE water", "",
             "#    s     t     w     y  offset freq min_h max_h quads type name",
             f"TREE {c * C} {t} {C} {C} {C // 2} 100 {lo_h} {hi_h} 2 0 {species}{variant}"]
    return "\n".join(lines) + "\n"
