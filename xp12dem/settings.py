"""Small JSON settings file remembering the user's last choices."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from .paths import settings_file


@dataclass
class Settings:
    xplane_path: str = ""
    install_target: str = "autoortho"  # or "custom_scenery"
    resolution: str = "1"
    mesh_detail: str = "high"
    parallel: int = 1
    local_dem_folder: str = ""
    last_region: str = "tyrol"
    custom_tiles: list = field(default_factory=list)

    @classmethod
    def load(cls) -> "Settings":
        try:
            data = json.loads(settings_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self) -> None:
        path = settings_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
