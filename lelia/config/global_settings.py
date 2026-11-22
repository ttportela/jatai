import json
from pathlib import Path
from typing import Any

from lelia.config.settings import base_dir, ensure_dirs

DEFAULTS: dict[str, Any] = {
    "language": "pt-BR",
    "theme": "light",
    "port": 8000,
    "base_dir": str(Path.home() / ".lelia"),
    "current_project": "",
    "core_api_key": "",
}

_PATH = "settings.json"


def _path() -> Path:
    ensure_dirs()
    return base_dir() / _PATH


def load() -> dict[str, Any]:
    path = _path()
    if path.exists():
        data = json.loads(path.read_text())
        return {**DEFAULTS, **data}
    return dict(DEFAULTS)


def save(data: dict[str, Any]) -> dict[str, Any]:
    allowed = {k: data.get(k, v) for k, v in DEFAULTS.items() if k != "base_dir"}
    allowed["base_dir"] = DEFAULTS["base_dir"]
    _path().write_text(json.dumps(allowed, ensure_ascii=False, indent=2))
    return load()
