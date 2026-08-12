"""Explicit, atomic status reporting for refresh transactions."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Literal

from pipeline_errors import PipelineError
from time_utils import utc_timestamp

RunStatus = Literal["running", "success", "degraded", "partial", "failed"]
IssueSeverity = Literal["degraded", "partial"]


def _status_path() -> Path | None:
    value = os.environ.get("WTARG_RUN_STATUS_PATH", "").strip()
    return Path(value) if value else None


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_run_state(path: Path | None = None) -> dict[str, Any]:
    selected = path or _status_path()
    if selected is None or not selected.exists():
        return {}
    with selected.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"invalid run-state object: {selected}")
    return payload


def initialize_run_state(path: Path, run_id: str, staging_dir: Path) -> None:
    _atomic_json(
        path,
        {
            "run_id": run_id,
            "status": "running",
            "started_at": utc_timestamp(),
            "finished_at": None,
            "staging_dir": str(staging_dir),
            "issues": [],
        },
    )


def record_run_issue(
    component: str,
    error: BaseException,
    *,
    severity: IssueSeverity = "partial",
    context: dict[str, Any] | None = None,
) -> None:
    path = _status_path()
    if path is None:
        return
    state = load_run_state(path)
    details = error.as_dict() if isinstance(error, PipelineError) else {
        "type": type(error).__name__,
        "component": component,
        "operation": "unspecified",
        "message": str(error),
        "context": context or {},
        "retryable": False,
    }
    details["severity"] = severity
    details["recorded_at"] = utc_timestamp()
    state.setdefault("issues", []).append(details)
    current = state.get("status", "running")
    if current not in {"failed", "partial"}:
        state["status"] = severity
    _atomic_json(path, state)


def report_run_issue(
    component: str,
    operation: str,
    error: BaseException,
    *,
    severity: IssueSeverity = "degraded",
    context: dict[str, Any] | None = None,
) -> PipelineError:
    """Emit a machine-readable warning and attach it to the active run state."""
    structured = error if isinstance(error, PipelineError) else PipelineError(
        component=component,
        operation=operation,
        message=str(error) or type(error).__name__,
        context=context or {},
    )
    record_run_issue(component, structured, severity=severity, context=context)
    payload = structured.as_dict()
    payload.update({"event": "pipeline_issue", "severity": severity})
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    return structured


def finalize_run_state(
    path: Path,
    status: RunStatus,
    **details: Any,
) -> dict[str, Any]:
    state = load_run_state(path)
    state.update(details)
    state["status"] = status
    state["finished_at"] = utc_timestamp()
    _atomic_json(path, state)
    return state


def copy_run_state(source: Path, destination: Path) -> None:
    _atomic_json(destination, load_run_state(source))
