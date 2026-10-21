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
