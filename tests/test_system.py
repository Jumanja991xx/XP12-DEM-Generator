import json

from xp12dem import settings
from xp12dem.system import recommended_parallel, total_ram_gb


def test_recommended_parallel():
    assert recommended_parallel("improve", cores=16, ram_gb=32) == 6
    assert recommended_parallel("rebuild", cores=16, ram_gb=32) == 4
    assert recommended_parallel("improve", cores=8, ram_gb=16) == 4
    assert recommended_parallel("rebuild", cores=8, ram_gb=16) == 4
    assert recommended_parallel("rebuild", cores=4, ram_gb=8) == 1
    assert recommended_parallel("improve", cores=2, ram_gb=4) == 1
    assert total_ram_gb() is None or total_ram_gb() > 0.1


def test_old_settings_switch_to_automatic(tmp_path, monkeypatch):
    monkeypatch.setenv("XP12DEM_DATA", str(tmp_path))
    (tmp_path / "settings.json").write_text(json.dumps({"parallel": 1}))
    assert settings.Settings.load().parallel == 0
    s = settings.Settings.load()
    s.parallel = 3
    s.save()
    assert settings.Settings.load().parallel == 3
