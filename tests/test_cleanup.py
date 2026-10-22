from xp12dem import cleanup, inventory


def test_delete_local_items_and_packs(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setenv("XP12DEM_DATA", str(data))
    (data / "dem" / "1s" / "sonny").mkdir(parents=True)
    (data / "dem" / "1s" / "sonny" / "N47E011.hgt").write_bytes(b"\0" * 5000)
    (data / "maptiles" / "opentopomap").mkdir(parents=True)
    (data / "settings.json").write_text("{}")
    items = {i.key: i for i in cleanup.local_items()}
    assert items["dem"].size == 5000 and items["work"].size == 0
    assert not items["settings"].default

    xp = tmp_path / "X-Plane 12"
    custom = xp / "Custom Scenery"
    dsf = custom / "z_dem_tyrol" / "Earth nav data" / "+40+010"
    dsf.mkdir(parents=True)
    (dsf / "+47+011.dsf").write_bytes(b"x")
    other = custom / "LOWI" / "Earth nav data"
    other.mkdir(parents=True)
    (custom / "scenery_packs.ini").write_text(
        "I\n1000 Version\nSCENERY\n\nSCENERY_PACK Custom Scenery/LOWI/\n"
        "SCENERY_PACK Custom Scenery/z_dem_tyrol/\n", encoding="utf-8")

    assert cleanup.delete_items([items["dem"], items["maps"]]) == []
    assert not (data / "dem").exists() and not (data / "maptiles").exists()
    assert (data / "settings.json").exists()

    packs = inventory.scan(xp, data / "dem").packs
    assert [p.name for p in packs] == ["z_dem_tyrol"]
    assert cleanup.delete_packs(packs, xp) == []
    assert not (custom / "z_dem_tyrol").exists() and (custom / "LOWI").exists()
    ini = (custom / "scenery_packs.ini").read_text()
    assert "z_dem_tyrol" not in ini and "LOWI" in ini


def test_delete_packs_refuses_foreign_folders(tmp_path):
    pack = inventory.InstalledPack("LOWI", tmp_path / "LOWI", "improve", "", [])
    (tmp_path / "LOWI").mkdir()
    assert cleanup.delete_packs([pack], None)
    assert (tmp_path / "LOWI").exists()


def test_human_size():
    assert cleanup.human_size(500) == "500 B"
    assert cleanup.human_size(3 * 1024 ** 3) == "3.0 GB"


def test_remove_single_tiles_from_packs(tmp_path):
    xp = tmp_path / "X-Plane 12"
    custom = xp / "Custom Scenery"
    folder = custom / "z_dem_tyrol" / "Earth nav data" / "+40+010"
    folder.mkdir(parents=True)
    for tile in ("+47+011", "+47+012"):
        (folder / f"{tile}.dsf").write_bytes(b"x")
    mesh = custom / "z_dem_mesh_tyrol" / "Earth nav data" / "+40+010"
    mesh.mkdir(parents=True)
    (mesh / "+47+011.dsf").write_bytes(b"x")
    (custom / "scenery_packs.ini").write_text(
        "I\n1000 Version\nSCENERY\n\nSCENERY_PACK Custom Scenery/z_dem_mesh_tyrol/\n"
        "SCENERY_PACK Custom Scenery/z_dem_tyrol/\n", encoding="utf-8")

    inv = inventory.scan(xp, tmp_path / "dem")
    assert cleanup.remove_tiles_from_packs(inv.packs, [(47, 11)], xp) == []
    # the pack with a tile left stays, the emptied one is gone with its ini line
    assert not (folder / "+47+011.dsf").exists() and (folder / "+47+012.dsf").exists()
    assert not (custom / "z_dem_mesh_tyrol").exists()
    ini = (custom / "scenery_packs.ini").read_text()
    assert "z_dem_mesh_tyrol" not in ini and "z_dem_tyrol" in ini
    assert (47, 11) not in inventory.scan(xp, tmp_path / "dem").installed
