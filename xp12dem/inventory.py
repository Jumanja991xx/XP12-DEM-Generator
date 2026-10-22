"""What the user already has: installed DEM packs and downloaded elevation tiles.

Everything is read from disk on demand (pack manifests, the DEM cache), so
the map always shows the real state - also after packs were deleted or
moved by hand.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .dem import SOURCE_LABELS
from .installer import IMPROVE_PREFIX, MANIFEST, PACK_PREFIX
from .regions import Tile, parse_tile

_HGT = re.compile(r"^([NS])(\d{2})([EW])(\d{3})\.hgt$", re.IGNORECASE)


@dataclass
class InstalledPack:
    name: str
    path: Path
    method: str  # "improve" or "rebuild"
    resolution: str
    tiles: list[Tile]
    elevation_sources: dict[str, str] = field(default_factory=dict)
    built: str = ""
    enabled: bool = True  # listed (and not disabled) in scenery_packs.ini
    improves: list[str] = field(default_factory=list)  # packs whose terrain it re-heights
    misplaced: bool = False  # stored outside Custom Scenery (e.g. in XPME's folder) - should move
    broken: dict[Tile, list[str]] = field(default_factory=dict)  # tile -> missing terrain files

    def describe(self) -> str:
        what = "improved terrain" if self.method == "improve" else "new mesh"
        return f"{self.name} ({what}, {self.resolution or '?'}{', ' + self.built if self.built else ''})"


@dataclass
class Inventory:
    packs: list[InstalledPack] = field(default_factory=list)
    installed: dict[Tile, list[InstalledPack]] = field(default_factory=dict)
    cached: dict[Tile, list[str]] = field(default_factory=dict)  # tile -> ["1\" Sonny's ...", ...]

    def pack(self, name: str) -> Optional[InstalledPack]:
        for pack in self.packs:
            if pack.name.lower() == name.lower():
                return pack
        return None

    def missing(self, tiles: Iterable[Tile]) -> list[Tile]:
        return sorted(t for t in tiles if t not in self.installed)

    def inactive(self) -> list[InstalledPack]:
        """Packs that exist on disk but X-Plane does not load (not in scenery_packs.ini)."""
        return [p for p in self.packs if not p.enabled]

    def needs_fix(self) -> list[InstalledPack]:
        """Inactive packs, packs in a folder another tool manages (XPME), packs with
        tiles X-Plane refuses (missing terrain files)."""
        return [p for p in self.packs if not p.enabled or p.misplaced or p.broken]

    def active_on(self, tile: Tile) -> bool:
        return any(p.enabled and tile not in p.broken for p in self.installed.get(tile, []))


def _is_dem_pack(name: str) -> bool:
    low = name.lower()
    return low.startswith(IMPROVE_PREFIX) or low.startswith(PACK_PREFIX)


def _tiles_from_dsfs(pack: Path) -> list[Tile]:
    tiles = []
    for dsf in (pack / "Earth nav data").glob("*/*.dsf"):
        try:
            tiles.append(parse_tile(dsf.stem))
        except ValueError:
            continue
    return sorted(tiles)


def read_pack(path: Path, enabled: bool = True) -> Optional[InstalledPack]:
    """Describe a pack made by this app (None if the folder is not one)."""
    if not path.is_dir():
        return None
    manifest = {}
    manifest_file = path / MANIFEST
    if manifest_file.is_file():
        try:
            manifest = json.loads(manifest_file.read_text("utf-8"))
        except (ValueError, OSError):
            manifest = {}
    # only trust tiles whose DSF is really there
    present = set(_tiles_from_dsfs(path))
    if not present:
        return None
    method = manifest.get("method") or ("improve" if path.name.lower().startswith(IMPROVE_PREFIX)
                                        else "rebuild")
    improves = sorted(set(manifest.get("sources", {}).values())) if isinstance(manifest.get("sources"), dict) else []
    return InstalledPack(name=path.name, path=path, method=method,
                         resolution=manifest.get("resolution", ""), tiles=sorted(present),
                         elevation_sources=dict(manifest.get("elevation_sources", {})),
                         built=manifest.get("built", ""), enabled=enabled,
                         improves=[i for i in improves if not i.startswith("X-Plane 12 Global")],
                         broken=_broken_tiles(path))


def _broken_tiles(pack: Path) -> dict[Tile, list[str]]:
    """Tiles whose DSF needs terrain files the pack lacks - X-Plane refuses the whole pack."""
    from .reheight import missing_terrain

    broken = {}
    for dsf in (pack / "Earth nav data").glob("*/*.dsf"):
        try:
            tile = parse_tile(dsf.stem)
        except ValueError:
            continue
        missing = missing_terrain(pack, dsf)
        if missing:
            broken[tile] = missing
    return broken


def remove_tiles(pack: InstalledPack, tiles) -> None:
    """Delete tiles from a pack (their original terrain shows again)."""
    from .reheight import dsf_relpath

    for lat, lon in tiles:
        (pack.path / dsf_relpath(lat, lon)).unlink(missing_ok=True)
    manifest_file = pack.path / MANIFEST
    try:
        manifest = json.loads(manifest_file.read_text("utf-8"))
    except (OSError, ValueError):
        return
    gone = {tuple(t) for t in tiles}
    manifest["tiles"] = [t for t in manifest.get("tiles", []) if tuple(t) not in gone]
    manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return str(a).lower() == str(b).lower()


def installed_packs(xplane: Optional[Path], extra_roots: Iterable[Path] = ()) -> list[InstalledPack]:
    """All packs made by this app: listed in scenery_packs.ini (also absolute
    paths, e.g. next to XPME) or lying in Custom Scenery / AutoOrtho's folder."""
    from .installer import known_install_paths
    from .reheight import is_absolute_entry, pack_entries, resolve_pack

    found: dict[str, tuple[Path, bool]] = {}
    listed: dict[str, Path] = {}  # pack name -> folder enabled in scenery_packs.ini
    roots = list(extra_roots)
    if xplane is not None:
        custom = xplane / "Custom Scenery"
        for name, raw in pack_entries(custom):
            if _is_dem_pack(name):
                path = resolve_pack(xplane, raw)
                listed[name.lower()] = path
                found[str(path).lower()] = (path, True)
            elif is_absolute_entry(raw):
                # packages stored outside X-Plane (XPME): our packs sit next to them
                roots.append(resolve_pack(xplane, raw).parent)
        roots.insert(0, custom)
    for known in known_install_paths():  # remembered installs, wherever they are
        roots.append(known.parent)
    roots = list(dict.fromkeys(roots))
    for root in roots:
        try:
            children = list(Path(root).iterdir())
        except OSError:
            continue
        for child in children:
            key = str(child).lower()
            if _is_dem_pack(child.name) and key not in found:
                # e.g. AutoOrtho's folder, or a pack missing from scenery_packs.ini
                entry = listed.get(child.name.lower())
                # active if scenery_packs.ini points here - or to a place that only exists
                # while AutoOrtho runs (its packs are mounted into Custom Scenery)
                active = xplane is None or (entry is not None and (_same(entry, child) or not entry.exists()))
                found[key] = (child, active)
    packs = []
    seen: set[str] = set()
    for path, is_enabled in found.values():
        try:
            real = str(path.resolve()).lower()
        except OSError:
            real = str(path).lower()
        if real in seen:  # same folder reached twice (e.g. via the ini and a folder scan)
            continue
        pack = read_pack(path, is_enabled)
        if pack is not None:
            seen.add(real)
            packs.append(pack)
    # one name in several places (e.g. an old copy in Custom Scenery): keep all, the
    # one scenery_packs.ini points to counts as active
    if xplane is not None:
        custom = xplane / "Custom Scenery"
        mounted = [Path(r) for r in extra_roots]  # AutoOrtho storage: fine where it is
        for pack in packs:
            parent = pack.path.parent
            pack.misplaced = not _same(parent, custom) and not any(_same(parent, m) for m in mounted)
    return sorted(packs, key=lambda p: (p.name.lower(), not p.enabled))


def cached_dems(cache_dir: Path) -> dict[Tile, list[str]]:
    """Elevation tiles already downloaded, with resolution and source."""
    result: dict[Tile, list[str]] = {}
    res_names = {"13s": '1/3"', "1s": '1"', "3s": '3"'}
    for res_dir, res_name in res_names.items():
        base = Path(cache_dir) / res_dir
        if not base.is_dir():
            continue
        folders = [(p, p.name) for p in base.iterdir() if p.is_dir()] + [(base, None)]
        for folder, source in folders:
            for hgt in folder.glob("*.hgt"):
                match = _HGT.match(hgt.name)
                if not match or hgt.stat().st_size < 1000:
                    continue
                lat = int(match.group(2)) * (1 if match.group(1).upper() == "N" else -1)
                lon = int(match.group(4)) * (1 if match.group(3).upper() == "E" else -1)
                key = source
                if key is None:  # current layout: sources recorded next to the tile
                    try:
                        key = json.loads(hgt.with_suffix(".json").read_text("utf-8")).get("main", "sonny")
                    except (OSError, ValueError):
                        key = "sonny"  # version 0.1 cache
                label = f"{res_name} {SOURCE_LABELS.get(key, key)}"
                entries = result.setdefault((lat, lon), [])
                if label not in entries:
                    entries.append(label)
    return result


def scan(xplane: Optional[Path], cache_dir: Path, extra_roots: Iterable[Path] = ()) -> Inventory:
    inv = Inventory(packs=installed_packs(xplane, extra_roots), cached=cached_dems(cache_dir))
    for pack in inv.packs:
        for tile in pack.tiles:
            inv.installed.setdefault(tile, []).append(pack)
    return inv
