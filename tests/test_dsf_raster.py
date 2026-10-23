import hashlib
import struct

import numpy as np

from xp12dem import dsf_raster as d
from xp12dem.inventory import add_rasters, read_pack


def _hgt(path, n=1201):
    z = np.zeros((n, n), np.int16)
    z[: n // 2] = 2000  # northern half high (hgt rows: north first)
    z[n // 2:] = 500
    z.astype(">i2").tofile(path)
    return path


def _dsf(path, names=b"", dems=b""):
    body = d._atom("HEAD", d._atom("PROP", b"sim/west\x0011\x00"))
    body += d._atom("DEFN", d._atom("TERT", b"") + d._atom("OBJT", b"") + d._atom("POLY", b"")
                    + d._atom("NETW", b"") + d._atom("DEMN", names))
    body += d._atom("GEOD", b"") + d._atom("CMDS", b"\x02\x00\x00\x00\x00")
    if dems:
        body += d._atom("DEMS", dems)
    data = b"XPLNEDSF" + struct.pack("<I", 1) + body
    path.write_bytes(data + hashlib.md5(data).digest())
    return path


def _read(path):
    data = path.read_bytes()
    assert hashlib.md5(data[:-16]).digest() == data[-16:]
    atoms = dict(d._top_atoms(data))
    names = dict(d._sub_atoms(atoms["DEFN"]))["DEMN"]
    return d.parse_rasters(names, atoms["DEMS"])


def test_adds_lidar_elevation_raster(tmp_path):
    dsf = _dsf(tmp_path / "+47+011.dsf")
    assert d.has_elevation(dsf) is False
    d.add_to_dsf(dsf, _hgt(tmp_path / "N47E011.hgt"))
    assert d.has_elevation(dsf) is True
    (name, header, elev), = _read(dsf)
    assert name == "elevation" and header[:5] == (1, 2, 1, 1201, 1201)
    assert elev[0, 0] == 500 and elev[-1, 0] == 2000  # DSF rasters: south row first


def test_keeps_global_bathymetry_below_new_ground(tmp_path):
    old = np.full((1201, 1201), 100, "<i2")
    info = struct.pack("<BBHIIff", 1, 2, 1, 1201, 1201, 1.0, 0.0)
    dems = (d._atom("DEMI", info) + d._atom("DEMD", old.tobytes())) * 2
    dsf = _dsf(tmp_path / "a.dsf", b"elevation\0bathymetry\0", dems)
    text = d.add_to_dsf(dsf, _hgt(tmp_path / "N47E011.hgt"))
    assert "bathymetry" in text
    rasters = _read(dsf)
    assert [r[0] for r in rasters] == ["elevation", "bathymetry"]
    assert rasters[0][2][0, 0] == 500 and rasters[1][2].max() == 100


def test_inventory_flags_and_repairs_tiles_without_raster(tmp_path):
    pack = tmp_path / "z_dem_mesh_test"
    (pack / "Earth nav data" / "+40+010").mkdir(parents=True)
    _dsf(pack / "Earth nav data" / "+40+010" / "+47+011.dsf")
    cache = tmp_path / "dem"
    (cache / "1s").mkdir(parents=True)
    _hgt(cache / "1s" / "N47E011.hgt")
    info = read_pack(pack)
    assert info.no_raster == [(47, 11)]
    assert add_rasters(info, cache) == []
    assert read_pack(pack).no_raster == []


def test_xpme_high_zoom_blocks(tmp_path, monkeypatch):
    from xp12dem import paths, pipeline

    monkeypatch.setattr(paths, "work_dir", lambda: tmp_path / "work")
    xpme = tmp_path / "XPME_Europe"
    (xpme / "Earth nav data" / "+40+010").mkdir(parents=True)
    dsf = _dsf(xpme / "Earth nav data" / "+40+010" / "+47+011.dsf")
    data = dsf.read_bytes()[:-16]
    tert = d._atom("TERT", b"terrain/22928_34688_BI16.ter\0terrain/91712_138752_BI18.ter\0")
    data = data.replace(d._atom("TERT", b""), tert)
    # fix the DEFN length after growing TERT
    i = data.index(b"NFED")
    defn_len = struct.unpack("<I", data[i + 4:i + 8])[0] + len(tert) - 8
    data = data[:i + 4] + struct.pack("<I", defn_len) + data[i + 8:]
    dsf.write_bytes(data + hashlib.md5(data).digest())
    pipe = pipeline.Pipeline.__new__(pipeline.Pipeline)
    pipe._xpme_packs = {(47, 11): ("XPME_Europe", xpme)}
    assert pipe._xpme_high_zoom((47, 11)) == ["91712_138752_BI18.dds"]
