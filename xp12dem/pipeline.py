"""End-to-end build: DEM download -> Ortho4XP mesh per tile -> pack -> install.

The pipeline is independent from the GUI; it reports through an ``Events``
object so it can also be driven from the command line or tests.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import paths
from .dem import Cancelled, DemStore, resolution
from .installer import InstallTarget, assemble_pack, install_pack, pack_name
from .o4x_worker import PROGRESS_PREFIX
from .regions import Tile, tile_name


@dataclass(frozen=True)
class MeshDetail:
    key: str
    label: str
    curvature_tol: float
    limit_tris: float  # millions of triangles per tile
    description: str


MESH_DETAILS: tuple[MeshDetail, ...] = (
    MeshDetail("balanced", "Balanced", 2.0, 3,
               "Same density as AutoOrtho's own packs. Best performance."),
    MeshDetail("high", "High", 1.2, 4,
               "Noticeably crisper ridges and valleys. Recommended for VFR."),
    MeshDetail("maximum", "Maximum", 0.8, 5,
               "Densest mesh X-Plane handles comfortably. Needs a strong CPU."),
)


WORKER_EXE = "xp12dem-worker"

# Share of a tile's build time already spent when each Ortho4XP step starts.
STEP_START = {"vector": 0.0, "mesh": 0.12, "masks": 0.45, "dsf": 0.55}


def mesh_detail(key: str) -> MeshDetail:
    for detail in MESH_DETAILS:
        if detail.key == key:
            return detail
    raise KeyError(key)


@dataclass
class BuildRequest:
    tiles: list[Tile]
    region_name: str
    slug: str
    resolution_key: str
    mesh_key: str
    xplane: Path
    target: InstallTarget
    local_dem_source: Optional[Path] = None
    parallel: int = 1
    install: bool = True
    keep_work_files: bool = False

    @property
    def pack(self) -> str:
        return pack_name(self.slug)


class Events:
    """Callbacks used by the pipeline. Override what you need."""

    def log(self, text: str) -> None:  # noqa: D401 - simple callback
        print(text)

    def stage(self, name: str) -> None:
        print(f"== {name}")

    def progress(self, done: float, total: float) -> None:
        pass

    def tile_state(self, tile: Tile, state: str) -> None:
        pass


@dataclass
class BuildResult:
    ok_tiles: list[Tile] = field(default_factory=list)
    failed_tiles: dict[Tile, str] = field(default_factory=dict)
    installed_to: Optional[Path] = None
    cancelled: bool = False
    seconds: float = 0.0


def worker_command(job_file: Path) -> list[str]:
    """Command line that starts ``o4x_worker`` in a fresh process.

    The packaged app ships a separate console-mode worker executable next to
    the windowed GUI executable, so the worker has real stdout pipes.
    """
    if paths.is_frozen():
        exe = Path(sys.executable).with_name(WORKER_EXE + (".exe" if sys.platform.startswith("win") else ""))
        return [str(exe), str(job_file)]
    return [sys.executable, "-m", "xp12dem", "--o4x-worker", str(job_file)]


class Pipeline:
    def __init__(self, request: BuildRequest, events: Events):
        self.req = request
        self.events = events
        self.cancel_event = threading.Event()
        self._procs: set[subprocess.Popen] = set()
        self._lock = threading.Lock()
        self._tile_fraction: dict[Tile, float] = {}
        self._on_partial = lambda: None

    # ------------------------------------------------------------------
    def cancel(self) -> None:
        self.cancel_event.set()
        with self._lock:
            for proc in list(self._procs):
                try:
                    proc.kill()
                except OSError:
                    pass

    # ------------------------------------------------------------------
    def run(self) -> BuildResult:
        started = time.time()
        result = BuildResult()
        req = self.req
        res = resolution(req.resolution_key)
        detail = mesh_detail(req.mesh_key)
        tiles = list(req.tiles)
        total_units = len(tiles) * 2 + 1  # download + build per tile, install
        done_units = 0.0

        def bump(units: float = 1.0) -> None:
            nonlocal done_units
            done_units += units
            self.events.progress(done_units, total_units)

        def partial() -> None:
            # finer progress while tiles are being meshed
            with self._lock:
                running = sum(self._tile_fraction.values())
            self.events.progress(done_units + running, total_units)

        self._on_partial = partial

        build_root = paths.work_dir() / "builds" / req.pack
        staged = paths.work_dir() / "staging" / req.pack

        # 1. Elevation data ------------------------------------------------
        self.events.stage(f"Downloading Sonny's DTM ({res.label})")
        store = DemStore(paths.dem_cache_dir(), req.local_dem_source)
        dem_files: dict[Tile, Path] = {}
        for tile in tiles:
            if self.cancel_event.is_set():
                result.cancelled = True
                return self._finish(result, started)
            self.events.tile_state(tile, "downloading")
            base = done_units

            def on_bytes(done: int, total: int, base=base) -> None:
                if total:
                    self.events.progress(base + min(done / total, 1.0) * 0.95, total_units)

            try:
                dem_files[tile] = store.ensure(tile[0], tile[1], res.arcsec, on_bytes, self.cancel_event)
                self.events.log(f"DEM {tile_name(*tile)} ready: {dem_files[tile].name}")
                self.events.tile_state(tile, "dem_ready")
                bump()
            except Cancelled:
                result.cancelled = True
                return self._finish(result, started)
            except Exception as exc:
                result.failed_tiles[tile] = str(exc)
                self.events.log(f"ERROR {tile_name(*tile)}: {exc}")
                self.events.tile_state(tile, "failed")
                bump(2)  # its build step will not run either

        # 2. Meshes ---------------------------------------------------------
        self.events.stage("Building terrain meshes with Ortho4XP")
        global_scenery = req.xplane / "Global Scenery" / "X-Plane 12 Global Scenery"
        tile_dirs: dict[Tile, Path] = {}
        todo = [t for t in tiles if t in dem_files]
        workers = max(1, min(req.parallel, len(todo) or 1))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._build_tile, tile, dem_files[tile], build_root,
                            global_scenery, detail): tile
                for tile in todo
            }
            for future in as_completed(futures):
                tile = futures[future]
                try:
                    tile_dirs[tile] = future.result()
                    result.ok_tiles.append(tile)
                    self.events.tile_state(tile, "built")
                except Cancelled:
                    result.cancelled = True
                except Exception as exc:
                    result.failed_tiles[tile] = str(exc)
                    self.events.log(f"ERROR {tile_name(*tile)}: {exc}")
                    self.events.tile_state(tile, "failed")
                bump()
        if self.cancel_event.is_set():
            result.cancelled = True
            return self._finish(result, started)
        if not tile_dirs:
            self.events.log("No tile could be built - nothing to install.")
            return self._finish(result, started)

        # 3. Pack + install ---------------------------------------------------
        self.events.stage("Assembling scenery pack")
        if staged.exists():
            shutil.rmtree(staged)
        assemble_pack(tile_dirs, staged, region_name=req.region_name,
                      resolution_label=res.label, mesh_label=detail.label)
        if req.install:
            self.events.stage("Installing into X-Plane 12")
            result.installed_to = install_pack(staged, req.target, req.pack)
            self.events.log(f"Installed {req.pack} -> {result.installed_to}")
            self.events.log("scenery_packs.ini updated (backup: scenery_packs.ini.xp12dem.bak)")
        else:
            result.installed_to = staged
        if not req.keep_work_files:
            shutil.rmtree(build_root, ignore_errors=True)
            if req.install:
                shutil.rmtree(staged, ignore_errors=True)
        bump()
        return self._finish(result, started)

    # ------------------------------------------------------------------
    def _finish(self, result: BuildResult, started: float) -> BuildResult:
        result.seconds = time.time() - started
        return result

    def _build_tile(self, tile: Tile, dem_file: Path, build_root: Path,
                    global_scenery: Path, detail: MeshDetail) -> Path:
        if self.cancel_event.is_set():
            raise Cancelled()
        lat, lon = tile
        name = tile_name(lat, lon)
        build_dir = build_root / f"zOrtho4XP_{name}"
        if build_dir.exists():
            shutil.rmtree(build_dir)
        job = {
            "ortho4xp_root": str(paths.ortho4xp_root()),
            "work_dir": str(paths.work_dir()),
            "lat": lat,
            "lon": lon,
            "dem_file": str(dem_file),
            "build_dir": str(build_dir),
            "global_scenery": str(global_scenery) if global_scenery.is_dir() else "",
            "curvature_tol": detail.curvature_tol,
            "limit_tris": detail.limit_tris,
            "provider": "BI",
            "zl": 16,
            "cover_airports": "True",
            "cover_zl": 18,
            "water_tech": "XP12",
        }
        if not job["global_scenery"]:
            self.events.log(f"{name}: X-Plane 12 Global Scenery not found - "
                            "DSF will be built without XP12 bathymetry rasters.")
        jobs_dir = paths.work_dir() / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        job_file = jobs_dir / f"{name}.json"
        job_file.write_text(json.dumps(job), encoding="utf-8")
        log_file = paths.log_dir() / f"{name}.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)

        self.events.tile_state(tile, "building")
        self.events.log(f"{name}: starting Ortho4XP (log: {log_file})")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        proc = subprocess.Popen(worker_command(job_file), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                errors="replace", env=env, creationflags=creationflags)
        with self._lock:
            self._procs.add(proc)
        error: Optional[str] = None
        try:
            with open(log_file, "w", encoding="utf-8") as log:
                assert proc.stdout is not None
                for line in proc.stdout:
                    log.write(line)
                    if not line.startswith(PROGRESS_PREFIX):
                        continue
                    try:
                        event = json.loads(line[len(PROGRESS_PREFIX):])
                    except ValueError:
                        continue
                    kind = event.get("kind")
                    if kind == "step":
                        self.events.log(f"{name}: [{event['index'] + 1}/{event['total']}] {event['label']}")
                        self.events.tile_state(tile, f"step:{event['step']}")
                        with self._lock:
                            self._tile_fraction[tile] = STEP_START.get(event["step"], 0.0)
                        self._on_partial()
                    elif kind == "step_done":
                        self.events.log(f"{name}:   done in {event['seconds']} s")
                    elif kind == "warning":
                        self.events.log(f"{name}: warning - {event.get('message', '')}")
                    elif kind == "error":
                        error = event.get("message", "unknown error")
            code = proc.wait()
        finally:
            with self._lock:
                self._procs.discard(proc)
                self._tile_fraction.pop(tile, None)
        if self.cancel_event.is_set():
            raise Cancelled()
        if code != 0 or error:
            raise RuntimeError(error or f"Ortho4XP exited with code {code} (see {log_file})")
        self.events.log(f"{name}: finished")
        return build_dir
