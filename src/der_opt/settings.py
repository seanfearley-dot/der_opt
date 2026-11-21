"""der-opt's own settings, kept between runs: currently where OpenBCA is installed.

The file is ~/.der-opt/config.yaml, or wherever DER_OPT_CONFIG points. Nothing is
written to it except by `der-opt setup`.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import yaml

OPENBCA_REPO = "https://github.com/openbca/openbca.git"
OPENBCA_TESTED_COMMIT = "7e13b36"  # the export's mapping was built and verified against this commit
DEFAULT_OPENBCA_DIR = Path.home() / ".der-opt" / "openbca"


def config_path() -> Path:
    return Path(os.environ.get("DER_OPT_CONFIG") or Path.home() / ".der-opt" / "config.yaml")


def load_settings() -> dict:
    path = config_path()
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def save_settings(settings: dict) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(settings, sort_keys=False), encoding="utf-8")
    return path


def openbca_dir(explicit: str | Path | None = None) -> Path:
    """Where OpenBCA is: an explicit path, else DER_OPT_OPENBCA, else the path
    `der-opt setup openbca` remembered."""
    for candidate, source in ((explicit, "--openbca"), (os.environ.get("DER_OPT_OPENBCA"), "DER_OPT_OPENBCA"),
                              (load_settings().get("openbca", {}).get("path"), str(config_path()))):
        if candidate:
            path = Path(candidate).expanduser()
            if not (path / "core").is_dir():
                raise FileNotFoundError(f"{path} (from {source}) isn't an OpenBCA checkout; run `der-opt setup openbca`")
            return path
    raise FileNotFoundError("OpenBCA isn't set up: run `der-opt setup openbca` once (or pass --openbca PATH)")


def find_uv() -> str | None:
    """uv, from PATH or its usual install locations."""
    found = shutil.which("uv")
    if found:
        return found
    home = Path.home()
    candidates = [home / ".local" / "bin" / "uv", home / ".cargo" / "bin" / "uv",
                  *sorted((home / "Library" / "Python").glob("*/bin/uv"))]
    return next((str(c) for c in candidates if c.exists()), None)
