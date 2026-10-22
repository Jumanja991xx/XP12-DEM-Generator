"""Delete what the app has stored: downloads, caches, settings and - on
request - the scenery packs it installed."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from . import paths
from .installer import remove_from_scenery_packs_ini
from .inventory import InstalledPack


@dataclass
class DataItem:
    key: str
    label: str
    paths: list[Path]
    default: bool  # pre-ticked in the dialog

    @property
    def size(self) -> int:
        return sum(folder_size(p) for p in self.paths)


def folder_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size} B"


def local_items() -> list[DataItem]:
    root = paths.data_root()
    return [
        DataItem("dem", "Downloaded elevation data (DEM tiles)", [paths.dem_cache_dir()], True),
        DataItem("work", "Temporary build files, OpenStreetMap cache and logs",
                 [paths.work_dir(), paths.log_dir()], True),
        DataItem("maps", "Cached background map", [root / "maptiles"], True),
        DataItem("settings", "Settings and remembered build times",
                 [paths.settings_file(), root / "timings.json"], False),
    ]


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def delete_items(items: Iterable[DataItem]) -> list[str]:
    """Delete local app data. Returns error messages (empty = all deleted)."""
    errors = []
    for item in items:
        for path in item.paths:
            try:
                _remove(path)
            except OSError as exc:
                errors.append(f"{path}: {exc}")
    return errors


def delete_packs(packs: Iterable[InstalledPack], xplane: Optional[Path]) -> list[str]:
    """Remove installed packs of this app and their scenery_packs.ini lines."""
    errors = []
    for pack in packs:
        name = pack.path.name.lower()
        if not (name.startswith("z_dem_") or name.startswith("z_ao_dem_")):
            errors.append(f"{pack.path}: not a pack of this app - skipped")
            continue
        try:
            _remove(pack.path)
        except OSError as exc:
            errors.append(f"{pack.path}: {exc}")
            continue
        if xplane is not None:
            try:
                remove_from_scenery_packs_ini(xplane / "Custom Scenery", pack.path.name)
            except OSError as exc:
                errors.append(f"scenery_packs.ini: {exc}")
    return errors


def remove_tiles_from_packs(packs: Iterable[InstalledPack], tiles: Iterable, xplane: Optional[Path]) -> list[str]:
    """Take tiles out of this app's packs, so X-Plane uses the scenery below again
    (XPME, AutoOrtho, default). A pack left without tiles is removed completely."""
    from .inventory import remove_tiles

    wanted = {tuple(t) for t in tiles}
    errors: list[str] = []
    for pack in packs:
        hit = [t for t in pack.tiles if tuple(t) in wanted]
        if not hit:
            continue
        try:
            remove_tiles(pack, hit)
        except OSError as exc:
            errors.append(f"{pack.name}: {exc}")
            continue
        if not any(pack.path.glob("Earth nav data/*/*.dsf")):
            errors += delete_packs([pack], xplane)
    return errors
