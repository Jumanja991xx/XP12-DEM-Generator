import json

from xp12dem import inventory


def _pack(root, name, tiles, manifest=None):
    pack = root / name
    for lat, lon in tiles:
        folder = pack / "Earth nav data" / f"{lat // 10 * 10:+03d}{lon // 10 * 10:+04d}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{lat:+03d}{lon:+04d}.dsf").write_bytes(b"XPLNEDSF")
    if manifest is not None:
        (pack / "xp12dem_pack.json").write_text(json.dumps(manifest), encoding="utf-8")
    return pack


def test_scan_finds_custom_scenery_and_absolute_packs(tmp_path):
    xp = tmp_path / "X-Plane 12"
    custom = xp / "Custom Scenery"
    custom.mkdir(parents=True)
    _pack(custom, "z_dem_tyrol", [(47, 11), (47, 10)],
          {"method": "improve", "resolution": "1\"", "built": "2026-10-20 18:00",
           "tiles": [[47, 11], [47, 10], [46, 11]]})  # +46+011 listed but DSF deleted
    xpme = tmp_path / "XPME" / "BasePack"
    xpme.mkdir(parents=True)
    _pack(xpme.parent, "z_dem_budapest", [(47, 18)])
    _pack(custom, "z_ao_dem_old", [(46, 8)])  # not in the ini -> disabled
    _pack(custom, "LOWI_airport", [(47, 11)])  # not ours
    (custom / "scenery_packs.ini").write_text(
        "I\n1000 Version\nSCENERY\n\n"
        "SCENERY_PACK Custom Scenery/z_dem_tyrol/\n"
        f"SCENERY_PACK {(xpme.parent / 'z_dem_budapest').as_posix()}/\n"
        "SCENERY_PACK Custom Scenery/LOWI_airport/\n", encoding="utf-8")

    cache = tmp_path / "dem"
    (cache / "1s" / "sonny").mkdir(parents=True)
    (cache / "1s" / "sonny" / "N47E011.hgt").write_bytes(b"\0" * 2000)
    (cache / "3s" / "fabdem").mkdir(parents=True)
    (cache / "3s" / "fabdem" / "N45E016.hgt").write_bytes(b"\0" * 2000)
    (cache / "3s" / "N44E007.hgt").write_bytes(b"\0" * 2000)  # legacy layout

    inv = inventory.scan(xp, cache)
    assert sorted(p.name for p in inv.packs) == ["z_ao_dem_old", "z_dem_budapest", "z_dem_tyrol"]
    tyrol = inv.pack("z_dem_tyrol")
    assert tyrol.tiles == [(47, 10), (47, 11)] and tyrol.method == "improve" and tyrol.enabled
    assert not inv.pack("z_ao_dem_old").enabled
    assert inv.pack("z_ao_dem_old").method == "rebuild"
    assert (47, 18) in inv.installed and (46, 11) not in inv.installed
    assert inv.missing([(47, 11), (46, 11), (47, 18)]) == [(46, 11)]
    assert inv.cached[(47, 11)] == ['1" Sonny\'s LiDAR DTM']
    assert inv.cached[(45, 16)] == ['3" FABDEM V1-2']
    assert (44, 7) in inv.cached


def test_scan_without_xplane(tmp_path):
    inv = inventory.scan(None, tmp_path / "nothing")
    assert inv.packs == [] and inv.installed == {} and inv.cached == {}


def test_same_name_in_two_places_counts_both(tmp_path):
    xp = tmp_path / "X-Plane 12"
    custom = xp / "Custom Scenery"
    custom.mkdir(parents=True)
    _pack(custom, "z_dem_my_region", [(47, 11)])  # old copy, not listed
    xpme = tmp_path / "XPME" / "BasePack"
    xpme.mkdir(parents=True)
    (xpme / "XPME_Europe").mkdir()
    _pack(xpme, "z_dem_my_region", [(47, 10), (46, 11), (45, 11)])
    (custom / "scenery_packs.ini").write_text(
        "I\n1000 Version\nSCENERY\n\n"
        f"SCENERY_PACK {(xpme / 'z_dem_my_region').as_posix()}/\n"
        f"SCENERY_PACK {(xpme / 'XPME_Europe').as_posix()}/\n", encoding="utf-8")
    inv = inventory.scan(xp, tmp_path / "dem")
    assert len(inv.packs) == 2
    assert {t for t in inv.installed} == {(47, 11), (47, 10), (46, 11), (45, 11)}
    assert inv.active_on((47, 10)) and not inv.active_on((47, 11))


def test_pack_next_to_xpme_found_when_ini_lost_it(tmp_path):
    xp = tmp_path / "X-Plane 12"
    custom = xp / "Custom Scenery"
    custom.mkdir(parents=True)
    xpme = tmp_path / "XPME" / "BasePack"
    (xpme / "XPME_Europe").mkdir(parents=True)
    _pack(xpme, "z_dem_my_region", [(47, 10), (47, 11)])
    # XPME rewrote the file without our line
    (custom / "scenery_packs.ini").write_text(
        f"I\n1000 Version\nSCENERY\n\nSCENERY_PACK {(xpme / 'XPME_Europe').as_posix()}/\n",
        encoding="utf-8")
    inv = inventory.scan(xp, tmp_path / "dem")
    assert [p.name for p in inv.inactive()] == ["z_dem_my_region"]
    from xp12dem.installer import reactivate_pack

    reactivate_pack(inv.inactive()[0].path, xp, ["XPME_Europe"])
    lines = (custom / "scenery_packs.ini").read_text().splitlines()
    ours = [i for i, l in enumerate(lines) if "z_dem_my_region" in l]
    theirs = [i for i, l in enumerate(lines) if "XPME_Europe" in l]
    assert ours and ours[0] < theirs[0] and lines[ours[0]].endswith("BasePack/z_dem_my_region/")
    assert not inventory.scan(xp, tmp_path / "dem").inactive()


def test_users_xpme_setup_is_detected_and_fixed(tmp_path):
    """Reproduces a real setup: tiles in XPME's BasePack (entry dropped from the
    ini), an old copy in Custom Scenery that the ini still lists."""
    from xp12dem.installer import reactivate_pack, relocate_pack

    xp = tmp_path / "X-Plane 12"
    custom = xp / "Custom Scenery"
    custom.mkdir(parents=True)
    basepack = tmp_path / "Users" / "gerik" / "Documents" / "XPMapEnhancement" / "BasePack"
    for name in ("XPME_Africa", "XPME_Europe", "XPME_North_America"):
        (basepack / name).mkdir(parents=True)
    _pack(custom, "z_dem_my_region", [(47, 11)], {"method": "improve", "sources": {"+47+011": "XPME_Europe"}})
    xpme_pack = _pack(basepack, "z_dem_my_region", [(47, 10), (46, 11), (45, 11)],
                      {"method": "improve", "sources": {"+47+010": "XPME_Europe"}})
    (xpme_pack / "terrain").mkdir()
    (xpme_pack / "terrain" / "t.ter").write_text("BASE_TEX ../../XPME_Europe/textures/t.dds\n")
    bp = str(basepack).replace("/", "\\")  # XPME writes Windows style paths
    (custom / "scenery_packs.ini").write_text(
        "I\n1000 version\nSCENERY\n\n"
        "SCENERY_PACK Custom Scenery/LOWI/\n"
        "SCENERY_PACK *GLOBAL_AIRPORTS*\n"
        "SCENERY_PACK Custom Scenery/z_dem_my_region/\n"
        "SCENERY_PACK_DISABLED Custom Scenery/zOrtho4XP_+48+011/\n\n"
        f"SCENERY_PACK {bp}/\n"
        f"SCENERY_PACK {bp}\\XPME_Africa/\n"
        f"SCENERY_PACK {bp}\\XPME_Europe/\n"
        f"SCENERY_PACK {bp}\\XPME_North_America/\n", encoding="utf-8")

    inv = inventory.scan(xp, tmp_path / "dem")
    assert set(inv.installed) == {(47, 11), (47, 10), (46, 11), (45, 11)}  # all four found
    fix = inv.needs_fix()
    assert [p.path.parent for p in fix] == [basepack]  # the BasePack copy: not listed, misplaced
    assert not fix[0].enabled and fix[0].misplaced

    for pack in fix:  # what "Fix now" does
        relocate_pack(pack.path, custom / pack.name)
        reactivate_pack(custom / pack.name, xp, pack.improves)

    inv = inventory.scan(xp, tmp_path / "dem")
    assert not inv.needs_fix() and len(inv.packs) == 1
    assert sorted(inv.packs[0].tiles) == [(45, 11), (46, 11), (47, 10), (47, 11)]
    assert not (basepack / "z_dem_my_region").exists()
    lines = (custom / "scenery_packs.ini").read_text().splitlines()
    ours = lines.index("SCENERY_PACK Custom Scenery/z_dem_my_region/")
    assert ours < next(i for i, l in enumerate(lines) if "XPMapEnhancement" in l)  # before XPME's block
    assert lines[ours - 1].startswith("SCENERY_PACK_DISABLED Custom Scenery/zOrtho4XP") and lines[ours + 1] == ""
    assert sum("z_dem_my_region" in l for l in lines) == 1
    ref = (custom / "z_dem_my_region" / "terrain" / "t.ter").read_text().split()[1]
    assert (custom / "z_dem_my_region" / "terrain" / ref).resolve() == \
        (basepack / "XPME_Europe" / "textures" / "t.dds").resolve()
