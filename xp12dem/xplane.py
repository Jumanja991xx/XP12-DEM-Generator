"""Find X-Plane 12 and AutoOrtho on the user's machine."""

from __future__ import annotations

import configparser
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

GLOBAL_SCENERY_REL = Path("Global Scenery") / "X-Plane 12 Global Scenery"


def _install_list_files() -> list[Path]:
    """X-Plane writes ``x-plane_install_12.txt`` listing every install."""
    files: list[Path] = []
    if sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA")
        if local:
            files.append(Path(local) / "x-plane_install_12.txt")
    elif sys.platform == "darwin":
        files.append(Path.home() / "Library" / "Preferences" / "x-plane_install_12.txt")
    else:
        files.append(Path.home() / ".x-plane" / "x-plane_install_12.txt")
    return files


def is_xplane12(path: Path) -> bool:
    return (path / "Custom Scenery").is_dir() and (path / GLOBAL_SCENERY_REL).is_dir()


def find_xplane12_installs() -> list[Path]:
    found: list[Path] = []
    for listing in _install_list_files():
        try:
            lines = listing.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line in lines:
            candidate = Path(line.strip())
            if line.strip() and is_xplane12(candidate) and candidate not in found:
                found.append(candidate)
    if sys.platform.startswith("win"):
        for drive in "CDEFGH":
            for sub in ("X-Plane 12", r"SteamLibrary\steamapps\common\X-Plane 12",
                        r"Program Files (x86)\Steam\steamapps\common\X-Plane 12"):
                candidate = Path(f"{drive}:\\") / sub
                if candidate not in found and is_xplane12(candidate):
                    found.append(candidate)
    return found


@dataclass
class AutoOrthoConfig:
    config_file: Path
    xplane_path: Optional[Path]
    scenery_path: Optional[Path]

    @property
    def ao_scenery_dir(self) -> Optional[Path]:
        """Directory whose sub-folders AutoOrtho mounts into Custom Scenery."""
        if not self.scenery_path:
            return None
        return self.scenery_path / "z_autoortho" / "scenery"

    def installed_packs(self) -> list[str]:
        base = self.ao_scenery_dir
        if not base or not base.is_dir():
            return []
        return sorted(p.name for p in base.iterdir() if p.is_dir())


def autoortho_config_file() -> Path:
    return Path.home() / ".autoortho"


def read_autoortho_config(path: Optional[Path] = None) -> Optional[AutoOrthoConfig]:
    """Parse AutoOrtho's (and AutoOrtho Continued's) ``~/.autoortho`` file."""
    path = path or autoortho_config_file()
    if not path.is_file():
        return None
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read(path, encoding="utf-8")
    except (configparser.Error, UnicodeDecodeError):
        return None

    def get(key: str) -> Optional[Path]:
        value = parser.get("paths", key, fallback="").strip()
        return Path(os.path.expanduser(value)) if value else None

    return AutoOrthoConfig(config_file=path, xplane_path=get("xplane_path"),
                           scenery_path=get("scenery_path"))


def global_scenery_dir(xplane: Path) -> Path:
    return xplane / GLOBAL_SCENERY_REL
