"""Draw the comparison figures of the README (docs/img/mesh_*.png).

A generic, made-up Alpine slope - a sharp ridge, a cliff, a gully and a flat
valley floor - meshed three ways:

* XPME / default: evenly spaced points (~100 m) with heights from 30-90 m
  satellite / radar data (smoothed, peaks cut, small artefacts)
* Improve: the very same points, heights taken from LiDAR
* New mesh: Ortho4XP-style adaptive mesh from LiDAR with ~2.2x the points
  (the ratio measured for Innsbruck: 1.6 vs 3.6 million triangles)

Usage:  python packaging/make_readme_figures.py   (needs matplotlib and scipy)
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.colors import LightSource  # noqa: E402
from matplotlib.tri import Triangulation  # noqa: E402
from scipy.interpolate import LinearNDInterpolator, RegularGridInterpolator  # noqa: E402
from scipy.ndimage import gaussian_filter  # noqa: E402
from scipy.spatial import Delaunay  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "docs" / "img"
SIZE = 3000.0   # metres
STEP = 10.0     # "LiDAR" grid spacing
RNG = np.random.default_rng(7)

COLORS = {"real": "#222222", "xpme": "#d9822b", "improve": "#2b7bd9", "mesh": "#2ba84a"}
LABELS = {"xpme": "XPME / X-Plane default", "improve": "Improve my current terrain",
          "mesh": "Build a new, denser mesh"}


# ---------------------------------------------------------------------------
# Terrain
# ---------------------------------------------------------------------------
def _smooth(t: np.ndarray) -> np.ndarray:
    t = np.clip(t, 0, 1)
    return t * t * (3 - 2 * t)


def terrain() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    x = np.arange(0, SIZE + STEP, STEP)
    gx, gy = np.meshgrid(x, x)
    # flat valley floor in the south, slope rising to a knife-edge ridge in the north
    z = 1200 + 850 * _smooth((gy - 450) / 1700) ** 1.2
    ridge_y = 2380 + 110 * np.sin(gx / 650)
    z += 430 * np.exp(-np.abs(gy - ridge_y) / 105)
    # a 220 m rock band across the slope in the east
    band = _smooth((gx - 1300) / 400) * _smooth((3000 - gx) / 300)
    z += 220 / (1 + np.exp(-(gy - 1550) / 18)) * band
    # a gully cut into the slope in the west
    z -= 110 * np.exp(-((gx - 800) / 55) ** 2) * _smooth((gy - 650) / 650) * _smooth((2200 - gy) / 300)
    z += 5 * np.sin(gx / 37) * np.cos(gy / 53) * _smooth((gy - 450) / 300)
    return gx, gy, gaussian_filter(z, 0.6)


def satellite(z: np.ndarray) -> np.ndarray:
    """What 30-90 m radar data makes of it: blurred, peaks cut, a bit of noise."""
    blurred = gaussian_filter(z, 4.5)
    noise = gaussian_filter(RNG.normal(0, 1, z.shape), 3) * 25
    return blurred + noise


# ---------------------------------------------------------------------------
# Meshes
# ---------------------------------------------------------------------------
def even_points(spacing: float = 100.0) -> np.ndarray:
    n = int(SIZE / spacing) + 1
    p = np.array([(i * spacing, j * spacing) for j in range(n) for i in range(n)], float)
    inner = (p[:, 0] > 0) & (p[:, 0] < SIZE) & (p[:, 1] > 0) & (p[:, 1] < SIZE)
    p[inner] += RNG.uniform(-25, 25, (inner.sum(), 2))
    return p


def adaptive_points(sample, budget: int) -> np.ndarray:
    """Greedy insertion where the mesh deviates most from the terrain (like Ortho4XP's
    curvature tolerance): many points on ridges and cliffs, few on the valley floor."""
    border = np.linspace(0, SIZE, 16)
    pts = [(a, b) for a in border for b in (0.0, SIZE)] + [(a, b) for b in border[1:-1] for a in (0.0, SIZE)]
    pts = np.array(pts + [(SIZE / 2, SIZE / 2)], float)
    probe = np.arange(5, SIZE, 20.0)
    px, py = np.meshgrid(probe, probe)
    probe_xy = np.column_stack([px.ravel(), py.ravel()])
    probe_z = sample(probe_xy)
    while len(pts) < budget:
        interp = LinearNDInterpolator(pts, sample(pts))
        err = np.abs(interp(probe_xy) - probe_z)
        err = np.nan_to_num(err)
        k = min(max(len(pts) // 4, 20), budget - len(pts))
        worst = np.argpartition(-err, k)[:k]
        chosen = []
        for i in worst[np.argsort(-err[worst])]:  # spread new points a little
            if all(np.hypot(*(probe_xy[i] - c)) > 30 for c in chosen):
                chosen.append(probe_xy[i])
        pts = np.vstack([pts, np.array(chosen)])
    return pts


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    gx, gy, real = terrain()
    sat = satellite(real)
    axis = gx[0]
    real_at = RegularGridInterpolator((axis, axis), real.T)
    sat_at = RegularGridInterpolator((axis, axis), sat.T)

    coarse = even_points()
    dense = adaptive_points(real_at, int(len(coarse) * 2.2))
    meshes = {
        "xpme": (coarse, sat_at(coarse)),
        "improve": (coarse, real_at(coarse)),
        "mesh": (dense, real_at(dense)),
    }

    def error_of(pts, z):
        interp = LinearNDInterpolator(pts, z)
        return np.abs(np.nan_to_num(interp(np.column_stack([gx.ravel(), gy.ravel()]))) - real.ravel())

    errors = {k: error_of(*v) for k, v in meshes.items()}

    # -- Figure 1: the three meshes, shaded, plus their triangles ------------
    ls = LightSource(azdeg=315, altdeg=35)
    fig = plt.figure(figsize=(15, 10.6), dpi=110)
    fig.patch.set_facecolor("white")
    view = {"elev": 32, "azim": -112}
    for col, key in enumerate(["xpme", "improve", "mesh"]):
        pts, z = meshes[key]
        tri = Triangulation(pts[:, 0], pts[:, 1], Delaunay(pts).simplices)
        ax = fig.add_subplot(2, 3, col + 1, projection="3d")
        ax.plot_trisurf(tri, z, cmap="terrain", vmin=900, vmax=2900, linewidth=0.15,
                        edgecolor=(0, 0, 0, 0.25), antialiased=True, shade=True, lightsource=ls)
        ax.view_init(**view)
        ax.set_box_aspect((1, 1, 0.5), zoom=1.35)
        ax.set_axis_off()
        ax.set_zlim(1100, 2800)
        ax.text2D(0.5, 1.0, f"{LABELS[key]}\n{len(pts)} points · {len(tri.triangles)} triangles",
                  transform=ax.transAxes, ha="center", va="top", fontsize=13,
                  color=COLORS[key], fontweight="bold")
        ax2 = fig.add_subplot(2, 3, col + 4)
        shade = ls.shade(real, cmap=plt.get_cmap("Greys_r"), vert_exag=1.5, blend_mode="soft",
                         vmin=900, vmax=2900)
        ax2.imshow(shade, extent=(0, SIZE, 0, SIZE), origin="lower", alpha=0.55)
        ax2.triplot(tri, color=COLORS[key], linewidth=0.45)
        ax2.set_xlim(0, SIZE)
        ax2.set_ylim(0, SIZE)
        ax2.set_aspect("equal")
        ax2.set_xticks([])
        ax2.set_yticks([])
        e = errors[key]
        ax2.set_xlabel(f"height error: average {e.mean():.1f} m · "
                       f"steep spots {np.percentile(e, 95):.0f} m", fontsize=12)
    fig.text(0.5, 0.975, "The same 3 × 3 km of mountain, meshed three ways", ha="center",
             fontsize=17, fontweight="bold")
    fig.text(0.5, 0.95, "Top: what X-Plane draws · Bottom: the triangles seen from above "
             "(grey shading = real terrain: ridge in the north, rock band, gully, flat valley floor)",
             ha="center", fontsize=11, color="#555555")
    fig.subplots_adjust(left=0.01, right=0.99, top=0.93, bottom=0.04, wspace=0.03, hspace=0.04)
    fig.savefig(OUT / "mesh_compare.png", facecolor="white")
    plt.close(fig)

    # -- Figure 2: a cross-section over the cliff and the ridge --------------
    xs = 2200.0
    ys = np.linspace(0, SIZE, 1200)
    line = np.column_stack([np.full_like(ys, xs), ys])
    fig, axes = plt.subplots(3, 1, figsize=(13, 9.5), dpi=110, sharex=True)
    for ax, key in zip(axes, ["xpme", "improve", "mesh"]):
        pts, z = meshes[key]
        interp = LinearNDInterpolator(pts, z)
        ax.fill_between(ys, 1100, real_at(line), color="#dddddd", label="real terrain (LiDAR)")
        ax.plot(ys, real_at(line), color=COLORS["real"], linewidth=1.2)
        ax.plot(ys, interp(line), color=COLORS[key], linewidth=2.4, label=LABELS[key])
        near = np.abs(pts[:, 0] - xs) < 18
        ax.plot(pts[near, 1], z[near], "o", color=COLORS[key], markersize=4)
        ax.set_ylim(1150, 2750)
        ax.set_xlim(0, SIZE)
        ax.set_ylabel("height (m)")
        ax.legend(loc="upper left", fontsize=11, frameon=False)
        ax.grid(alpha=0.25)
    axes[0].annotate("ridge cut off,\nslope smeared", xy=(2380, 2480), xytext=(2600, 1650),
                     arrowprops={"arrowstyle": "->"}, fontsize=10)
    axes[1].annotate("right heights at the points,\nbut too few points on the crest",
                     xy=(2330, 2540), xytext=(2480, 1650), arrowprops={"arrowstyle": "->"}, fontsize=10)
    axes[2].annotate("points where the\nterrain bends", xy=(2300, 2560), xytext=(2530, 1700),
                     arrowprops={"arrowstyle": "->"}, fontsize=10)
    axes[2].annotate("few points on the\nflat valley floor", xy=(300, 1210), xytext=(250, 1750),
                     arrowprops={"arrowstyle": "->"}, fontsize=10)
    axes[2].set_xlabel("distance south → north (m), cut through the rock band and the ridge")
    fig.suptitle("Cross-section: dots are mesh points, X-Plane draws straight lines between them",
                 fontsize=14, fontweight="bold")
    fig.tight_layout()
    fig.savefig(OUT / "mesh_profile.png", facecolor="white")
    plt.close(fig)
    for key in meshes:
        print(f"{key:8s} points {len(meshes[key][0]):5d}  mean error {errors[key].mean():5.1f} m  "
              f"p95 {np.percentile(errors[key], 95):5.0f} m")


if __name__ == "__main__":
    main()
