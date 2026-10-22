import pytest


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """Keep caches, settings and the install registry of every test in its own folder."""
    monkeypatch.setenv("XP12DEM_DATA", str(tmp_path / "xp12dem-data"))
