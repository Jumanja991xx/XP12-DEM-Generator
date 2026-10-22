"""Headless Ortho4XP driver.

This module runs in its own process (one process per 1x1 degree tile) so that
Ortho4XP's module-level state, memory use and crashes stay isolated from the
GUI. The parent process talks to it through a JSON job file and reads progress
from stdout lines that start with ``@@``.

Ortho4XP computes its directory layout and the paths of its helper binaries at
import time, so the order of operations below matters: the path module is
patched first, then the rest of Ortho4XP is imported.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
import types
from pathlib import Path

PROGRESS_PREFIX = "@@"
OSM_FAILED = ("OpenStreetMap data could not be downloaded - all Overpass servers are busy "
              "or unreachable. Please try again in a few minutes")


def emit(kind: str, **payload) -> None:
    """Write a machine readable event line for the parent process."""
    payload["kind"] = kind
    print(PROGRESS_PREFIX + json.dumps(payload), flush=True)


def _install_tkinter_stub() -> None:
    """Ortho4XP imports tkinter for its GUI classes; we never use them.

    Windows builds ship without Tk to stay small, so provide a stub module
    whose attributes are harmless placeholder classes.
    """
    try:
        import tkinter  # noqa: F401
        import tkinter.ttk  # noqa: F401
        import tkinter.filedialog  # noqa: F401
        return
    except Exception:
        pass

    class _Placeholder:
        def __init__(self, *args, **kwargs):
            pass

    def _module(name: str) -> types.ModuleType:
        mod = types.ModuleType(name)
        mod.__getattr__ = lambda attr: _Placeholder  # type: ignore[attr-defined]
        return mod

    tk = _module("tkinter")
    ttk = _module("tkinter.ttk")
    filedialog = _module("tkinter.filedialog")
    tk.ttk = ttk  # type: ignore[attr-defined]
    tk.filedialog = filedialog  # type: ignore[attr-defined]
    for const in ("RIDGE", "N", "S", "E", "W", "END", "NORMAL", "DISABLED"):
        setattr(tk, const, const.lower())
    sys.modules["tkinter"] = tk
    sys.modules["tkinter.ttk"] = ttk
    sys.modules["tkinter.filedialog"] = filedialog


class _ProgressVar:
    def __init__(self, bar: int):
        self.bar = bar
        self._last = -1

    def set(self, value) -> None:
        value = int(value)
        if value != self._last:
            self._last = value
            emit("bar", bar=self.bar, value=value)


class _FakeGui:
    """Receives Ortho4XP's progress bar updates (bars 1-3)."""

    def __init__(self):
        self.pgrbv = [_ProgressVar(i) for i in range(4)]


def _configure_paths(o4x_root: Path, work: Path) -> None:
    import O4_File_Names as FNAMES

    work.mkdir(parents=True, exist_ok=True)
    writable = {
        "Ortho4XP_dir": work,
        "Preview_dir": work / "Previews",
        "OSM_dir": work / "OSM_data",
        "Mask_dir": work / "Masks",
        "Imagery_dir": work / "Orthophotos",
        "Elevation_dir": work / "Elevation_data",
        "Geotiff_dir": work / "Geotiffs",
        "Patch_dir": work / "Patches",
        "Tile_dir": work / "Tiles",
        "Tmp_dir": work / "tmp",
        "Overlay_dir": work / "yOrtho4XP_Overlays",
        # Extents are only used by regional imagery providers, which we don't
        # need (no imagery is downloaded); an empty directory is enough.
        "Extent_dir": work / "Extents",
    }
    read_only = {
        "Provider_dir": o4x_root / "Providers",
        "Filter_dir": o4x_root / "Filters",
        "Utils_dir": o4x_root / "Utils",
    }
    for name, path in {**writable, **read_only}.items():
        setattr(FNAMES, name, str(path))
    for path in writable.values():
        path.mkdir(parents=True, exist_ok=True)

    import O4_UI_Utils as UI

    UI.Ortho4XP_dir = str(work)


def _mark_binaries_executable(o4x_root: Path) -> None:
    if sys.platform.startswith("win"):
        return
    sub = "mac" if sys.platform == "darwin" else "lin"
    for exe in (o4x_root / "Utils" / sub).glob("*"):
        try:
            exe.chmod(exe.stat().st_mode | 0o111)
        except OSError:
            pass


def _seven_zip(o4x_root: Path) -> str | None:
    if sys.platform.startswith("win"):
        exe = o4x_root / "Utils" / "win" / "7z.exe"
        return str(exe) if exe.is_file() else None
    import shutil

    return shutil.which("7z") or shutil.which("7za") or shutil.which("7zz")


def prepare_global_dsf(global_scenery: str, lat: int, lon: int, o4x_root: Path,
                       work: Path) -> str:
    """Provide an *uncompressed* copy of the tile's XP12 Global Scenery DSF.

    Ortho4XP copies the elevation/bathymetry rasters of the default scenery
    into the new DSF. X-Plane ships those DSFs 7-Zip compressed and Ortho4XP
    unpacks them with an unquoted shell command, which breaks on paths with
    spaces (``C:\\Program Files``, user names...). We unpack the file
    ourselves and point Ortho4XP at the result. Returns the directory to use
    as Ortho4XP's ``custom_overlay_src`` ("" when unavailable).
    """
    if not global_scenery:
        return ""
    import math
    import subprocess

    lat10, lon10 = math.floor(lat / 10) * 10, math.floor(lon / 10) * 10
    rel = Path("Earth nav data") / f"{lat10:+03d}{lon10:+04d}" / f"{lat:+03d}{lon:+04d}.dsf"
    src = Path(global_scenery) / rel
    if not src.is_file():
        emit("warning", message=f"Global Scenery DSF missing: {src}")
        return ""
    cache_root = work / "global_dsf"
    dst = cache_root / rel
    if dst.is_file() and dst.stat().st_mtime >= src.stat().st_mtime:
        return str(cache_root)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(src, "rb") as handle:
        magic = handle.read(2)
    if magic != b"7z":
        import shutil

        shutil.copy2(src, dst)
        return str(cache_root)
    seven = _seven_zip(o4x_root)
    if not seven:
        emit("warning", message="7-Zip not found: XP12 bathymetry rasters will be skipped")
        return ""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    out_dir = dst.parent / "_unpack"
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run([seven, "e", "-y", f"-o{out_dir}", str(src)],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          creationflags=flags)
    unpacked = [p for p in out_dir.iterdir() if p.is_file()]
    if proc.returncode != 0 or len(unpacked) != 1:
        emit("warning", message=f"Could not unpack {src.name}: XP12 rasters skipped")
        return ""
    os.replace(unpacked[0], dst)
    os.rmdir(out_dir)
    return str(cache_root)


def run_reheight(job: dict) -> int:
    """Replace the elevations of an existing base mesh (see :mod:`xp12dem.reheight`)."""
    from xp12dem import reheight as RH

    lat, lon = int(job["lat"]), int(job["lon"])
    src = job["source"]
    source = RH.MeshSource(src["kind"], src["name"], Path(src["dsf"]), Path(src["read_root"]),
                           Path(src["pack_root"]) if src.get("pack_root") else None)
    runways = [RH.Runway(*r) for r in job.get("runways", [])]
    hf = RH.HeightField.from_hgt(Path(job["dem_file"]), lat, lon, runways=runways,
                                 feather_edges=job.get("feather", {}))
    dsftool, seven = RH.platform_tools(Path(job["ortho4xp_root"]))
    emit("step", step="reheight", index=0, total=1,
         label=f"Applying LiDAR heights to {source.label}")
    started = time.time()
    stats = RH.reheight_tile((lat, lon), source, hf, Path(job["build_dir"]), Path(job["work_dir"]),
                             dsftool, seven, log=lambda text: emit("log", message=text.strip()),
                             install_dir=Path(job["install_dir"]) if job.get("install_dir") else None)
    emit("log", message=f"{stats.changed:,} of {stats.vertices:,} vertices raised/lowered, "
                        f"mean change {stats.mean_abs_delta:.1f} m, max {stats.max_delta:.0f} m, "
                        f"{stats.big:,} by more than 10 m")
    emit("highlights", items=stats.highlights())
    emit("step_done", step="reheight", seconds=round(time.time() - started, 1))
    emit("done", dsf=str(Path(job["build_dir"]) / RH.dsf_relpath(lat, lon)))
    return 0


def run_job(job: dict) -> int:
    if job.get("mode") == "reheight":
        return run_reheight(job)
    o4x_root = Path(job["ortho4xp_root"])
    work = Path(job["work_dir"])
    lat, lon = int(job["lat"]), int(job["lon"])

    _install_tkinter_stub()
    sys.path.insert(0, str(o4x_root / "src"))
    sys.path.insert(1, str(o4x_root / "Providers"))  # O4_Custom_URL
    _mark_binaries_executable(o4x_root)
    _configure_paths(o4x_root, work)

    import O4_UI_Utils as UI

    UI.gui = _FakeGui()
    UI.verbosity = int(job.get("verbosity", 1))
    UI.cleaning_level = int(job.get("cleaning_level", 2))

    import O4_File_Names as FNAMES
    import O4_Imagery_Utils as IMG
    import O4_OSM_Utils as OSM
    import O4_Overlay_Utils as OVL
    import O4_Vector_Map as VMAP
    import O4_Mesh_Utils as MESH
    import O4_Mask_Utils as MASK
    import O4_Tile_Utils as TILE
    import O4_Config_Utils as CFG  # imported last, it may rewrite module vars

    # Application level settings (module globals in Ortho4XP).
    TILE.skip_downloads = True  # imagery is streamed by AutoOrtho
    TILE.skip_converts = True
    from xp12dem import overpass

    overpass.install(OSM, UI)
    OVL.custom_overlay_src = prepare_global_dsf(job.get("global_scenery", ""), lat, lon,
                                                o4x_root, work)

    IMG.initialize_extents_dict()
    IMG.initialize_color_filters_dict()
    IMG.initialize_providers_dict()
    IMG.initialize_combined_providers_dict()

    build_dir = Path(job["build_dir"])
    tile = CFG.Tile(lat, lon, str(build_dir))
    tile.make_dirs()

    # Tile level settings.
    tile.default_website = job.get("provider", "BI")
    tile.default_zl = int(job.get("zl", 16))
    tile.custom_dem = job["dem_file"]
    tile.fill_nodata = True
    tile.mesh_zl = int(job.get("mesh_zl", 19))
    tile.curvature_tol = float(job.get("curvature_tol", 2.0))
    tile.limit_tris = float(job.get("limit_tris", 3))
    tile.min_angle = float(job.get("min_angle", 10))
    tile.water_tech = job.get("water_tech", "XP12")
    tile.cover_airports_with_highres = job.get("cover_airports", "True")
    tile.cover_zl = int(job.get("cover_zl", 18))
    tile.road_level = int(job.get("road_level", 1))
    tile.use_decal_on_terrain = bool(job.get("use_decal_on_terrain", False))
    # AutoOrtho serves plain photo DDS files, so water transition masks must
    # be separate PNGs referenced by the .ter files (BORDER_TEX), not baked
    # into the textures.
    tile.imprint_masks_to_dds = False
    tile.zone_list = []
    tile.write_to_config()

    steps = (
        ("vector", "Downloading OpenStreetMap data (water, airports, roads)",
         lambda: VMAP.build_poly_file(tile),
         lambda: os.path.isfile(FNAMES.input_poly_file(tile))),
        ("mesh", "Triangulating terrain mesh from the DEM",
         lambda: MESH.build_mesh(tile),
         lambda: os.path.isfile(FNAMES.mesh_file(tile.build_dir, lat, lon))),
        ("masks", "Computing water transition masks",
         lambda: MASK.build_masks(tile),
         lambda: True),
        ("dsf", "Writing X-Plane DSF and terrain files",
         lambda: TILE.build_tile(tile),
         lambda: os.path.isfile(FNAMES.dsf_file(tile.build_dir, lat, lon))),
    )
    for index, (key, label, action, check) in enumerate(steps):
        emit("step", step=key, index=index, total=len(steps), label=label)
        started = time.time()
        UI.is_working = False
        UI.red_flag = False
        try:
            action()
        except Exception:
            if key == "vector" and overpass.failed_queries:
                emit("error", step=key, message=f"{OSM_FAILED} ({overpass.failed_queries[-1]})")
                return 2
            raise
        if key == "vector" and overpass.failed_queries:
            emit("error", step=key, message=f"{OSM_FAILED} ({overpass.failed_queries[-1]})")
            return 2
        if not check():
            emit("error", step=key, message=f"Ortho4XP step '{key}' did not produce its output")
            return 2
        emit("step_done", step=key, seconds=round(time.time() - started, 1))

    emit("done", dsf=FNAMES.dsf_file(tile.build_dir, lat, lon))
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: o4x_worker <job.json>", file=sys.stderr)
        return 64
    job = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    try:
        return run_job(job)
    except SystemExit as exc:  # Ortho4XP calls sys.exit() on some errors
        emit("error", step="?", message=f"Ortho4XP exited ({exc.code})")
        return 3
    except Exception as exc:  # pragma: no cover - reported to the GUI
        emit("error", step="?", message=f"{type(exc).__name__}: {exc}",
             trace=traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
