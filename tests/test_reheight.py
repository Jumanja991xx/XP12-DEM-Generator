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


def test_absolute_pack_paths_outside_xplane(tmp_path):
    xp = _xplane(tmp_path, [])
    external = tmp_path / "XPME Data" / "scenery"
    _write_dsf(external / "XPME_Europe" / "Earth nav data/+40+010/+47+011.dsf", {})
    (external / "XPME_Africa").mkdir(parents=True)
    ini = xp / "Custom Scenery" / "scenery_packs.ini"
    ini.write_text(ini.read_text()
                   + f"SCENERY_PACK {external.as_posix()}/XPME_Africa/\n"
                   + f"SCENERY_PACK {str(external / 'XPME_Europe')}\\\n")
    entries = RH.pack_entries(xp / "Custom Scenery")
    assert [name for name, _ in entries] == ["XPME_Africa", "XPME_Europe"]
    src = RH.find_mesh_source((47, 11), xp)
    assert src.name == "XPME_Europe" and src.pack_root == external / "XPME_Europe"
    assert src.unreadable_above == ["XPME_Africa"]


def test_unavailable_error_lists_checked_paths(tmp_path):
    xp = _xplane(tmp_path, ["XPME_Europe"])
    with pytest.raises(RH.SourceUnavailable) as err:
        RH.find_mesh_source((47, 11), xp)
    assert "Checked:" in str(err.value) and "XPME_Europe" in str(err.value)


def test_external_install_target_writes_absolute_entry(tmp_path):
    xp = tmp_path / "X-Plane 12"
    (xp / "Custom Scenery").mkdir(parents=True)
    parent = tmp_path / "XPME Data"
    target = installer.InstallTarget.beside(parent, xp)
    assert target.ini_entry("z_dem_x") == f"{parent.as_posix()}/z_dem_x/"
    lines = ["I", "1000 Version", "SCENERY", "", f"SCENERY_PACK {parent.as_posix()}/XPME_Europe/"]
    out = installer.insert_pack_line(lines, "z_dem_x", {"XPME_Europe"}, target.ini_entry("z_dem_x"))
    assert out[4] == f"SCENERY_PACK {parent.as_posix()}/z_dem_x/"


DSF_TEXT = """I
800
DSF2TEXT

PROPERTY sim/planet earth
PROPERTY sim/overlay 0
PROPERTY sim/west 11
PROPERTY sim/east 12
PROPERTY sim/north 46
PROPERTY sim/south 45
DIVISIONS 8
HEIGHTS 0.05000 0.0
TERRAIN_DEF terrain_Water
TERRAIN_DEF lib/g10/terrain10/fake.ter
BEGIN_PATCH 1 0.0 -1.0 1 5
BEGIN_PRIMITIVE 0
PATCH_VERTEX 11.95 45.09 1.0 0.0 0.0
PATCH_VERTEX 11.96 45.09 2.0 0.0 0.0
PATCH_VERTEX 11.95 45.10 1.5 0.0 0.0
PATCH_VERTEX 11.20 45.80 3000.0 0.0 0.0
PATCH_VERTEX 11.21 45.80 3000.0 0.0 0.0
PATCH_VERTEX 11.20 45.81 3000.0 0.0 0.0
END_PRIMITIVE
END_PATCH
"""


def _dsftool():
    from xp12dem import paths

    tool, _ = RH.platform_tools(paths.ortho4xp_root())
    try:
        RH.run_dsftool(tool, "--version")
    except Exception:
        pass
    import os
    if not os.path.isfile(tool) or not os.access(tool, os.X_OK):
        pytest.skip("DSFTool not runnable here")
    return tool


@pytest.mark.parametrize("heights_line", ["HEIGHTS 0.05000 0.0\n", ""])
def test_heights_below_sea_level_and_above_range_are_written(tmp_path, heights_line):
    """Po delta / Netherlands: LiDAR below 0 m, while the source mesh never was."""
    tool = _dsftool()
    text = tmp_path / "src.txt"
    text.write_text(DSF_TEXT.replace("HEIGHTS 0.05000 0.0\n", heights_line))
    src_dsf = tmp_path / "src" / "+45+011.dsf"
    src_dsf.parent.mkdir()
    RH.run_dsftool(tool, "--text2dsf", str(text), str(src_dsf))

    grid = np.full((121, 121), -1.5, dtype=np.float32)  # reclaimed land
    grid[:30, :30] = 3400.0  # north-west: higher than 0.05 * 65535 = 3276 m
    hf = RH.HeightField(45, 11, grid, feather_edges={k: False for k in "nsew"})
    source = RH.MeshSource("pack", "XPME_Europe", src_dsf, tmp_path)
    out_pack = tmp_path / "pack"
    stats = RH.reheight_tile((45, 11), source, hf, out_pack, tmp_path / "work", tool, None,
                             log=lambda *_: None)
    assert stats.heights_range[0] < -1.5 and stats.clamped == 0
    result = tmp_path / "result.txt"
    RH.run_dsftool(tool, "--dsf2text", str(out_pack / "Earth nav data" / "+40+010" / "+45+011.dsf"),
                   str(result))
    elev = [float(line.split()[3]) for line in result.read_text().splitlines()
            if line.startswith("PATCH_VERTEX")]
    assert min(elev) == pytest.approx(-1.5, abs=0.1)
    assert max(elev) == pytest.approx(3400.0, abs=0.5)


def test_tight_heights():
    step, offset = RH.tight_heights(-1.5, 3400.2)
    assert offset == -4.0 and offset + step * 65535 == pytest.approx(3403.0)
    assert step < 0.06  # far finer than DSFTool's default 1 m steps


def test_raster_marker_vertices_still_fit(tmp_path):
    tool = _dsftool()
    text = tmp_path / "src.txt"
    text.write_text(DSF_TEXT.replace("HEIGHTS 0.05000 0.0", "HEIGHTS 1.0 -32768.0").replace(
        "PATCH_VERTEX 11.20 45.80 3000.0", "PATCH_VERTEX 11.20 45.80 -32768.0"))
    src_dsf = tmp_path / "src" / "+45+011.dsf"
    src_dsf.parent.mkdir()
    RH.run_dsftool(tool, "--text2dsf", str(text), str(src_dsf))
    hf = RH.HeightField(45, 11, np.full((121, 121), -1.5, dtype=np.float32),
                        feather_edges={k: False for k in "nsew"})
    source = RH.MeshSource("pack", "XPME_Europe", src_dsf, tmp_path)
    RH.reheight_tile((45, 11), source, hf, tmp_path / "pack", tmp_path / "work", tool, None,
                     log=lambda *_: None)
    result = tmp_path / "result.txt"
    RH.run_dsftool(tool, "--dsf2text",
                   str(tmp_path / "pack" / "Earth nav data" / "+40+010" / "+45+011.dsf"), str(result))
    elev = sorted(float(line.split()[3]) for line in result.read_text().splitlines()
                  if line.startswith("PATCH_VERTEX"))
    assert elev[0] == pytest.approx(-32768.0, abs=1) and elev[1] == pytest.approx(-1.5, abs=0.6)


def test_fallback_limits_heights_when_dsftool_refuses(tmp_path, monkeypatch):
    tool = _dsftool()
    text = tmp_path / "src.txt"
    text.write_text(DSF_TEXT)
    src_dsf = tmp_path / "src" / "+45+011.dsf"
    src_dsf.parent.mkdir()
    RH.run_dsftool(tool, "--text2dsf", str(text), str(src_dsf))
    real = RH.run_dsftool
    calls = []

    def flaky(tool_path, *args):
        if args[0] == "--text2dsf":
            calls.append(args)
            if len(calls) == 1:
                raise RuntimeError('DSFTool --text2dsf failed: x\nERROR: !"ERROR: could not sink vertex:\\n"')
        return real(tool_path, *args)

    monkeypatch.setattr(RH, "run_dsftool", flaky)
    logs = []
    hf = RH.HeightField(45, 11, np.full((121, 121), -1.5, dtype=np.float32),
                        feather_edges={k: False for k in "nsew"})
    source = RH.MeshSource("pack", "XPME_Europe", src_dsf, tmp_path)
    stats = RH.reheight_tile((45, 11), source, hf, tmp_path / "pack", tmp_path / "work", tool, None,
                             log=logs.append)
    assert len(calls) == 2 and stats.clamped > 0
    assert any("could not sink vertex" in line and "retrying" in line for line in logs)


def test_short_error_for_dsftool_dump():
    from xp12dem.pipeline import short_error

    message = ('RuntimeError: DSFTool --text2dsf failed: ator\\ortho4xp\\+45+011.dsf\n'
               'Scanning for dimension properties...\n 11.95,45.08,-1.53\n'
               'ERROR: !"ERROR: could not sink vertex:\\n"\n(D:\\a\\DSFLibWrite.cpp, 834.)')
    assert short_error(message) == "RuntimeError: DSFTool --text2dsf failed: ERROR: could not sink vertex"
    assert short_error("No elevation data for N47E011") == "No elevation data for N47E011"


def test_ter_paths_reach_xpme_from_custom_scenery(tmp_path):
    text = "A\n800\nTERRAIN\nBASE_TEX ../textures/24000_34000_BI16.dds\nNO_ALPHA\n"
    source_dir = tmp_path / "Users" / "me" / "Documents" / "XPMapEnhancement" / "BasePack" / "XPME_Europe"
    install_dir = tmp_path / "X-Plane 12" / "Custom Scenery" / "z_dem_tyrol"
    out = RH.rewrite_ter(text, "terrain/24000_34000_BI16.ter", "XPME_Europe", source_dir, install_dir)
    ref = [l for l in out.splitlines() if l.startswith("BASE_TEX")][0].split(None, 1)[1]
    resolved = (install_dir / "terrain" / ref).resolve()
    assert resolved == (source_dir / "textures" / "24000_34000_BI16.dds").resolve()
    # side by side (no folders given) keeps the old short form
    assert "BASE_TEX ../../XPME_Europe/textures/24000_34000_BI16.dds" in RH.rewrite_ter(
        text, "terrain/24000_34000_BI16.ter", "XPME_Europe")


def test_relocating_a_pack_keeps_texture_paths_and_merges(tmp_path):
    basepack = tmp_path / "XPME" / "BasePack"
    old = basepack / "z_dem_my_region"
    (old / "terrain").mkdir(parents=True)
    (old / "terrain" / "a.ter").write_text("BASE_TEX ../../XPME_Europe/textures/a.dds\n")
    dsf = old / "Earth nav data" / "+40+010" / "+47+010.dsf"
    dsf.parent.mkdir(parents=True)
    dsf.write_bytes(b"x")
    (old / "xp12dem_pack.json").write_text('{"tiles": [[47, 10]], "sources": {"+47+010": "XPME_Europe"}}')
    custom = tmp_path / "X-Plane 12" / "Custom Scenery"
    existing = custom / "z_dem_my_region"
    (existing / "Earth nav data" / "+40+010").mkdir(parents=True)
    (existing / "Earth nav data" / "+40+010" / "+47+011.dsf").write_bytes(b"y")
    (existing / "xp12dem_pack.json").write_text('{"tiles": [[47, 11]], "sources": {"+47+011": "XPME_Europe"}}')
    installer.relocate_pack(old, existing)
    assert not old.exists()
    ref = (existing / "terrain" / "a.ter").read_text().split()[1]
    assert (existing / "terrain" / ref).resolve() == (basepack / "XPME_Europe" / "textures" / "a.dds").resolve()
    import json
    manifest = json.loads((existing / "xp12dem_pack.json").read_text())
    assert manifest["tiles"] == [[47, 10], [47, 11]] and set(manifest["sources"]) == {"+47+010", "+47+011"}
    assert (existing / "Earth nav data" / "+40+010" / "+47+010.dsf").is_file()
