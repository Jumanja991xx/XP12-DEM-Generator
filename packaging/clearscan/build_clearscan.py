"""ClearScan Schleißheim - X-Plane 12 overlay from Bavarian survey data (test pack).

* Buildings: LoD2 geometry (CityGML). Walls cut into bays x storeys on a shared
  facade atlas (plaster colour from the photogrammetry, quantised to a palette;
  night version with lit windows). Roofs baked per house: colour from the
  photogrammetry (lies exactly on the roof in 3D), roofing only as a brightness
  pattern.
* Landmarks (LoD2 culture/religious buildings + attached wings): photogrammetry.
* Trees: single trees from DOM20 - DGM1, placed as X-Plane point forests
  (7 height classes x broadleaf/conifer).
* The airfield (EDNX, OSM boundary) is left out: no objects, no trees, no
  exclusion zones there - the airport comes from the X-Plane gateway.

Usage (from the repository): python packaging/clearscan/build_clearscan.py <out dir>
"""

import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import requests
import shapely
import tifffile
from PIL import Image, ImageDraw
from pyproj import Transformer
from scipy import ndimage
from scipy.spatial import cKDTree
from shapely.geometry import Point, Polygon, box

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "clearscan_out").resolve()
WORK = Path(os.environ.get("CLEARSCAN_WORK", OUT / "work"))
os.environ.setdefault("DOMMESH_WORK", str(WORK / "slpk"))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "dommesh"))
sys.path.insert(0, str(REPO))
import textures as T  # noqa: E402
import roofs as R2  # noqa: E402
import trees as TR  # noqa: E402
from slpk import Slpk  # noqa: E402
from xp12dem.imagery import write_dds  # noqa: E402

PACK = OUT / "ClearScan_Schleissheim"
OBJ_DIR = PACK / "objects"
DSFTOOL = REPO / "third_party" / "Ortho4XP" / "Utils" / "lin" / "DSFTool"
CIRCLES = [(11.5640, 48.2510, 1300), (11.5770, 48.2808, 1300), (11.5835, 48.2490, 450),
           (11.5594, 48.2394, 900)]
CELL = 400.0                 # m: buildings are grouped into objects per cell
to_utm = Transformer.from_crs(4326, 25832, always_xy=True)
to_ll = Transformer.from_crs(25832, 4326, always_xy=True)
t0 = time.time()


def log(*a):
    print(f"[{time.time() - t0:6.0f}s]", *a, flush=True)


def fetch(url, path, min_size=1000):
    path = Path(path)
    if path.exists() and path.stat().st_size >= min_size:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(8):
        u = url if attempt % 2 == 0 else url.replace("download1.", "download2.")
        try:
            r = requests.get(u, timeout=600)
            if r.status_code == 200 and len(r.content) >= min_size:
                path.write_bytes(r.content)
                return path
        except Exception:
            pass
        time.sleep(min(30, 3 * (attempt + 1)))
    raise IOError(f"download failed: {url}")


# --------------------------------------------------------------------------- Gebiet
airport_ll = json.loads((HERE / "ednx.json").read_text())["lonlat"]
airport = Polygon([to_utm.transform(*p) for p in airport_ll]).buffer(40)
area = shapely.union_all([Point(to_utm.transform(lo, la)).buffer(r, 64) for lo, la, r in CIRCLES]).difference(airport)
shapely.prepare(area)
X0, Y0, X1, Y1 = area.bounds
KM = [(x, y) for x in range(int(X0 // 1000), int(X1 // 1000) + 1) for y in range(int(Y0 // 1000), int(Y1 // 1000) + 1)
      if area.intersects(box(x * 1000, y * 1000, x * 1000 + 1000, y * 1000 + 1000))]
log("Gebiet", round(area.area / 1e6, 1), "km²,", len(KM), "km-Kacheln")

# --------------------------------------------------------------------------- Geländemodell
kx0, kx1 = min(k[0] for k in KM), max(k[0] for k in KM)
ky0, ky1 = min(k[1] for k in KM), max(k[1] for k in KM)
with ThreadPoolExecutor(8) as pool:
    list(pool.map(lambda t: fetch(f"https://download1.bayernwolke.de/a/dgm/dgm1/{t[0]}_{t[1]}.tif",
                                  WORK / "dgm" / f"{t[0]}_{t[1]}.tif", 100000),
                  [(x, y) for x in range(kx0, kx1 + 1) for y in range(ky0, ky1 + 1)]))
DGM = np.vstack([np.hstack([tifffile.imread(WORK / "dgm" / f"{x}_{y}.tif").astype(np.float32)
                            for x in range(kx0, kx1 + 1)]) for y in range(ky1, ky0 - 1, -1)])
DGM_E0, DGM_N1 = kx0 * 1000.0, ky1 * 1000.0 + 1000.0


def ground(e, n):
    e, n = np.asarray(e, float), np.asarray(n, float)
    col = np.clip(e - DGM_E0, 0, DGM.shape[1] - 1.001)
    row = np.clip(DGM_N1 - n, 0, DGM.shape[0] - 1.001)
    c0, r0 = col.astype(int), row.astype(int)
    fc, fr = col - c0, row - r0
    return ((DGM[r0, c0] * (1 - fc) + DGM[r0, c0 + 1] * fc) * (1 - fr)
            + (DGM[r0 + 1, c0] * (1 - fc) + DGM[r0 + 1, c0 + 1] * fc) * fr)


log("DGM", DGM.shape)

# --------------------------------------------------------------------------- Luftbild (Vegetation, Reservefarbe)
DOP_M = 40


def dop(t):
    path = WORK / "dop" / f"{t[0]}_{t[1]}.jpg"
    if not path.exists():
        x0, y0 = t[0] * 1000 - DOP_M, t[1] * 1000 - DOP_M
        for attempt in range(6):
            try:
                r = requests.get("https://geoservices.bayern.de/od/wms/dop/v1/dop40", params={
                    "SERVICE": "WMS", "VERSION": "1.1.1", "REQUEST": "GetMap", "LAYERS": "by_dop40c", "STYLES": "",
                    "SRS": "EPSG:25832", "BBOX": f"{x0},{y0},{x0 + 1000 + 2 * DOP_M},{y0 + 1000 + 2 * DOP_M}",
                    "WIDTH": 2700, "HEIGHT": 2700, "FORMAT": "image/jpeg"}, timeout=300)
                if r.headers.get("content-type", "").startswith("image"):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(r.content)
                    break
            except Exception:
                pass
            time.sleep(3 * (attempt + 1))
    return path


with ThreadPoolExecutor(6) as pool:
    list(pool.map(dop, KM))
log("Luftbild")

# --------------------------------------------------------------------------- LoD2-Gebäude
LOD = sorted({(x // 2 * 2, y // 2 * 2) for x, y in KM})
with ThreadPoolExecutor(6) as pool:
    list(pool.map(lambda t: fetch(f"https://download1.bayernwolke.de/a/lod2/citygml/{t[0]}_{t[1]}.gml",
                                  WORK / "lod2" / f"{t[0]}_{t[1]}.gml", 2000), LOD))
NB = "{http://www.opengis.net/citygml/building/1.0}"
NG = "{http://www.opengis.net/gml}"
buildings, mask_fp = [], []
for t in LOD:
    for ev, el in ET.iterparse(WORK / "lod2" / f"{t[0]}_{t[1]}.gml", events=("end",)):
        if el.tag != NB + "Building":
            continue
        surfs = []
        for kind in ("WallSurface", "RoofSurface"):
            for s in el.iter(NB + kind):
                for pl in s.iter(NG + "posList"):
                    a = np.array(pl.text.split(), float).reshape(-1, 3)
                    if len(a) >= 4:
                        surfs.append((kind[0], a[:-1] if np.allclose(a[0], a[-1]) else a))
        if surfs:
            allp = np.vstack([s[1] for s in surfs])
            c = allp[:, :2].mean(0)
            try:                                       # every building masks trees, also outside the area
                mask_fp.append(Polygon(allp[:, :2]).convex_hull if len(allp) > 2 else None)
            except Exception:
                pass
            if area.contains(Point(c)):
                gs = None
                for g in el.iter(NB + "GroundSurface"):
                    for pl in g.iter(NG + "posList"):
                        gs = np.array(pl.text.split(), float).reshape(-1, 3)
                fn, st = el.find(NB + "function"), el.find(NB + "storeysAboveGround")
                buildings.append({"surfs": surfs, "ground": gs, "function": fn.text if fn is not None else "",
                                  "storeys": int(st.text) if st is not None and st.text.isdigit() else 0})
        el.clear()
log("Gebäude", len(buildings))


def footprint(b):
    g = b["ground"] if b["ground"] is not None else np.vstack([s[1] for s in b["surfs"]])
    try:
        p = Polygon(g[:, :2]).buffer(0)
        return p if p.area > 0.5 else Polygon(g[:, :2]).convex_hull
    except Exception:
        return Polygon(g[:, :2]).convex_hull


for b in buildings:
    b["fp"] = footprint(b)
    allp = np.vstack([s[1] for s in b["surfs"]])
    b["height"] = float(np.ptp(allp[:, 2]))
    b["area"] = b["fp"].area

# Wahrzeichen: Kultur- und Religionsbauten + direkt angebaute höhere Gebäude
seeds = [b["fp"] for b in buildings if b["function"][:9] in ("31001_303", "31001_304")]
seed_u = shapely.union_all(seeds).buffer(2.0) if seeds else Polygon()
for b in buildings:
    b["landmark"] = b["function"][:9] in ("31001_303", "31001_304") or (b["height"] > 8 and b["fp"].intersects(seed_u))
landmark_poly = shapely.union_all([b["fp"] for b in buildings if b["landmark"]]).buffer(1.5)
shapely.prepare(landmark_poly)
foot_buf = shapely.union_all([b["fp"] for b in buildings]).buffer(1.2)
shapely.prepare(foot_buf)
log("Wahrzeichen", sum(b["landmark"] for b in buildings))

# --------------------------------------------------------------------------- Photogrammetrie
kml = requests.get("https://geodaten.bayern.de/odd/m/3/daten/DOMMesh/DOM_Mesh_projektgebiete_2026.kml", timeout=300).text
los = {}
for pm in re.findall(r"<Placemark[^>]*>(.*?)</Placemark>", kml, re.S):
    name = re.search(r"<name>([^<]*)", pm).group(1)
    for c in re.findall(r"<coordinates>(.*?)</coordinates>", pm, re.S):
        poly = Polygon([to_utm.transform(*map(float, p.split(",")[:2])) for p in c.split()])
        if poly.intersects(area):
            los[name] = poly
log("DOM-Mesh-Gebiete", sorted(los))

cent, cols, norms = [], [], []
landmark_parts = []
for key, lp in los.items():
    S = Slpk(f"https://download1.bayernwolke.de/p/dom-mesh-slpk/{key}/DSM_Mesh.slpk", key)
    clip = lp.intersection(area)
    nodes = [i for i in S.select(*clip.bounds, 200)
             if clip.intersects(box(*(lambda c, e: (c[0] - e[0], c[1] - e[1], c[0] + e[0], c[1] + e[1]))(*S.extent(S.node(i)))))]
    names = []
    for i in nodes:
        r = S.node(i)["mesh"]["geometry"]["resource"]
        names += [f"nodes/{r}/geometries/0.bin.gz", f"nodes/{r}/textures/0.jpg"]
    S.fetch(names, 8)
    log(key, len(nodes), "Knoten (Farbproben)")
    for i in nodes:
        try:
            pos, uv, res = S.geometry(i)
            tex = np.asarray(Image.open(io.BytesIO(S.get(f"nodes/{res}/textures/0.jpg"))).convert("RGB"))
        except Exception as exc:
            log("skip", i, exc)
            continue
        tri = pos.reshape(-1, 3, 3)
        c = tri.mean(1)
        h = (pos[:, 2] - ground(pos[:, 0], pos[:, 1])).reshape(-1, 3).max(1)
        ok = (h >= 2.0) & shapely.contains_xy(clip, c[:, 0], c[:, 1]) & shapely.contains_xy(foot_buf, c[:, 0], c[:, 1])
        e1, e2 = tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]
        nrm = np.cross(e1, e2)
        ar = np.linalg.norm(nrm, axis=1) / 2
        nrm = nrm / np.maximum(2 * ar, 1e-9)[:, None]
        sel = np.where(ok & (ar > 0.01))[0]
        if not len(sel):
            continue
        cnt = np.minimum(np.ceil(ar[sel] / 0.16).astype(int), 300)
        rep = np.repeat(sel, cnt)
        rng = np.random.default_rng(i)
        r1, r2 = rng.random(len(rep)), rng.random(len(rep))
        flip = r1 + r2 > 1
        r1, r2 = np.where(flip, 1 - r1, r1), np.where(flip, 1 - r2, r2)
        tuv = uv.reshape(-1, 3, 2)
        suv = tuv[rep, 0] + r1[:, None] * (tuv[rep, 1] - tuv[rep, 0]) + r2[:, None] * (tuv[rep, 2] - tuv[rep, 0])
        sx = np.clip((suv[:, 0] * tex.shape[1]).astype(int), 0, tex.shape[1] - 1)
        sy = np.clip((suv[:, 1] * tex.shape[0]).astype(int), 0, tex.shape[0] - 1)
        cent.append((tri[rep, 0] + r1[:, None] * e1[rep] + r2[:, None] * e2[rep]).astype(np.float32))
        cols.append(tex[sy, sx])
        norms.append(nrm[rep].astype(np.float16))
    # Wahrzeichen in feinster Stufe
    lm = landmark_poly.intersection(clip)
    done = set()
    for part in getattr(lm, "geoms", [lm]):
        if part.is_empty:
            continue
        for i in S.select(*part.bounds, 40):
            if i in done:
                continue
            done.add(i)
            try:
                pos, uv, res = S.geometry(i)
                img = Image.open(io.BytesIO(S.get(f"nodes/{res}/textures/0.jpg"))).convert("RGB")
            except Exception as exc:
                log("skip", i, exc)
                continue
            c = pos.reshape(-1, 3, 3).mean(1)
            hh = (pos[:, 2] - ground(pos[:, 0], pos[:, 1])).reshape(-1, 3).max(1)
            m = shapely.contains_xy(landmark_poly, c[:, 0], c[:, 1]) & (hh >= 0.8)
            if m.sum() >= 3:
                m3 = np.repeat(m, 3)
                landmark_parts.append((f"lm_{key}_{i}", pos[m3], uv[m3].astype(np.float64), img))
cent = np.vstack(cent)
cols = np.vstack(cols).astype(np.float32)
norms = np.vstack(norms).astype(np.float32)
kd = cKDTree(cent)
log("Farbproben", len(cent), "Wahrzeichen-Teile", len(landmark_parts))


def sample(points, n, min_facing):
    dist, idx = kd.query(points, k=8, distance_upper_bound=1.4)
    ok = dist < np.inf
    idc = np.minimum(idx, len(cols) - 1)
    wgt = (ok & ((norms[idc] @ n) > min_facing)) / (0.15 + np.where(ok, dist, 1e9))
    ws = wgt.sum(1)
    col = (cols[idc] * wgt[..., None]).sum(1) / np.maximum(ws, 1e-9)[:, None]
    return col, ws > 0


# --------------------------------------------------------------------------- Flächen vorbereiten
def plane(a):
    n = np.zeros(3)
    for i in range(len(a)):
        p, q = a[i], a[(i + 1) % len(a)]
        n += [(p[1] - q[1]) * (p[2] + q[2]), (p[2] - q[2]) * (p[0] + q[0]), (p[0] - q[0]) * (p[1] + q[1])]
    ln = np.linalg.norm(n)
    return n / ln if ln > 1e-9 else None


def tris2d(geom):
    out = []
    for part in getattr(geom, "geoms", [geom]):
        if part.geom_type == "Polygon" and part.area > 0.01:
            for t in shapely.constrained_delaunay_triangles(part).geoms:
                out.append(np.array(t.exterior.coords)[:3])
    return out


normal_b = [b for b in buildings if not b["landmark"]]
for bi, b in enumerate(normal_b):
    cen = np.vstack([s[1] for s in b["surfs"]]).mean(0)
    b["walls"], b["roofs"] = [], []
    for kind, a in b["surfs"]:
        n = plane(a)
        if n is None:
            continue
        if kind == "R" or abs(n[2]) > 0.3:
            b["roofs"].append((a, n if n[2] > 0 else -n))
        else:
            if (a[:, :2].mean(0) - cen[:2]) @ n[:2] < 0:
                n = -n
            b["walls"].append((a, n))
    # Putzfarbe: Photogrammetrie vor den Wänden
    acc, cnt = np.zeros(3), 0
    for a, n in b["walls"]:
        u = np.cross([0, 0, 1], n)
        u /= max(np.linalg.norm(u), 1e-9)
        L = np.linalg.norm((a[:, :2] - a[0, :2]) @ u[:2][:, None], axis=1).max()
        zz = a[:, 2]
        gu, gv = np.meshgrid(np.arange(0.5, max(L, 0.6), 1.5), np.arange(zz.min() + 0.8, max(zz.max() - 0.3, zz.min() + 0.9), 1.5))
        P = a[0] + gu.ravel()[:, None] * u
        P[:, 2] = gv.ravel()
        col, ok = sample(P + n * 0.4, n, 0.4)
        acc += col[ok].sum(0)
        cnt += int(ok.sum())
    c = acc / cnt if cnt > 10 else np.array([205, 198, 186.0])
    c = c * 1.08 + 6
    c = c.mean() + (c - c.mean()) * 1.15
    b["plaster"] = np.clip(c, 60, 238)
log("Putzfarben")

# Palette: k-means über die Putzfarben
P = np.array([b["plaster"] for b in normal_b])
K = 24
rng = np.random.default_rng(3)
centers = P[rng.choice(len(P), K, replace=False)]
for _ in range(25):
    lab = np.argmin(((P[:, None] - centers[None]) ** 2).sum(-1), 1)
    for k in range(K):
        if (lab == k).any():
            centers[k] = P[lab == k].mean(0)
for b, l in zip(normal_b, lab):
    b["ci"] = int(l)
rgb, lit, cells = T.facade_atlas(centers)
OBJ_DIR.mkdir(parents=True, exist_ok=True)
write_dds(rgb, OBJ_DIR / "facade.dds")
write_dds(lit, OBJ_DIR / "facade_LIT.dds")
log("Fassaden-Atlas", K, "Farben")


def kind_of(b):
    f = b["function"]
    if f.startswith("51009"):
        return "roofonly"
    if f in ("31001_2463", "31001_2465"):
        return "garage"
    if f.startswith("31001_1"):
        return "house"
    if f.startswith("31001_3"):
        return "public"
    if b["area"] < 45 and b["height"] < 5:
        return "shed"
    if f.startswith("31001_2"):
        return "commercial" if b["height"] > 6 or b["area"] > 300 else "shed"
    return "house"


def cell_uv(ci, t, pu, pv):
    """pu, pv: pixel offset inside the cell from its bottom-left corner -> texture (u, v)."""
    x0, y0 = cells[(ci, t)]
    return (x0 + np.clip(pu, 1, T.CELL_W - 1)) / T.ATLAS_W, 1 - (y0 + T.CELL_H - np.clip(pv, 1, T.CELL_H - 1)) / T.ATLAS_H


def wall_triangles(b, a, n, door_wall, rng_b):
    """[(world tri 3x3, uv 3x2)] for one wall."""
    kind = b["kind"]
    u = np.cross([0, 0, 1], n)
    u /= max(np.linalg.norm(u), 1e-9)
    o = a[0].copy()
    zmin = a[:, 2].min()
    uu = (a - o) @ u
    vv = a[:, 2] - zmin
    umin = uu.min()
    uu -= umin
    o = o + u * umin
    w, H = uu.max(), vv.max()
    if w < 0.3 or H < 0.3:
        return []
    poly = Polygon(np.column_stack([uu, vv])).buffer(0)
    left = vv[uu < 0.5].max() if (uu < 0.5).any() else H
    right = vv[uu > w - 0.5].max() if (uu > w - 0.5).any() else H
    eave = min(left, right)
    ci = b["ci"]
    pieces = []        # (geometry, mapper(u,v)->uv)

    def mapper(t, u0, v0, uw):
        return lambda U, V: cell_uv(ci, t, (U - u0) / uw * T.CELL_W, (V - v0) * T.PX_M)

    if kind in ("shed", "roofonly"):
        t = "wood" if kind == "shed" else "plain_g"
        pieces.append((poly, lambda U, V, t=t: cell_uv(ci, t, (U % T.BAY_M) / T.BAY_M * T.CELL_W,
                                                          np.minimum(V, 3.9) * T.PX_M)))
    else:
        sh = {"house": 2.8, "public": 3.4, "commercial": 3.9, "garage": max(2.4, min(eave, 3.9))}[kind]
        nbay = 1 if kind == "garage" else max(1, int(round(w / T.BAY_M)))
        bw = w / nbay
        nst = max(1, int((eave + 0.2) // sh)) if kind != "garage" else 1
        for k in range(nst):
            for j in range(nbay):
                piece = poly.intersection(box(j * bw, k * sh, (j + 1) * bw, (k + 1) * sh))
                if piece.is_empty:
                    continue
                if kind == "garage":
                    t = "garage" if 2.6 < w < 9 else "plain_g"
                elif kind == "commercial":
                    t = "com_plain" if rng_b.random() < 0.15 else "ribbon"
                elif k == 0:
                    if door_wall and j == nbay // 2 and kind == "house":
                        t = "door_g"
                    else:
                        t = "plain_g" if rng_b.random() < 0.08 else "win_g"
                else:
                    t = "plain" if rng_b.random() < 0.08 else b["upper"]
                pieces.append((piece, mapper(t, j * bw, k * sh, bw)))
        rest = poly.intersection(box(0, nst * sh, w, H + 1))
        if not rest.is_empty:
            pieces.append((rest, lambda U, V: cell_uv(ci, "plain", np.full_like(U, T.CELL_W / 2),
                                                      np.full_like(V, T.CELL_H / 2))))
    out = []
    for geom, mp in pieces:
        for t2 in tris2d(geom):
            U, V = t2[:, 0], t2[:, 1]
            tu, tv = mp(U, V)
            world = o + U[:, None] * u
            world[:, 2] = zmin + V - np.where(V < 0.05, 1.0, 0.0)     # 1 m unter Grund: keine Lücke
            out.append((world, np.column_stack([tu, tv])))
    return out


def roof_faces(b, a, n, atlas):
    """[(world tri 3x3, atlas item, (u, v) metres inside the tile)] for one roof face."""
    flat = n[2] > 0.985
    if flat:
        uax = np.array([1.0, 0, 0]) - n * n[0]
        uax /= np.linalg.norm(uax)
        vax = np.cross(n, uax)
    else:
        vax = np.array([0, 0, 1.0]) - n * n[2]          # v points up the slope
        vax /= np.linalg.norm(vax)
        uax = np.cross(vax, n)
    o = a[0]
    uv = np.column_stack([(a - o) @ uax, (a - o) @ vax])
    mn = uv.min(0)
    uv -= mn
    w, h = uv.max(0)
    if w < 0.3 or h < 0.3:
        return []
    o2 = o + uax * mn[0] + vax * mn[1]
    step = 0.3
    gu, gv = np.meshgrid(np.arange(0.15, w, step), np.arange(0.15, h, step))
    W = o2 + gu.reshape(-1, 1) * uax + gv.reshape(-1, 1) * vax + n * 0.3
    col, ok = sample(W, n, 0.5)
    poly = Polygon(uv).buffer(0)
    if len(ok):
        ok &= shapely.contains_xy(poly, gu.ravel(), gv.ravel())
    c = R2.robust_colour(col, ok) if len(ok) else None
    if c is None:
        c = b.get("roof_colour", np.array([150, 82, 62.0]))
    b["roof_colour"] = c
    item = atlas.add(R2.material_of(c, flat, b["kind"], b["area"]), c)
    rng = np.random.default_rng(int(abs(o[0] * 13 + o[1] * 7)) % 100000)
    du, dv = rng.uniform(0, R2.TILE_M, 2) if flat else (rng.uniform(0, R2.TILE_M), 0.0)
    out = []
    TM = R2.TILE_M
    for i in range(int((w + du) // TM) + 1):
        for j in range(int((h + dv) // TM) + 1):
            u0, v0 = i * TM - du, j * TM - dv
            piece = poly.intersection(box(u0, v0, u0 + TM, v0 + TM))
            for t2 in tris2d(piece):
                world = o2 + t2[:, :1] * uax + t2[:, 1:2] * vax + n * 0.02
                out.append((world, item, np.column_stack([t2[:, 0] - u0, t2[:, 1] - v0])))
    return out


# --------------------------------------------------------------------------- OBJ-Ausgabe
def to_local(P, lon0, lat0, h0):
    lon, lat = to_ll.transform(P[:, 0], P[:, 1])
    m_lon = 111319.49 * math.cos(math.radians(lat0))
    return np.column_stack([(lon - lon0) * m_lon, P[:, 2] - h0, -(lat - lat0) * 111132.95])


def write_obj(name, tex, tris, uvs, nrm, lon0, lat0, h0, lit=None, lod=(0, 7000), no_cull=False):
    """tris (N,3,3) UTM+height, uvs (N,3,2), nrm (N,3) outward normals (UTM axes)."""
    N = len(tris)
    loc = to_local(tris.reshape(-1, 3), lon0, lat0, h0).reshape(N, 3, 3)
    nl = np.column_stack([nrm[:, 0], nrm[:, 2], -nrm[:, 1]])
    cr = np.cross(loc[:, 1] - loc[:, 0], loc[:, 2] - loc[:, 0])
    swap = np.einsum("ij,ij->i", cr, nl) > 0                 # X-Plane: Uhrzeigersinn von außen
    loc[swap] = loc[swap][:, [0, 2, 1]]
    uvs = uvs.copy()
    uvs[swap] = uvs[swap][:, [0, 2, 1]]
    v = np.column_stack([loc.reshape(-1, 3), np.repeat(nl, 3, 0), uvs.reshape(-1, 2)])
    key = np.round(v * [500, 500, 500, 100, 100, 100, 1e5, 1e5]).astype(np.int64)
    _, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    verts, idx = v[first], inv.reshape(-1)
    with open(OBJ_DIR / f"{name}.obj", "w") as f:
        f.write(f"I\n800\nOBJ\n\nTEXTURE {tex}\n" + (f"TEXTURE_LIT {lit}\n" if lit else "")
                + f"POINT_COUNTS {len(verts)} 0 0 {len(idx)}\n\n")
        np.savetxt(f, verts, fmt="VT %.3f %.3f %.3f %.3f %.3f %.3f %.5f %.5f")
        full = len(idx) // 10 * 10
        np.savetxt(f, idx[:full].reshape(-1, 10), fmt="IDX10 " + " ".join(["%d"] * 10))
        for i in idx[full:]:
            f.write(f"IDX {i}\n")
        f.write(f"\nATTR_LOD {lod[0]} {lod[1]}\n" + ("ATTR_no_cull\n" if no_cull else "") + f"TRIS 0 {len(idx)}\n")
    return N


placements = []           # (obj name, lon, lat)
stats = {"walls": 0, "roofs": 0, "landmark_tris": 0}
groups = {}
for b in normal_b:
    c = b["fp"].centroid
    groups.setdefault((int(c.x // CELL), int(c.y // CELL)), []).append(b)
atlas = R2.RoofAtlas()
cell_geo = {}
for (gx, gy), bl in sorted(groups.items()):
    W_tris, W_uv, W_n, RF = [], [], [], []
    for b in bl:
        rng_b = np.random.default_rng(int(b["fp"].centroid.x * 7 + b["fp"].centroid.y))
        b["kind"] = kind_of(b)
        b["upper"] = ["win", "win", "win", "win_shut_g", "win_shut_b"][rng_b.integers(0, 5)] \
            if b["kind"] == "house" else "win"
        lens = [np.ptp((a - a[0]) @ np.cross([0, 0, 1], n)) for a, n in b["walls"]]
        door = int(np.argmax(lens)) if lens else -1
        for wi, (a, n) in enumerate(b["walls"]):
            for world, uv in wall_triangles(b, a, n, wi == door, rng_b):
                W_tris.append(world)
                W_uv.append(uv)
                W_n.append(n)
        for a, n in b["roofs"]:
            for world, item, luv in roof_faces(b, a, n, atlas):
                RF.append((world, item, luv, n))
    cell_geo[(gx, gy)] = (W_tris, W_uv, W_n, RF)
write_dds(atlas.build(), OBJ_DIR / "roofs.dds")
log("Dach-Atlas", len(atlas.items), "Dachflächen,", len(atlas.cells), "Material-Farben")
for (gx, gy), (W_tris, W_uv, W_n, RF) in sorted(cell_geo.items()):
    ce, cn = (gx + 0.5) * CELL, (gy + 0.5) * CELL
    lon0, lat0 = to_ll.transform(ce, cn)
    h0 = float(ground(np.array([ce]), np.array([cn]))[0])
    if W_tris:
        name = f"walls_{gx}_{gy}"
        stats["walls"] += write_obj(name, "facade.dds", np.array(W_tris), np.array(W_uv), np.array(W_n),
                                    lon0, lat0, h0, lit="facade_LIT.dds")
        placements.append((name, lon0, lat0))
    if RF:
        name = f"roofs_{gx}_{gy}"
        uvs = np.array([np.column_stack(atlas.uv(item, luv[:, 0], luv[:, 1])) for _, item, luv, _ in RF])
        stats["roofs"] += write_obj(name, "roofs.dds", np.array([r[0] for r in RF]), uvs,
                                    np.array([r[3] for r in RF]), lon0, lat0, h0)
        placements.append((name, lon0, lat0))
log("Gebäude-Objekte", stats)

for name, pos, uv, img in landmark_parts:
    e0, n0 = (pos[:, 0].min() + pos[:, 0].max()) / 2, (pos[:, 1].min() + pos[:, 1].max()) / 2
    lon0, lat0 = to_ll.transform(e0, n0)
    h0 = float(ground(np.array([e0]), np.array([n0]))[0])
    tri = pos.reshape(-1, 3, 3)
    nrm = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    nrm /= np.maximum(np.linalg.norm(nrm, axis=1, keepdims=True), 1e-9)
    small = img.resize((min(img.size[0], 1024), min(img.size[1], 1024)), Image.BOX)
    write_dds(np.asarray(small), OBJ_DIR / f"{name}.dds")
    uvs = uv.reshape(-1, 3, 2).copy()
    uvs[..., 1] = 1 - uvs[..., 1]
    # Photogrammetrie: Normalen nach oben (Licht ist im Bild), beidseitig zeichnen
    up = np.tile([0, 0, 1.0], (len(tri), 1))
    stats["landmark_tris"] += write_obj(name, f"{name}.dds", tri, uvs, up, lon0, lat0, h0, no_cull=True)
    placements.append((name, lon0, lat0))
log("Objekte", len(placements), stats)

# --------------------------------------------------------------------------- Bäume
dom_tiles = [t for t in KM]
with ThreadPoolExecutor(4) as pool:
    list(pool.map(lambda t: fetch(f"https://download1.bayernwolke.de/a/dom20/DOM/32{t[0]}_{t[1]}_20_DOM.tif",
                                  WORK / "dom" / f"{t[0]}_{t[1]}.tif", 1_000_000), dom_tiles))
RES = 0.4
BROAD_MIX = [("linde", 30), ("ahorn", 30), ("eiche", 14), ("buche", 16), ("birke", 10)]


def species_of(con, peaked, h, crown, lum, rgb, e, n):
    """Plausible species from crown shape and aerial colour (no species map for single trees)."""
    rnd = (math.sin(e * 12.9898 + n * 78.233) * 43758.5453) % 1.0
    if con:
        if not peaked and h > 12:
            return "kiefer"                            # flat, irregular crown on a tall trunk
        if lum > 70 and rnd < 0.35:
            return "laerche"                           # lighter green conifer
        return "fichte"
    if h > 18 and crown / h < 0.13 and rnd < 0.6:
        return "pappel"                                # columnar
    if lum > 100 and h < 18 and rnd < 0.5:
        return "birke"                                 # bright, light crown
    tot = sum(w for _, w in BROAD_MIX)
    acc = 0.0
    for name, w in BROAD_MIX:
        acc += w / tot
        if rnd < acc:
            return name
    return "linde"


all_fp = [b["fp"].buffer(1.2) for b in buildings] + [p.buffer(1.2) for p in mask_fp if p is not None and p.area > 1]
fp_tree = shapely.STRtree(all_fp)
trees = []
for x, y in dom_tiles:
    dom = tifffile.imread(WORK / "dom" / f"{x}_{y}.tif").astype(np.float32)
    dom[dom < -100] = np.nan
    dsm = np.nan_to_num(np.nanmax(dom.reshape(2500, 2, 2500, 2), axis=(1, 3)), nan=0)
    xe = x * 1000 + (np.arange(2500) + 0.5) * RES
    yn = y * 1000 + 1000 - (np.arange(2500) + 0.5) * RES
    EE, NN = np.meshgrid(xe, yn)
    gnd = ground(EE, NN)
    chm = dsm - gnd
    mask = Image.new("L", (2500, 2500), 0)
    dr = ImageDraw.Draw(mask)
    for i in fp_tree.query(box(x * 1000, y * 1000, x * 1000 + 1000, y * 1000 + 1000)):
        for part in getattr(all_fp[i], "geoms", [all_fp[i]]):
            dr.polygon([((e - x * 1000) / RES, (y * 1000 + 1000 - n) / RES) for e, n in part.exterior.coords], fill=255)
    bld = np.asarray(mask) > 0
    d = np.asarray(Image.open(dop((x, y))).convert("RGB").resize((2700, 2700)), np.float32)[100:2600, 100:2600]
    r_, g_, b_ = d[..., 0], d[..., 1], d[..., 2]
    veg = ndimage.binary_opening(((g_ >= r_ * 0.92) & (g_ >= b_ * 0.95)) | ((2 * g_ - r_ - b_) > 8), iterations=1)
    chs = ndimage.gaussian_filter(chm, 1.3)
    cand = (chs == ndimage.maximum_filter(chs, size=5)) & (chm > 3.0) & ~bld & veg & (chs > 3.0)
    rows, cols_ = np.nonzero(cand)
    E, N = xe[cols_], yn[rows]
    inside = shapely.contains_xy(area, E, N)
    rows, cols_, E, N = rows[inside], cols_[inside], E[inside], N[inside]
    hs = chs[rows, cols_]
    order = np.argsort(-hs)
    rows, cols_, E, N, hs = rows[order], cols_[order], E[order], N[order], hs[order]
    keep = np.ones(len(E), bool)
    kdt = cKDTree(np.column_stack([E, N])) if len(E) else None
    for i in range(len(E)):
        if keep[i]:
            for j in kdt.query_ball_point([E[i], N[i]], max(1.6, 0.11 * hs[i] + 1.0)):
                if j > i:
                    keep[j] = False
    rows, cols_, E, N, hs = rows[keep], cols_[keep], E[keep], N[keep], hs[keep]
    k = 3
    sub = np.stack([chm[np.clip(rows + dy, 0, 2499), np.clip(cols_ + dx, 0, 2499)]
                    for dy in (-k, 0, k) for dx in (-k, 0, k)], 1)
    peaked = (hs - sub.mean(1)) / np.maximum(hs, 1) > 0.09
    lum = d[rows, cols_] @ [0.3, 0.59, 0.11]
    con = (lum < 62) | (peaked & (lum < 85))
    rgb_t = d[rows, cols_]
    dn = cKDTree(np.column_stack([E, N])).query(np.column_stack([E, N]), k=2)[0][:, 1] if len(E) > 1 \
        else np.full(len(E), 10.0)
    crown = np.minimum(np.clip(0.16 * hs + 0.8, 1.2, 7.5), 0.62 * dn + 0.6)
    for i in range(len(E)):
        trees.append((E[i], N[i], hs[i], species_of(con[i], peaked[i], hs[i], crown[i], lum[i], rgb_t[i], E[i], N[i])))
    log(f"Bäume {x},{y}: {len(E)}")
log("Bäume gesamt", len(trees))

# Waldarten: Baumart x Höhenklasse; Texturen je Jahreszeit, X-Plane wählt über library.txt
FOR_DIR = PACK / "forests"
for season in TR.SEASONS:
    (FOR_DIR / season).mkdir(parents=True, exist_ok=True)
    Image.fromarray(TR.season_atlas(season), "RGBA").save(FOR_DIR / season / "trees.png")
H_EDGES = [3, 7, 10, 13, 16, 20, 25, 40]
for_defs, for_points = [], {}
lib = {s: [] for s in TR.SEASONS}
for sp in TR.NAMES:
    for hi in range(len(H_EDGES) - 1):
        lo_h, hi_h = H_EDGES[hi], H_EDGES[hi + 1]
        name = f"{sp}_{lo_h}_{hi_h}"
        for season in TR.SEASONS:
            (FOR_DIR / season / f"{name}.for").write_text(
                TR.for_text(sp, lo_h, min(hi_h, 38), "trees.png"))
            lib[season].append((f"clearscan/trees/{name}.for", f"forests/{season}/{name}.for"))
        for_defs.append(name)
        for_points[name] = []
for e, n, h, sp in trees:
    hi = int(np.clip(np.searchsorted(H_EDGES, h) - 1, 0, len(H_EDGES) - 2))
    lon, lat = to_ll.transform(e, n)
    for_points[f"{sp}_{H_EDGES[hi]}_{H_EDGES[hi + 1]}"].append((lon, lat))
for_defs = [f for f in for_defs if for_points[f]]
head = ["A", "800", "LIBRARY", ""]
SEASON_NAMES = {"spr": "fruehling", "sum": "sommer", "fal": "herbst", "win": "winter"}
(PACK / "library.txt").write_text("\n".join(
    head + ["# Baeume wechseln mit der Jahreszeit (X-Plane 12)"]
    + [f"EXPORT_SEASON {season} {v} {r}" for season in TR.SEASONS for v, r in lib[season]]) + "\n")
alt = PACK / "library_feste_jahreszeit"
alt.mkdir(exist_ok=True)
for season in TR.SEASONS:
    (alt / f"library_{SEASON_NAMES[season]}.txt").write_text("\n".join(
        head + [f"# Feste Jahreszeit: {SEASON_NAMES[season]}"] + [f"EXPORT {v} {r}" for v, r in lib[season]]) + "\n")
species_count = {sp: sum(len(for_points[f]) for f in for_points if f.startswith(sp + "_")) for sp in TR.NAMES}
log("Baumarten", species_count)

# --------------------------------------------------------------------------- DSF
excl = []
G = 250.0
for gx in range(int(X0 // G), int(X1 // G) + 1):
    for gy in range(int(Y0 // G), int(Y1 // G) + 1):
        cellb = box(gx * G, gy * G, gx * G + G, gy * G + G)
        if cellb.intersects(area) and not cellb.intersects(airport):
            lo0, la0 = to_ll.transform(gx * G, gy * G)
            lo1, la1 = to_ll.transform(gx * G + G, gy * G + G)
            excl.append(f"{lo0:.6f}/{la0:.6f}/{lo1:.6f}/{la1:.6f}")
lines = ["I", "800", "DSF2TEXT", "", "PROPERTY sim/planet earth", "PROPERTY sim/overlay 1",
         "PROPERTY sim/require_object 1/0", "PROPERTY sim/require_facade 1/0",
         "PROPERTY sim/creation_agent ClearScan (XP12 DEM Generator)"]
for kind in ("obj", "fac", "for"):
    lines += [f"PROPERTY sim/exclude_{kind} {e}" for e in excl]
lines += ["PROPERTY sim/west 11", "PROPERTY sim/east 12", "PROPERTY sim/south 48", "PROPERTY sim/north 49", ""]
names = sorted({p[0] for p in placements})
lines += [f"OBJECT_DEF objects/{n}.obj" for n in names]
lines += [f"POLYGON_DEF clearscan/trees/{f}.for" for f in for_defs] + [""]
idx = {n: i for i, n in enumerate(names)}
lines += [f"OBJECT {idx[n]} {lo:.9f} {la:.9f} 0.0" for n, lo, la in placements]
n_trees = 0
for fi, f in enumerate(for_defs):
    pts = for_points[f]
    for k in range(0, len(pts), 2000):
        chunk = pts[k:k + 2000]
        lines += [f"BEGIN_POLYGON {fi} {255 + 256 * 2} 2", "BEGIN_WINDING"]
        lines += [f"POLYGON_POINT {lo:.9f} {la:.9f}" for lo, la in chunk]
        lines += ["END_WINDING", "END_POLYGON"]
        n_trees += len(chunk)
txt = WORK / "clearscan_dsf.txt"
txt.write_text("\n".join(lines) + "\n")
dsf = PACK / "Earth nav data" / "+40+010" / "+48+011.dsf"
dsf.parent.mkdir(parents=True, exist_ok=True)
os.chmod(DSFTOOL, 0o755)
r = subprocess.run([str(DSFTOOL), "--text2dsf", str(txt), str(dsf)], capture_output=True, text=True)
log(r.stdout.strip()[-200:], r.stderr.strip()[-300:])
if not dsf.exists():
    raise SystemExit("DSFTool did not write the DSF")

(PACK / "README.txt").write_text(
    "ClearScan Schleissheim - Testpaket (Unter-/Oberschleissheim, Schlosspark)\n\n"
    "Gebaeude aus LoD2 mit Fassaden und Daechern (Dachfarbe aus der Photogrammetrie),\n"
    "Einzelbaeume aus dem Oberflaechenmodell, Wahrzeichen als Photogrammetrie.\n"
    "Der Flugplatz EDNX selbst ist ausgespart - er kommt aus dem X-Plane-Gateway.\n\n"
    "Installation: Ordner nach X-Plane 12/Custom Scenery; in scenery_packs.ini oberhalb von\n"
    "simHeaven/X-World und anderen Overlays (X-Plane traegt neue Ordner oben ein).\n\n"
    "Quellen: Bayerische Vermessungsverwaltung - LoD2, DOM20, DGM1, DOP40, DOM-Mesh\n"
    "(www.geodaten.bayern.de, CC BY 4.0); Flugplatzgrenze: OpenStreetMap (ODbL).\n"
    "Jahreszeiten: Die Baeume wechseln automatisch (library.txt, EXPORT_SEASON). Falls X-Plane\n"
    "die Baeume nicht findet oder nur eine Jahreszeit gewuenscht ist: library.txt durch eine Datei\n"
    "aus 'library_feste_jahreszeit' ersetzen (umbenennen in library.txt).\n\n"
    "Erzeugt mit XP12 DEM Generator (Prototyp ClearScan). Nur fuer private Nutzung/Test.\n")
summary = {"buildings": len(normal_b), "landmarks": sum(b["landmark"] for b in buildings),
           "trees": n_trees, "species": species_count, "objects": len(placements), "exclusion_cells": len(excl), **stats,
           "seconds": round(time.time() - t0)}
(OUT / "stats.json").write_text(json.dumps(summary, indent=1))
log(summary)
