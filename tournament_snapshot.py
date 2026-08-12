"""Typed contract and versioned codec for the active tournament snapshot."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import date
from typing import Literal, TypedDict

TOURNAMENT_SNAPSHOT_SCHEMA_VERSION: Literal[1] = 1
TOURNAMENT_SNAPSHOT_FIELDS = (
    "name",
    "level",
    "surface",
    "country",
    "startDate",
    "endDate",
    "week",
)

_WTA_TOURNAMENT_KEY_RE = re.compile(
    r"https://www\.wtatennis\.com/tournaments/[0-9]+/[^/\s]+/[0-9]{4}/player-list"
)
_ITF_TOURNAMENT_KEY_RE = re.compile(r"w-itf-[a-z]{3}-[0-9]{4}-[0-9]{3}")


class TournamentSnapshotRecord(TypedDict):
    """Normalized internal representation of one active tournament."""

    name: str
    level: str
    surface: str
    country: str
    startDate: str
    endDate: str
    week: str


class TournamentSnapshotDocument(TypedDict):
    """Versioned JSON representation persisted in tournament_snapshot.json."""

    schemaVersion: Literal[1]
    fields: list[str]
    tournaments: dict[str, list[str]]


def normalize_tournament_snapshot_key(value: object) -> str:
    """Return a canonical WTA URL or lowercase ITF tournament ID."""

    if not isinstance(value, str):
        raise ValueError("tournament snapshot keys must be strings")
    key = value.strip()
    if _WTA_TOURNAMENT_KEY_RE.fullmatch(key):
        return key
    lowered = key.lower()
    if _ITF_TOURNAMENT_KEY_RE.fullmatch(lowered):
        return lowered
    raise ValueError(f"unsupported tournament snapshot key: {value!r}")


def tournament_snapshot_source(key: str) -> Literal["wta", "itf"]:
    """Identify the source represented by a validated mixed-format key."""

    normalized = normalize_tournament_snapshot_key(key)
    return "wta" if normalized.startswith("https://") else "itf"


def _text(value: object, field: str, *, required: bool = False) -> str:
    normalized = "" if value is None else str(value).strip()
    if required and not normalized:
        raise ValueError(f"tournament snapshot {field} must not be blank")
    return normalized


def _date_only(value: object, field: str, *, required: bool = False) -> str:
    normalized = _text(value, field, required=required)
    if not normalized:
        return ""
    candidate = normalized[:10]
    try:
        date.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"tournament snapshot {field} must use YYYY-MM-DD") from exc
    return candidate


def normalize_tournament_snapshot_record(
    value: Mapping[str, object],
) -> TournamentSnapshotRecord:
    """Normalize one mapping at the ingestion boundary."""

    start_date = _date_only(value.get("startDate"), "startDate", required=True)
    end_date = _date_only(value.get("endDate"), "endDate")
    if end_date and end_date < start_date:
        raise ValueError("tournament snapshot endDate must not precede startDate")
    return TournamentSnapshotRecord(
        name=_text(value.get("name"), "name", required=True),
        level=_text(value.get("level"), "level", required=True),
        surface=_text(value.get("surface"), "surface"),
        country=_text(value.get("country"), "country", required=True).upper(),
        startDate=start_date,
        endDate=end_date,
        week=_text(value.get("week"), "week", required=True),
    )


def compress_tournament_snapshot(
    payload: Mapping[str, Mapping[str, object]],
) -> TournamentSnapshotDocument:
    """Convert typed records into the versioned compact JSON representation."""

    tournaments: dict[str, list[str]] = {}
    for raw_key, raw_record in payload.items():
        key = normalize_tournament_snapshot_key(raw_key)
        record = normalize_tournament_snapshot_record(raw_record)
        tournaments[key] = [
            record["name"],
            record["level"],
            record["surface"],
            record["country"],
            record["startDate"],
            record["endDate"],
            record["week"],
        ]
    return TournamentSnapshotDocument(
        schemaVersion=TOURNAMENT_SNAPSHOT_SCHEMA_VERSION,
        fields=list(TOURNAMENT_SNAPSHOT_FIELDS),
        tournaments=tournaments,
    )


def expand_tournament_snapshot(payload: object) -> dict[str, TournamentSnapshotRecord]:
    """Expand current or legacy compact JSON into normalized typed records."""

    if not isinstance(payload, Mapping):
        raise ValueError("tournament snapshot must be a JSON object")

    if "schemaVersion" in payload:
        if payload.get("schemaVersion") != TOURNAMENT_SNAPSHOT_SCHEMA_VERSION:
            raise ValueError(f"unsupported tournament snapshot schemaVersion: {payload.get('schemaVersion')!r}")
        if payload.get("fields") != list(TOURNAMENT_SNAPSHOT_FIELDS):
            raise ValueError("tournament snapshot fields do not match the supported contract")
        raw_tournaments = payload.get("tournaments")
        if not isinstance(raw_tournaments, Mapping):
            raise ValueError("tournament snapshot tournaments must be an object")
    else:
        # Read compatibility for snapshots written before schemaVersion 1.
        raw_tournaments = payload

    expanded: dict[str, TournamentSnapshotRecord] = {}
    for raw_key, raw_record in raw_tournaments.items():
        key = normalize_tournament_snapshot_key(raw_key)
        if isinstance(raw_record, list):
            if len(raw_record) != len(TOURNAMENT_SNAPSHOT_FIELDS):
                raise ValueError(f"tournament snapshot row for {key!r} must contain seven values")
            record_mapping = dict(zip(TOURNAMENT_SNAPSHOT_FIELDS, raw_record, strict=True))
        elif isinstance(raw_record, Mapping):
            record_mapping = dict(raw_record)
        else:
            raise ValueError(f"tournament snapshot row for {key!r} must be an array or object")
        expanded[key] = normalize_tournament_snapshot_record(record_mapping)
    return expanded


def dumps_tournament_snapshot(
    payload: TournamentSnapshotDocument,
    *,
    ensure_ascii: bool = False,
    indent: int = 2,
) -> str:
    """Serialize the compact document with one reviewable tournament per line."""

    del indent  # The contract has a fixed compact-but-readable layout.
    lines = ["{"]
    lines.append(f'  "schemaVersion": {payload["schemaVersion"]},')
    lines.append(
        '  "fields": '
        + json.dumps(payload["fields"], ensure_ascii=ensure_ascii, separators=(",", ":"))
        + ","
    )
    lines.append('  "tournaments": {')
    items = list(payload["tournaments"].items())
    for index, (key, row) in enumerate(items):
        comma = "," if index < len(items) - 1 else ""
        lines.append(
            "    "
            + json.dumps(key, ensure_ascii=ensure_ascii)
            + ": "
            + json.dumps(row, ensure_ascii=ensure_ascii, separators=(",", ":"))
            + comma
        )
    lines.append("  }")
    lines.append("}")
    return "\n".join(lines)
