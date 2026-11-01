"""ClearScan textures: facade atlas (+ night version), roof pattern, tree atlas.

Facade atlas: one cell per (plaster colour, bay type). A wall is cut into bays
(~3 m) x storeys and every piece maps onto one cell, so all walls of an area
share one small texture - and X-Plane gets lit windows at night from the
_LIT version of the same atlas.
"""

import numpy as np
from PIL import Image, ImageDraw

PX_M = 40                      # atlas pixels per metre
CELL_W, CELL_H = 120, 160      # 3.0 m x 4.0 m
BAY_M = CELL_W / PX_M          # 3.0 m
ATLAS_W, ATLAS_H = 4096, 2048

TYPES = ["win", "win_shut_g", "win_shut_b", "plain",
         "win_g", "door_g", "plain_g",
         "ribbon", "com_plain",
         "garage", "wood"]
GLASS = np.array([38, 46, 56], np.float32)
FRAME = np.array([228, 228, 222], np.float32)
LIT = np.array([255, 205, 130], np.float32)


def _cell(kind, base, rng):
    """(rgb, lit) of one cell, row 0 = bottom (v = 0 at the bottom of the storey)."""
    img = np.empty((CELL_H, CELL_W, 3), np.float32)
    img[:] = base
    img *= (0.965 + 0.05 * rng.random((CELL_H, CELL_W, 1))).astype(np.float32)
    lit = np.zeros((CELL_H, CELL_W, 3), np.float32)

    def rect(u0, v0, u1, v1, col, a=1.0, target=None):
        t = img if target is None else target
        x0, x1 = int(round(u0 * PX_M)), int(round(u1 * PX_M))
        y0, y1 = int(round(v0 * PX_M)), int(round(v1 * PX_M))
        x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1, CELL_W), min(y1, CELL_H)
        if x0 < x1 and y0 < y1:
            t[y0:y1, x0:x1] = t[y0:y1, x0:x1] * (1 - a) + np.asarray(col, np.float32) * a

    def window(u, v0, ww, wh, shut=None, light=True):
        rect(u - ww / 2 - 0.08, v0 - 0.06, u + ww / 2 + 0.08, v0 + wh + 0.08, FRAME * 0.97)
        rect(u - ww / 2, v0, u + ww / 2, v0 + wh, GLASS)
        rect(u - ww / 2, v0 + wh * 0.55, u + ww / 2, v0 + wh, GLASS * 1.6, 0.35)
        for a, b in ((v0 + wh - 0.07, v0 + wh), (v0, v0 + 0.07)):
            rect(u - ww / 2, a, u + ww / 2, b, FRAME)
        rect(u - ww / 2, v0, u - ww / 2 + 0.06, v0 + wh, FRAME)
        rect(u + ww / 2 - 0.06, v0, u + ww / 2, v0 + wh, FRAME)
        rect(u - 0.03, v0, u + 0.03, v0 + wh, FRAME)
        rect(u - ww / 2 - 0.12, v0 - 0.1, u + ww / 2 + 0.12, v0 - 0.02, FRAME * 0.85)
        if shut is not None:
            rect(u - ww / 2 - ww * 0.5 - 0.08, v0, u - ww / 2 - 0.1, v0 + wh, shut)
            rect(u + ww / 2 + 0.1, v0, u + ww / 2 + ww * 0.5 + 0.08, v0 + wh, shut)
        if light:
            rect(u - ww / 2 + 0.06, v0 + 0.07, u + ww / 2 - 0.06, v0 + wh - 0.07, LIT, target=lit)

    mid = BAY_M / 2
    ground = kind.endswith("_g")
    if kind in ("win", "win_g"):
        window(mid, 0.95, 1.15, 1.35, light=rng.random() < 0.5)
    elif kind == "win_shut_g":
        window(mid, 0.95, 1.1, 1.35, [70, 85, 60], light=rng.random() < 0.4)
    elif kind == "win_shut_b":
        window(mid, 0.95, 1.1, 1.35, [95, 70, 50], light=rng.random() < 0.4)
    elif kind == "door_g":
        rect(mid - 0.55, 0.0, mid + 0.55, 2.15, [110, 80, 60])
        rect(mid - 0.55, 2.15, mid + 0.55, 2.25, FRAME)
        rect(mid - 0.12, 2.3, mid + 0.12, 2.42, LIT * 0.9, target=lit)          # Lampe
    elif kind == "ribbon":
        rect(0.0, 1.2, BAY_M, 2.7, GLASS * 1.15)
        rect(0.0, 1.2, 0.07, 2.7, [170, 172, 175])
        rect(0.0, 2.15, BAY_M, 2.7, GLASS * 1.7, 0.3)
        if rng.random() < 0.55:
            rect(0.1, 1.3, BAY_M - 0.1, 2.6, LIT * 0.85, target=lit)
    elif kind == "garage":
        rect(mid - 1.25, 0, mid + 1.25, 2.1, [185, 182, 175])
        for vv in np.arange(0.15, 2.1, 0.3):
            rect(mid - 1.25, vv, mid + 1.25, vv + 0.04, [150, 148, 142])
    elif kind == "wood":
        img[:] = np.array([120, 92, 66], np.float32)
        img *= (0.92 + 0.08 * (np.sin(np.arange(CELL_W) * 2 * np.pi / (0.18 * PX_M)) > 0))[None, :, None]
    if ground or kind in ("garage",):
        rect(0, 0, BAY_M, 0.45, np.asarray(base) * 0.72)                         # Sockel
    return np.clip(img, 0, 255), lit


def facade_atlas(palette, seed=1):
    """Returns (rgb, lit, cells) - cells[(colour index, type)] = (x0, y0) pixel origin
    (top-left in the image), cell row 0 of the drawing is the image's bottom row."""
    rng = np.random.default_rng(seed)
    rgb = np.zeros((ATLAS_H, ATLAS_W, 3), np.uint8)
    lit = np.zeros((ATLAS_H, ATLAS_W, 3), np.uint8)
    cells = {}
    per_row = ATLAS_W // CELL_W
    k = 0
    for ci, base in enumerate(palette):
        for t in TYPES:
            if t == "wood" and ci > 0:
                cells[(ci, t)] = cells[(0, t)]
                continue
            x0, y0 = (k % per_row) * CELL_W, (k // per_row) * CELL_H
            if y0 + CELL_H > ATLAS_H:
                raise ValueError("facade atlas full - fewer colours")
            c, l = _cell(t, np.asarray(base, np.float32), rng)
            rgb[y0:y0 + CELL_H, x0:x0 + CELL_W] = c[::-1].astype(np.uint8)
            lit[y0:y0 + CELL_H, x0:x0 + CELL_W] = l[::-1].astype(np.uint8)
            cells[(ci, t)] = (x0, y0)
            k += 1
    return rgb, lit, cells


def roof_pattern(w, h, base, flat, seed, texel):
    """Brightness pattern (mean 1) of the roofing: rows from eave (v=0) to ridge.
    Clay tiles for reddish roofs, concrete tiles otherwise, membrane for flat roofs."""
    rng = np.random.default_rng(seed)
    tw, th = max(2, int(np.ceil(w / texel)) + 2), max(2, int(np.ceil(h / texel)) + 2)
    uu, vv = np.meshgrid((np.arange(tw) - 1) * texel, (np.arange(th) - 1) * texel)
    low = rng.random((th // 40 + 2, tw // 40 + 2)).astype(np.float32)
    low = np.asarray(Image.fromarray((low * 255).astype(np.uint8)).resize((tw, th), Image.BICUBIC), np.float32) / 255
    if flat:
        shade = (0.93 + 0.1 * rng.random((th, tw))) * (0.95 + 0.08 * low)
        seam = (np.abs((uu % 1.0) - 0.5) > 0.48) | (np.abs((vv % 6.0) - 3.0) > 2.97)
        shade[seam] *= 0.9
    else:
        base = np.asarray(base, np.float32)
        clay = base[0] > base[2] + 25 and (base.max() - base.min()) > 30
        row_h, col_w = (0.34, 0.22) if clay else (0.31, 0.30)
        row = np.floor(vv / row_h)
        pos_v = vv / row_h - row
        shift = (row % 2) * 0.5
        col = np.floor(uu / col_w + shift)
        pos_u = uu / col_w + shift - col
        tile_rand = (np.sin(row * 12.9898 + col * 78.233 + seed) * 43758.5453) % 1.0
        shade = 0.9 + 0.12 * tile_rand
        shade *= np.where(pos_v < 0.12, 0.62 + 2.5 * pos_v, 1.0) * (0.93 + 0.1 * pos_v)
        if clay:
            shade *= 0.88 + 0.12 * np.sin(pos_u * np.pi)
        shade *= np.where(np.minimum(pos_u, 1 - pos_u) < 0.04, 0.85, 1.0)
        shade *= 0.94 + 0.1 * low
        shade[:max(1, int(0.25 / texel))] *= 0.75
        shade[-max(1, int(0.28 / texel)):] = 0.82
    return (shade / max(float(shade.mean()), 1e-3)).astype(np.float32)


# --------------------------------------------------------------------------- Bäume
TREE_CELL = 512


def _deciduous(rng, shade):
    C = TREE_CELL
    img = np.zeros((C, C, 4), np.float32)
    yy, xx = np.mgrid[0:C, 0:C]
    st = Image.new("L", (C, C), 0)
    d = ImageDraw.Draw(st)
    d.polygon([(C * 0.47, C), (C * 0.53, C), (C * 0.515, C * 0.45), (C * 0.485, C * 0.45)], fill=255)
    for _ in range(7):
        y0 = rng.uniform(0.45, 0.62) * C
        x1 = C * 0.5 + rng.choice([-1, 1]) * rng.uniform(0.12, 0.3) * C
        d.line([(C * 0.5, y0), (x1, y0 - rng.uniform(0.08, 0.18) * C)], fill=255, width=6)
    img[np.asarray(st) > 0] = [70, 55, 40, 255]
    cx, cy, rx, ry = C * 0.5, C * 0.37, C * 0.44, C * 0.33
    base = np.array([60, 90, 36], np.float32) * shade
    core = (((xx - cx) / (rx * 0.78)) ** 2 + ((yy - cy) / (ry * 0.78)) ** 2) < 1
    img[core, :3] = base * 0.42
    img[core, 3] = 255
    for _ in range(900):
        a, rr = rng.uniform(0, 2 * np.pi), np.sqrt(rng.uniform(0, 1)) ** 0.8
        px, py = cx + np.cos(a) * rr * rx, cy + np.sin(a) * rr * ry
        rad = rng.uniform(0.022, 0.05) * C
        x0, x1 = int(max(px - rad, 0)), int(min(px + rad + 1, C))
        y0, y1 = int(max(py - rad, 0)), int(min(py + rad + 1, C))
        sx, sy = xx[y0:y1, x0:x1], yy[y0:y1, x0:x1]
        dd = np.hypot(sx - px, sy - py) / rad
        m = dd < 1
        light = (1.05 - 0.35 * dd + 0.25 * (-(sy - py) - (sx - px) * 0.5) / rad) \
            * (1.15 - 0.45 * (py - (cy - ry)) / (2 * ry))
        col = base * rng.uniform(0.85, 1.15) * np.clip(light, 0.35, 1.45)[..., None]
        reg = img[y0:y1, x0:x1]
        reg[m, :3] = col[m]
        reg[m, 3] = 255
    img[..., :3] *= (0.86 + 0.28 * rng.random((C, C, 1)))
    return img


def _conifer(rng, shade):
    C = TREE_CELL
    img = np.zeros((C, C, 4), np.float32)
    st = Image.new("L", (C, C), 0)
    ImageDraw.Draw(st).rectangle([C * 0.485, C * 0.8, C * 0.515, C], fill=255)
    img[np.asarray(st) > 0] = [65, 50, 38, 255]
    base = np.array([36, 60, 38], np.float32) * shade
    tiers = 14
    from PIL import ImageFilter
    for k in range(tiers):
        t0 = k / tiers
        top = C * (0.02 + 0.8 * t0 * 0.95)
        bottom = top + C * 0.16
        half = C * (0.06 + 0.4 * (t0 + 0.08))
        layer = Image.new("L", (C, C), 0)
        pts = [(C / 2, top)] + sorted([(C / 2 + s * half * rng.uniform(0.8, 1.05), bottom - rng.uniform(0, 0.05) * C)
                                       for s in np.linspace(-1, 1, 15)], key=lambda p: p[0])
        ImageDraw.Draw(layer).polygon(pts, fill=255)
        m = np.asarray(layer.filter(ImageFilter.MaxFilter(3))) > 0
        yy, xx = np.nonzero(m)
        light = 1.15 - 0.6 * (yy - top) / (bottom - top) - 0.2 * (xx - C / 2) / max(half, 1)
        img[yy, xx, :3] = base * np.clip(light, 0.4, 1.3)[:, None] * rng.uniform(0.9, 1.1)
        img[yy, xx, 3] = 255
    img[..., :3] *= (0.88 + 0.24 * rng.random((C, C, 1)))
    return img


def tree_atlas(seed=7):
    """2048 x 1024 RGBA: top row 4 broadleaf, bottom row 4 conifer shades."""
    rng = np.random.default_rng(seed)
    C = TREE_CELL
    atlas = np.zeros((2 * C, 4 * C, 4), np.float32)
    for i, s in enumerate((0.8, 0.95, 1.1, 1.25)):
        atlas[:C, i * C:(i + 1) * C] = _deciduous(rng, s)
        atlas[C:, i * C:(i + 1) * C] = _conifer(rng, s)
    return np.clip(atlas, 0, 255).astype(np.uint8)
