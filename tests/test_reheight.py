import struct

import numpy as np
import pytest

from xp12dem import installer
from xp12dem import reheight as RH


def _write_dsf(path, props):
    """Minimal uncompressed DSF with just a HEAD/PROP atom."""
    body = b"".join(k.encode() + b"\0" + v.encode() + b"\0" for k, v in props.items())
    prop = b"PORP" + struct.pack("<I", 8 + len(body)) + body
    head = b"DAEH" + struct.pack("<I", 8 + len(prop)) + prop
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"XPLNEDSF" + struct.pack("<I", 1) + head + b"\0" * 16)


def _xplane(tmp_path, packs):
    xp = tmp_path / "X-Plane 12"
    (xp / "Custom Scenery").mkdir(parents=True)
    (xp / "Global Scenery" / "X-Plane 12 Global Scenery").mkdir(parents=True)
    lines = ["I", "1000 Version", "SCENERY", "", "SCENERY_PACK *GLOBAL_AIRPORTS*"]
    lines += [f"SCENERY_PACK Custom Scenery/{p}/" for p in packs]
    (xp / "Custom Scenery" / "scenery_packs.ini").write_text("\n".join(lines) + "\n")
    return xp


def test_find_source_skips_overlays_and_prefers_priority(tmp_path):
    xp = _xplane(tmp_path, ["simHeaven", "XPME_Europe", "z_ao_eur"])
    rel = "Earth nav data/+40+010/+47+011.dsf"
    _write_dsf(xp / "Custom Scenery" / "simHeaven" / rel, {"sim/overlay": "1"})
    _write_dsf(xp / "Custom Scenery" / "XPME_Europe" / rel, {"sim/west": "11"})
    _write_dsf(xp / "Custom Scenery" / "z_ao_eur" / rel, {"sim/west": "11"})
    src = RH.find_mesh_source((47, 11), xp)
    assert (src.kind, src.name) == ("pack", "XPME_Europe")
    assert src.unreadable_above == []


def test_find_source_uses_autoortho_storage_and_reports_empty_mounts(tmp_path):
    xp = _xplane(tmp_path, ["XPME_Asia", "z_ao_eur"])
    (xp / "Custom Scenery" / "XPME_Asia").mkdir()  # virtual mount, not running
    ao_store = tmp_path / "ao"
    _write_dsf(ao_store / "z_ao_eur" / "Earth nav data/+40+010/+47+011.dsf", {})
    src = RH.find_mesh_source((47, 11), xp, extra_roots=[ao_store])
    assert src.name == "z_ao_eur" and src.read_root == ao_store / "z_ao_eur"
    assert src.unreadable_above == ["XPME_Asia"]


def test_find_source_refuses_silent_fallback_to_global(tmp_path):
    xp = _xplane(tmp_path, ["XPME_Europe"])
    (xp / "Custom Scenery" / "XPME_Europe").mkdir()
    with pytest.raises(RH.SourceUnavailable):
        RH.find_mesh_source((47, 11), xp)
    src = RH.find_mesh_source((47, 11), xp, strict=False)
    assert src.kind == "global"


def test_find_source_global_when_no_pack(tmp_path):
    xp = _xplane(tmp_path, [])
    src = RH.find_mesh_source((46, 7), xp)
    assert src.kind == "global"
    assert src.dsf.as_posix().endswith("X-Plane 12 Global Scenery/Earth nav data/+40+000/+46+007.dsf")


def test_rewrite_ter_points_textures_to_source_pack():
    ter = ("A\n800\nTERRAIN\n\nLOAD_CENTER 47.0 11.0 6544 4096\n"
           "BASE_TEX_NOWRAP ../textures/1_2_BI16.dds\nBORDER_TEX ../textures/1_2_BI16.png\n"
           "DECAL_LIB lib/g10/decals/maquify_2_green_key.dcl\nNO_ALPHA\n")
    out = RH.rewrite_ter(ter, "terrain/1_2_BI16.ter", "XPME_Europe")
    assert "BASE_TEX_NOWRAP ../../XPME_Europe/textures/1_2_BI16.dds" in out
    assert "BORDER_TEX ../../XPME_Europe/textures/1_2_BI16.png" in out
    assert "DECAL_LIB lib/g10/decals/maquify_2_green_key.dcl" in out
    assert "LOAD_CENTER 47.0 11.0 6544 4096" in out


def _field(**kwargs):
    grid = np.full((121, 121), 1000.0, dtype=np.float32)
    grid[:, :] += np.arange(121)[None, :]  # rises towards the east
    return RH.HeightField(47, 11, grid, **kwargs)


def test_height_field_sampling_and_normals():
    hf = _field(feather_edges={k: False for k in "nsew"})
    lon = np.array([11.5]); lat = np.array([47.5])
    assert hf.sample(lon, lat)[0] == pytest.approx(1060.0)
    east, south = hf.normals(lon, lat)
    assert east[0] < 0 and abs(south[0]) < 1e-6  # surface faces west


def test_weights_keep_runways_and_feather_edges():
    rwy = RH.Runway(47.5, 11.4, 47.5, 11.45, 22.5)
    hf = _field(runways=[rwy], feather_edges={"n": True, "s": False, "e": False, "w": False})
    w = hf.weight(np.array([11.42, 11.42, 11.7, 11.7]), np.array([47.5, 47.52, 47.5, 47.999]))
    assert w[0] == 0.0          # on the runway
    assert 0.0 < w[1] <= 1.0    # ~2 km away: LiDAR again
    assert w[2] == 1.0          # open terrain
    assert w[3] < 0.05          # next to the feathered north edge


def test_reheight_text_keeps_water_and_updates_land(tmp_path):
    src = tmp_path / "src.txt"
    src.write_text(
        "I\n800\nDSF2TEXT\n"
        "TERRAIN_DEF terrain_Water\n"
        "TERRAIN_DEF lib/g10/terrain10/fake.ter\n"
        "BEGIN_PATCH 0 0.0 -1.0 1 5\nBEGIN_PRIMITIVE 0\n"
        "PATCH_VERTEX 11.5 47.5 500.0 0.0 0.0\n"
        "PATCH_VERTEX 11.6 47.5 500.0 0.0 0.0\n"
        "PATCH_VERTEX 11.5 47.6 500.0 0.0 0.0\n"
        "END_PRIMITIVE\nEND_PATCH\n"
        "BEGIN_PATCH 1 0.0 -1.0 1 5\nBEGIN_PRIMITIVE 0\n"
        "PATCH_VERTEX 11.5 47.5 500.0 0.0 0.0\n"
        "PATCH_VERTEX 11.3 47.3 900.0 0.1 0.1\n"
        "PATCH_VERTEX 11.3 47.4 -32768.0 0.1 0.1\n"
        "END_PRIMITIVE\nEND_PATCH\n")
    hf = _field(feather_edges={k: False for k in "nsew"})
    source = RH.MeshSource("global", "X-Plane 12 Global Scenery", tmp_path / "x.dsf", tmp_path)
    dst = tmp_path / "dst.txt"
    stats = RH.reheight_dsf_text(src, dst, hf, source, tmp_path / "pack", tmp_path)
    rows = [line.split() for line in dst.read_text().splitlines() if line.startswith("PATCH_VERTEX")]
    elev = [float(r[3]) for r in rows]
    assert elev[:3] == [500.0, 500.0, 500.0]          # water patch untouched
    assert elev[3] == 500.0                            # shoreline vertex shared with water
    assert elev[4] == pytest.approx(1036.0, abs=0.01)  # land: LiDAR height
    assert elev[5] == -32768.0                         # "use raster" marker kept
    assert stats.changed == 1


def test_insert_pack_line_above_source():
    lines = ["I", "1000 Version", "SCENERY", "", "SCENERY_PACK Custom Scenery/simHeaven/",
             "SCENERY_PACK Custom Scenery/XPME_Europe/", "SCENERY_PACK Custom Scenery/z_ao_eur/"]
    out = installer.insert_pack_line(lines, "z_dem_alps", above={"XPME_Europe"})
    assert out.index("SCENERY_PACK Custom Scenery/z_dem_alps/") == out.index(
        "SCENERY_PACK Custom Scenery/XPME_Europe/") - 1


def test_pack_names_per_method():
    assert installer.pack_name("alps", "improve") == "z_dem_alps"
    assert installer.pack_name("alps", "rebuild") == "z_ao_dem_alps"


def test_find_source_never_chains_improved_packs(tmp_path):
    xp = _xplane(tmp_path, ["z_dem_old", "my_lidar", "XPME_Europe"])
    rel = "Earth nav data/+40+010/+47+011.dsf"
    _write_dsf(xp / "Custom Scenery" / "z_dem_old" / rel, {})
    _write_dsf(xp / "Custom Scenery" / "my_lidar" / rel, {})
    (xp / "Custom Scenery" / "my_lidar" / "xp12dem_pack.json").write_text('{"method": "improve"}')
    _write_dsf(xp / "Custom Scenery" / "XPME_Europe" / rel, {})
    assert RH.find_mesh_source((47, 11), xp).name == "XPME_Europe"
