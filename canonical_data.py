"""Canonical identity and natural-key definitions for the WTARG data pipeline.

The repository still stores source-compatible CSV/JSON files, but all identity,
ranking, match, and tournament uniqueness rules live here.  Loaders and
validators must use these definitions instead of inventing local keys.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

PLAYER_FIELDS = (
    "player_key",
    "display_name",
    "country",
    "dob",
    "wta_id",
    "wta_name",
    "itf_id",
    "itf_name",
    "bjkc_id",
    "bjkc_name",
    "aliases",
    "additional_wta_ids",
    "additional_itf_ids",
    "additional_bjkc_ids",
)

RANKING_FILENAMES = (
    "wta_rankings_83_99.csv",
    "wta_rankings_00_09.csv",
    "wta_rankings_10_19.csv",
    "wta_rankings_20_29.csv",
)

MATCH_SOURCES = {
    "wta_matches_arg.csv": "wta",
    "itf_matches_arg.csv": "itf",
    "gs_matches_arg.csv": "grand_slam",
    "og_matches_arg.csv": "olympics",
    "bjkc_matches_arg.csv": "bjkc",
    "united_cup_matches_arg.csv": "united_cup",
    "manually_added_matches.csv": "manual",
}


class CanonicalConstraintError(ValueError):
    """Raised when a canonical table violates an identity or key constraint."""


def compact_text(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def normalized_name(value: object) -> str:
    text = compact_text(value).casefold()
    folded = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in folded if not unicodedata.combining(ch))


def normalized_identifier(value: object) -> str:
    text = compact_text(value)
    return "" if text.casefold() in {"", "nan", "none", "null", "unknown"} else text


def _normalized_id_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    result = []
    for item in value:
        identifier = normalized_identifier(item)
        if identifier and identifier not in result:
            result.append(identifier)
    return tuple(result)


def make_player_key(row: Mapping[str, object]) -> str:
    """Build a deterministic key for a new identity.

    Persisted ``player_key`` values always win.  Source IDs are preferred over
    names so spelling changes do not change an existing identity.
    """

    current = compact_text(row.get("player_key"))
    if current:
        return current
    wta_id = normalized_identifier(row.get("wta_id"))
    if wta_id:
        return f"wta:{wta_id}"
    itf_id = normalized_identifier(row.get("itf_id"))
    if itf_id:
        return f"itf:{itf_id}"
    bjkc_name = normalized_name(row.get("bjkc_name"))
    if bjkc_name:
        return f"bjkc:{_slug(bjkc_name)}"
    display_name = normalized_name(row.get("display_name"))
    if display_name:
        return f"name:{_slug(display_name)}"
    raise CanonicalConstraintError("player row has neither a player_key, source ID, nor display name")


@dataclass(frozen=True)
class PlayerRecord:
    player_key: str
    display_name: str
    presentation_name: str = ""
    country: str = ""
    dob: str = ""
    wta_id: str = ""
    wta_name: str = ""
    itf_id: str = ""
    itf_name: str = ""
    bjkc_id: str = ""
    bjkc_name: str = ""
    aliases: tuple[str, ...] = ()
    additional_wta_ids: tuple[str, ...] = ()
    additional_itf_ids: tuple[str, ...] = ()
    additional_bjkc_ids: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, row: Mapping[str, object]) -> PlayerRecord:
        display_name = compact_text(
            row.get("display_name") or row.get("wta_name") or row.get("itf_name") or row.get("bjkc_name")
        )
        aliases: list[str] = []
        raw_aliases = row.get("aliases")
        for value in raw_aliases if isinstance(raw_aliases, list) else []:
            text = compact_text(value)
            if text and text not in aliases:
                aliases.append(text)
        return cls(
            player_key=make_player_key(row),
            display_name=display_name,
            presentation_name=compact_text(row.get("presentation_name")) or display_name,
            country=compact_text(row.get("country")).upper(),
            dob=compact_text(row.get("dob"))[:10],
            wta_id=normalized_identifier(row.get("wta_id")),
            wta_name=compact_text(row.get("wta_name")),
            itf_id=normalized_identifier(row.get("itf_id")),
            itf_name=compact_text(row.get("itf_name")),
            bjkc_id=normalized_identifier(row.get("bjkc_id")),
            bjkc_name=compact_text(row.get("bjkc_name")),
            aliases=tuple(aliases),
            additional_wta_ids=_normalized_id_list(row.get("additional_wta_ids")),
            additional_itf_ids=_normalized_id_list(row.get("additional_itf_ids")),
            additional_bjkc_ids=_normalized_id_list(row.get("additional_bjkc_ids")),
        )

    def source_ids(self, source: str) -> tuple[str, ...]:
        if source == "wta":
            values = (self.wta_id, *self.additional_wta_ids)
        elif source == "itf":
            values = (self.itf_id, *self.additional_itf_ids)
        elif source == "bjkc":
            values = (self.bjkc_id, *self.additional_bjkc_ids)
        else:
            values = ()
        return tuple(value for value in values if value)

    def names(self) -> tuple[str, ...]:
        values = (
            self.display_name,
            self.presentation_name,
            self.wta_name,
            self.itf_name,
            self.bjkc_name,
            *self.aliases,
        )
        result = []
        for value in values:
            text = compact_text(value)
            if text and text not in result:
                result.append(text)
        return tuple(result)


class PlayerIdentityIndex:
    """Validated player table plus deterministic ID/name indexes."""

    def __init__(self, rows: Iterable[Mapping[str, object]]):
        records = []
        for row_number, row in enumerate(rows, 1):
            if not isinstance(row, Mapping):
                raise CanonicalConstraintError(f"player row {row_number} is not an object")
            records.append(PlayerRecord.from_mapping(row))
        self.records = tuple(records)
        self.by_key: dict[str, PlayerRecord] = {}
        self.by_display_name: dict[str, PlayerRecord] = {}
        self.by_wta_id: dict[str, PlayerRecord] = {}
        self.by_itf_id: dict[str, PlayerRecord] = {}
        self.by_bjkc_id: dict[str, PlayerRecord] = {}
        name_candidates: dict[str, dict[str, PlayerRecord]] = {}

        for record in self.records:
            if not record.display_name:
                raise CanonicalConstraintError(f"{record.player_key}: display_name is required")
            if record.player_key in self.by_key:
                raise CanonicalConstraintError(f"duplicate player_key: {record.player_key}")
            self.by_key[record.player_key] = record
            display_key = normalized_name(record.display_name)
            previous_display = self.by_display_name.get(display_key)
            if previous_display:
                raise CanonicalConstraintError(
                    f"display name {record.display_name!r} is shared by "
                    f"{previous_display.player_key} and {record.player_key}"
                )
            self.by_display_name[display_key] = record

            self._add_source_ids(self.by_wta_id, record.source_ids("wta"), record, "WTA")
            self._add_source_ids(self.by_itf_id, record.source_ids("itf"), record, "ITF")
            self._add_source_ids(self.by_bjkc_id, record.source_ids("bjkc"), record, "BJKC")

            for name in record.names():
                key = normalized_name(name)
                if key:
                    name_candidates.setdefault(key, {})[record.player_key] = record

        self.name_candidates = {key: tuple(candidates.values()) for key, candidates in name_candidates.items()}
        self.by_unique_name = {
            key: candidates[0] for key, candidates in self.name_candidates.items() if len(candidates) == 1
        }
        self.ambiguous_names = {
            key: candidates for key, candidates in self.name_candidates.items() if len(candidates) > 1
        }

    @staticmethod
    def _add_source_ids(
        target: dict[str, PlayerRecord],
        values: Iterable[str],
        record: PlayerRecord,
        label: str,
    ) -> None:
        for value in values:
            if label == "WTA" and not value.isdigit():
                raise CanonicalConstraintError(f"{record.player_key}: invalid WTA ID {value!r}")
            if label == "ITF" and (not value.isdigit() or not value.startswith("800")):
                raise CanonicalConstraintError(f"{record.player_key}: invalid ITF ID {value!r}")
            previous = target.get(value)
            if previous and previous.player_key != record.player_key:
                raise CanonicalConstraintError(
                    f"{label} ID {value} maps to both {previous.player_key} and {record.player_key}"
                )
            target[value] = record

    def resolve(self, source: str, *, player_id: object = "", name: object = "") -> PlayerRecord | None:
        identifier = normalized_identifier(player_id)
        if identifier:
            if source == "wta":
                return self.by_wta_id.get(identifier)
            if source == "itf":
                return self.by_itf_id.get(identifier)
            if source == "bjkc":
                return self.by_bjkc_id.get(identifier)
        return self.by_unique_name.get(normalized_name(name))

    def resolve_any_id(self, player_id: object) -> PlayerRecord | None:
        identifier = normalized_identifier(player_id)
        if not identifier:
            return None
        wta_record = self.by_wta_id.get(identifier)
        itf_record = self.by_itf_id.get(identifier)
        bjkc_record = self.by_bjkc_id.get(identifier)
        records = {record.player_key: record for record in (wta_record, itf_record, bjkc_record) if record}
        if len(records) > 1:
            return None
        return next(iter(records.values()), None)


@dataclass(frozen=True)
class RankingRecord:
    week_date: str
    player_key: str
    rank: int
    points: int
    country: str
    dob: str


@dataclass(frozen=True)
class TournamentRecord:
    tournament_key: str
    source: str
    source_id: str
    season: str
    name: str


@dataclass(frozen=True)
class MatchRecord:
    source: str
    source_match_key: str
    tournament_key: str
    date: str
    winner_key: str
    loser_key: str


def _slug(value: object) -> str:
    text = normalized_name(value)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "unknown"


def tournament_key(row: Mapping[str, object], source: str) -> str:
    source = compact_text(source).casefold() or "unknown"
    source_id = normalized_identifier(row.get("tournamentId")) or _slug(row.get("tournamentName"))
    date_value = compact_text(row.get("date"))[:10]
    season = date_value[:4] or "unknown"
    name = _slug(row.get("tournamentName"))
    return f"{source}:{source_id}:{season}:{name}"


def canonical_tournament(row: Mapping[str, object], source: str) -> TournamentRecord:
    source = compact_text(source).casefold() or "unknown"
    source_id = normalized_identifier(row.get("tournamentId")) or _slug(row.get("tournamentName"))
    date_value = compact_text(row.get("date"))[:10]
    return TournamentRecord(
        tournament_key=tournament_key(row, source),
        source=source,
        source_id=source_id,
        season=date_value[:4] or "unknown",
        name=compact_text(row.get("tournamentName")),
    )


def source_match_key(row: Mapping[str, object], source: str) -> str:
    """Return the documented natural key used by one match source."""

    source = compact_text(source).casefold()
    match_id = normalized_identifier(row.get("matchId"))
    date_value = compact_text(row.get("date"))[:10]
    season = date_value[:4]
    tournament_id = normalized_identifier(row.get("tournamentId"))
    round_name = _slug(row.get("roundName"))
    winner_id = normalized_identifier(row.get("winnerId")) or _slug(row.get("winnerName"))
    loser_id = normalized_identifier(row.get("loserId")) or _slug(row.get("loserName"))

    if not match_id:
        raise CanonicalConstraintError(f"{source or 'unknown'} match is missing matchId")
    if source == "wta":
        if not tournament_id or not season:
            raise CanonicalConstraintError("WTA match natural key requires tournamentId and a dated season")
        return f"{tournament_id}:{season}:{match_id}"
    if source == "itf":
        tournament_part = tournament_id or _slug(row.get("tournamentName"))
        if tournament_part == "unknown" or not season:
            raise CanonicalConstraintError("ITF match natural key requires a tournament identifier and a dated season")
        return f"{tournament_part}:{season}:{match_id}"
    if source == "grand_slam":
        return f"{match_id}:{_slug(row.get('draw'))}:{round_name}:{winner_id}:{loser_id}"
    if source == "bjkc":
        return f"{tournament_id}:{season}:{round_name}:{match_id}:{winner_id}:{loser_id}"
    if source == "united_cup":
        return f"{season}:{round_name}:{match_id}:{winner_id}:{loser_id}"
    return f"{tournament_id}:{date_value}:{round_name}:{match_id}:{winner_id}:{loser_id}"


def canonical_match_key(row: Mapping[str, object], source: str) -> tuple[str, str]:
    source = compact_text(source).casefold()
    return source, source_match_key(row, source)


def _player_key_for_match_id(index: PlayerIdentityIndex, source: str, player_id: object, name: object) -> str:
    name_key = normalized_name(name)
    if name_key in {"bye", "unknown"}:
        return f"special:{name_key}"
    identifier = normalized_identifier(player_id)
    if source == "bjkc":
        record = index.resolve("bjkc", name=name)
        if record:
            return record.player_key
        return f"bjkc-side:{identifier or _slug(name)}"
    if source == "united_cup":
        record = index.resolve("wta", player_id=identifier, name=name)
    else:
        record = index.resolve_any_id(identifier)
    if record:
        return record.player_key
    if identifier:
        namespace = "itf" if identifier.startswith("800") else "wta"
        return f"{namespace}:{identifier}"
    return f"name:{_slug(name)}"


def canonical_match(row: Mapping[str, object], source: str, index: PlayerIdentityIndex) -> MatchRecord:
    return MatchRecord(
        source=source,
        source_match_key=source_match_key(row, source),
        tournament_key=tournament_key(row, source),
        date=compact_text(row.get("date"))[:10],
        winner_key=_player_key_for_match_id(index, source, row.get("winnerId"), row.get("winnerName")),
        loser_key=_player_key_for_match_id(index, source, row.get("loserId"), row.get("loserName")),
    )


def canonical_ranking(row: Mapping[str, object], index: PlayerIdentityIndex) -> RankingRecord:
    player_id = normalized_identifier(row.get("id"))
    record = index.by_wta_id.get(player_id)
    if not record:
        raise CanonicalConstraintError(f"WTA ID {player_id} is missing from the player table")
    return RankingRecord(
        week_date=compact_text(row.get("week_date")),
        player_key=record.player_key,
        rank=int(compact_text(row.get("rank"))),
        points=int(compact_text(row.get("points")) or 0),
        country=compact_text(row.get("country")).upper(),
        dob=compact_text(row.get("dob"))[:10],
    )


def validate_rankings(data_dir: Path, index: PlayerIdentityIndex) -> int:
    total = 0
    seen_dates: set[str] = set()
    for filename in RANKING_FILENAMES:
        path = data_dir / filename
        previous_date = ""
        ids_for_date: set[str] = set()
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for line_number, row in enumerate(csv.DictReader(handle), 2):
                week_date = compact_text(row.get("week_date"))
                player_id = normalized_identifier(row.get("id"))
                if not week_date or not player_id:
                    raise CanonicalConstraintError(f"{filename}:{line_number}: week_date and id are required")
                if previous_date and week_date < previous_date:
                    raise CanonicalConstraintError(f"{filename}:{line_number}: rankings are not date-sorted")
                if week_date != previous_date:
                    if week_date in seen_dates:
                        raise CanonicalConstraintError(f"ranking date appears in multiple partitions: {week_date}")
                    if previous_date:
                        seen_dates.add(previous_date)
                    ids_for_date = set()
                    previous_date = week_date
                if player_id in ids_for_date:
                    raise CanonicalConstraintError(
                        f"{filename}:{line_number}: duplicate ranking key ({week_date}, {player_id})"
                    )
                ids_for_date.add(player_id)
                try:
                    ranking = canonical_ranking(row, index)
                except CanonicalConstraintError as exc:
                    raise CanonicalConstraintError(f"{filename}:{line_number}: {exc}") from exc
                except ValueError as exc:
                    raise CanonicalConstraintError(
                        f"{filename}:{line_number}: rank and points must be integers"
                    ) from exc
                if ranking.rank <= 0 or ranking.points < 0:
                    raise CanonicalConstraintError(f"{filename}:{line_number}: invalid rank/points")
                total += 1
        if previous_date:
            seen_dates.add(previous_date)
    return total


def validate_matches(data_dir: Path, index: PlayerIdentityIndex) -> tuple[int, int]:
    total = 0
    tournament_keys: set[str] = set()
    seen_match_keys: set[tuple[str, str]] = set()
    for filename, source in MATCH_SOURCES.items():
        path = data_dir / filename
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for line_number, row in enumerate(csv.DictReader(handle), 2):
                key = canonical_match_key(row, source)
                if key in seen_match_keys:
                    raise CanonicalConstraintError(f"{filename}:{line_number}: duplicate canonical match key {key}")
                seen_match_keys.add(key)
                record = canonical_match(row, source, index)
                if not record.date or not compact_text(row.get("winnerName")):
                    raise CanonicalConstraintError(f"{filename}:{line_number}: match date/winner is required")
                if not compact_text(row.get("loserName")):
                    raise CanonicalConstraintError(f"{filename}:{line_number}: loser must be explicit or Unknown")
                if (
                    source == "itf" and compact_text(row.get("resultStatusDesc")).casefold() == "walkover"
                ) and compact_text(row.get("result")) != "W/O":
                    raise CanonicalConstraintError(f"{filename}:{line_number}: walkover result must be W/O")
                for side in ("winner", "loser"):
                    player_id = normalized_identifier(row.get(f"{side}Id"))
                    player_name = normalized_name(row.get(f"{side}Name"))
                    if player_name in {"bye", "unknown"}:
                        continue
                    if player_id.startswith("800") and player_id not in index.by_itf_id:
                        raise CanonicalConstraintError(
                            f"{filename}:{line_number}: ITF ID {player_id} is missing from the player table"
                        )
                    if (
                        source in {"wta", "united_cup"}
                        and player_id.isdigit()
                        and not player_id.startswith("800")
                        and player_id not in index.by_wta_id
                    ):
                        raise CanonicalConstraintError(
                            f"{filename}:{line_number}: WTA ID {player_id} is missing from the player table"
                        )
                tournament = canonical_tournament(row, source)
                tournament_keys.add(tournament.tournament_key)
                total += 1
    return total, len(tournament_keys)


def load_player_rows(path: Path) -> list[dict]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, list):
        raise CanonicalConstraintError(f"{path}: player table must be a JSON array")
    for row_number, row in enumerate(value, 1):
        if not isinstance(row, dict):
            raise CanonicalConstraintError(f"{path}: player row {row_number} is not an object")
    return value


def write_player_rows(path: Path, rows: Iterable[Mapping[str, object]]) -> None:
    """Atomically persist the canonical player schema in stable order."""
    normalized_rows = []
    for row in rows:
        normalized = {
            field: row.get(
                field,
                [] if field.startswith("additional_") or field == "aliases" else "",
            )
            for field in PLAYER_FIELDS
        }
        presentation_name = compact_text(row.get("presentation_name"))
        if presentation_name and presentation_name != compact_text(normalized["display_name"]):
            normalized = {
                "player_key": normalized["player_key"],
                "display_name": normalized["display_name"],
                "presentation_name": presentation_name,
                **{field: value for field, value in normalized.items() if field not in {"player_key", "display_name"}},
            }
        normalized_rows.append(normalized)
    normalized_rows.sort(
        key=lambda row: (
            compact_text(row.get("display_name")).casefold(),
            compact_text(row.get("player_key")),
        )
    )
    temp_path = path.with_name(path.name + ".tmp")
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write("[\n")
            for index, row in enumerate(normalized_rows):
                suffix = "," if index + 1 < len(normalized_rows) else ""
                handle.write("  " + json.dumps(row, ensure_ascii=False) + suffix + "\n")
            handle.write("]\n")
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _verified_name_country_identity_row(
    rows: list[dict],
    *,
    name: object,
    country: object,
) -> dict | None:
    """Return the sole identity matching exact name and country.

    DOB is supplemental metadata and is not used to decide whether source
    profiles represent the same player. Missing name/country metadata or more
    than one qualifying identity still require manual review instead.
    """
    name_key = normalized_name(name)
    country_key = compact_text(country).upper()
    if not name_key or not country_key:
        return None

    matches = []
    for row in rows:
        record = PlayerRecord.from_mapping(row)
        if record.country != country_key:
            continue
        if name_key not in {normalized_name(value) for value in record.names()}:
            continue
        matches.append(row)
    return matches[0] if len(matches) == 1 else None


def _append_unique(row: dict, field: str, value: str) -> None:
    values = row.get(field)
    normalized = list(values) if isinstance(values, list) else []
    if value and value not in normalized:
        normalized.append(value)
    row[field] = normalized


def _link_source_identity(
    row: dict,
    *,
    source: str,
    player_id: str,
    source_name: str,
    country: str,
    dob: str,
) -> None:
    """Attach a newly observed source ID to a verified canonical identity."""
    primary_field = f"{source}_id"
    additional_field = f"additional_{source}_ids"
    name_field = f"{source}_name"
    current_primary = normalized_identifier(row.get(primary_field))
    if current_primary:
        if current_primary != player_id:
            _append_unique(row, additional_field, player_id)
    else:
        row[primary_field] = player_id
        if source == "wta":
            row["player_key"] = f"wta:{player_id}"

    current_source_name = compact_text(row.get(name_field))
    if not current_source_name:
        row[name_field] = source_name
    elif source_name and normalized_name(source_name) != normalized_name(current_source_name):
        _append_unique(row, "aliases", source_name)

    if not compact_text(row.get("country")):
        row["country"] = country
    if not compact_text(row.get("dob")):
        row["dob"] = dob


def sync_wta_players(path: Path, ranking_rows: Iterable[Mapping[str, object]]) -> int:
    """Add new WTA IDs, linking identities with matching name and country."""
    rows = load_player_rows(path)
    index = PlayerIdentityIndex(rows)
    added = 0
    for ranking in ranking_rows:
        player_id = normalized_identifier(ranking.get("id") or ranking.get("Id"))
        if not player_id or player_id in index.by_wta_id:
            continue
        display_name = (
            compact_text(ranking.get("player") or ranking.get("OfficialPlayer") or ranking.get("Player"))
            or f"WTA player {player_id}"
        )
        country = compact_text(ranking.get("country") or ranking.get("Country")).upper()
        dob = compact_text(ranking.get("dob") or ranking.get("DOB"))[:10]
        verified_row = _verified_name_country_identity_row(
            rows,
            name=display_name,
            country=country,
        )
        if verified_row is not None:
            _link_source_identity(
                verified_row,
                source="wta",
                player_id=player_id,
                source_name=display_name,
                country=country,
                dob=dob,
            )
            index = PlayerIdentityIndex(rows)
            added += 1
            continue
        canonical_display = display_name
        presentation_name = ""
        if normalized_name(canonical_display) in index.by_display_name:
            canonical_display = f"{display_name} (WTA {player_id})"
            presentation_name = display_name
        new_row = {
            "player_key": f"wta:{player_id}",
            "display_name": canonical_display,
            "presentation_name": presentation_name,
            "country": country,
            "dob": dob,
            "wta_id": player_id,
            "wta_name": display_name,
            "itf_id": "",
            "itf_name": "",
            "bjkc_id": "",
            "bjkc_name": "",
            "aliases": [],
            "additional_wta_ids": [],
            "additional_itf_ids": [],
            "additional_bjkc_ids": [],
        }
        rows.append(new_row)
        # Avoid rebuilding the complete index for every addition while still
        # rejecting duplicate IDs within the incoming ranking.
        new_record = PlayerRecord.from_mapping(new_row)
        index.by_wta_id[player_id] = new_record
        index.by_display_name[normalized_name(canonical_display)] = new_record
        added += 1
    if added:
        PlayerIdentityIndex(rows)
        write_player_rows(path, rows)
    return added


def sync_itf_players(path: Path, match_rows: Iterable[Mapping[str, object]]) -> int:
    """Add new ITF IDs, linking identities with matching name and country."""
    rows = load_player_rows(path)
    index = PlayerIdentityIndex(rows)
    added = 0
    for match in match_rows:
        for side in ("winner", "loser"):
            player_id = normalized_identifier(match.get(f"{side}Id"))
            source_name = compact_text(match.get(f"{side}Name"))
            if (
                not player_id.startswith("800")
                or player_id in index.by_itf_id
                or normalized_name(source_name) in {"", "bye", "unknown"}
            ):
                continue
            country = compact_text(match.get(f"{side}Country")).upper()
            dob = compact_text(
                match.get(f"{side}Dob") or match.get(f"{side}DOB")
            )[:10]
            verified_row = _verified_name_country_identity_row(
                rows,
                name=source_name,
                country=country,
            )
            if verified_row is not None:
                _link_source_identity(
                    verified_row,
                    source="itf",
                    player_id=player_id,
                    source_name=source_name,
                    country=country,
                    dob=dob,
                )
                index = PlayerIdentityIndex(rows)
                added += 1
                continue
            canonical_display = source_name
            presentation_name = ""
            if normalized_name(canonical_display) in index.by_display_name:
                canonical_display = f"{source_name} (ITF {player_id})"
                presentation_name = source_name
            new_row = {
                "player_key": f"itf:{player_id}",
                "display_name": canonical_display,
                "presentation_name": presentation_name,
                "country": country,
                "dob": dob,
                "wta_id": "",
                "wta_name": "",
                "itf_id": player_id,
                "itf_name": source_name,
                "bjkc_id": "",
                "bjkc_name": "",
                "aliases": [],
                "additional_wta_ids": [],
                "additional_itf_ids": [],
                "additional_bjkc_ids": [],
            }
            rows.append(new_row)
            new_record = PlayerRecord.from_mapping(new_row)
            index.by_itf_id[player_id] = new_record
            index.by_display_name[normalized_name(canonical_display)] = new_record
            added += 1
    if added:
        PlayerIdentityIndex(rows)
        write_player_rows(path, rows)
    return added


def sync_wta_match_players(path: Path, match_rows: Iterable[Mapping[str, object]]) -> int:
    """Add WTA IDs first observed in match data rather than a ranking."""
    ranking_rows = []
    for match in match_rows:
        for side in ("winner", "loser"):
            player_id = normalized_identifier(match.get(f"{side}Id"))
            source_name = compact_text(match.get(f"{side}Name"))
            if (
                not player_id.isdigit()
                or player_id.startswith("800")
                or normalized_name(source_name) in {"", "bye", "unknown"}
            ):
                continue
            ranking_rows.append(
                {
                    "id": player_id,
                    "player": source_name,
                    "country": compact_text(match.get(f"{side}Country")).upper(),
                    "dob": "",
                }
            )
    return sync_wta_players(path, ranking_rows)


def validate_project_data(data_dir: Path | str) -> dict[str, int]:
    data_dir = Path(data_dir)
    player_rows = load_player_rows(data_dir / "player_aliases_wta_itf.json")
    for row_number, row in enumerate(player_rows, 1):
        missing = [field for field in PLAYER_FIELDS if field not in row]
        if missing:
            raise CanonicalConstraintError(f"player row {row_number} is missing canonical fields: {', '.join(missing)}")
    index = PlayerIdentityIndex(player_rows)
    ranking_count = validate_rankings(data_dir, index)
    match_count, tournament_count = validate_matches(data_dir, index)
    return {
        "players": len(index.records),
        "rankings": ranking_count,
        "matches": match_count,
        "tournaments": tournament_count,
        "ambiguous_names": len(index.ambiguous_names),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate canonical WTARG tables and source keys.")
    parser.add_argument("command", choices=("validate",))
    parser.add_argument("--data-dir", default=str(Path(__file__).resolve().parent / "data"))
    args = parser.parse_args(argv)
    counts = validate_project_data(args.data_dir)
    print("Canonical data valid: " + ", ".join(f"{key}={value}" for key, value in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
