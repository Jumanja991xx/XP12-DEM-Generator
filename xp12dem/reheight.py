"""Improve the elevation of the terrain mesh you already use.

Instead of building a new mesh (which only works with the program that serves
its textures), this mode takes the base-mesh DSF that X-Plane currently uses
for a tile - an XPME base package, an AutoOrtho / Ortho4XP pack or the default
X-Plane 12 Global Scenery - and replaces only the vertex elevations (and the
vertex normals used for shading) with values from Sonny's LiDAR DTM.

Everything else stays: triangulation, terrain types, textures, water, roads,
forests and objects. Terrain definitions that live inside the source pack are
copied into the new pack with their texture paths pointing back to the
source pack, so XPME / AutoOrtho keep serving the photos.

Elevations are left unchanged
* on water patches (lake and sea surfaces stay flat),
* around runways (airport flattening done by X-Plane, XPME or Ortho4XP stays),
* towards tile edges whose neighbour is not processed (no steps at seams),
with smooth transitions in between.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import struct
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

import numpy as np

from .regions import Tile, dsf_relpath

LIB_PREFIXES = ("lib/",)
WATER_DEFS = {"terrain_water"}
NODATA = -32768.0
RUNWAY_KEEP_M = 120.0  # beyond the runway half width: keep source heights
RUNWAY_BLEND_M = 450.0  # then blend to LiDAR heights over this distance
EDGE_BLEND_DEG = 0.02  # feather towards unprocessed neighbour tiles (~2 km)
CHUNK = 200_000

Log = Callable[[str], None]


# ---------------------------------------------------------------------------
# Finding the active base mesh
# ---------------------------------------------------------------------------
@dataclass
class MeshSource:
    kind: str  # "pack" or "global"
    name: str  # pack folder name, or "X-Plane 12 Global Scenery"
    dsf: Path  # file to read
    read_root: Path  # where the pack's files can be read (.ter etc.)
    pack_root: Optional[Path] = None  # where X-Plane sees the pack (for texture paths)
    unreadable_above: list[str] = field(default_factory=list)  # empty packs with higher priority

    @property
    def label(self) -> str:
        return self.name if self.kind == "pack" else "X-Plane 12 Global Scenery (default)"


def pack_entries(custom_scenery: Path) -> list[tuple[str, str]]:
    """``(folder name, path as written)`` of enabled packs, in priority order.

    Paths are usually ``Custom Scenery/<name>/`` but X-Plane also accepts
    absolute paths (``D:/XPME/XPME_Europe/``), which tools like XPME use for
    packages stored outside the X-Plane folder.
    """
    ini = custom_scenery / "scenery_packs.ini"
    if not ini.is_file():
        return []
    entries = []
    for line in ini.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("SCENERY_PACK "):
            continue
        raw = line[len("SCENERY_PACK "):].strip()
        path = raw.replace("\\", "/").rstrip("/")
        if path.startswith("*") or not path:
            continue
        entries.append((path.rsplit("/", 1)[-1], raw))
    return entries


def enabled_packs(custom_scenery: Path) -> list[str]:
    """Pack folder names in scenery_packs.ini priority order (enabled only)."""
    return [name for name, _ in pack_entries(custom_scenery)]


def is_absolute_entry(raw: str) -> bool:
    raw = raw.replace("\\", "/")
    return raw.startswith("/") or bool(re.match(r"^[A-Za-z]:/", raw))


def resolve_pack(xplane: Path, raw: str) -> Path:
    """Folder of a scenery_packs.ini entry (relative entries are relative to X-Plane)."""
    raw = raw.replace("\\", "/").rstrip("/")
    return Path(raw) if is_absolute_entry(raw) else xplane / raw


def read_dsf_properties(path: Path, seven_zip: Optional[str] = None,
                        tmp_dir: Optional[Path] = None) -> dict[str, str]:
    """Read the PROP atom of a (possibly 7-Zip compressed) DSF."""
    data_path = ensure_uncompressed(path, seven_zip, tmp_dir) if tmp_dir else path
    with open(data_path, "rb") as handle:
        if handle.read(8) != b"XPLNEDSF":
            return {}
        handle.read(4)
        size = data_path.stat().st_size - 16
        while handle.tell() < size:
            header = handle.read(8)
            if len(header) < 8:
                break
            atom_id, atom_len = header[:4], struct.unpack("<I", header[4:])[0]
            if atom_id == b"DAEH":  # 'HEAD' stored little endian
                end = handle.tell() + atom_len - 8
                while handle.tell() < end:
                    sub = handle.read(8)
                    sub_id, sub_len = sub[:4], struct.unpack("<I", sub[4:])[0]
                    body = handle.read(sub_len - 8)
                    if sub_id == b"PORP":
                        parts = body.split(b"\0")
                        return {parts[i].decode("latin-1"): parts[i + 1].decode("latin-1")
                                for i in range(0, len(parts) - 1, 2)}
                return {}
            handle.seek(atom_len - 8, os.SEEK_CUR)
    return {}


def read_dsf_terrain_defs(path: Path) -> list[str]:
    """TERRAIN_DEF names of an uncompressed DSF (DEFN atom, TERT string table)."""
    with open(path, "rb") as handle:
        if handle.read(8) != b"XPLNEDSF":
            return []
        handle.read(4)
        size = path.stat().st_size - 16
        while handle.tell() < size:
            header = handle.read(8)
            if len(header) < 8:
                break
            atom_id, atom_len = header[:4], struct.unpack("<I", header[4:])[0]
            if atom_id == b"NFED":  # 'DEFN' stored little endian
                end = handle.tell() + atom_len - 8
                while handle.tell() < end:
                    sub = handle.read(8)
                    if len(sub) < 8:
                        break
                    sub_id, sub_len = sub[:4], struct.unpack("<I", sub[4:])[0]
                    body = handle.read(sub_len - 8)
                    if sub_id == b"TRET":
                        return [b.decode("utf-8", "replace") for b in body.split(b"\0") if b]
                return []
            if atom_len < 8:
                break
            handle.seek(atom_len - 8, os.SEEK_CUR)
    return []


def missing_terrain(pack_dir: Path, dsf_path: Path) -> list[str]:
    """Pack-local terrain definitions a DSF needs but the pack does not contain."""
    try:
        defs = read_dsf_terrain_defs(dsf_path)
    except OSError:
        return []
    return [d for d in defs
            if not d.startswith(LIB_PREFIXES) and d.lower() not in WATER_DEFS
            and not (pack_dir / d).is_file()]


def read_text_retry(path: Path, attempts: int = 5, delay: float = 1.0) -> Optional[str]:
    """Read a file that a virtual drive (XPME, AutoOrtho) may only provide on access."""
    import time

    for attempt in range(attempts):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                return handle.read()
        except OSError:
            if attempt + 1 < attempts:
                time.sleep(delay)
    return None


class SourceUnavailable(RuntimeError):
    """A pack above the found mesh exists but cannot be read (virtual mount not running?)."""

    def __init__(self, tile: Tile, packs: list[str], checked: Optional[list[Path]] = None):
        self.packs = packs
        where = ""
        if checked:
            where = " Checked: " + "; ".join(str(p) for p in checked[:6]) + \
                    (" ..." if len(checked) > 6 else "") + "."
        super().__init__(
            f"Tile {tile}: scenery pack(s) {', '.join(packs)} are listed in scenery_packs.ini but "
            "their folders are empty or missing. If they belong to XPME or AutoOrtho, start that "
            f"program first so its scenery can be read, then build again.{where}")


def _is_improved_pack(folder: Path) -> bool:
    """Packs made by this mode are never used as a source (no chaining)."""
    if folder.name.lower().startswith("z_dem_"):
        return True
    manifest = folder / "xp12dem_pack.json"
    try:
        return '"method": "improve"' in manifest.read_text("utf-8")
    except OSError:
        return False


def _empty_or_missing(folder: Path) -> bool:
    try:
        return not any(folder.iterdir())
    except OSError:
        return True


def find_mesh_source(tile: Tile, xplane: Path, *, skip: Iterable[str] = (),
                     extra_roots: Iterable[Path] = (), seven_zip: Optional[str] = None,
                     tmp_dir: Optional[Path] = None, strict: bool = True) -> MeshSource:
    """Return the base mesh X-Plane uses for ``tile``.

    ``extra_roots`` are folders where packs can be read when their Custom
    Scenery entry is a virtual mount that is not running (e.g. AutoOrtho's
    ``z_autoortho/scenery``). With ``strict``, packs that are listed but
    unreadable (empty folder = mount not running) raise
    :class:`SourceUnavailable` instead of silently falling through to a
    lower-priority mesh.
    """
    custom = xplane / "Custom Scenery"
    rel = dsf_relpath(*tile)
    skip = {s.lower() for s in skip}
    unreadable: list[str] = []
    checked: list[Path] = []
    extra_roots = [Path(r) for r in extra_roots]
    for name, raw in pack_entries(custom):
        pack_root = resolve_pack(xplane, raw)
        if name.lower() in skip or _is_improved_pack(pack_root):
            continue
        roots = [pack_root] + [r / name for r in extra_roots]
        if all(_empty_or_missing(root) for root in roots):
            unreadable.append(name)
            checked.extend(roots)
            continue
        for root in roots:
            dsf = root / rel
            if dsf.is_file():
                props = read_dsf_properties(dsf, seven_zip, tmp_dir)
                if props.get("sim/overlay", "0").strip() == "1":
                    break  # overlay only (roads/objects), not a base mesh
                return MeshSource("pack", name, dsf, root, pack_root, list(unreadable))
    if unreadable and strict:
        raise SourceUnavailable(tile, unreadable, checked)
    global_root = xplane / "Global Scenery" / "X-Plane 12 Global Scenery"
    return MeshSource("global", "X-Plane 12 Global Scenery", global_root / rel, global_root,
                      global_root, list(unreadable))


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
def ensure_uncompressed(path: Path, seven_zip: Optional[str], tmp_dir: Path) -> Path:
    with open(path, "rb") as handle:
        magic = handle.read(2)
    if magic != b"7z":
        return path
    if not seven_zip:
        raise RuntimeError(f"{path.name} is 7-Zip compressed and 7-Zip is not available")
    out_dir = tmp_dir / "unz"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run([seven_zip, "e", "-y", f"-o{out_dir}", str(path)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    files = [p for p in out_dir.iterdir() if p.is_file()]
    if len(files) != 1:
        raise RuntimeError(f"Unexpected content when unpacking {path}")
    target = tmp_dir / (path.stem + ".unpacked.dsf")
    os.replace(files[0], target)
    shutil.rmtree(out_dir, ignore_errors=True)
    return target


def run_dsftool(dsftool: str, *args: str) -> None:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run([dsftool, *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, errors="replace", creationflags=flags)
    if proc.returncode != 0:
        raise RuntimeError(f"DSFTool {args[0]} failed: {proc.stdout[-500:]}")


# ---------------------------------------------------------------------------
# Height field
# ---------------------------------------------------------------------------
@dataclass
class Runway:
    lat1: float
    lon1: float
    lat2: float
    lon2: float
    half_width: float


@dataclass
class HeightField:
    """LiDAR heights for one tile plus the masks that decide where to use them."""

    lat: int
    lon: int
    grid: np.ndarray  # (n, n) float32, north row first (hgt order), NaN = nodata
    runways: list[Runway] = field(default_factory=list)
    feather_edges: dict[str, bool] = field(default_factory=dict)  # n/s/e/w -> feather?

    @classmethod
    def from_hgt(cls, path: Path, lat: int, lon: int, **kwargs) -> "HeightField":
        size = int(round(math.sqrt(path.stat().st_size / 2)))
        grid = np.fromfile(path, ">i2").reshape(size, size).astype(np.float32)
        grid[grid <= -32000] = np.nan
        return cls(lat, lon, grid, **kwargs)

    @property
    def n(self) -> int:
        return self.grid.shape[0]

    def _rc(self, lon: np.ndarray, lat: np.ndarray):
        col = (lon - self.lon) * (self.n - 1)
        row = (self.lat + 1 - lat) * (self.n - 1)
        return np.clip(row, 0, self.n - 1), np.clip(col, 0, self.n - 1)

    def sample(self, lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
        row, col = self._rc(lon, lat)
        r0 = np.minimum(np.floor(row).astype(np.int64), self.n - 2)
        c0 = np.minimum(np.floor(col).astype(np.int64), self.n - 2)
        fr, fc = row - r0, col - c0
        g = self.grid
        top = g[r0, c0] * (1 - fc) + g[r0, c0 + 1] * fc
        bottom = g[r0 + 1, c0] * (1 - fc) + g[r0 + 1, c0 + 1] * fc
        return top * (1 - fr) + bottom * fr

    def normals(self, lon: np.ndarray, lat: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(east, south) components of the unit surface normal."""
        step = 2.0 / (self.n - 1) if self.n > 2000 else 1.0 / (self.n - 1)
        mid_lat = np.radians(lat)
        dx = 6371000.0 * np.radians(step) * np.cos(mid_lat)
        dy = 6371000.0 * np.radians(step)
        dzdx = (self.sample(lon + step, lat) - self.sample(lon - step, lat)) / (2 * dx)
        dzdy = (self.sample(lon, lat + step) - self.sample(lon, lat - step)) / (2 * dy)
        norm = np.sqrt(dzdx ** 2 + dzdy ** 2 + 1.0)
        return -dzdx / norm, dzdy / norm  # east, south

    def weight(self, lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
        """0 = keep source height, 1 = use LiDAR height."""
        w = np.ones_like(lon, dtype=np.float64)
        for rwy in self.runways:
            d = _distance_to_segment_m(lon, lat, rwy)
            w = np.minimum(w, _smoothstep((d - rwy.half_width - RUNWAY_KEEP_M) / RUNWAY_BLEND_M))
        edge = {
            "w": lon - self.lon, "e": self.lon + 1 - lon,
            "s": lat - self.lat, "n": self.lat + 1 - lat,
        }
        for side, dist in edge.items():
            if self.feather_edges.get(side, True):
                w = np.minimum(w, _smoothstep(dist / EDGE_BLEND_DEG))
        return w


def _smoothstep(x):
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3 - 2 * x)


def _distance_to_segment_m(lon, lat, rwy: Runway) -> np.ndarray:
    k = math.cos(math.radians((rwy.lat1 + rwy.lat2) / 2)) * 111_320.0
    ax, ay = rwy.lon1 * k, rwy.lat1 * 110_574.0
    bx, by = rwy.lon2 * k, rwy.lat2 * 110_574.0
    px, py = lon * k, lat * 110_574.0
    vx, vy = bx - ax, by - ay
    length2 = vx * vx + vy * vy or 1.0
    t = np.clip(((px - ax) * vx + (py - ay) * vy) / length2, 0.0, 1.0)
    return np.hypot(px - (ax + t * vx), py - (ay + t * vy))


def load_runways(apt_files: Iterable[Path], tiles: Iterable[Tile], margin: float = 0.05) -> dict[Tile, list[Runway]]:
    """Land runways (apt.dat row code 100) touching each tile."""
    tiles = list(tiles)
    result: dict[Tile, list[Runway]] = {t: [] for t in tiles}
    if not tiles:
        return result
    lat_min = min(t[0] for t in tiles) - margin
    lat_max = max(t[0] for t in tiles) + 1 + margin
    lon_min = min(t[1] for t in tiles) - margin
    lon_max = max(t[1] for t in tiles) + 1 + margin
    seen = set()
    for apt in apt_files:
        try:
            handle = open(apt, "r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if not line.startswith("100 "):
                    continue
                parts = line.split()
                try:
                    width = float(parts[1])
                    lat1, lon1 = float(parts[9]), float(parts[10])
                    lat2, lon2 = float(parts[18]), float(parts[19])
                except (IndexError, ValueError):
                    continue
                if not (lat_min <= lat1 <= lat_max and lon_min <= lon1 <= lon_max):
                    continue
                key = (round(lat1, 4), round(lon1, 4), round(lat2, 4), round(lon2, 4))
                if key in seen:
                    continue
                seen.add(key)
                rwy = Runway(lat1, lon1, lat2, lon2, width / 2)
                for tile in tiles:
                    t_lat, t_lon = tile
                    if (t_lat - margin <= min(lat1, lat2) <= t_lat + 1 + margin
                            or t_lat - margin <= max(lat1, lat2) <= t_lat + 1 + margin) and \
                            (t_lon - margin <= min(lon1, lon2) <= t_lon + 1 + margin
                             or t_lon - margin <= max(lon1, lon2) <= t_lon + 1 + margin):
                        result[tile].append(rwy)
    return result


def apt_files_for(xplane: Path, packs: Iterable[str] = ()) -> list[Path]:
    files = [xplane / "Global Scenery" / "Global Airports" / "Earth nav data" / "apt.dat"]
    for _, raw in pack_entries(xplane / "Custom Scenery"):
        files.append(resolve_pack(xplane, raw) / "Earth nav data" / "apt.dat")
    return [f for f in files if f.is_file()]


# ---------------------------------------------------------------------------
# DSF text rewriting
# ---------------------------------------------------------------------------
_PATH_EXT = re.compile(r"\.(dds|png|bmp|ter|pol|dcl|for|fac|obj|net|lin|str|agp)$", re.IGNORECASE)


def relative_ref(target: Path, from_dir: Path) -> str:
    """Path from ``from_dir`` to ``target`` for a .ter file (forward slashes).

    X-Plane resolves .ter references against the .ter file's folder, also
    across folders outside X-Plane (XPME keeps its packages in Documents).
    Different drives cannot be reached relatively: then the absolute path is used.
    """
    try:
        return os.path.relpath(str(target), str(from_dir)).replace("\\", "/")
    except ValueError:  # different drive on Windows
        return Path(target).as_posix()


def rewrite_ter(text: str, ter_rel: str, source_pack: str, source_dir: Optional[Path] = None,
                install_dir: Optional[Path] = None) -> str:
    """Point relative file references of a .ter at the source pack.

    The copy lives at ``<install_dir>/<ter_rel>``; references are resolved
    against the original location ``<source_dir>/<ter_rel>``. Without the two
    folders both packs are assumed to sit side by side.
    """
    ter_dir = Path(ter_rel).parent
    up = "../" * (len(ter_dir.parts) + 1)
    out = []
    for line in text.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and _PATH_EXT.search(parts[1].strip()) and not parts[1].startswith(LIB_PREFIXES):
            ref = parts[1].strip().replace("\\", "/")
            resolved = os.path.normpath(os.path.join(str(ter_dir), ref)).replace("\\", "/")
            if not resolved.startswith(".."):
                if source_dir is not None and install_dir is not None:
                    line = f"{parts[0]} {relative_ref(Path(source_dir) / resolved, Path(install_dir) / ter_dir)}"
                else:
                    line = f"{parts[0]} {up}{source_pack}/{resolved}"
        out.append(line)
    return "\n".join(out) + "\n"


def retarget_ter(text: str, old_ter: Path, new_ter: Path) -> str:
    """Keep the references of a .ter valid when the file moves to ``new_ter``."""
    out = []
    for line in text.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and _PATH_EXT.search(parts[1].strip()) and not parts[1].startswith(LIB_PREFIXES):
            ref = parts[1].strip().replace("\\", "/")
            target = Path(ref) if Path(ref).is_absolute() or re.match(r"^[A-Za-z]:/", ref) else \
                Path(os.path.normpath(os.path.join(str(old_ter.parent), ref)))
            line = f"{parts[0]} {relative_ref(target, new_ter.parent)}"
        out.append(line)
    return "\n".join(out) + "\n"


def _is_water_ter(path: Path) -> bool:
    try:
        return any(line.strip() == "WET" for line in path.read_text("utf-8", "replace").splitlines())
    except OSError:
        return False


@dataclass
class ReheightStats:
    vertices: int = 0
    changed: int = 0
    max_delta: float = 0.0
    mean_abs_delta: float = 0.0
    copied_terrains: int = 0
    rasters: int = 0
    heights_range: Optional[tuple[float, float]] = None  # storable heights of the new DSF
    clamped: int = 0  # vertices limited to the original height range (fallback mode)
    big: int = 0  # vertices moved by more than 10 m
    spots: list = field(default_factory=list)  # (abs change, lon, lat, old, new) candidates

    def highlights(self, count: int = 3, min_distance_deg: float = 0.03) -> list[dict]:
        """The places with the biggest height change, a few km apart - where to look."""
        chosen: list[tuple] = []
        for spot in sorted(self.spots, key=lambda s: -s[0]):
            if all(abs(spot[1] - c[1]) > min_distance_deg or abs(spot[2] - c[2]) > min_distance_deg
                   for c in chosen):
                chosen.append(spot)
            if len(chosen) == count:
                break
        return [{"lat": round(c[2], 4), "lon": round(c[1], 4), "old": round(c[3]), "new": round(c[4])}
                for c in chosen]


def heights_range(step: float, offset: float) -> tuple[float, float]:
    """Elevations a DSF can store according to its ``HEIGHTS step offset`` line."""
    return offset, offset + step * 65535.0


def tight_heights(need_lo: float, need_hi: float) -> tuple[float, float]:
    """``(step, offset)`` of a ``HEIGHTS`` line covering exactly the heights of a tile.

    DSFTool stores elevations as 16-bit steps within this range and refuses
    ("could not sink vertex") anything outside it - e.g. LiDAR heights below
    sea level in reclaimed land (Po delta, Netherlands) when the source mesh
    never went below 0 m. A tight range also gives the finest height steps.
    """
    lo, hi = math.floor(need_lo) - 2.0, math.ceil(need_hi) + 2.0
    return (hi - lo) / 65535.0, lo


def reheight_dsf_text(src_txt: Path, dst_txt: Path, hf: HeightField, source: MeshSource,
                      out_pack: Path, work: Path, log: Log = print,
                      keep_height_range: bool = False, install_dir: Optional[Path] = None) -> ReheightStats:
    """Rewrite a DSFTool text file with LiDAR elevations.

    ``keep_height_range`` is the fallback mode: instead of widening the DSF's
    storable height range, new heights are limited to the original range.
    """
    stats = ReheightStats()
    defs: list[str] = []
    water_ids: set[int] = set()

    # Pass 1: terrain definitions and vertices on water patches.
    water_keys: set[tuple[int, int]] = set()
    current_water = False
    orig_lo, orig_hi = math.inf, -math.inf
    other_lo, other_hi = math.inf, -math.inf  # "use raster" markers, objects: must stay storable
    with open(src_txt, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.startswith("PATCH_VERTEX"):
                elev = float(line.split(None, 4)[3])
                if elev > NODATA + 1:
                    orig_lo, orig_hi = min(orig_lo, elev), max(orig_hi, elev)
                else:
                    other_lo, other_hi = min(other_lo, elev), max(other_hi, elev)
                if current_water:
                    p = line.split(None, 3)
                    water_keys.add((round(float(p[1]) * 1e7), round(float(p[2]) * 1e7)))
                continue
            if line.startswith(("OBJECT_MSL", "OBJECT_AGL")):
                parts = line.split()
                if len(parts) >= 6:
                    elev = float(parts[5])
                    other_lo, other_hi = min(other_lo, elev), max(other_hi, elev)
                continue
            if line.startswith("TERRAIN_DEF"):
                name = line.split(None, 1)[1].strip()
                if name.lower() in WATER_DEFS or (
                        not name.startswith(LIB_PREFIXES) and _is_water_ter(source.read_root / name)):
                    water_ids.add(len(defs))
                defs.append(name)
            elif line.startswith("BEGIN_PATCH"):
                current_water = int(line.split()[1]) in water_ids

    # Copy local terrain definitions so their textures point back to the source pack.
    if source.kind == "pack":
        unreadable = []
        for name in defs:
            if name.startswith(LIB_PREFIXES) or name.lower() in WATER_DEFS:
                continue
            text = read_text_retry(source.read_root / name)
            if text is None:
                unreadable.append(name)
                continue
            dst = out_pack / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(rewrite_ter(text, name, source.name, source.pack_root or source.read_root,
                                       install_dir), encoding="utf-8")
            stats.copied_terrains += 1
        if unreadable:
            raise RuntimeError(
                f"{source.name} did not provide {len(unreadable)} terrain file(s), e.g. "
                f"{unreadable[0]} - make sure XPME / AutoOrtho is running and has finished "
                "loading, then retry this tile. (The tile was not installed, so X-Plane keeps "
                "the original terrain.)")

    # Height range the new DSF must be able to store.
    finite = hf.grid[np.isfinite(hf.grid)]
    if orig_lo > orig_hi:  # no vertex heights at all (raster-only mesh)
        orig_lo, orig_hi = 0.0, 0.0
    need_lo = min(orig_lo, float(finite.min())) if finite.size else orig_lo
    need_hi = max(orig_hi, float(finite.max())) if finite.size else orig_hi
    need_lo, need_hi = min(need_lo, other_lo), max(need_hi, other_hi)
    heights_line = "HEIGHTS {:.9f} {:.1f}\n".format(*tight_heights(need_lo, need_hi))

    # Pass 2: write the new text file.
    deltas: list[float] = []
    saw_heights = False
    current_water = False
    buffer: list[str] = []

    def flush() -> None:
        if not buffer:
            return
        rows = [line.split() for line in buffer]
        width = max(len(r) for r in rows)
        same = all(len(r) == width for r in rows)
        if not same:  # unusual: mixed widths, process one by one
            for r in rows:
                _write_vertices([r], out)
        else:
            _write_vertices(rows, out)
        buffer.clear()

    def _write_vertices(rows, sink) -> None:
        arr = np.array([[float(v) for v in r[1:]] for r in rows], dtype=np.float64)
        lon, lat, elev = arr[:, 0], arr[:, 1], arr[:, 2]
        keep = elev <= NODATA + 1  # "use raster" marker
        new = hf.sample(lon, lat)
        w = hf.weight(lon, lat)
        valid = ~np.isnan(new) & ~keep
        target = np.where(valid, elev + w * (np.nan_to_num(new) - elev), elev)
        stats.vertices += len(rows)
        changed = valid & (np.abs(target - elev) > 0.05)
        stats.changed += int(changed.sum())
        if changed.any():
            d = np.abs(target - elev)[changed]
            stats.max_delta = max(stats.max_delta, float(d.max()))
            stats.big += int((d > 10).sum())
            deltas.append(float(d.sum()))
            where = np.nonzero(changed)[0]
            for i in where[np.argsort(-d)[:10]]:
                stats.spots.append((abs(float(target[i] - elev[i])), float(lon[i]), float(lat[i]),
                                    float(elev[i]), float(target[i])))
            if len(stats.spots) > 400:
                stats.spots = sorted(stats.spots, key=lambda s: -s[0])[:200]
        if keep_height_range:
            limited = np.clip(target, orig_lo, orig_hi)
            stats.clamped += int((valid & (limited != target)).sum())
            target = np.where(valid, limited, target)
        arr[:, 2] = target
        if arr.shape[1] >= 5:
            plausible = (np.abs(arr[:, 3]) <= 1.0) & (np.abs(arr[:, 4]) <= 1.0)
            ne, ns = hf.normals(lon, lat)
            upd = valid & plausible & ~np.isnan(ne) & ~np.isnan(ns)
            arr[:, 3] = np.where(upd, arr[:, 3] + w * (ne - arr[:, 3]), arr[:, 3])
            arr[:, 4] = np.where(upd, arr[:, 4] + w * (ns - arr[:, 4]), arr[:, 4])
        for row in arr:
            sink.write("PATCH_VERTEX " + " ".join(f"{v:.9f}" for v in row) + "\n")

    with open(src_txt, "r", encoding="utf-8", errors="replace") as handle, \
            open(dst_txt, "w", encoding="utf-8", newline="\n") as out:
        for line in handle:
            if line.startswith("PATCH_VERTEX"):
                if current_water:
                    flush()
                    out.write(line)
                    stats.vertices += 1
                    continue
                p = line.split(None, 3)
                if (round(float(p[1]) * 1e7), round(float(p[2]) * 1e7)) in water_keys:
                    flush()
                    out.write(line)  # shoreline vertex shared with water: keep
                    stats.vertices += 1
                    continue
                buffer.append(line)
                if len(buffer) >= CHUNK:
                    flush()
                continue
            flush()
            if not keep_height_range:
                if line.startswith("HEIGHTS"):
                    continue  # replaced by our own line (written before the first PROPERTY)
                if line.startswith("PROPERTY") and not saw_heights:
                    saw_heights = True
                    out.write(heights_line)
                    step, offset = tight_heights(need_lo, need_hi)
                    stats.heights_range = (offset, offset + step * 65535.0)
            if line.startswith("BEGIN_PATCH"):
                current_water = int(line.split()[1]) in water_ids
            elif line.startswith("RASTER_DATA"):
                line = _rewrite_raster(line, hf, work, stats, log)
            out.write(line)
        flush()
    if stats.changed:
        stats.mean_abs_delta = sum(deltas) / stats.changed
    return stats


_RASTER_RE = re.compile(r"RASTER_DATA version=(\d+) bpp=(\d+) flags=(\d+) width=(\d+) "
                        r"height=(\d+) scale=([-\d.eE+]+) offset=([-\d.eE+]+) (.+)$")


def _rewrite_raster(line: str, hf: HeightField, work: Path, stats: ReheightStats, log: Log) -> str:
    """Replace the first raster (XP12 'elevation') with LiDAR heights."""
    match = _RASTER_RE.match(line.strip())
    if not match or stats.rasters:  # only the first raster is the elevation raster
        stats.rasters += 1 if match else 0
        return line
    stats.rasters += 1
    version, bpp, flags, width, height = (int(match[i]) for i in range(1, 6))
    scale, offset, path = float(match[6]), float(match[7]), Path(match[8])
    if bpp == 2:
        dtype = np.dtype("<i2") if flags & 1 else np.dtype("<u2")
    elif bpp == 4 and flags & 2:
        dtype = np.dtype("<f4")
    elif bpp == 4:
        dtype = np.dtype("<i4") if flags & 1 else np.dtype("<u4")
    elif bpp == 1:
        dtype = np.dtype("i1") if flags & 1 else np.dtype("u1")
    else:
        log(f"   raster with bpp={bpp} flags={flags} left unchanged")
        return line
    try:
        raw = np.fromfile(path, dtype).reshape(height, width).astype(np.float64)
    except (OSError, ValueError):
        log("   could not read elevation raster - left unchanged")
        return line
    old = raw * scale + offset
    # DSF rasters start at the south-west corner, rows go northwards.
    lats = hf.lat + np.arange(height) / max(height - 1, 1)
    lons = hf.lon + np.arange(width) / max(width - 1, 1)
    glon, glat = np.meshgrid(lons, lats)
    new = hf.sample(glon.ravel(), glat.ravel()).reshape(height, width)
    w = hf.weight(glon.ravel(), glat.ravel()).reshape(height, width)
    merged = np.where(np.isnan(new), old, old + w * (np.nan_to_num(new) - old))
    encoded = np.round((merged - offset) / (scale or 1.0))
    info = np.iinfo(dtype) if dtype.kind in "iu" else None
    if info is not None:
        encoded = np.clip(encoded, info.min, info.max)
    out_path = work / f"raster_elevation_{hf.lat}_{hf.lon}.raw"
    encoded.astype(dtype).tofile(out_path)
    return (f"RASTER_DATA version={version} bpp={bpp} flags={flags} width={width} height={height} "
            f"scale={scale:.6f} offset={offset:.6f} {out_path}\n")


# ---------------------------------------------------------------------------
# One tile end to end
# ---------------------------------------------------------------------------
def _first_error(text: str) -> str:
    for line in text.splitlines():
        if "ERROR" in line:
            return line.strip().strip('!"').replace("\\n", "").strip()[:120]
    return text.strip().splitlines()[-1][:120] if text.strip() else "unknown error"


def reheight_tile(tile: Tile, source: MeshSource, hf: HeightField, out_pack: Path,
                  work: Path, dsftool: str, seven_zip: Optional[str],
                  log: Log = print, install_dir: Optional[Path] = None) -> ReheightStats:
    work.mkdir(parents=True, exist_ok=True)
    if not source.dsf.is_file():
        raise FileNotFoundError(f"Source DSF not found: {source.dsf}")
    log(f"   source mesh: {source.label}")
    plain = ensure_uncompressed(source.dsf, seven_zip, work)
    src_txt = work / "source.txt"
    dst_txt = work / "lidar.txt"
    log("   converting DSF to text")
    run_dsftool(dsftool, "--dsf2text", str(plain), str(src_txt))
    log("   applying LiDAR elevations")
    stats = reheight_dsf_text(src_txt, dst_txt, hf, source, out_pack, work, log, install_dir=install_dir)
    if stats.heights_range:
        lo, hi = stats.heights_range
        log(f"   storable height range {lo:.0f} .. {hi:.0f} m")
    target = out_pack / dsf_relpath(*tile)
    target.parent.mkdir(parents=True, exist_ok=True)
    log("   writing new DSF")
    try:
        run_dsftool(dsftool, "--text2dsf", str(dst_txt), str(target))
    except RuntimeError as exc:
        # Fallback: keep the source's height range and limit new heights to it.
        log(f"   DSFTool refused the new heights ({_first_error(str(exc))}) - "
            "retrying with heights limited to the original range")
        stats = reheight_dsf_text(src_txt, dst_txt, hf, source, out_pack, work, log,
                                  keep_height_range=True, install_dir=install_dir)
        log(f"   {stats.clamped} vertices limited")
        log("   writing new DSF (fallback)")
        run_dsftool(dsftool, "--text2dsf", str(dst_txt), str(target))
    missing = missing_terrain(out_pack, target)
    if missing:  # never install a tile X-Plane would refuse
        raise RuntimeError(f"terrain file {missing[0]} missing from the new tile "
                           f"({len(missing)} in total) - tile not installed")
    for tmp in (src_txt, dst_txt):
        tmp.unlink(missing_ok=True)
    if plain != source.dsf:
        plain.unlink(missing_ok=True)
    for raw in work.glob("*.raw"):  # rasters exported by DSFTool and our rewrites
        raw.unlink(missing_ok=True)
    return stats


def platform_tools(o4x_root: Path) -> tuple[str, Optional[str]]:
    """Paths of DSFTool and 7-Zip shipped with the vendored Ortho4XP."""
    if sys.platform.startswith("win"):
        utils = o4x_root / "Utils" / "win"
        return str(utils / "DSFTool.exe"), str(utils / "7z.exe")
    sub = "mac" if sys.platform == "darwin" else "lin"
    dsftool = o4x_root / "Utils" / sub / "DSFTool"
    try:
        dsftool.chmod(dsftool.stat().st_mode | 0o111)
    except OSError:
        pass
    return str(dsftool), shutil.which("7z") or shutil.which("7za") or shutil.which("7zz")
