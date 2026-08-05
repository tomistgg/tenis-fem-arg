"""Run-scoped staging and all-or-nothing dataset promotion."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from data_quality import GENERATED_SITE_FILES, run_data_quality_gate, validate_site_artifacts
from pipeline_errors import DataPromotionError, DataValidationError
from run_state import (
    RunStatus,
    copy_run_state,
    finalize_run_state,
    initialize_run_state,
    load_run_state,
)
from time_utils import utc_now
from runtime_logging import get_logger


logger = get_logger("transaction")
PROJECT_ROOT = Path(__file__).resolve().parent
PRODUCTION_DATA_DIR = PROJECT_ROOT / "data"
STAGING_PARENT = PROJECT_ROOT / ".run_staging"
STATE_PARENT = PROJECT_ROOT / ".run_state"
LATEST_STATE_PATH = STATE_PARENT / "latest.json"
RETIRED_GENERATED_DATA_FILES = {
    "history_data.json",
    "wta_rankings_20_29_bundle.js",
    "wta_rankings_10_19_bundle.js",
    "wta_rankings_00_09_bundle.js",
    "wta_rankings_83_99_bundle.js",
}


def transaction_is_active() -> bool:
    return os.environ.get("WTARG_TRANSACTION_ACTIVE", "").strip() == "1"


def _new_run_id() -> str:
    timestamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex[:10]}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_csv(path: Path) -> int:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        first_line = handle.readline()
        if not first_line:
            raise DataValidationError(
                component="dataset",
                operation="validate CSV",
                message=f"CSV is empty: {path.name}",
                context={"path": str(path)},
            )
        delimiters = (",", ";", "\t", "|")
        delimiter = max(delimiters, key=first_line.count)
        handle.seek(0)
        reader = csv.reader(handle, delimiter=delimiter)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise DataValidationError(
                component="dataset",
                operation="validate CSV",
                message=f"CSV is empty: {path.name}",
                context={"path": str(path)},
            ) from exc
        if not header or any(not column.strip() for column in header):
            raise DataValidationError(
                component="dataset",
                operation="validate CSV",
                message=f"CSV has an invalid header: {path.name}",
                context={"path": str(path), "header": header},
            )
        row_count = 0
        for line_number, row in enumerate(reader, 2):
            if len(row) != len(header):
                raise DataValidationError(
                    component="dataset",
                    operation="validate CSV",
                    message=f"CSV width mismatch in {path.name} at line {line_number}",
                    context={"expected": len(header), "actual": len(row)},
                )
            row_count += 1
    return row_count


def validate_staged_dataset(data_dir: Path) -> dict[str, Any]:
    if not data_dir.is_dir():
        raise DataValidationError(
            component="dataset",
            operation="validate staging",
            message="staged data directory does not exist",
            context={"path": str(data_dir)},
        )

    missing = [
        str(path.relative_to(PRODUCTION_DATA_DIR))
        for path in PRODUCTION_DATA_DIR.rglob("*")
        if (
            path.is_file()
            and path.relative_to(PRODUCTION_DATA_DIR).as_posix() not in RETIRED_GENERATED_DATA_FILES
            and not (data_dir / path.relative_to(PRODUCTION_DATA_DIR)).is_file()
        )
    ]
    if missing:
        raise DataValidationError(
            component="dataset",
            operation="validate staging",
            message="staged dataset dropped existing files",
            context={"missing": missing[:25], "missing_count": len(missing)},
        )

    json_count = 0
    for path in data_dir.rglob("*.json"):
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DataValidationError(
                component="dataset",
                operation="validate JSON",
                message=f"invalid staged JSON: {path.name}",
                context={"path": str(path), "cause": str(exc)},
            ) from exc
        json_count += 1

    csv_rows = 0
    csv_count = 0
    for path in data_dir.rglob("*.csv"):
        csv_rows += _validate_csv(path)
        csv_count += 1

    quality = run_data_quality_gate(data_dir, baseline_dir=PRODUCTION_DATA_DIR)
    return {
        "json_files": json_count,
        "csv_files": csv_count,
        "csv_rows": csv_rows,
        "quality_gate": quality,
    }


def validate_staged_site(site_root: Path, deploy_root: Path) -> dict[str, Any]:
    return validate_site_artifacts(site_root, deploy_root)


def _build_staged_deploy_site(
    deploy_root: Path,
    environment: dict[str, str],
) -> None:
    """Build the immutable Pages payload once, after staged data validation."""

    try:
        timeout_seconds = int(os.environ.get("WTARG_SITE_BUILD_TIMEOUT_SECONDS", "1800"))
    except ValueError as exc:
        raise DataValidationError(
            component="site",
            operation="parse build timeout",
            message="WTARG_SITE_BUILD_TIMEOUT_SECONDS must be an integer",
            context={"value": os.environ.get("WTARG_SITE_BUILD_TIMEOUT_SECONDS")},
        ) from exc

    command = [
        sys.executable,
        str(PROJECT_ROOT / "build_deploy_site.py"),
        "--output",
        str(deploy_root),
        "--max-edge",
        "2400",
        "--quality",
        "88",
    ]
    logger.debug(f"Building validated deploy site at {deploy_root}...")
    try:
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            env=environment,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise DataValidationError(
            component="site",
            operation="build deploy artifact",
            message="deploy site build timed out",
            context={"timeout_seconds": timeout_seconds, "path": str(deploy_root)},
        ) from exc
    except OSError as exc:
        raise DataValidationError(
            component="site",
            operation="build deploy artifact",
            message="deploy site builder could not start",
            context={"cause": str(exc), "path": str(deploy_root)},
        ) from exc

    if completed.returncode != 0:
        raise DataValidationError(
            component="site",
            operation="build deploy artifact",
            message="deploy site builder failed",
            context={"returncode": completed.returncode, "path": str(deploy_root)},
        )


def _atomic_copy(source: Path, destination: Path, run_id: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{run_id}.promote")
    try:
        shutil.copy2(source, temporary)
        # Windows rejects fsync() on a read-only descriptor with errno 9.
        # Open read/write so the copied bytes can be durably flushed before
        # the atomic replacement.
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _promote_site_files(
    staging_site: Path,
    backup_root: Path,
    run_id: str,
) -> list[tuple[Path, Path | None]]:
    promoted: list[tuple[Path, Path | None]] = []
    for source in sorted(path for path in staging_site.rglob("*") if path.is_file()):
        relative = source.relative_to(staging_site)
        destination = PROJECT_ROOT / relative
        if destination.exists() and _sha256(source) == _sha256(destination):
            continue
        backup = backup_root / relative if destination.exists() else None
        if backup is not None:
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination, backup)
        _atomic_copy(source, destination, run_id)
        promoted.append((destination, backup))
    return promoted


def _rollback_site_files(promoted: list[tuple[Path, Path | None]], run_id: str) -> None:
    for destination, backup in reversed(promoted):
        if backup is None:
            destination.unlink(missing_ok=True)
        else:
            _atomic_copy(backup, destination, f"{run_id}.rollback")


def _promote_all(
    staging_root: Path,
    staging_data: Path,
    staging_site: Path,
    staging_deploy: Path,
    run_id: str,
) -> dict[str, Any]:
    previous_data = staging_root / "previous-data"
    previous_deploy = staging_root / "previous-deploy-site"
    site_backup = staging_root / "previous-generated-site"
    failed_data = staging_root / "failed-promoted-data"
    promoted_site: list[tuple[Path, Path | None]] = []
    data_swapped = False
    deploy_swapped = False
    deploy_target = PROJECT_ROOT / ".site"

    try:
        os.replace(PRODUCTION_DATA_DIR, previous_data)
        try:
            os.replace(staging_data, PRODUCTION_DATA_DIR)
        except BaseException:
            os.replace(previous_data, PRODUCTION_DATA_DIR)
            raise
        data_swapped = True

        promoted_site = _promote_site_files(staging_site, site_backup, run_id)

        if staging_deploy.exists():
            if deploy_target.exists():
                os.replace(deploy_target, previous_deploy)
            try:
                os.replace(staging_deploy, deploy_target)
            except BaseException:
                if previous_deploy.exists():
                    os.replace(previous_deploy, deploy_target)
                raise
            deploy_swapped = True

        return {
            "dataset_files": sum(1 for path in PRODUCTION_DATA_DIR.rglob("*") if path.is_file()),
            "generated_site_files": len(promoted_site),
            "deploy_site_promoted": deploy_swapped,
        }
    except BaseException as exc:
        rollback_errors = []
        try:
            if deploy_swapped and deploy_target.exists():
                os.replace(deploy_target, staging_root / "failed-deploy-site")
            if not deploy_target.exists() and previous_deploy.exists():
                os.replace(previous_deploy, deploy_target)
        except BaseException as rollback_exc:
            rollback_errors.append(f"deploy rollback: {rollback_exc}")
        try:
            _rollback_site_files(promoted_site, run_id)
        except BaseException as rollback_exc:
            rollback_errors.append(f"generated-site rollback: {rollback_exc}")
        try:
            if data_swapped and PRODUCTION_DATA_DIR.exists():
                os.replace(PRODUCTION_DATA_DIR, failed_data)
            if not PRODUCTION_DATA_DIR.exists() and previous_data.exists():
                os.replace(previous_data, PRODUCTION_DATA_DIR)
        except BaseException as rollback_exc:
            rollback_errors.append(f"dataset rollback: {rollback_exc}")
        rollback_complete = PRODUCTION_DATA_DIR.exists() and not rollback_errors
        error = DataPromotionError(
            component="dataset",
            operation="promote transaction",
            message=(
                "promotion failed; production paths were rolled back"
                if rollback_complete
                else "promotion failed and rollback was incomplete"
            ),
            context={
                "run_id": run_id,
                "cause": str(exc),
                "rollback_complete": rollback_complete,
                "rollback_errors": rollback_errors,
            },
        )
        raise error from exc


def _finish(
    run_state_path: Path,
    status: RunStatus,
    **details: Any,
) -> int:
    state = finalize_run_state(run_state_path, status, **details)
    copy_run_state(run_state_path, LATEST_STATE_PATH)
    logger.info(
        f"Run {state.get('run_id', 'unknown')} finished with status={status}; "
        f"details={run_state_path}"
    )
    return 0 if status in {"success", "degraded"} else (2 if status == "partial" else 1)


def run_refresh_transaction(
    command: list[str],
    *,
    include_generated_site: bool = True,
    timeout_seconds: int | None = None,
) -> int:
    """Execute ``command`` against a staged copy and promote on full success."""

    if transaction_is_active():
        return subprocess.run(command, check=False).returncode

    run_id = _new_run_id()
    staging_root = STAGING_PARENT / run_id
    staging_data = staging_root / "data"
    staging_site = staging_root / "generated-site"
    staging_deploy = staging_root / "deploy-site"
    run_state_path = STATE_PARENT / f"{run_id}.json"

    staging_root.mkdir(parents=True, exist_ok=False)
    initialize_run_state(run_state_path, run_id, staging_root)
    copy_run_state(run_state_path, LATEST_STATE_PATH)
    logger.info(f"Run {run_id} staging at {staging_root}")

    try:
        shutil.copytree(PRODUCTION_DATA_DIR, staging_data)
        staging_site.mkdir(parents=True)
    except BaseException as exc:
        return _finish(
            run_state_path,
            "failed",
            error={
                "type": type(exc).__name__,
                "operation": "initialize staging",
                "message": str(exc),
            },
            staging_retained=True,
        )

    environment = os.environ.copy()
    environment.update(
        {
            "WTARG_TRANSACTION_ACTIVE": "1",
            "WTARG_RUN_ID": run_id,
            "WTARG_RUN_STATUS_PATH": str(run_state_path),
            "WTARG_DATA_DIR": str(staging_data),
            "WTARG_SITE_ROOT": str(staging_site),
            "WTARG_DEPLOY_SITE_DIR": str(staging_deploy),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )
    try:
        configured_timeout = int(os.environ.get("WTARG_RUN_TIMEOUT_SECONDS", "6300"))
    except ValueError as exc:
        return _finish(
            run_state_path,
            "failed",
            error={
                "type": type(exc).__name__,
                "operation": "parse run timeout",
                "message": str(exc),
            },
            staging_retained=True,
        )
    effective_timeout = timeout_seconds or configured_timeout

    try:
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            env=environment,
            check=False,
            timeout=effective_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return _finish(
            run_state_path,
            "failed",
            error={"type": type(exc).__name__, "message": str(exc)},
            staging_retained=True,
        )
    except BaseException as exc:
        return _finish(
            run_state_path,
            "failed",
            error={"type": type(exc).__name__, "message": str(exc)},
            staging_retained=True,
        )

    if completed.returncode != 0:
        return _finish(
            run_state_path,
            "failed",
            error={"type": "ChildProcessError", "returncode": completed.returncode},
            staging_retained=True,
        )

    state = load_run_state(run_state_path)
    issue_statuses = {issue.get("severity") for issue in state.get("issues", [])}
    if "partial" in issue_statuses or state.get("status") == "partial":
        return _finish(
            run_state_path,
            "partial",
            promotion="blocked",
            staging_retained=True,
        )

    try:
        validation = {"dataset": validate_staged_dataset(staging_data)}
        if include_generated_site:
            _build_staged_deploy_site(staging_deploy, environment)
            validation["site"] = validate_staged_site(staging_site, staging_deploy)
        promotion = _promote_all(
            staging_root,
            staging_data,
            staging_site,
            staging_deploy,
            run_id,
        )
    except BaseException as exc:
        details = exc.as_dict() if isinstance(exc, (DataValidationError, DataPromotionError)) else {
            "type": type(exc).__name__,
            "message": str(exc),
        }
        return _finish(
            run_state_path,
            "failed",
            error=details,
            staging_retained=True,
        )

    final_status: RunStatus = "degraded" if "degraded" in issue_statuses else "success"
    staging_retained = False
    try:
        shutil.rmtree(staging_root)
    except OSError as exc:
        final_status = "degraded"
        staging_retained = staging_root.exists()
        promotion["cleanup_warning"] = {
            "type": type(exc).__name__,
            "message": str(exc),
            "path": str(staging_root),
        }
    return _finish(
        run_state_path,
        final_status,
        validation=validation,
        promotion=promotion,
        staging_retained=staging_retained,
    )


def run_current_script_transaction(
    script_path: str,
    argv: list[str] | None = None,
    *,
    include_generated_site: bool = False,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    return run_refresh_transaction(
        [sys.executable, str(Path(script_path).resolve()), *arguments],
        include_generated_site=include_generated_site,
    )


def cli_refresh() -> None:
    """Console-script entry point that preserves the transaction boundary."""

    raise SystemExit(
        run_refresh_transaction(
            [sys.executable, str(PROJECT_ROOT / "main.py"), *sys.argv[1:]],
            include_generated_site=True,
        )
    )
