"""Assemble finished tiles into a scenery pack and install it into X-Plane 12."""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from . import __version__
from .dem import ATTRIBUTION
from .regions import Tile, dsf_relpath, tile_name
from .xplane import AutoOrthoConfig

PACK_PREFIX = "z_ao_dem_"  # new Ortho4XP mesh, lives in AutoOrtho's scenery folder
IMPROVE_PREFIX = "z_dem_"  # re-heighted copy of the mesh the user already has
MESH_PREFIX = "z_dem_mesh_"  # new Ortho4XP mesh using XPME's photos, lives in Custom Scenery
MANIFEST = "xp12dem_pack.json"
INI_NAME = "scenery_packs.ini"
INI_BACKUP_SUFFIX = ".xp12dem.bak"

# Base-mesh packs that ours must be loaded above (= listed before).
_MESH_PACK_PREFIXES = ("z_ao_", "z_autoortho", "zortho4xp_", "z_ortho", "zzz")


def pack_name(slug: str, method: str = "rebuild", photos: str = "autoortho") -> str:
    if method == "improve":
        return IMPROVE_PREFIX + slug
    return (MESH_PREFIX if photos == "xpme" else PACK_PREFIX) + slug


@dataclass
class InstallTarget:
    """Where a pack goes and how X-Plane will see it."""

    kind: str  # "autoortho" or "custom_scenery"
    pack_parent: Path  # directory the pack folder is copied into
    custom_scenery: Path  # X-Plane's Custom Scenery folder (for the .ini)

    @classmethod
    def for_autoortho(cls, ao: AutoOrthoConfig, xplane: Path) -> "InstallTarget":
        if not ao.ao_scenery_dir:
            raise ValueError("AutoOrtho has no scenery path configured")
        return cls("autoortho", ao.ao_scenery_dir, xplane / "Custom Scenery")

    @classmethod
    def for_custom_scenery(cls, xplane: Path) -> "InstallTarget":
        return cls("custom_scenery", xplane / "Custom Scenery", xplane / "Custom Scenery")

    @classmethod
    def beside(cls, parent: Path, xplane: Path) -> "InstallTarget":
        """Next to packs stored outside X-Plane (absolute scenery_packs.ini entries)."""
        return cls("external", parent, xplane / "Custom Scenery")

    def pack_dir(self, name: str) -> Path:
        return self.pack_parent / name

    def ini_entry(self, name: str) -> Optional[str]:
        """Path to write into scenery_packs.ini (None = default 'Custom Scenery/<name>/')."""
        if self.kind == "external":
            return f"{self.pack_parent.as_posix().rstrip('/')}/{name}/"
        return None


# ---------------------------------------------------------------------------
# Pack assembly
# ---------------------------------------------------------------------------
def assemble_pack(tile_dirs: dict[Tile, Path], pack_dir: Path, *, region_name: str,
                  resolution_label: str, mesh_label: str,
                  sources: Optional[dict[Tile, str]] = None,
                  dem_sources: Optional[dict[Tile, str]] = None,
                  method: Optional[str] = None) -> list[Tile]:
    """Merge Ortho4XP tile build folders into one scenery pack folder.

    Only what X-Plane needs is copied: the DSF, the .ter terrain definitions
    and the water mask PNGs. The photo .dds files are *not* created - they
    are generated on the fly by AutoOrtho.
    Returns the tiles that were added.
    """
    added: list[Tile] = []
    for (lat, lon), build in sorted(tile_dirs.items()):
        dsf = build / dsf_relpath(lat, lon)
        if not dsf.is_file():
            continue
        target_dsf = pack_dir / dsf_relpath(lat, lon)
        target_dsf.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dsf, target_dsf)
        for sub in ("terrain", "textures"):
            src = build / sub
            if not src.is_dir():
                continue
            dst = pack_dir / sub
            dst.mkdir(parents=True, exist_ok=True)
            for item in src.iterdir():
                if item.is_file() and item.suffix.lower() != ".dds":
                    shutil.copy2(item, dst / item.name)
        added.append((lat, lon))
    if sources is None:
        (pack_dir / "textures").mkdir(parents=True, exist_ok=True)
    _write_manifest(pack_dir, added, region_name, resolution_label, mesh_label, sources, dem_sources,
                    method or ("improve" if sources is not None else "rebuild"))
    return added


def _write_manifest(pack_dir: Path, added: list[Tile], region_name: str,
                    resolution_label: str, mesh_label: str,
                    sources: Optional[dict[Tile, str]] = None,
                    dem_sources: Optional[dict[Tile, str]] = None,
                    method: str = "rebuild") -> None:
    manifest_path = pack_dir / MANIFEST
    tiles = set()
    known_sources: dict[str, str] = {}
    known_dems: dict[str, str] = {}
    if manifest_path.is_file():
        try:
            previous = json.loads(manifest_path.read_text("utf-8"))
            tiles = {tuple(t) for t in previous.get("tiles", [])}
            known_sources = dict(previous.get("sources", {}))
            known_dems = dict(previous.get("elevation_sources", {}))
        except (ValueError, OSError):
            tiles = set()
    tiles.update(added)
    for tile, name in (sources or {}).items():
        if tile in added:
            known_sources[tile_name(*tile)] = name
    for tile, name in (dem_sources or {}).items():
        if tile in added and name:
            known_dems[tile_name(*tile)] = name
    manifest = {
        "generator": f"XP12 DEM Generator {__version__}",
        "region": region_name,
        "dem": ", ".join(sorted(set(known_dems.values()))) or "Sonny's LiDAR DTM Europe",
        "elevation_sources": known_dems,
        "resolution": resolution_label,
        "method": method,
        "mesh_detail": mesh_label,
        "sources": known_sources,
        "built": time.strftime("%Y-%m-%d %H:%M"),
        "tiles": sorted([list(t) for t in tiles]),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if sources is not None:
        what = ("This pack is a copy of the terrain mesh you already had (see 'sources' in\n"
                f"{MANIFEST}) with LiDAR elevations. Textures, water and roads still come\n"
                "from the original packs - keep them installed (and XPME/AutoOrtho running).\n"
                "Rebuild this pack after the original scenery is updated.\n\n")
    else:
        what = ("This pack contains terrain (mesh) only. The photo textures it references\n"
                "(textures/*.dds) are streamed by AutoOrtho, which must be running.\n\n")
    readme = (
        f"{region_name} - terrain mesh for X-Plane 12\n"
        f"Generated by XP12 DEM Generator {__version__}\n\n"
        f"DEM resolution: {resolution_label}\nMesh detail:    {mesh_label}\n"
        f"Tiles:          {', '.join(tile_name(*t) for t in sorted(tiles))}\n\n"
        + what
        + _credits(set(known_dems.values()))
        + ("Elevations applied with DSFTool (Laminar Research) to the source meshes listed above.\n"
           if sources is not None else
           "Mesh built with Ortho4XP by Oscar Pilote (GPL v3).\n"
           "Water/airport/road vector data (c) OpenStreetMap contributors (ODbL).\n")
    )
    (pack_dir / "README.txt").write_text(readme, encoding="utf-8")


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------
def _credits(dems: set[str]) -> str:
    """Licence / attribution lines for the elevation sources named in ``dems``."""
    from .dem import SOURCE_LABELS
    from .sources import SOURCES

    text = " ".join(dems)
    lines = []
    for key, src in SOURCES.items():
        label = SOURCE_LABELS.get(key, src.label)
        if label.split(" (")[0] in text or (key == "local" and "own" in text):
            lines.append(src.licence)
    if not dems or (not lines and "Sonny" in text):
        lines.append(ATTRIBUTION)
    return "\n".join(dict.fromkeys(lines)) + "\n"


def install_pack(staged_pack: Path, target: InstallTarget, name: str,
                 above: Iterable[str] = ()) -> Path:
    """Copy a staged pack into its final place (merging with an older build)."""
    dest = target.pack_dir(name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    before = _read_json(dest / MANIFEST)
    shutil.copytree(staged_pack, dest, dirs_exist_ok=True)
    if before:  # keep what earlier builds recorded about their tiles
        new = _read_json(dest / MANIFEST)
        for key in ("sources", "elevation_sources"):
            new[key] = {**before.get(key, {}), **new.get(key, {})}
        tiles = {tuple(t) for t in before.get("tiles", [])} | {tuple(t) for t in new.get("tiles", [])}
        new["tiles"] = sorted([list(t) for t in tiles])
        (dest / MANIFEST).write_text(json.dumps(new, indent=2), encoding="utf-8")
    update_scenery_packs_ini(target.custom_scenery, name, above, target.ini_entry(name))
    remember_install(dest)
    return dest


# ---------------------------------------------------------------------------
# Where packs were installed (so they are found even if scenery_packs.ini changed)
# ---------------------------------------------------------------------------
def _registry_file() -> Path:
    from .paths import data_root

    return data_root() / "installed_packs.json"


def known_install_paths() -> list[Path]:
    try:
        return [Path(p) for p in json.loads(_registry_file().read_text("utf-8"))]
    except (OSError, ValueError, TypeError):
        return []


def remember_install(dest: Path) -> None:
    paths = [str(p) for p in known_install_paths() if p.exists() and p != dest] + [str(dest)]
    try:
        _registry_file().parent.mkdir(parents=True, exist_ok=True)
        _registry_file().write_text(json.dumps(paths, indent=1), encoding="utf-8")
    except OSError:
        pass


def relocate_pack(src: Path, dest: Path) -> Path:
    """Move a pack to ``dest`` (merging with a pack already there) and keep the
    texture references of its .ter files valid. Returns ``dest``."""
    from .reheight import retarget_ter

    src, dest = Path(src), Path(dest)
    old_manifest = _read_json(src / MANIFEST)
    dest_manifest = _read_json(dest / MANIFEST)
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(src)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix.lower() == ".ter":
            text = retarget_ter(path.read_text("utf-8", "replace"), path, target)
            target.write_text(text, encoding="utf-8")
        elif path.name != MANIFEST:
            shutil.copy2(path, target)
    merged = dict(dest_manifest)
    merged.update({k: v for k, v in old_manifest.items() if k not in ("tiles", "sources", "elevation_sources")})
    for key in ("sources", "elevation_sources"):
        merged[key] = {**dest_manifest.get(key, {}), **old_manifest.get(key, {})}
    tiles = {tuple(t) for t in dest_manifest.get("tiles", [])} | {tuple(t) for t in old_manifest.get("tiles", [])}
    merged["tiles"] = sorted([list(t) for t in tiles])
    if merged:
        (dest / MANIFEST).write_text(json.dumps(merged, indent=2), encoding="utf-8")
    shutil.rmtree(src)
    paths = [str(p) for p in known_install_paths() if p.exists()]
    try:
        _registry_file().write_text(json.dumps(paths, indent=1), encoding="utf-8")
    except OSError:
        pass
    remember_install(dest)
    return dest


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def reactivate_pack(pack_dir: Path, xplane: Path, improves: Iterable[str] = ()) -> None:
    """Put an existing pack (back) into scenery_packs.ini, above the packs it improves."""
    custom = xplane / "Custom Scenery"
    inside = pack_dir.parent.resolve() == custom.resolve()
    entry = None if inside else f"{pack_dir.as_posix().rstrip('/')}/"
    update_scenery_packs_ini(custom, pack_dir.name, improves, entry)


def uninstall_pack(target: InstallTarget, name: str) -> None:
    dest = target.pack_dir(name)
    if dest.is_dir() and (dest / MANIFEST).is_file():
        shutil.rmtree(dest)
    remove_from_scenery_packs_ini(target.custom_scenery, name)


def _pack_of(line: str) -> Optional[str]:
    """Folder name of a SCENERY_PACK line, e.g. ``z_ao_eur``."""
    stripped = line.strip()
    for key in ("SCENERY_PACK_DISABLED ", "SCENERY_PACK "):
        if stripped.startswith(key):
            path = stripped[len(key):].strip().replace("\\", "/").rstrip("/")
            return path.rsplit("/", 1)[-1]
    return None


def insert_pack_line(lines: list[str], name: str, above: Iterable[str] = (),
                     entry_path: Optional[str] = None) -> list[str]:
    """Return ``lines`` with ``name`` enabled directly above the first mesh pack.

    X-Plane uses only the first base mesh it finds for each tile, in
    scenery_packs.ini order, so our pack has to come before the packs it
    replaces: the ``above`` packs (e.g. the XPME package we re-heighted), or
    AutoOrtho's own packs (``z_ao_*``) and any other Ortho4XP mesh.
    """
    entry = f"SCENERY_PACK {entry_path or f'Custom Scenery/{name}/'}"
    kept = [line for line in lines if _pack_of(line) != name]
    above = {a.lower() for a in above}
    insert_at = None
    for index, line in enumerate(kept):
        pack = _pack_of(line)
        if pack and (pack.lower() in above or (not above and pack.lower().startswith(_MESH_PACK_PREFIXES))):
            insert_at = index
            break
    if insert_at is None:
        insert_at = len(kept)
        while insert_at > 0 and not kept[insert_at - 1].strip():
            insert_at -= 1
    elif not _is_absolute_line(entry) and _is_absolute_line(kept[insert_at]):
        # Tools like XPME keep their packages as a block of absolute paths at the end of
        # the file and rewrite that block on start: stay in front of the whole block.
        while insert_at > 0 and _is_absolute_line(kept[insert_at - 1]):
            insert_at -= 1
        while insert_at > 0 and not kept[insert_at - 1].strip():
            insert_at -= 1
    return kept[:insert_at] + [entry] + kept[insert_at:]


def _is_absolute_line(line: str) -> bool:
    from .reheight import is_absolute_entry

    stripped = line.strip()
    for key in ("SCENERY_PACK_DISABLED ", "SCENERY_PACK "):
        if stripped.startswith(key):
            return is_absolute_entry(stripped[len(key):].strip())
    return False


def _read_ini(custom_scenery: Path) -> tuple[list[str], str]:
    ini = custom_scenery / INI_NAME
    if not ini.is_file():
        return ["I", "1000 Version", "SCENERY", ""], "\n"
    raw = ini.read_bytes().decode("utf-8", errors="replace")
    newline = "\r\n" if "\r\n" in raw else "\n"
    return raw.splitlines(), newline


def _write_ini(custom_scenery: Path, lines: list[str], newline: str) -> None:
    ini = custom_scenery / INI_NAME
    if ini.is_file():
        shutil.copy2(ini, ini.with_name(INI_NAME + INI_BACKUP_SUFFIX))
    custom_scenery.mkdir(parents=True, exist_ok=True)
    tmp = ini.with_name(INI_NAME + ".tmp")
    tmp.write_bytes((newline.join(lines) + newline).encode("utf-8"))
    tmp.replace(ini)


def update_scenery_packs_ini(custom_scenery: Path, name: str, above: Iterable[str] = (),
                             entry_path: Optional[str] = None) -> None:
    lines, newline = _read_ini(custom_scenery)
    _write_ini(custom_scenery, insert_pack_line(lines, name, above, entry_path), newline)


def remove_from_scenery_packs_ini(custom_scenery: Path, name: str) -> None:
    if not (custom_scenery / INI_NAME).is_file():
        return
    lines, newline = _read_ini(custom_scenery)
    _write_ini(custom_scenery, [line for line in lines if _pack_of(line) != name], newline)


def list_installed(targets: Iterable[InstallTarget]) -> list[tuple[InstallTarget, Path, dict]]:
    found = []
    for target in targets:
        if not target.pack_parent.is_dir():
            continue
        for child in sorted(target.pack_parent.iterdir()):
            manifest = child / MANIFEST
            if child.is_dir() and manifest.is_file():
                try:
                    found.append((target, child, json.loads(manifest.read_text("utf-8"))))
                except (ValueError, OSError):
                    continue
    return found
