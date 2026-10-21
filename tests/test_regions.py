import pytest

from xp12dem import regions


def test_tile_names():
    assert regions.tile_name(47, 11) == "+47+011"
    assert regions.tile_name(-3, -60) == "-03-060"
    assert regions.hgt_name(47, 11) == "N47E011"
    assert regions.hgt_name(43, -2) == "N43W002"


@pytest.mark.parametrize("text, tile", [
    ("+47+011", (47, 11)),
    ("N47E011", (47, 11)),
    ("n43w002", (43, -2)),
    ("46, 7", (46, 7)),
])
def test_parse_tile(text, tile):
    assert regions.parse_tile(text) == tile


def test_parse_tile_rejects_garbage():
    with pytest.raises(ValueError):
        regions.parse_tile("Innsbruck")


def test_dsf_relpath_uses_ten_degree_folders():
    assert regions.dsf_relpath(47, 11) == "Earth nav data/+40+010/+47+011.dsf"
    assert regions.dsf_relpath(43, -2) == "Earth nav data/+40-010/+43-002.dsf"


def test_every_preset_has_covered_tiles():
    available = regions.available_tiles()
    assert (47, 11) in available
    for region in regions.PRESETS:
        tiles = region.tiles()
        assert tiles, region.key
        assert all(t in available for t in tiles)


def test_preset_keys_unique():
    keys = [r.key for r in regions.PRESETS]
    assert len(keys) == len(set(keys))


def test_slugify_and_bounds():
    assert regions.slugify("Tyrol & Salzburg") == "tyrol_salzburg"
    assert regions.slugify("!!!") == "custom"
    assert regions.bounds([(46, 10), (47, 13)]) == "46°N–48°N, 10°E–14°E"
    assert regions.bounds([(42, -2)]) == "42°N–43°N, 2°W–1°W"
