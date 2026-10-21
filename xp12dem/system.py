"""Hardware facts used to pick sensible defaults."""

from __future__ import annotations

import ctypes
import os
import sys
from typing import Optional

# Rough peak memory of one tile job (GB) and RAM left for Windows / X-Plane tools.
RAM_PER_TILE = {"improve": 2.0, "rebuild": 3.0}
RAM_RESERVE = 4.0
MAX_PARALLEL = {"improve": 6, "rebuild": 4}


def total_ram_gb() -> Optional[float]:
    try:
        if sys.platform.startswith("win"):
            class MemoryStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(MemoryStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
                return status.ullTotalPhys / 1024 ** 3
            return None
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3
    except (AttributeError, OSError, ValueError):
        return None


def recommended_parallel(method: str, cores: Optional[int] = None,
                         ram_gb: Optional[float] = None) -> int:
    """How many tiles to build at once without making the PC sluggish.

    Each tile job is mostly single threaded, so CPU cores limit little;
    memory is what runs out first.
    """
    cores = cores if cores is not None else (os.cpu_count() or 2)
    ram_gb = ram_gb if ram_gb is not None else (total_ram_gb() or 8.0)
    by_cpu = max(1, cores // 2)
    by_ram = int(max(0.0, ram_gb - RAM_RESERVE) // RAM_PER_TILE.get(method, 3.0))
    return max(1, min(by_cpu, by_ram, MAX_PARALLEL.get(method, 4)))
