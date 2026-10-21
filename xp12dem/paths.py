"""Locations of bundled resources and per-user working data."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    """Directory that contains ``third_party/`` and ``xp12dem/``.

    In a PyInstaller build this is the unpacked bundle, otherwise the
    repository checkout.
    """
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def ortho4xp_root() -> Path:
    return bundle_root() / "third_party" / "Ortho4XP"


def resources_dir() -> Path:
    return Path(__file__).resolve().parent / "resources"


def data_root() -> Path:
    """Writable per-user directory for downloads, caches and build output.

    Can be overridden with the ``XP12DEM_DATA`` environment variable.
    """
    override = os.environ.get("XP12DEM_DATA")
    if override:
        return Path(override)
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "XP12DEMGenerator"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "XP12DEMGenerator"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "XP12DEMGenerator"


def dem_cache_dir() -> Path:
    return data_root() / "dem"


def work_dir() -> Path:
    """Root of the Ortho4XP working tree (OSM cache, tiles, tmp...)."""
    return data_root() / "ortho4xp"


def settings_file() -> Path:
    return data_root() / "settings.json"


def log_dir() -> Path:
    return data_root() / "logs"
