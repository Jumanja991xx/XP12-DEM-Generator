"""ClearScan roofs v2: one shared atlas of tileable roofing materials.

Each roof face gets ONE robust colour (median of the photogrammetry without shadows and
highlights); the colours are clustered per material and every cluster gets its own atlas
cell. A cell covers TILE_M x TILE_M metres and tiles seamlessly, so roof faces are cut
into TILE_M pieces that all map into their cell.
"""

import numpy as np

TILE_M = 4.0
CELL = 256
ATLAS = 2048
PER_ROW = ATLAS // CELL
MATERIALS = {"tile": 26, "metal": 6, "gravel": 8, "bitumen": 4, "sedum": 4, "membrane": 4}


def _periodic_noise(rng, n, freqs=(1, 2, 3, 4, 6)):
    """Seamless low-frequency noise in [0, 1] on an n x n grid."""
    t = np.arange(n) / n * 2 * np.pi
    U, V = np.meshgrid(t, t)
    out = np.zeros((n, n), np.float32)
    for f in freqs:
        for _ in range(2):
            a, b = rng.integers(-f, f + 1, 2)
            out += np.sin(a * U + b * V + rng.uniform(0, 6.3)) / f
    out -= out.min()
    return out / max(out.max(), 1e-6)


def _pattern(material, colour, seed):
    """CELL x CELL brightness pattern (mean 1), row 0 = bottom (eave side)."""
    rng = np.random.default_rng(seed)
    n = CELL
    px = TILE_M / n
    uu, vv = np.meshgrid((np.arange(n) + 0.5) * px, (np.arange(n) + 0.5) * px)
    low = _periodic_noise(rng, n)
    fine = rng.random((n, n)).astype(np.float32)
    if material == "tile":
        c = np.asarray(colour, np.float32)
        clay = c[0] > c[2] + 20 and (c.max() - c.min()) > 28
        rows, cols = (12, 18) if clay else (13, 13)            # divide TILE_M exactly
        row_h, col_w = TILE_M / rows, TILE_M / cols
        row = np.floor(vv / row_h)
        pos_v = vv / row_h - row
        shift = (row % 2) * 0.5
        col = np.floor(uu / col_w + shift)
        pos_u = uu / col_w + shift - col
        colw = np.mod(col, cols)
        tile_rand = (np.sin(row * 12.9898 + colw * 78.233 + seed) * 43758.5453) % 1.0
        s = 0.9 + 0.14 * tile_rand
        s *= np.where(pos_v < 0.13, 0.6 + 2.6 * pos_v, 1.0) * (0.94 + 0.09 * pos_v)
        if clay:
            s *= 0.86 + 0.14 * np.sin(pos_u * np.pi)
        s *= np.where(np.minimum(pos_u, 1 - pos_u) < 0.045, 0.84, 1.0)
        s *= 0.92 + 0.12 * low
        s *= 0.97 + 0.06 * fine
    elif material == "metal":
        seam = 0.5                                             # standing seam every 0.5 m
        pos = (uu % seam) / seam
        s = np.where(pos < 0.04, 1.25, np.where(pos < 0.08, 0.75, 1.0))
        s = s * (0.96 + 0.06 * low) * (0.99 + 0.02 * fine)
    elif material == "gravel":
        g = rng.random((n, n)).astype(np.float32)
        s = 0.86 + 0.24 * g * g + 0.12 * (low - 0.5)
    elif material == "bitumen":
        lap = (vv % 1.0) / 1.0                                 # 1 m membrane rolls
        s = np.where(lap < 0.03, 1.12, 1.0) * (0.95 + 0.1 * low) * (0.97 + 0.05 * fine)
    elif material == "sedum":
        blob = _periodic_noise(rng, n, freqs=(4, 6, 8, 12, 16))
        s = 0.75 + 0.45 * blob + 0.15 * (fine - 0.5)
    else:                                                      # membrane (light PVC)
        seam = (uu % 2.0) / 2.0
        s = np.where(seam < 0.02, 0.88, 1.0) * (0.97 + 0.05 * low) * (0.99 + 0.02 * fine)
    s = np.asarray(s, np.float32)
    return s / float(s.mean())


def robust_colour(col, ok):
    """Median of the photogrammetry samples without shadows (dark) and glints (bright)."""
    c = col[ok]
    if len(c) < 6:
        return None
    lum = c @ np.array([0.3, 0.59, 0.11])
    lo, hi = np.percentile(lum, [35, 88])
    keep = (lum >= lo) & (lum <= hi)
    return np.median(c[keep] if keep.sum() >= 3 else c, 0)


def material_of(colour, flat, kind, area):
    r, g, b = colour
    lum = 0.3 * r + 0.59 * g + 0.11 * b
    if flat:
        if g > r + 6 and g > b + 4:
            return "sedum"
        if lum < 78:
            return "bitumen"
        if lum > 175 and max(colour) - min(colour) < 18:
            return "membrane"
        return "gravel"
    sat = max(colour) - min(colour)
    if kind in ("commercial", "shed") and (area > 250 or kind == "shed") and sat < 30:
        return "metal"
    return "tile"


def _kmeans(P, k, seed=5):
    k = min(k, len(P))
    rng = np.random.default_rng(seed)
    cen = P[rng.choice(len(P), k, replace=False)].astype(np.float64)
    for _ in range(30):
        lab = np.argmin(((P[:, None] - cen[None]) ** 2).sum(-1), 1)
        for j in range(k):
            if (lab == j).any():
                cen[j] = P[lab == j].mean(0)
    return cen, lab


class RoofAtlas:
    """Collects (material, colour) per face, then clusters and paints the atlas."""

    def __init__(self):
        self.items = []                     # (material, colour)

    def add(self, material, colour):
        self.items.append((material, np.asarray(colour, np.float64)))
        return len(self.items) - 1

    def build(self):
        self.cell_of_item = np.zeros(len(self.items), int)
        img = np.zeros((ATLAS, ATLAS, 3), np.uint8)
        k = 0
        self.cells = []
        for mat, kmax in MATERIALS.items():
            idx = [i for i, (m, _) in enumerate(self.items) if m == mat]
            if not idx:
                continue
            P = np.array([self.items[i][1] for i in idx])
            cen, lab = _kmeans(P, kmax)
            for j, c in enumerate(cen):
                if k >= PER_ROW * PER_ROW:
                    raise ValueError("roof atlas full")
                x0, y0 = (k % PER_ROW) * CELL, (k // PER_ROW) * CELL
                pat = _pattern(mat, c, 100 + k)
                cell = np.clip(c[None, None, :] * pat[..., None], 0, 255).astype(np.uint8)
                img[y0:y0 + CELL, x0:x0 + CELL] = cell[::-1]             # row 0 of pat = bottom
                self.cells.append((x0, y0))
                for ii in np.array(idx)[lab == j]:
                    self.cell_of_item[ii] = k
                k += 1
        self.img = img
        return img

    def uv(self, item, u_local, v_local):
        """u_local, v_local in [0, TILE_M] inside the piece -> atlas (u, v), v up."""
        x0, y0 = self.cells[self.cell_of_item[item]]
        su = (x0 + 0.5 + np.clip(u_local / TILE_M, 0, 1) * (CELL - 1)) / ATLAS
        sv = 1 - (y0 + CELL - 0.5 - np.clip(v_local / TILE_M, 0, 1) * (CELL - 1)) / ATLAS
        return su, sv
