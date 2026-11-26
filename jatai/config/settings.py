import os
from pathlib import Path


def base_dir() -> Path:
    return Path(os.environ.get("JATAI_HOME", Path.home() / ".jatai")).expanduser()


def ensure_dirs() -> Path:
    root = base_dir()
    for sub in ("projects", "cache"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root
