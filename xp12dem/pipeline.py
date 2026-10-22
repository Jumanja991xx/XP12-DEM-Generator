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
from .eta import DEFAULT_BUILD, Estimator, TimingHistory, build_key
from .installer import IMPROVE_PREFIX, InstallTarget, assemble_pack, install_pack, pack_name
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
    method: str = "improve"  # "improve" existing terrain, or "rebuild" with Ortho4XP
    extra_roots: list[Path] = field(default_factory=list)  # e.g. AutoOrtho's real scenery dir

    @property
    def pack(self) -> str:
        return pack_name(self.slug, self.method)


class Events:
    """Callbacks used by the pipeline. Override what you need."""

    def log(self, text: str) -> None:  # noqa: D401 - simple callback
        print(text)

    def stage(self, name: str) -> None:
        print(f"== {name}")

    def progress(self, done: float, total: float) -> None:
        """``done`` = seconds elapsed, ``total`` = elapsed + estimated remaining seconds."""

    def tile_state(self, tile: Tile, state: str) -> None:
        pass


@dataclass
class BuildResult:
    ok_tiles: list[Tile] = field(default_factory=list)
    failed_tiles: dict[Tile, str] = field(default_factory=dict)
    installed_to: Optional[Path] = None
    cancelled: bool = False
    seconds: float = 0.0


def _same_drive(a: Path, b: Path) -> bool:
    return Path(a).drive.lower() == Path(b).drive.lower()


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return str(a).lower() == str(b).lower()


def short_error(message: str, limit: int = 200) -> str:
    """One readable line of a (possibly multi-line tool) error message."""
    lines = [line.strip() for line in message.splitlines() if line.strip()]
    if not lines:
        return "unknown error"
    head = lines[0].split(" failed:")[0] + (" failed" if " failed:" in lines[0] else "")
    detail = next((line for line in lines if "ERROR" in line), "")
    detail = detail.replace('!"', "").replace('\\n"', "").replace('\\n', "").strip(' "')
    while detail.startswith("ERROR: ERROR:"):
        detail = detail[len("ERROR: "):]
    detail = detail.rstrip(":").strip()
    text = f"{head}: {detail}" if detail and detail not in head else lines[0]
    return text if len(text) <= limit else text[:limit - 1] + "…"


def code_ok(proc: subprocess.Popen) -> bool:
    return proc.poll() == 0


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
        self.dem_sources: dict[Tile, str] = {}  # tile -> elevation source used
        self._install_dir: Optional[Path] = None
        self.eta: Optional[Estimator] = None
        self._history = TimingHistory(paths.data_root() / "timings.json")
        self._build_key = build_key(request.method, request.resolution_key, request.mesh_key)
        self._ticking = threading.Event()

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
        try:
            return self._run()
        finally:
            self._ticking.set()

    def _run(self) -> BuildResult:
        started = time.time()
        result = BuildResult()
        req = self.req
        res = resolution(req.resolution_key)
        detail = mesh_detail(req.mesh_key)
        tiles = list(req.tiles)
        store = DemStore(paths.dem_cache_dir(), req.local_dem_source, log=self.events.log)
        cached = {t for t in tiles if store.is_cached(t[0], t[1], res.key)}
        expected = (self._history.typical(self._build_key)
                    or float(DEFAULT_BUILD.get((req.method, req.resolution_key), 120)))
        eta = self.eta = Estimator(tiles, cached, req.parallel, expected)
        self._ticking.clear()

        def ticker() -> None:
            while not self._ticking.wait(0.5):
                self._report()

        threading.Thread(target=ticker, daemon=True, name="progress").start()

        build_root = paths.work_dir() / "builds" / req.pack
        staged = paths.work_dir() / "staging" / req.pack

        # 1. Elevation data ------------------------------------------------
        self.events.stage(f"Getting elevation data ({res.label})")
        dem_files: dict[Tile, Path] = {}
        for tile in tiles:
            if self.cancel_event.is_set():
                result.cancelled = True
                return self._finish(result, started)
            self.events.tile_state(tile, "downloading")
            eta.download_started(tile)

            def on_bytes(done: int, total: int) -> None:
                pass  # the ticker reports time based progress

            try:
                dem_files[tile], source = store.ensure_with_source(tile[0], tile[1], res.key,
                                                                   on_bytes, self.cancel_event)
                self.dem_sources[tile] = store.describe(tile[0], tile[1], res.key)
                self.events.log(f"DEM {tile_name(*tile)} ready: {self.dem_sources[tile]}")
                while store.warnings:
                    self.events.log(f"warning - {store.warnings.pop(0)}")
                self.events.tile_state(tile, "dem_ready")
                eta.download_finished(tile)
            except Cancelled:
                result.cancelled = True
                return self._finish(result, started)
            except Exception as exc:
                result.failed_tiles[tile] = str(exc)
                self.events.log(f"ERROR {tile_name(*tile)}: {exc}")
                self.events.tile_state(tile, "failed")
                eta.download_finished(tile, ok=False)  # its build step will not run either

        # 2. Meshes ---------------------------------------------------------
        global_scenery = req.xplane / "Global Scenery" / "X-Plane 12 Global Scenery"
        tile_dirs: dict[Tile, Path] = {}
        todo = [t for t in tiles if t in dem_files]
        sources: dict[Tile, "reheight.MeshSource"] = {}
        runways: dict[Tile, list] = {}
        if req.method == "improve":
            self.events.stage("Finding the terrain you currently use")
            sources, runways = self._resolve_sources(todo, result, req.pack)
            target = self._improve_target(sources, result)
            self._install_dir = target.pack_dir(req.pack)
            for tile in [t for t in todo if t not in sources]:
                eta.skip(tile)  # failed while resolving, or lives in another folder
            todo = [t for t in todo if t in sources]
            self.events.stage("Applying LiDAR heights to your terrain")
        else:
            target = req.target
            self.events.stage("Building terrain meshes with Ortho4XP")
        selected = set(tiles)
        workers = max(1, min(req.parallel, len(todo) or 1))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            if req.method == "improve":
                futures = {
                    pool.submit(self._improve_tile, tile, dem_files[tile], build_root,
                                sources[tile], runways.get(tile, []), selected): tile
                    for tile in todo
                }
            else:
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
                eta.build_finished(tile, ok=False)  # no-op when the worker reported already
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
        improve = req.method == "improve"
        assemble_pack(tile_dirs, staged, region_name=req.region_name,
                      resolution_label=res.label,
                      mesh_label="unchanged (existing mesh)" if improve else detail.label,
                      sources={t: sources[t].label for t in tile_dirs} if improve else None,
                      dem_sources={t: self.dem_sources.get(t, "") for t in tile_dirs})
        if req.install:
            self.events.stage("Installing into X-Plane 12")
            above = {src.name for t, src in sources.items() if t in tile_dirs and src.kind == "pack"}
            if improve:
                # a fresh build must also win over older LiDAR packs for the same tiles
                from .reheight import enabled_packs

                above |= {p for p in enabled_packs(req.xplane / "Custom Scenery")
                          if p.lower().startswith(IMPROVE_PREFIX) and p != req.pack}
            if improve:
                self._move_old_copies(target, req.pack)
            result.installed_to = install_pack(staged, target, req.pack, above)
            self.events.log(f"Installed {req.pack} -> {result.installed_to}")
            self.events.log("scenery_packs.ini updated (backup: scenery_packs.ini.xp12dem.bak)")
        else:
            result.installed_to = staged
        if not req.keep_work_files:
            shutil.rmtree(build_root, ignore_errors=True)
            if req.install:
                shutil.rmtree(staged, ignore_errors=True)
        eta.final_done()
        self._report()
        return self._finish(result, started)

    # ------------------------------------------------------------------
    def _finish(self, result: BuildResult, started: float) -> BuildResult:
        self._ticking.set()
        self._history.save()
        result.seconds = time.time() - started
        return result

    def _report(self) -> None:
        if self.eta is not None:
            elapsed = self.eta.elapsed()
            self.events.progress(elapsed, elapsed + self.eta.remaining())

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
        self.events.log(f"{name}: starting Ortho4XP")
        return self._run_worker(tile, job, build_dir)

    def _resolve_sources(self, tiles: list[Tile], result: BuildResult, own_pack: str):
        """Which base mesh is active for each tile, plus nearby runways."""
        from . import reheight as RH

        _, seven = RH.platform_tools(paths.ortho4xp_root())
        tmp = paths.work_dir() / "probe"
        tmp.mkdir(parents=True, exist_ok=True)
        sources = {}
        for tile in tiles:
            try:
                src = RH.find_mesh_source(tile, self.req.xplane, skip=[own_pack],
                                          extra_roots=self.req.extra_roots,
                                          seven_zip=seven, tmp_dir=tmp)
                if not src.dsf.is_file():
                    raise FileNotFoundError(f"no terrain found for this tile ({src.dsf} missing)")
                sources[tile] = src
                self.events.log(f"{tile_name(*tile)}: current terrain = {src.label}")
                if src.unreadable_above:
                    self.events.log(
                        f"{tile_name(*tile)}: warning - {', '.join(src.unreadable_above)} could not be "
                        "read (empty folder). If XPME/AutoOrtho is not running, start it and build "
                        "again, otherwise the new heights are hidden by that pack.")
            except Exception as exc:
                result.failed_tiles[tile] = str(exc)
                self.events.log(f"ERROR {tile_name(*tile)}: {exc}")
                self.events.tile_state(tile, "failed")
        shutil.rmtree(tmp, ignore_errors=True)
        packs = RH.enabled_packs(self.req.xplane / "Custom Scenery")
        runways = RH.load_runways(RH.apt_files_for(self.req.xplane, packs), list(sources))
        count = sum(len(v) for v in runways.values())
        self.events.log(f"{count} runway(s) found - their surroundings keep the original heights")
        return sources, runways

    def _improve_target(self, sources: dict, result: BuildResult) -> InstallTarget:
        """Where the improved pack goes: always Custom Scenery.

        Its .ter files reference the textures of the packs it improves with
        paths relative to Custom Scenery, also when those packs live outside
        X-Plane (XPME keeps them in Documents). Packs inside XPME's own folder
        would be dropped from scenery_packs.ini whenever XPME rewrites it.
        Only a source on another drive cannot be reached relatively; such
        tiles go next to their source as before.
        """
        custom = self.req.xplane / "Custom Scenery"
        other_drive: dict[Path, list[Tile]] = {}
        for tile, src in sources.items():
            if src.kind == "pack" and src.pack_root is not None and not _same_drive(src.pack_root, custom):
                other_drive.setdefault(src.pack_root.parent, []).append(tile)
        if not other_drive:
            return InstallTarget.for_custom_scenery(self.req.xplane)
        if sum(len(t) for t in other_drive.values()) < len(sources):
            for tiles in other_drive.values():
                for tile in tiles:
                    message = (f"its terrain ({sources[tile].name}) is on another drive than "
                               "X-Plane - build it in a separate run")
                    result.failed_tiles[tile] = message
                    self.events.log(f"ERROR {tile_name(*tile)}: {message}")
                    self.events.tile_state(tile, "failed")
                    del sources[tile]
            return InstallTarget.for_custom_scenery(self.req.xplane)
        parent = max(other_drive, key=lambda p: len(other_drive[p]))
        self.events.log(f"Your terrain is on another drive ({parent}); the new pack is placed next "
                        "to it. If XPME removes it from scenery_packs.ini, use 'Activate' on step 1.")
        return InstallTarget.beside(parent, self.req.xplane)

    def _move_old_copies(self, target: InstallTarget, name: str) -> None:
        """Merge packs of the same name stored elsewhere (older versions put them next
        to XPME) into the new location, so tiles built earlier are not lost."""
        from .installer import known_install_paths, relocate_pack
        from .reheight import pack_entries, resolve_pack

        dest = target.pack_dir(name)
        candidates = [p for p in known_install_paths() if p.name.lower() == name.lower()]
        candidates += [resolve_pack(self.req.xplane, raw) for n, raw in
                       pack_entries(self.req.xplane / "Custom Scenery") if n.lower() == name.lower()]
        for old in dict.fromkeys(candidates):
            if old.is_dir() and not _same_path(old, dest):
                relocate_pack(old, dest)
                self.events.log(f"Moved the tiles of {old} into {dest}")

    def _improve_tile(self, tile: Tile, dem_file: Path, build_root: Path, source,
                      runways: list, selected: set[Tile]) -> Path:
        if self.cancel_event.is_set():
            raise Cancelled()
        lat, lon = tile
        name = tile_name(lat, lon)
        build_dir = build_root / name
        if build_dir.exists():
            shutil.rmtree(build_dir)
        neighbours = {"n": (lat + 1, lon), "s": (lat - 1, lon), "e": (lat, lon + 1), "w": (lat, lon - 1)}
        job = {
            "mode": "reheight",
            "ortho4xp_root": str(paths.ortho4xp_root()),
            "work_dir": str(paths.work_dir() / "reheight" / name),
            "lat": lat,
            "lon": lon,
            "dem_file": str(dem_file),
            "build_dir": str(build_dir),
            "source": {"kind": source.kind, "name": source.name, "dsf": str(source.dsf),
                       "read_root": str(source.read_root),
                       "pack_root": str(source.pack_root) if source.pack_root else ""},
            "install_dir": str(self._install_dir) if self._install_dir else "",
            "runways": [[r.lat1, r.lon1, r.lat2, r.lon2, r.half_width] for r in runways],
            # blend into the original terrain towards tiles that are not rebuilt now,
            # and towards tiles whose elevation comes from a different source
            # (their edge heights would not match exactly)
            "feather": {side: nb not in selected or self.dem_sources.get(nb) != self.dem_sources.get(tile)
                        for side, nb in neighbours.items()},
        }
        self.events.log(f"{name}: improving {source.label}")
        return self._run_worker(tile, job, build_dir)

    def _run_worker(self, tile: Tile, job: dict, build_dir: Path) -> Path:
        name = tile_name(*tile)
        jobs_dir = paths.work_dir() / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        job_file = jobs_dir / f"{name}.json"
        job_file.write_text(json.dumps(job), encoding="utf-8")
        log_file = paths.log_dir() / f"{name}.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)

        self.events.tile_state(tile, "building")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        proc = subprocess.Popen(worker_command(job_file), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                errors="replace", env=env, creationflags=creationflags)
        with self._lock:
            self._procs.add(proc)
        if self.eta is not None:
            self.eta.build_started(tile)
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
                    elif kind == "step_done":
                        self.events.log(f"{name}:   done in {event['seconds']} s")
                    elif kind == "log":
                        message = event.get("message", "")
                        self.events.log(f"{name}:   {message}")
                    elif kind == "warning":
                        self.events.log(f"{name}: warning - {event.get('message', '')}")
                    elif kind == "error":
                        error = event.get("message", "unknown error")
            code = proc.wait()
        finally:
            with self._lock:
                self._procs.discard(proc)
            ok = code_ok(proc) and not error
            seconds = self.eta.build_finished(tile, ok) if self.eta is not None else None
            if ok and seconds:
                self._history.add(self._build_key, seconds)
        if self.cancel_event.is_set():
            raise Cancelled()
        if code != 0 or error:
            raise RuntimeError(error or f"worker exited with code {code} (see {log_file})")
        self.events.log(f"{name}: finished")
        return build_dir
