import json

from xp12dem import installer
from xp12dem.xplane import AutoOrthoConfig, read_autoortho_config

HEADER = ["I", "1000 Version", "SCENERY", ""]


def test_insert_above_autoortho_packs():
    lines = HEADER + [
        "SCENERY_PACK Custom Scenery/LOWI/",
        "SCENERY_PACK *GLOBAL_AIRPORTS*",
        "SCENERY_PACK Custom Scenery/yOrtho4XP_Overlays/",
        "SCENERY_PACK Custom Scenery/z_ao_eur/",
        "SCENERY_PACK Custom Scenery/z_ao_na/",
    ]
    out = installer.insert_pack_line(lines, "z_ao_dem_alps")
    assert out.index("SCENERY_PACK Custom Scenery/z_ao_dem_alps/") == out.index(
        "SCENERY_PACK Custom Scenery/z_ao_eur/") - 1


def test_reinstall_moves_and_enables_existing_entry():
    lines = HEADER + [
        "SCENERY_PACK_DISABLED Custom Scenery/z_ao_dem_alps/",
        "SCENERY_PACK *GLOBAL_AIRPORTS*",
        "SCENERY_PACK Custom Scenery/zOrtho4XP_+47+011/",
    ]
    out = installer.insert_pack_line(lines, "z_ao_dem_alps")
    assert sum("z_ao_dem_alps" in line for line in out) == 1
    assert out.index("SCENERY_PACK Custom Scenery/z_ao_dem_alps/") < out.index(
        "SCENERY_PACK Custom Scenery/zOrtho4XP_+47+011/")
    assert "SCENERY_PACK_DISABLED Custom Scenery/z_ao_dem_alps/" not in out


def test_append_when_no_mesh_pack_and_skip_trailing_blank():
    lines = HEADER + ["SCENERY_PACK *GLOBAL_AIRPORTS*", ""]
    out = installer.insert_pack_line(lines, "z_ao_dem_x")
    assert out[-2] == "SCENERY_PACK Custom Scenery/z_ao_dem_x/"
    assert out[-1] == ""


def test_ini_roundtrip_keeps_crlf_and_backup(tmp_path):
    ini = tmp_path / "scenery_packs.ini"
    ini.write_bytes(b"I\r\n1000 Version\r\nSCENERY\r\n\r\nSCENERY_PACK Custom Scenery/z_ao_eur/\r\n")
    installer.update_scenery_packs_ini(tmp_path, "z_ao_dem_t")
    raw = ini.read_bytes()
    assert b"SCENERY_PACK Custom Scenery/z_ao_dem_t/\r\nSCENERY_PACK Custom Scenery/z_ao_eur/\r\n" in raw
    assert (tmp_path / "scenery_packs.ini.xp12dem.bak").is_file()
    installer.remove_from_scenery_packs_ini(tmp_path, "z_ao_dem_t")
    assert b"z_ao_dem_t" not in ini.read_bytes()


def test_assemble_pack_copies_only_needed_files(tmp_path):
    build = tmp_path / "zOrtho4XP_+47+011"
    dsf = build / "Earth nav data" / "+40+010" / "+47+011.dsf"
    dsf.parent.mkdir(parents=True)
    dsf.write_bytes(b"XPLNEDSF")
    (build / "terrain").mkdir()
    (build / "terrain" / "1_2_BI16.ter").write_text("A\n800\nTERRAIN\n")
    (build / "textures").mkdir()
    (build / "textures" / "1_2_BI16.dds").write_bytes(b"x")
    (build / "textures" / "1_2_BI16.png").write_bytes(b"mask")
    (build / "Data+47+011.mesh").write_text("big")
    pack = tmp_path / "pack"
    added = installer.assemble_pack({(47, 11): build}, pack, region_name="Test",
                                    resolution_label="1", mesh_label="High")
    assert added == [(47, 11)]
    assert (pack / "Earth nav data" / "+40+010" / "+47+011.dsf").is_file()
    assert (pack / "terrain" / "1_2_BI16.ter").is_file()
    assert (pack / "textures" / "1_2_BI16.png").is_file()
    assert not (pack / "textures" / "1_2_BI16.dds").exists()
    assert not (pack / "Data+47+011.mesh").exists()
    manifest = json.loads((pack / installer.MANIFEST).read_text())
    assert manifest["tiles"] == [[47, 11]]
    assert "Sonny" in (pack / "README.txt").read_text()


def test_install_into_autoortho(tmp_path):
    xplane = tmp_path / "X-Plane 12"
    (xplane / "Custom Scenery").mkdir(parents=True)
    ao = AutoOrthoConfig(tmp_path / ".autoortho", xplane, xplane / "Custom Scenery")
    target = installer.InstallTarget.for_autoortho(ao, xplane)
    staged = tmp_path / "staged"
    (staged / "terrain").mkdir(parents=True)
    (staged / installer.MANIFEST).write_text("{}")
    dest = installer.install_pack(staged, target, "z_ao_dem_t")
    assert dest == xplane / "Custom Scenery" / "z_autoortho" / "scenery" / "z_ao_dem_t"
    assert "z_ao_dem_t" in (xplane / "Custom Scenery" / "scenery_packs.ini").read_text()
    assert installer.list_installed([target])[0][1] == dest
    installer.uninstall_pack(target, "z_ao_dem_t")
    assert not dest.exists()


def test_read_autoortho_config(tmp_path):
    cfg = tmp_path / ".autoortho"
    cfg.write_text("[general]\nfoo = {bar}\n[paths]\nxplane_path = /xp\nscenery_path = /xp/Custom Scenery\n")
    ao = read_autoortho_config(cfg)
    assert str(ao.ao_scenery_dir).replace("\\", "/").endswith("/xp/Custom Scenery/z_autoortho/scenery")
    assert read_autoortho_config(tmp_path / "missing") is None
