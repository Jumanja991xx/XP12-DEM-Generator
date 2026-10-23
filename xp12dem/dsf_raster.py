"""XP12 elevation raster of a DSF, written from our own elevation data.

X-Plane 12 expects every base-mesh DSF to carry an ``elevation`` raster (and
usually ``bathymetry``). Ortho4XP copies them from the tile's X-Plane 12 Global
Scenery DSF - when that DSF is not installed (many XPME users never download
the default scenery of their region), the new tile has none and X-Plane
computes a "detailed DEM" from the triangles while loading. With several
4-million-triangle tiles that takes hours: the loading screen stops at
"finishing asynchronous loading".

Layout (as written by Laminar's DSFTool):

* ``DEMN`` (in ``DEFN``): raster names, each NUL terminated.
* ``DEMS`` (top level, after ``CMDS``): per raster a ``DEMI`` atom
  (u8 version=1, u8 bytes per pixel, u16 flags, u32 width, u32 height,
  f32 scale, f32 offset) followed by a ``DEMD`` atom with the samples,
  little endian, south row first. XP12's default scenery: int16 (flags=1),
  1201 x 1201, scale 1, offset 0.
"""

from __future__ import annotations

import hashlib
import math
import struct
from pathlib import Path
from typing import Optional

import numpy as np

SIZE = 1201
NODATA = -32768


def _atom(tag: str, data: bytes) -> bytes:
    """An atom as stored in the file (tags are written byte-reversed)."""
    return tag[::-1].encode("ascii") + struct.pack("<I", 8 + len(data)) + data


def _sub_atoms(data: bytes) -> list[tuple[str, bytes]]:
    out, i = [], 0
    while i + 8 <= len(data):
        tag = data[i:i + 4][::-1].decode("ascii", "replace")
        length = struct.unpack("<I", data[i + 4:i + 8])[0]
        if length < 8:
            break
        out.append((tag, data[i + 8:i + length]))
        i += length
    return out


def lidar_elevation(dem_file: Path, size: int = SIZE) -> np.ndarray:
    """Heights (size x size, south row first, metres) sampled from a tile's .hgt."""
    n = int(round(math.sqrt(Path(dem_file).stat().st_size // 2)))
    grid = np.memmap(dem_file, ">i2", "r", shape=(n, n))
    idx = np.round(np.arange(size) * (n - 1) / (size - 1)).astype(int)
    heights = np.asarray(grid[np.ix_(idx, idx)], np.int16)[::-1]  # hgt rows: north first
    return heights


def parse_rasters(names: bytes, dems: bytes) -> list[tuple[str, tuple, np.ndarray]]:
    """[(name, (version, bpp, flags, w, h, scale, offset), samples)] from DEMN + DEMS contents."""
    labels = [s.decode("utf-8", "replace") for s in names.split(b"\0") if s]
    atoms = _sub_atoms(dems)
    infos = [d for t, d in atoms if t == "DEMI"]
    datas = [d for t, d in atoms if t == "DEMD"]
    out = []
    for name, info, data in zip(labels, infos, datas):
        header = struct.unpack("<BBHIIff", info[:20])
        _, bpp, flags, w, h = header[:5]
        if bpp == 2 and len(data) >= w * h * 2:
            samples = np.frombuffer(data, "<i2" if flags & 1 else "<u2", w * h).reshape(h, w)
        else:
            samples = np.frombuffer(data, np.uint8)
        out.append((name, header, samples))
    return out


def build(dem_file: Path, names: bytes = b"", dems: bytes = b"") -> tuple[bytes, bytes, str]:
    """DEMN and DEMS contents with the elevation raster from ``dem_file``.

    ``names``/``dems`` are the rasters Ortho4XP took from the Global Scenery
    (may be empty): their bathymetry is kept (never above the new ground - 2 m,
    as Ortho4XP does), their elevation only fills gaps of our data.
    Returns (DEMN, DEMS, short description for the log).
    """
    old = {name: (header, samples) for name, header, samples in parse_rasters(names, dems)}
    size = SIZE
    if "elevation" in old and old["elevation"][0][1] == 2:
        size = old["elevation"][0][3]
    elev = lidar_elevation(dem_file, size).astype(np.int32)
    if "elevation" in old and old["elevation"][1].shape == elev.shape:
        header = old["elevation"][0]
        prev = old["elevation"][1].astype(np.float64) * header[5] + header[6]
        elev = np.where(elev <= NODATA + 1, np.round(prev), elev)
    elev = np.where(elev <= NODATA + 1, 0, elev)
    elev16 = np.clip(elev, -32767, 32767).astype("<i2")

    def raster(samples: np.ndarray) -> bytes:
        h, w = samples.shape
        info = struct.pack("<BBHIIff", 1, 2, 1, w, h, 1.0, 0.0)
        return _atom("DEMI", info) + _atom("DEMD", samples.astype("<i2").tobytes())

    out_names = [b"elevation"]
    out = raster(elev16)
    what = "LiDAR elevation raster"
    bathy = old.get("bathymetry")
    if bathy is not None and bathy[0][1] == 2 and bathy[1].shape == elev16.shape:
        safe = np.minimum(bathy[1].astype(np.int32), elev16.astype(np.int32) - 2)
        out_names.append(b"bathymetry")
        out += raster(np.clip(safe, -32767, 32767))
        what += " + Global Scenery bathymetry"
    else:
        what += " (no Global Scenery DSF for this tile: no bathymetry)"
    return b"".join(n + b"\0" for n in out_names), out, what


# ---------------------------------------------------------------------------
# Existing DSF files
# ---------------------------------------------------------------------------
def _top_atoms(data: bytes) -> list[tuple[str, bytes]]:
    if data[:8] != b"XPLNEDSF":
        raise ValueError("not an uncompressed DSF")
    return _sub_atoms(data[12:-16])


def has_elevation(dsf: Path) -> Optional[bool]:
    """True/False for an uncompressed DSF, None when it cannot be read."""
    try:
        with open(dsf, "rb") as handle:
            head = handle.read(12)
            if head[:8] != b"XPLNEDSF":
                return None
            while True:
                hdr = handle.read(8)
                if len(hdr) < 8:
                    return False
                tag = hdr[:4][::-1]
                length = struct.unpack("<I", hdr[4:])[0]
                if length < 8:
                    return False
                if tag == b"DEFN":
                    body = handle.read(length - 8)
                    names = dict(_sub_atoms(body)).get("DEMN", b"")
                    return b"elevation\0" in names
                handle.seek(length - 8, 1)
    except OSError:
        return None


def add_to_dsf(dsf: Path, dem_file: Path) -> str:
    """Write (or replace) the elevation raster of an uncompressed DSF in place."""
    data = Path(dsf).read_bytes()
    atoms = _top_atoms(data)
    defn = dict(atoms).get("DEFN", b"")
    names = dict(_sub_atoms(defn)).get("DEMN", b"")
    dems = dict(atoms).get("DEMS", b"")
    new_names, new_dems, what = build(dem_file, names, dems)
    body = b""
    for tag, content in atoms:
        if tag == "DEFN":
            subs = [(t, new_names if t == "DEMN" else c) for t, c in _sub_atoms(content)]
            if not any(t == "DEMN" for t, _ in subs):
                subs.append(("DEMN", new_names))
            content = b"".join(_atom(t, c) for t, c in subs)
        elif tag == "DEMS":
            continue
        body += _atom(tag, content)
    body += _atom("DEMS", new_dems)
    out = data[:12] + body
    out += hashlib.md5(out).digest()
    tmp = Path(dsf).with_suffix(".dsf.part")
    tmp.write_bytes(out)
    tmp.replace(dsf)
    return what
