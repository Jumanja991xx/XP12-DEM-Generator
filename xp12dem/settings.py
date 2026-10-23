"""Small JSON settings file remembering the user's last choices."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from .paths import settings_file


@dataclass
class Settings:
    xplane_path: str = ""
    method: str = "improve"  # or "rebuild"
    install_target: str = "autoortho"  # new mesh: "official"/"xpme" photos, "autoortho" folder or "custom_scenery"
    photo_zl: int = 17  # official photos: zoom level
    resolution: str = "1"
    mesh_detail: str = "auto"
    parallel: int = 0  # 0 = automatic (recommended for this PC)
    local_dem_folder: str = ""
    last_region: str = "tyrol"
    custom_tiles: list = field(default_factory=list)
    parallel_choice: bool = True  # marks settings written with the "automatic" option

    @classmethod
    def load(cls) -> "Settings":
        try:
            data = json.loads(settings_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        if "parallel_choice" not in data:  # before 0.1.1 parallel defaulted to 1 - switch to automatic
            known["parallel"] = 0
        return cls(**known)

    def save(self) -> None:
        path = settings_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
