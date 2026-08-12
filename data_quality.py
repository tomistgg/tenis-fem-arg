"""Blocking data-quality gate shared by transactions and publishing workflows."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

import pandas as pd
import pandera.pandas as pa
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from canonical_data import CanonicalConstraintError, validate_project_data
from pipeline_errors import DataValidationError
from time_utils import utc_now


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_POLICY_PATH = PROJECT_ROOT / "data_quality_policy.json"
SCHEMA_DIR = PROJECT_ROOT / "schemas"

GENERATED_SITE_FILES = (
    "app.html",
    "index.html",
    "404.html",
    "assets/app.css",
    "assets/js/app.js",
    "assets/js/data-loader.js",
    "assets/js/generated-data.js",
    "assets/js/router.js",
    "assets/js/tabs/draws.js",
    "assets/js/tabs/roadtogs.js",
    "assets/js/tabs/tstrength.js",
    "upcoming/index.html",
    "entrylists/index.html",
    "draws/index.html",
    "calendar/index.html",
    "rankings/index.html",
    "roadtogs/index.html",
    "history/index.html",
    "fedbcup/index.html",
    "tstrength/index.html",
)

_PLAYER_TEXT_FIELDS = (
    "player_key",
    "display_name",
    "presentation_name",
    "country",
    "dob",
    "wta_id",
    "wta_name",
    "itf_id",
    "itf_name",
    "bjkc_id",
    "bjkc_name",
)


class PlayerAliasModel(BaseModel):
    """Pydantic representation of one canonical player identity."""

    model_config = ConfigDict(extra="forbid", strict=True)

    player_key: str
    display_name: str
    presentation_name: str = ""
    country: str
    dob: str
    wta_id: str
    wta_name: str
    itf_id: str
    itf_name: str
    bjkc_id: str
    bjkc_name: str
    aliases: list[str]
    additional_wta_ids: list[str]
    additional_itf_ids: list[str]
    additional_bjkc_ids: list[str]

    @field_validator(*_PLAYER_TEXT_FIELDS)
    @classmethod
    def text_must_be_trimmed(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("must not have leading or trailing whitespace")
        return value

    @field_validator("player_key", "display_name")
    @classmethod
    def identity_must_not_be_blank(cls, value: str) -> str:
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("aliases", "additional_wta_ids", "additional_itf_ids", "additional_bjkc_ids")
    @classmethod
    def list_values_must_be_unique_and_nonempty(cls, values: list[str]) -> list[str]:
        if any(not value or value != value.strip() for value in values):
            raise ValueError("list values must be nonempty and trimmed")
        if len(values) != len(set(values)):
            raise ValueError("list values must be unique")
        return values


class CacheMetadataModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    fetchedAt: AwareDatetime | None = None
    completedAt: AwareDatetime | None = None

    @model_validator(mode="after")
    def has_timestamp(self) -> CacheMetadataModel:
        if self.fetchedAt is None and self.completedAt is None:
            raise ValueError("cache metadata must contain fetchedAt or completedAt")
        return self


class CacheStateModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    files: dict[str, CacheMetadataModel]
    entries: dict[str, dict[str, CacheMetadataModel]]


class RankingRefreshModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requested_date: date
    previous_date: date | None = None
    status: str = Field(min_length=1)
    comparison: str = Field(min_length=1)
    cutoff: str = Field(min_length=1)
    message: str = Field(min_length=1)


class LimitModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    absolute: int = Field(ge=0)
    fraction: float = Field(ge=0)


class FreshnessModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    column: str = Field(min_length=1)
    max_age_days: int = Field(ge=0)
    future_tolerance_days: int = Field(default=0, ge=0)


class TablePolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["json_array", "rankings", "matches"]
    minimum_rows: int = Field(ge=0)
    max_row_drop: LimitModel
    freshness: FreshnessModel | None = None


class QualityPolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1]
    tables: dict[str, TablePolicyModel]
    cache_freshness: dict[str, int]


def _quality_error(operation: str, message: str, **context: Any) -> DataValidationError:
    return DataValidationError(
        component="data-quality",
        operation=operation,
        message=message,
        context=context,
    )


def _read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _quality_error("parse JSON", f"invalid JSON: {path.name}", path=str(path), cause=str(exc)) from exc


def _load_policy(path: Path) -> QualityPolicyModel:
    payload = _read_json(path)
    try:
        return QualityPolicyModel.model_validate(payload)
    except ValidationError as exc:
        raise _quality_error("validate policy", "data quality policy is invalid", path=str(path), errors=exc.errors()) from exc


def _validate_json_schema(payload: Any, schema_path: Path, data_path: Path) -> None:
    schema = _read_json(schema_path)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.absolute_path))
    if not errors:
        return
    first = errors[0]
    location = "/".join(str(part) for part in first.absolute_path) or "<root>"
    raise _quality_error(
        "validate JSON Schema",
        f"{data_path.name} does not match {schema_path.name}: {first.message}",
        path=str(data_path),
        schema=str(schema_path),
        location=location,
        error_count=len(errors),
    )


def _validate_pydantic_json(data_dir: Path) -> dict[str, int]:
    aliases_path = data_dir / "player_aliases_wta_itf.json"
    aliases = _read_json(aliases_path)
    _validate_json_schema(aliases, SCHEMA_DIR / "player_aliases.schema.json", aliases_path)
    if not isinstance(aliases, list):
        raise _quality_error("validate aliases", "player aliases must be a JSON array", path=str(aliases_path))
    try:
        for row in aliases:
            PlayerAliasModel.model_validate(row)
    except ValidationError as exc:
        raise _quality_error(
            "validate Pydantic models",
            "player alias model validation failed",
            path=str(aliases_path),
            errors=exc.errors(),
        ) from exc

    cache_path = data_dir / "cache_state.json"
    cache_state = _read_json(cache_path)
    _validate_json_schema(cache_state, SCHEMA_DIR / "cache_state.schema.json", cache_path)
    ranking_status_path = data_dir / "wta_ranking_refresh_status.json"
    ranking_status = _read_json(ranking_status_path)
    try:
        CacheStateModel.model_validate(cache_state)
        RankingRefreshModel.model_validate(ranking_status)
    except ValidationError as exc:
        raise _quality_error(
            "validate Pydantic models",
            "cache or ranking status model validation failed",
            errors=exc.errors(),
        ) from exc
    return {"player_aliases": len(aliases), "pydantic_models": len(aliases) + 2, "json_schemas": 2}


def _string_check(pattern: str, description: str, *, allow_empty: bool = False) -> pa.Check:
    def check(series: pd.Series) -> pd.Series:
        values = series.astype(str)
        matches = values.str.fullmatch(pattern)
        return matches | values.eq("") if allow_empty else matches

    return pa.Check(check, error=description)


_DATE_CHECK = _string_check(r"\d{4}-\d{2}-\d{2}", "must use YYYY-MM-DD")
_OPTIONAL_DATE_CHECK = _string_check(r"\d{4}-\d{2}-\d{2}", "must be blank or YYYY-MM-DD", allow_empty=True)
_NONEMPTY_CHECK = pa.Check(lambda series: series.astype(str).str.strip().ne(""), error="must not be blank")

RANKING_SCHEMA = pa.DataFrameSchema(
    {
        "week_date": pa.Column(str, checks=_DATE_CHECK, nullable=False),
        "id": pa.Column(str, checks=_NONEMPTY_CHECK, nullable=False),
        "rank": pa.Column(str, checks=_string_check(r"\d+", "must be an integer"), nullable=False),
        "points": pa.Column(str, checks=_string_check(r"\d+", "must be a nonnegative integer", allow_empty=True), nullable=False),
        "player": pa.Column(str, checks=_NONEMPTY_CHECK, nullable=False),
        "country": pa.Column(str, checks=_string_check(r"[A-Z]{3}", "must be blank or a 3-letter code", allow_empty=True), nullable=False),
        "dob": pa.Column(str, checks=_OPTIONAL_DATE_CHECK, nullable=False),
    },
    strict=True,
    coerce=False,
)

MATCH_SCHEMA = pa.DataFrameSchema(
    {
        "matchType": pa.Column(str, checks=_NONEMPTY_CHECK, nullable=False),
        "matchId": pa.Column(str, checks=_NONEMPTY_CHECK, nullable=False),
        "date": pa.Column(str, checks=_DATE_CHECK, nullable=False),
        "tournamentId": pa.Column(str, checks=_NONEMPTY_CHECK, nullable=False),
        "tournamentName": pa.Column(str, checks=_NONEMPTY_CHECK, nullable=False),
        "tournamentCategory": pa.Column(str, nullable=False),
        "surface": pa.Column(str, nullable=False),
        "inOrOutdoor": pa.Column(str, nullable=False),
        "tournamentCountry": pa.Column(str, nullable=False),
        "roundName": pa.Column(str, checks=_NONEMPTY_CHECK, nullable=False),
        "draw": pa.Column(str, nullable=False),
        "result": pa.Column(str, nullable=False),
        "resultStatusDesc": pa.Column(str, nullable=False),
        "winnerId": pa.Column(str, checks=_NONEMPTY_CHECK, nullable=False),
        "winnerEntry": pa.Column(str, nullable=False),
        "winnerSeed": pa.Column(str, nullable=False),
        "winnerName": pa.Column(str, checks=_NONEMPTY_CHECK, nullable=False),
        "winnerCountry": pa.Column(str, nullable=False),
        "loserId": pa.Column(str, checks=_NONEMPTY_CHECK, nullable=False),
        "loserEntry": pa.Column(str, nullable=False),
        "loserSeed": pa.Column(str, nullable=False),
        "loserName": pa.Column(str, checks=_NONEMPTY_CHECK, nullable=False),
        "loserCountry": pa.Column(str, nullable=False),
    },
    strict=False,
    coerce=False,
)


def _csv_delimiter(path: Path) -> str:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        first_line = handle.readline()
    return max((",", ";", "\t", "|"), key=first_line.count)


def _validate_tabular_file(path: Path, kind: str) -> int:
    schema = RANKING_SCHEMA if kind == "rankings" else MATCH_SCHEMA
    total = 0
    try:
        chunks = pd.read_csv(
            path,
            sep=_csv_delimiter(path),
            dtype=str,
            keep_default_na=False,
            chunksize=100_000,
        )
        for chunk_number, chunk in enumerate(chunks, 1):
            schema.validate(chunk, lazy=True)
            total += len(chunk)
    except (OSError, UnicodeError, pd.errors.ParserError, pa.errors.SchemaError, pa.errors.SchemaErrors) as exc:
        raise _quality_error(
            "validate Pandera schema",
            f"tabular schema validation failed for {path.name}",
            path=str(path),
            kind=kind,
            chunk=locals().get("chunk_number", 0),
            cause=str(exc),
        ) from exc
    return total


def _count_json_rows(path: Path) -> int:
    payload = _read_json(path)
    if not isinstance(payload, list):
        raise _quality_error("count rows", f"expected a JSON array: {path.name}", path=str(path))
    return len(payload)


def _table_row_count(path: Path, kind: str) -> int:
    if kind == "json_array":
        return _count_json_rows(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle, delimiter=_csv_delimiter(path))
        next(reader, None)
        return sum(1 for _ in reader)


def _allowed_change(baseline_count: int, limit: LimitModel) -> int:
    return max(limit.absolute, math.ceil(baseline_count * limit.fraction))


def _validate_thresholds(
    data_dir: Path,
    baseline_dir: Path | None,
    policy: QualityPolicyModel,
    counts: Mapping[str, int],
) -> dict[str, dict[str, int]]:
    comparisons: dict[str, dict[str, int]] = {}
    for filename, table_policy in policy.tables.items():
        current = counts[filename]
        if current < table_policy.minimum_rows:
            raise _quality_error(
                "minimum row count",
                f"{filename} has {current:,} rows; minimum is {table_policy.minimum_rows:,}",
                path=str(data_dir / filename),
                actual=current,
                minimum=table_policy.minimum_rows,
            )
        if baseline_dir is None:
            continue
        baseline_path = baseline_dir / filename
        if not baseline_path.is_file():
            raise _quality_error(
                "change threshold",
                f"baseline file is missing: {filename}",
                path=str(baseline_path),
            )
        baseline = _table_row_count(baseline_path, table_policy.kind)
        drop = max(0, baseline - current)
        delta = abs(current - baseline)
        allowed_drop = _allowed_change(baseline, table_policy.max_row_drop)
        comparisons[filename] = {
            "baseline_rows": baseline,
            "current_rows": current,
            "row_drop": drop,
            "allowed_row_drop": allowed_drop,
            "row_count_change": delta,
        }
        if drop > allowed_drop:
            raise _quality_error(
                "row-drop threshold",
                f"{filename} dropped {drop:,} rows; maximum allowed is {allowed_drop:,}",
                **comparisons[filename],
            )
    return comparisons


def _parse_iso_date(value: str, *, path: Path, column: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise _quality_error(
            "freshness",
            f"invalid date in {path.name}.{column}: {value!r}",
            path=str(path),
            column=column,
            value=value,
        ) from exc


def _latest_csv_date(path: Path, column: str) -> date:
    latest: date | None = None
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=_csv_delimiter(path))
        if column not in (reader.fieldnames or []):
            raise _quality_error("freshness", f"missing freshness column {column} in {path.name}", path=str(path))
        for row in reader:
            value = (row.get(column) or "").strip()
            if value:
                parsed = _parse_iso_date(value, path=path, column=column)
                latest = parsed if latest is None or parsed > latest else latest
    if latest is None:
        raise _quality_error("freshness", f"no dates found in {path.name}.{column}", path=str(path))
    return latest


def _validate_freshness(
    data_dir: Path,
    policy: QualityPolicyModel,
    today: date,
) -> dict[str, str]:
    observed: dict[str, str] = {}
    for filename, table_policy in policy.tables.items():
        freshness = table_policy.freshness
        if freshness is None:
            continue
        latest = _latest_csv_date(data_dir / filename, freshness.column)
        age = (today - latest).days
        if age > freshness.max_age_days or age < -freshness.future_tolerance_days:
            raise _quality_error(
                "freshness",
                f"{filename} latest {freshness.column} is {latest.isoformat()} (age {age} days)",
                latest=latest.isoformat(),
                today=today.isoformat(),
                max_age_days=freshness.max_age_days,
                future_tolerance_days=freshness.future_tolerance_days,
            )
        observed[filename] = latest.isoformat()

    cache_state = CacheStateModel.model_validate(_read_json(data_dir / "cache_state.json"))
    now = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
    for filename, max_age_days in policy.cache_freshness.items():
        metadata = cache_state.files.get(filename)
        if metadata is None:
            raise _quality_error("freshness", f"cache freshness metadata missing for {filename}")
        if metadata.fetchedAt is None:
            raise _quality_error("freshness", f"cache fetchedAt metadata missing for {filename}")
        fetched_at = metadata.fetchedAt.astimezone(timezone.utc)
        age_seconds = (now - fetched_at).total_seconds()
        if age_seconds > max_age_days * 86400 or age_seconds < -2 * 86400:
            raise _quality_error(
                "freshness",
                f"{filename} cache was fetched at {fetched_at.isoformat()}",
                fetched_at=fetched_at.isoformat(),
                today=today.isoformat(),
                max_age_days=max_age_days,
            )
        observed[f"cache:{filename}"] = fetched_at.isoformat()
    return observed


def validate_site_artifacts(site_root: Path, deploy_root: Path | None = None) -> dict[str, int]:
    """Validate both generated source pages and the exact deploy directory."""

    for relative_name in GENERATED_SITE_FILES:
        path = site_root / relative_name
        if not path.is_file() or path.stat().st_size < 100:
            raise _quality_error(
                "validate generated site",
                f"generated site file is missing or incomplete: {relative_name}",
                path=str(path),
            )
    app_text = (site_root / "app.html").read_text(encoding="utf-8-sig")
    frontend_text = "\n".join(
        (site_root / relative_name).read_text(encoding="utf-8-sig")
        for relative_name in (
            "assets/js/app.js",
            "assets/js/data-loader.js",
            "assets/js/generated-data.js",
            "assets/js/tabs/roadtogs.js",
        )
    )
    required_markers = ('id="rankings-table"', "__WTA_RANKINGS_LATEST__")
    combined_text = app_text + frontend_text
    missing_markers = [marker for marker in required_markers if marker not in combined_text]
    if missing_markers:
        raise _quality_error(
            "validate generated site",
            "app.html is missing required ranking integration markers",
            path=str(site_root / "app.html"),
            missing=missing_markers,
        )

    deploy_files = 0
    if deploy_root is not None:
        required_deploy = (
            ".nojekyll",
            "app.html",
            "index.html",
            "rankings/index.html",
            "assets/app.css",
            "assets/js/app.js",
            "assets/js/generated-data.js",
            "data/wta_rankings_latest_bundle.js",
            "data/player_aliases_wta_itf_bundle.js",
        )
        for relative_name in required_deploy:
            path = deploy_root / relative_name
            if not path.is_file():
                raise _quality_error(
                    "validate deploy site",
                    f"deploy artifact is missing {relative_name}",
                    path=str(path),
                )
        if not any((deploy_root / "data").glob("wta_rankings_[0-9][0-9][0-9][0-9]_bundle.js")):
            raise _quality_error(
                "validate deploy site",
                "deploy artifact has no lazy yearly ranking bundles",
                path=str(deploy_root / "data"),
            )
        deploy_files = sum(1 for path in deploy_root.rglob("*") if path.is_file())
    return {
        "generated_files": sum(1 for path in site_root.rglob("*") if path.is_file()),
        "deploy_files": deploy_files,
    }


def run_data_quality_gate(
    data_dir: Path | str = DEFAULT_DATA_DIR,
    *,
    baseline_dir: Path | str | None = None,
    policy_path: Path | str = DEFAULT_POLICY_PATH,
    today: date | None = None,
    site_root: Path | str | None = None,
    deploy_root: Path | str | None = None,
) -> dict[str, Any]:
    """Run every blocking check and return a machine-readable report."""

    resolved_data = Path(data_dir).resolve()
    resolved_baseline = Path(baseline_dir).resolve() if baseline_dir is not None else None
    if not resolved_data.is_dir():
        raise _quality_error("start gate", "data directory does not exist", path=str(resolved_data))
    policy = _load_policy(Path(policy_path).resolve())

    pydantic_counts = _validate_pydantic_json(resolved_data)
    table_counts: dict[str, int] = {}
    for filename, table_policy in policy.tables.items():
        path = resolved_data / filename
        if not path.is_file():
            raise _quality_error("required tables", f"required table is missing: {filename}", path=str(path))
        if table_policy.kind in {"rankings", "matches"}:
            table_counts[filename] = _validate_tabular_file(path, table_policy.kind)
        else:
            table_counts[filename] = _count_json_rows(path)

    try:
        canonical = validate_project_data(resolved_data)
    except (CanonicalConstraintError, OSError, UnicodeError, csv.Error, json.JSONDecodeError) as exc:
        raise _quality_error(
            "unique keys and referential integrity",
            "canonical data constraints failed",
            path=str(resolved_data),
            cause=str(exc),
        ) from exc

    comparisons = _validate_thresholds(resolved_data, resolved_baseline, policy, table_counts)
    observed_freshness = _validate_freshness(resolved_data, policy, today or utc_now().date())
    site = None
    if site_root is not None:
        site = validate_site_artifacts(
            Path(site_root).resolve(),
            Path(deploy_root).resolve() if deploy_root is not None else None,
        )
    return {
        "status": "passed",
        "validated_at": utc_now().isoformat().replace("+00:00", "Z"),
        "data_dir": str(resolved_data),
        "baseline_dir": str(resolved_baseline) if resolved_baseline is not None else None,
        "policy_version": policy.schema_version,
        "schema_validation": pydantic_counts,
        "table_rows": table_counts,
        "canonical": canonical,
        "freshness": observed_freshness,
        "thresholds": comparisons,
        "site": site,
    }


def _atomic_write_report(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the blocking WTARG data-quality gate.")
    parser.add_argument("command", choices=("validate",))
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--baseline-dir")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY_PATH))
    parser.add_argument("--site-root")
    parser.add_argument("--deploy-root")
    parser.add_argument("--report")
    args = parser.parse_args(argv)
    try:
        report = run_data_quality_gate(
            args.data_dir,
            baseline_dir=args.baseline_dir,
            policy_path=args.policy,
            site_root=args.site_root,
            deploy_root=args.deploy_root,
        )
    except DataValidationError as exc:
        print(
            json.dumps({"status": "failed", "error": exc.as_dict()}, ensure_ascii=False, indent=2),
            file=sys.stderr,
        )
        return 1
    if args.report:
        _atomic_write_report(Path(args.report), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
