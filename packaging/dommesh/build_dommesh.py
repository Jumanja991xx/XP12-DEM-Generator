"""DOM-Mesh (Bayern, I3S/SLPK) -> X-Plane-12-Overlay "Schleißheim + EDNX" (Testpaket).

* Zwei Detailstufen aus dem Netz: nah (Knoten <= 200 m halbe Kantenlänge) bis
  NEAR_M Entfernung, fern (<= 400 m) darüber.
* Boden-Dreiecke fliegen raus (Höhe über DGM1 < MIN_ABOVE): den Boden zeichnet
  X-Plane selbst (Ortho/XPME + unser LiDAR-Netz). Übrig bleiben Gebäude, Bäume,
  Hallen, abgestellte Flugzeuge.
* Jeder Knoten wird ein OBJ (eigene Textur), lokal in Metern um seinen
  Mittelpunkt, nach geografisch Nord ausgerichtet (UTM-Gitternord weicht hier
  um ~1,9 Grad ab).
* DSF: Platzierung + Ausschlusszonen für Standard-Gebäude/Wald im Gebiet.

Aufruf (aus dem Projektordner): python packaging/dommesh/build_dommesh.py <Ausgabeordner>
Lädt alles selbst: Netz-Teile per HTTP-Range aus den SLPK-Archiven, DGM1-Kacheln,
Projektgebiete (KML). Braucht DSFTool (Ortho4XP/Utils/lin).
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
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image
from pyproj import Transformer
from shapely.geometry import Polygon, box
from shapely import contains_xy

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
OUT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "dommesh_out")
os.environ.setdefault("DOMMESH_WORK", os.path.join(OUT, "work"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import requests  # noqa: E402
import tifffile  # noqa: E402
from slpk import Slpk  # noqa: E402
from xp12dem.imagery import write_dds  # noqa: E402

HERE = os.environ["DOMMESH_WORK"]
PACK = os.path.join(OUT, "DOMMesh_Schleissheim_EDNX")
LON0, LAT0, LON1, LAT1 = 11.535, 48.228, 11.605, 48.300
MIN_ABOVE = 2.5   # m über Gelände: darunter = Boden -> weg
TUCK = 0.6        # Punkte bis so hoch über Grund werden 0,3 m unter Grund gezogen (keine Lücke)
NEAR_M = 1800
FAR_M = 30000
# Kern (fein + fern) um EDNX und Schloss Schleißheim, außen nur die gröbere Stufe
CORE_LONLAT, CORE_R = (11.5637, 48.2442), 1300.0
LEVELS = [("n", 200, 0, NEAR_M, 1.0, "core"),       # name, max_half, lod, tex scale, Bereich
          ("f", 400, NEAR_M, FAR_M, 0.5, "core"),
          ("a", 400, 0, FAR_M, 0.75, "rest")]
ARCHIVES = ["124023_0", "124028_0"]
DSFTOOL = os.path.join(REPO, "third_party", "Ortho4XP", "Utils", "lin", "DSFTool")
DGM_E, DGM_N = range(688, 694), range(5344, 5353)  # 1-km-Kacheln (UTM32) für das Gebiet

to_utm = Transformer.from_crs(4326, 25832, always_xy=True)
to_ll = Transformer.from_crs(25832, 4326, always_xy=True)


def load_dgm():
    """DGM1-Mosaik, 1 m, Zeile 0 = Nordrand (5353000), Spalte 0 = 688000."""
    folder = os.path.join(HERE, "dgm")
    os.makedirs(folder, exist_ok=True)

    def one(xy):
        x, y = xy
        path = os.path.join(folder, f"{x}_{y}.tif")
        for attempt in range(6):
            if os.path.exists(path) and os.path.getsize(path) > 100000:
                return
            host = "download1" if attempt % 2 == 0 else "download2"
            r = requests.get(f"https://{host}.bayernwolke.de/a/dgm/dgm1/{x}_{y}.tif", timeout=120)
            if r.status_code == 200:
                open(path, "wb").write(r.content)
            time.sleep(attempt)
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(one, [(x, y) for x in DGM_E for y in DGM_N]))
    rows = [np.hstack([tifffile.imread(os.path.join(folder, f"{x}_{y}.tif")) for x in DGM_E])
            for y in reversed(DGM_N)]
    return np.vstack(rows).astype(np.float32)


dgm = load_dgm()


def ground(e, n):
    col = np.clip(e - 688000.0, 0, dgm.shape[1] - 1.001)
    row = np.clip(5353000.0 - n, 0, dgm.shape[0] - 1.001)
    c0, r0 = col.astype(int), row.astype(int)
    fc, fr = col - c0, row - r0
    return ((dgm[r0, c0] * (1 - fc) + dgm[r0, c0 + 1] * fc) * (1 - fr)
            + (dgm[r0 + 1, c0] * (1 - fc) + dgm[r0 + 1, c0 + 1] * fc) * fr)


def los_polygons():
    s = requests.get("https://geodaten.bayern.de/odd/m/3/daten/DOMMesh/DOM_Mesh_projektgebiete_2026.kml",
                     timeout=120).text
    out = {}
    for pm in re.findall(r"<Placemark[^>]*>(.*?)</Placemark>", s, re.S):
        name = re.search(r"<name>([^<]*)", pm).group(1)
        if name in ARCHIVES:
            c = re.findall(r"<coordinates>(.*?)</coordinates>", pm, re.S)[0]
            pts = [tuple(map(float, p.split(",")[:2])) for p in c.split()]
            out[name] = Polygon([to_utm.transform(*p) for p in pts])
    return out


def write_obj(path, tex, verts, idx, lod):
    with open(path, "w") as f:
        f.write(f"I\n800\nOBJ\n\nTEXTURE {tex}\nPOINT_COUNTS {len(verts)} 0 0 {len(idx)}\n\n")
        np.savetxt(f, verts, fmt="VT %.3f %.3f %.3f 0 1 0 %.5f %.5f")
        full = len(idx) // 10 * 10
        np.savetxt(f, idx[:full].reshape(-1, 10), fmt="IDX10 " + " ".join(["%d"] * 10))
        for i in idx[full:]:
            f.write(f"IDX {i}\n")
        f.write(f"\nATTR_LOD {lod[0]} {lod[1]}\nATTR_no_cull\nTRIS 0 {len(idx)}\n")


def convert(S, i, level, los, area):
    name, _, lod0, lod1, tex_scale, _ = level
    pos, uv, res = S.geometry(i)
    tri = pos.reshape(-1, 3, 3)
    cen = tri.mean(1)
    keep = contains_xy(los, cen[:, 0], cen[:, 1]) & contains_xy(area, cen[:, 0], cen[:, 1])
    g = ground(pos[:, 0], pos[:, 1])
    above = (pos[:, 2] - g).reshape(-1, 3)
    keep &= above.max(1) >= MIN_ABOVE
    if keep.sum() < 4:
        return None
    sel = np.repeat(keep, 3)
    p, t, gg = pos[sel], uv[sel].astype(np.float64), g[sel]
    h = p[:, 2] - gg
    p[:, 2] = np.where(h < TUCK, gg - 0.3, p[:, 2])
    # Ursprung: Mitte, Höhe = Gelände dort (X-Plane setzt das Objekt aufs Gelände)
    e0, n0 = (p[:, 0].min() + p[:, 0].max()) / 2, (p[:, 1].min() + p[:, 1].max()) / 2
    h0 = float(ground(np.array([e0]), np.array([n0]))[0])
    lon, lat = to_ll.transform(p[:, 0], p[:, 1])
    lon0, lat0 = to_ll.transform(e0, n0)
    m_lon = 111319.49 * math.cos(math.radians(lat0))
    x = (lon - lon0) * m_lon
    z = -(lat - lat0) * 111132.95
    y = p[:, 2] - h0
    v = np.column_stack([x, y, z, t[:, 0], 1 - t[:, 1]])
    key = np.round(v * [1000, 1000, 1000, 1e5, 1e5]).astype(np.int64)
    uniq, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    verts = v[first]
    idx = inv.reshape(-1).astype(np.int64)
    obj = f"{name}_{S.key}_{i}"
    # Textur: JPEG -> DXT1 mit Mipmaps
    img = Image.open(io.BytesIO(S.get(f"nodes/{res}/textures/0.jpg"))).convert("RGB")
    if tex_scale != 1.0:
        img = img.resize((max(4, int(img.size[0] * tex_scale)), max(4, int(img.size[1] * tex_scale))), Image.BOX)
    from pathlib import Path
    write_dds(np.asarray(img), Path(PACK, "objects", obj + ".dds"))
    write_obj(os.path.join(PACK, "objects", obj + ".obj"), obj + ".dds", verts, idx, (lod0, lod1))
    return obj, lon0, lat0, len(idx) // 3, len(verts)


def main():
    t0 = time.time()
    if os.path.exists(PACK):
        shutil.rmtree(PACK)
    os.makedirs(os.path.join(PACK, "objects"))
    os.makedirs(os.path.join(PACK, "Earth nav data", "+40+010"))
    los = los_polygons()
    x0, y0 = to_utm.transform(LON0, LAT0)
    x1, y1 = to_utm.transform(LON1, LAT1)
    corners = [to_utm.transform(lo, la) for lo, la in ((LON0, LAT0), (LON1, LAT0), (LON1, LAT1), (LON0, LAT1))]
    full = Polygon(corners)
    bx = full.bounds
    from shapely.geometry import Point
    core = Point(*to_utm.transform(*CORE_LONLAT)).buffer(CORE_R, 64)
    areas = {"core": core, "rest": full.difference(core)}
    placed, tris = [], {"n": 0, "f": 0, "a": 0}
    for key in ARCHIVES:
        S = Slpk(f"https://download1.bayernwolke.de/p/dom-mesh-slpk/{key}/DSM_Mesh.slpk", key)
        for level in LEVELS:
            area = areas[level[5]]
            nodes = S.select(*area.bounds, level[1])
            names = []
            for i in nodes:
                r = S.node(i)["mesh"]["geometry"]["resource"]
                names += [f"nodes/{r}/geometries/0.bin.gz", f"nodes/{r}/textures/0.jpg"]
            print(f"{key} {level[0]}: {len(nodes)} Knoten, lade ...", flush=True)
            S.fetch(names, 8)
            with ThreadPoolExecutor(6) as pool:
                for res in pool.map(lambda i: convert(S, i, level, los[key], area), nodes):
                    if res:
                        placed.append(res)
                        tris[level[0]] += res[3]
            print(f"  fertig ({time.time() - t0:.0f} s), Dreiecke bisher {tris}", flush=True)
    # DSF (DSFTool-Text)
    defs = [p[0] for p in placed]
    lines = ["I", "800", "DSF2TEXT", "",
             "PROPERTY sim/planet earth", "PROPERTY sim/overlay 1", "PROPERTY sim/require_object 1/0",
             "PROPERTY sim/creation_agent XP12-DEM-Generator DOM-Mesh test",
             f"PROPERTY sim/exclude_obj {LON0}/{LAT0}/{LON1}/{LAT1}",
             f"PROPERTY sim/exclude_fac {LON0}/{LAT0}/{LON1}/{LAT1}",
             f"PROPERTY sim/exclude_for {LON0}/{LAT0}/{LON1}/{LAT1}",
             "PROPERTY sim/west 11", "PROPERTY sim/east 12", "PROPERTY sim/south 48", "PROPERTY sim/north 49", ""]
    lines += [f"OBJECT_DEF objects/{d}.obj" for d in defs] + [""]
    lines += [f"OBJECT {k} {p[1]:.9f} {p[2]:.9f} 0.0" for k, p in enumerate(placed)]
    txt = os.path.join(HERE, "dsf.txt")
    os.chmod(DSFTOOL, 0o755)
    open(txt, "w").write("\n".join(lines) + "\n")
    out = os.path.join(PACK, "Earth nav data", "+40+010", "+48+011.dsf")
    r = subprocess.run([DSFTOOL, "--text2dsf", txt, out], capture_output=True, text=True)
    print(r.stdout[-300:], r.stderr[-300:])
    if not os.path.exists(out):
        raise SystemExit("DSFTool did not write the DSF")
    open(os.path.join(PACK, "README.txt"), "w").write(
        "DOM-Mesh Schleißheim + EDNX - Testpaket (Photogrammetrie-Gebäude und -Bäume)\n\n"
        "Quelle: Bayerische Vermessungsverwaltung, DOM-Mesh und DGM1, www.geodaten.bayern.de, CC BY 4.0\n"
        "Umgewandelt mit XP12 DEM Generator (Prototyp). Nur für den privaten Test.\n\n"
        "Installation: Ordner nach X-Plane 12/Custom Scenery kopieren. In scenery_packs.ini muss die Zeile\n"
        "ganz oben stehen (über simHeaven, über Flughafen-Szenerien) - X-Plane trägt neue Ordner\n"
        "automatisch oben ein.\n")
    json.dump({"objects": len(placed), "tris": tris, "seconds": round(time.time() - t0)},
              open(os.path.join(OUT, "stats.json"), "w"))
    print("Objekte", len(placed), "Dreiecke", tris, f"{time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
