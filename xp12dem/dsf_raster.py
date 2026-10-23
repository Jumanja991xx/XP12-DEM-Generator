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


def _raster_atoms(header: tuple, samples: np.ndarray) -> bytes:
    version, bpp, flags, w, h, scale, offset = header
    info = struct.pack("<BBHIIff", version, bpp, flags, w, h, scale, offset)
    dtype = "<i2" if flags & 1 else "<u2"
    return _atom("DEMI", info) + _atom("DEMD", samples.astype(dtype).tobytes())


def build(dem_file: Path, names: bytes = b"", dems: bytes = b"") -> tuple[bytes, bytes, str]:
    """DEMN and DEMS contents with the elevation raster from ``dem_file``.

    ``names``/``dems`` are the rasters taken from the Global Scenery (or XPME's DSF of
    the tile; may be empty). All of them are kept as they are - X-Plane 12 expects
    the full set - except: elevation gets our heights (theirs only fill gaps),
    bathymetry stays at least 2 m below the new ground (as Ortho4XP does).
    Returns (DEMN, DEMS, short description for the log).
    """
    old = parse_rasters(names, dems)
    labels = [s.decode("utf-8", "replace") for s in names.split(b"\0") if s]
    infos = [d for t, d in _sub_atoms(dems) if t == "DEMI"]
    datas = [d for t, d in _sub_atoms(dems) if t == "DEMD"]
    by_name = {name: (header, samples) for name, header, samples in old}
    size = SIZE
    if "elevation" in by_name and by_name["elevation"][0][1] == 2:
        size = by_name["elevation"][0][3]
    elev = lidar_elevation(dem_file, size).astype(np.int32)
    if "elevation" in by_name and by_name["elevation"][1].shape == elev.shape:
        header = by_name["elevation"][0]
        prev = by_name["elevation"][1].astype(np.float64) * header[5] + header[6]
        elev = np.where(elev <= NODATA + 1, np.round(prev), elev)
    elev = np.clip(np.where(elev <= NODATA + 1, 0, elev), -32767, 32767)
    plain = (1, 2, 1, size, size, 1.0, 0.0)

    out_names, out = [], b""
    kept = []
    for i, name in enumerate(labels):
        if name == "elevation" or i >= len(infos) or i >= len(datas):
            continue
        header, samples = by_name.get(name, (None, None))
        if name == "bathymetry" and header is not None and header[1] == 2 and samples.shape == elev.shape:
            safe = np.minimum(samples.astype(np.int32) * header[5] + header[6], elev - 2)
            out_names.append(name)
            out += _raster_atoms(plain, np.clip(np.round(safe), -32767, 32767))
        else:  # any other raster: untouched
            out_names.append(name)
            out += _atom("DEMI", infos[i]) + _atom("DEMD", datas[i])
        kept.append(name)
    if "bathymetry" not in kept:
        # no source for it: water bottoms 2 m under the surface (better than none -
        # without it X-Plane derives it while loading)
        out_names.insert(0, "bathymetry")
        out = _raster_atoms(plain, elev - 2) + out
        kept.insert(0, "bathymetry (2 m below ground)")
    out_names.insert(0, "elevation")
    out = _raster_atoms(plain, elev) + out
    what = "rasters: LiDAR elevation + " + ", ".join(kept)
    return b"".join(n.encode() + b"\0" for n in out_names), out, what


# ---------------------------------------------------------------------------
# Existing DSF files
# ---------------------------------------------------------------------------
def _top_atoms(data: bytes) -> list[tuple[str, bytes]]:
    if data[:8] != b"XPLNEDSF":
        raise ValueError("not an uncompressed DSF")
    return _sub_atoms(data[12:-16])


def raster_names(dsf: Path) -> Optional[list[str]]:
    """Raster names of an uncompressed DSF, None when it cannot be read."""
    try:
        with open(dsf, "rb") as handle:
            head = handle.read(12)
            if head[:8] != b"XPLNEDSF":
                return None
            while True:
                hdr = handle.read(8)
                if len(hdr) < 8:
                    return []
                tag = hdr[:4][::-1]
                length = struct.unpack("<I", hdr[4:])[0]
                if length < 8:
                    return []
                if tag == b"DEFN":
                    body = handle.read(length - 8)
                    names = dict(_sub_atoms(body)).get("DEMN", b"")
                    return [n.decode("utf-8", "replace") for n in names.split(b"\0") if n]
                handle.seek(length - 8, 1)
    except OSError:
        return None


def has_elevation(dsf: Path) -> Optional[bool]:
    """Does the DSF carry the rasters X-Plane 12 needs (elevation and bathymetry)?
    None when it cannot be read."""
    names = raster_names(dsf)
    return None if names is None else {"elevation", "bathymetry"} <= set(names)


def rasters_of(dsf: Path) -> tuple[bytes, bytes]:
    """(DEMN, DEMS) contents of an uncompressed DSF."""
    atoms = dict(_top_atoms(Path(dsf).read_bytes()))
    return dict(_sub_atoms(atoms.get("DEFN", b""))).get("DEMN", b""), atoms.get("DEMS", b"")


def add_to_dsf(dsf: Path, dem_file: Path, donor: Optional[Path] = None) -> str:
    """Write (or replace) the elevation raster of an uncompressed DSF in place.
    ``donor``: another DSF of the same tile (XPME's) whose other rasters are used
    when this one has none."""
    data = Path(dsf).read_bytes()
    atoms = _top_atoms(data)
    defn = dict(atoms).get("DEFN", b"")
    names = dict(_sub_atoms(defn)).get("DEMN", b"")
    dems = dict(atoms).get("DEMS", b"")
    if donor is not None and b"bathymetry\0" not in names:
        try:
            names, dems = rasters_of(donor)
        except (OSError, ValueError):
            pass
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
