"""Entry point.

* no arguments                 -> graphical wizard
* ``--o4x-worker <job.json>``  -> internal: build one tile with Ortho4XP
* ``build ...``                -> command line build (see ``build --help``)
"""

from __future__ import annotations

import argparse
import multiprocessing
import sys
from pathlib import Path


def _ensure_console_output() -> None:
    """The windowed Windows executable has no stdout; log to a file instead."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    from .paths import log_dir

    log_dir().mkdir(parents=True, exist_ok=True)
    stream = open(log_dir() / "cli.log", "w", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or stream
    sys.stderr = sys.stderr or stream


def _cli_build(argv: list[str]) -> int:
    _ensure_console_output()
    from . import regions
    from .installer import InstallTarget
    from .pipeline import BuildRequest, Events, MESH_DETAILS, Pipeline
    from .system import recommended_parallel
    from .xplane import find_xplane12_installs, read_autoortho_config

    parser = argparse.ArgumentParser(prog="xp12dem build", description="Build and install a DEM mesh pack.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--region", choices=[r.key for r in regions.PRESETS])
    group.add_argument("--tiles", help="comma separated tiles, e.g. +47+011,+46+011")
    parser.add_argument("--name", help="pack name (default: region name)")
    parser.add_argument("--method", choices=["improve", "rebuild"], default="improve",
                        help="improve = LiDAR heights for the terrain you already use (XPME, "
                             "AutoOrtho, Ortho4XP, default scenery); rebuild = new Ortho4XP mesh "
                             "with photos from XPME or AutoOrtho (see --target)")
    parser.add_argument("--resolution", choices=["1", "13", "3"], default="1",
                        help="1 = 1\" (30 m), 13 = 1/3\" (10 m where available), 3 = 3\" (90 m)")
    parser.add_argument("--mesh", choices=[m.key for m in MESH_DETAILS], default="high")
    parser.add_argument("--xplane", type=Path, help="X-Plane 12 folder (auto-detected)")
    parser.add_argument("--target", choices=["official", "xpme", "autoortho", "custom_scenery"], default="autoortho",
                        help="'rebuild' only: official = official aerial photos (Bavaria/Austria/"
                             "Switzerland) stored in the pack, xpme = Custom Scenery with XPME's photos, autoortho = "
                             "AutoOrtho's folder, custom_scenery = Custom Scenery with AutoOrtho photos "
                             "('improve' always uses Custom Scenery)")
    parser.add_argument("--photo-zl", type=int, choices=[16, 17, 18], default=17,
                        help="zoom level of official photos (17 = ~0.8 m, ~9 GB per tile)")
    parser.add_argument("--dem-folder", type=Path, help="folder with your own Sonny .hgt/.zip files")
    parser.add_argument("--parallel", type=int, default=0, help="tiles at once (0 = automatic)")
    parser.add_argument("--no-install", action="store_true", help="only build, keep the pack in the work folder")
    args = parser.parse_args(argv)

    if args.region:
        region = regions.preset(args.region)
        tiles, name = region.tiles(), args.name or region.name
    else:
        tiles = [regions.parse_tile(t) for t in args.tiles.split(",")]
        name = args.name or "Custom region"
    xplane = args.xplane or next(iter(find_xplane12_installs()), None)
    if xplane is None:
        parser.error("X-Plane 12 not found, pass --xplane")
    ao = read_autoortho_config()
    extra_roots = [ao.ao_scenery_dir] if ao and ao.ao_scenery_dir else []
    if args.method == "improve" or args.target in ("custom_scenery", "xpme", "official"):
        target = InstallTarget.for_custom_scenery(xplane)
    else:
        if ao is None or ao.ao_scenery_dir is None:
            parser.error("AutoOrtho configuration (~/.autoortho) not found; use --target custom_scenery")
        target = InstallTarget.for_autoortho(ao, xplane)

    request = BuildRequest(tiles=tiles, region_name=name, slug=regions.slugify(name),
                           resolution_key=args.resolution, mesh_key=args.mesh, xplane=xplane,
                           target=target, local_dem_source=args.dem_folder,
                           parallel=args.parallel or recommended_parallel(args.method), install=not args.no_install,
                           method=args.method, extra_roots=extra_roots,
                           photos=(args.target if args.method == "rebuild" and args.target in ("xpme", "official")
                                   else "autoortho"),
                           photo_zl=args.photo_zl)
    class CliEvents(Events):
        last = -1e9

        def progress(self, done: float, total: float) -> None:
            if total > 0 and done - self.last >= 15:  # a line every 15 s
                self.last = done
                print(f"progress {100 * done / total:.0f}% - about {total - done:.0f} s left", flush=True)

    result = Pipeline(request, CliEvents()).run()
    for tile, message in result.failed_tiles.items():
        print(f"FAILED {regions.tile_name(*tile)}: {message}")
    print(f"Built {len(result.ok_tiles)} tile(s), failed {len(result.failed_tiles)}, "
          f"in {result.seconds / 60:.1f} min -> {result.installed_to}")
    return 0 if result.ok_tiles and not result.failed_tiles else 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--o4x-worker"]:
        from .o4x_worker import main as worker_main
        return worker_main(argv[1:])
    if argv[:1] == ["build"]:
        return _cli_build(argv[1:])
    from .gui.app import run
    return run()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
