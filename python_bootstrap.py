"""Restart user-facing scripts with the supported project environment."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path


SUPPORTED_PYTHON = (3, 11)
REQUIRED_IMPORTS = ("pandera", "pydantic", "jsonschema")
_BOOTSTRAP_FLAG = "WTARG_ENV_BOOTSTRAPPED"


def _project_interpreters(project_root: Path) -> tuple[Path, ...]:
    return (
        project_root / ".venv" / "Scripts" / "python.exe",
        project_root / ".venv" / "bin" / "python",
    )


def _missing_imports(required_imports: Sequence[str]) -> list[str]:
    return [name for name in required_imports if importlib.util.find_spec(name) is None]


def _environment_is_ready(required_imports: Sequence[str]) -> bool:
    return sys.version_info[:2] == SUPPORTED_PYTHON and not _missing_imports(required_imports)


def _same_interpreter(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return os.path.normcase(str(left.absolute())) == os.path.normcase(str(right.absolute()))


def _environment_error(project_root: Path, required_imports: Sequence[str]) -> str:
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    missing = ", ".join(_missing_imports(required_imports)) or "none"
    return (
        "WTARG could not start with the supported project environment.\n"
        f"Current Python: {sys.executable} ({version})\n"
        f"Missing packages: {missing}\n\n"
        "From the project folder, run these commands once:\n"
        "  py -3.11 -m venv .venv\n"
        "  .\\.venv\\Scripts\\python.exe -m pip install --require-hashes -r requirements.lock\n\n"
        f"Project folder: {project_root}"
    )


def ensure_project_environment(
    project_root: Path | str,
    *,
    required_imports: Sequence[str] = REQUIRED_IMPORTS,
) -> None:
    """Use ``.venv`` automatically when the current interpreter is unsuitable."""

    root = Path(project_root).resolve()
    if _environment_is_ready(required_imports):
        return

    current = Path(sys.executable)
    already_restarted = os.environ.get(_BOOTSTRAP_FLAG) == "1"
    if not already_restarted:
        for candidate in _project_interpreters(root):
            if candidate.is_file() and not _same_interpreter(candidate, current):
                environment = os.environ.copy()
                environment[_BOOTSTRAP_FLAG] = "1"
                completed = subprocess.run(
                    [str(candidate), *sys.argv],
                    env=environment,
                    check=False,
                )
                raise SystemExit(completed.returncode)

    raise SystemExit(_environment_error(root, required_imports))
