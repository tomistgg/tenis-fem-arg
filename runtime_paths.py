"""Runtime-selectable paths used by transactional refresh runs.

Normal commands read and write the repository paths.  The refresh transaction
sets the environment variables before importing any pipeline module so every
child process operates on the same run-scoped staging tree.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent


def _runtime_path(variable: str, default: Path) -> Path:
    configured = os.environ.get(variable, "").strip()
    return Path(configured).resolve() if configured else default.resolve()


DATA_DIR = _runtime_path("WTARG_DATA_DIR", PROJECT_ROOT / "data")
SITE_ROOT = _runtime_path("WTARG_SITE_ROOT", PROJECT_ROOT)
DEPLOY_SITE_DIR = _runtime_path("WTARG_DEPLOY_SITE_DIR", PROJECT_ROOT / ".site")


def data_path(*parts: str) -> Path:
    return DATA_DIR.joinpath(*parts)

