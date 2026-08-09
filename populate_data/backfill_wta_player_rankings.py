"""Backfill historical rankings from the official WTA player-history API.

The API behind each WTA player stats page returns a ``weeklyRankings`` series.
This command transposes those player histories into the repository's
week-oriented ranking CSV without replacing an existing player/week value.

Preview a player by WTA ID or profile URL::

    python populate_data/backfill_wta_player_rankings.py --player-id 50020
    python populate_data/backfill_wta_player_rankings.py \
        --player-url https://www.wtatennis.com/legends/50020/chris-evert/stats

Nothing is written unless ``--apply`` is supplied.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Mapping, Sequence

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from canonical_data import sync_wta_players
from config import PLAYER_ALIASES_WTA_ITF_FILE, WTA_RANKINGS_CSV_83_99
from http_client import get_with_retry
from runtime_logging import get_logger
from transactional_io import atomic_write_csv


logger = get_logger("historical-wta-player-rankings")

CSV_FIELDNAMES = ["week_date", "id", "rank", "points", "player", "country", "dob"]
PLAYER_RANKING_URL = "https://api.wtatennis.com/tennis/players/{player_id}/ranking"
REQUEST_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.wtatennis.com",
    "Referer": "https://www.wtatennis.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
    ),
    "account": "wta",
}
PLAYER_ID_PATTERN = re.compile(r"(?:^|/)(\d+)(?:/|$)")
UNRANKED_SENTINEL = 9999


@dataclass(frozen=True)
class PlayerProfile:
    player_id: str
    name: str
    country: str
    dob: str


@dataclass(frozen=True)
class MergeResult:
    rows: list[dict[str, str]]
    additions: list[dict[str, str]]
    conflicts: list[dict[str, str]]
    date_aliases: list[dict[str, str]]
    skipped_new_weeks: list[dict[str, str]]
    unchanged: int


def _compact(value: object) -> str:
    return " ".join(str(value or "").split())


def parse_player_id(value: str) -> str:
    """Return a numeric WTA player ID from an ID or player-profile URL."""

    text = _compact(value)
    if text.isdigit():
        return text
    match = PLAYER_ID_PATTERN.search(text)
    if match:
        return match.group(1)
    raise ValueError(f"cannot find a numeric WTA player ID in {value!r}")


def parse_weekly_singles_rankings(
    payload: object,
    *,
    from_year: int | None = None,
    to_year: int | None = None,
) -> tuple[PlayerProfile, list[tuple[str, int]]]:
    """Extract player metadata and ``(week_date, rank)`` pairs from API JSON."""

    if not isinstance(payload, dict):
        raise ValueError("WTA player ranking response is not an object")
    raw_player = payload.get("player")
    if not isinstance(raw_player, dict):
        raise ValueError("WTA player ranking response has no player metadata")
    player_id = _compact(raw_player.get("id"))
    name = _compact(raw_player.get("fullName"))
    if not name:
        name = _compact(f"{raw_player.get('firstName', '')} {raw_player.get('lastName', '')}")
    if not player_id or not name:
        raise ValueError("WTA player ranking response has incomplete player metadata")
    profile = PlayerProfile(
        player_id=player_id,
        name=name,
        country=_compact(raw_player.get("countryCode")).upper(),
        dob=_compact(raw_player.get("dateOfBirth"))[:10],
    )

    weekly_rankings = payload.get("weeklyRankings")
    if not isinstance(weekly_rankings, list):
        raise ValueError("WTA player ranking response has no weeklyRankings list")

    rankings: dict[str, int] = {}
    for item in weekly_rankings:
        if not isinstance(item, dict):
            continue
        date_text = _compact(item.get("rankedAt"))[:10]
        rank_text = _compact(item.get("singlesRanking")).replace(",", "")
        if not date_text or not rank_text.isdigit():
            continue
        parsed_rank = int(rank_text)
        if parsed_rank <= 0 or parsed_rank == UNRANKED_SENTINEL:
            continue
        try:
            week_date = date.fromisoformat(date_text)
        except ValueError as exc:
            raise ValueError(f"unexpected WTA ranking date {date_text!r}") from exc
        if from_year is not None and week_date.year < from_year:
            continue
        if to_year is not None and week_date.year > to_year:
            continue
        date_key = week_date.isoformat()
        rank = parsed_rank
        previous = rankings.get(date_key)
        if previous is not None and previous != rank:
            raise ValueError(
                f"WTA page contains two singles ranks for {date_key}: {previous} and {rank}"
            )
        rankings[date_key] = rank

    return profile, sorted(rankings.items())


def _player_cache_path(
    cache_dir: Path,
    player_id: str,
    from_year: int,
    to_year: int,
) -> Path:
    return cache_dir / f"{player_id}_{from_year}_{to_year}.json"


def fetch_player_ranking_rows(
    player_id: str,
    *,
    from_year: int,
    to_year: int,
    cache_dir: Path | None = None,
) -> list[dict[str, str]]:
    cache_path = None
    if cache_dir is not None:
        cache_path = _player_cache_path(cache_dir, player_id, from_year, to_year)
    if cache_path is not None and cache_path.exists():
        with cache_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        logger.info(f"Loaded cached official WTA response for player {player_id}.")
    else:
        response = get_with_retry(
            PLAYER_RANKING_URL.format(player_id=player_id),
            component="historical-wta-player-rankings",
            headers=REQUEST_HEADERS,
            params={
                "from": f"{from_year:04d}-01-01",
                "to": f"{to_year:04d}-12-31",
                "aggregation-method": "weekly",
            },
        )
        payload = response.json()
        # Validate the response before retaining it as resumable progress.
        profile, _ = parse_weekly_singles_rankings(
            payload,
            from_year=from_year,
            to_year=to_year,
        )
        if profile.player_id != player_id:
            raise ValueError(
                f"WTA returned player {profile.player_id} when {player_id} was requested"
            )
        if cache_path is not None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache_path.with_name(cache_path.name + ".tmp")
            try:
                with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                for attempt in range(4):
                    try:
                        os.replace(temporary, cache_path)
                        break
                    except PermissionError:
                        if attempt == 3:
                            raise
                        time.sleep(0.1 * (2**attempt))
            finally:
                temporary.unlink(missing_ok=True)
    profile, rankings = parse_weekly_singles_rankings(
        payload,
        from_year=from_year,
        to_year=to_year,
    )
    if profile.player_id != player_id:
        raise ValueError(
            f"WTA returned player {profile.player_id} when {player_id} was requested"
        )
    return [
        {
            "week_date": week_date,
            "id": profile.player_id,
            "rank": str(rank),
            "points": "",
            "player": profile.name,
            "country": profile.country,
            "dob": profile.dob,
        }
        for week_date, rank in rankings
    ]


def load_ranking_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != CSV_FIELDNAMES:
            raise ValueError(
                f"unexpected ranking CSV columns in {path}: {reader.fieldnames!r}"
            )
        return [
            {field: _compact(row.get(field)) for field in CSV_FIELDNAMES}
            for row in reader
        ]


def merge_ranking_rows(
    existing_rows: Iterable[Mapping[str, object]],
    incoming_rows: Iterable[Mapping[str, object]],
    *,
    allow_new_weeks: bool = False,
) -> MergeResult:
    """Add absent player/weeks while reporting rank and date disagreements."""

    existing = [
        {field: _compact(row.get(field)) for field in CSV_FIELDNAMES}
        for row in existing_rows
    ]
    by_key = {(row["week_date"], row["id"]): row for row in existing}
    if len(by_key) != len(existing):
        raise ValueError("ranking CSV contains duplicate (week_date, id) keys")
    existing_dates = {row["week_date"] for row in existing}
    rows_by_player: dict[str, list[dict[str, str]]] = {}
    for row in existing:
        rows_by_player.setdefault(row["id"], []).append(row)

    additions: list[dict[str, str]] = []
    conflicts: list[dict[str, str]] = []
    date_aliases: list[dict[str, str]] = []
    skipped_new_weeks: list[dict[str, str]] = []
    unchanged = 0
    seen_incoming: dict[tuple[str, str], dict[str, str]] = {}
    for raw_row in incoming_rows:
        row = {field: _compact(raw_row.get(field)) for field in CSV_FIELDNAMES}
        key = (row["week_date"], row["id"])
        duplicate = seen_incoming.get(key)
        if duplicate is not None:
            if duplicate["rank"] != row["rank"]:
                raise ValueError(f"incoming rankings disagree for {key}: {duplicate['rank']} and {row['rank']}")
            continue
        seen_incoming[key] = row

        current = by_key.get(key)
        if current is None:
            incoming_date = date.fromisoformat(row["week_date"])
            nearby = sorted(
                (
                    candidate
                    for candidate in rows_by_player.get(row["id"], [])
                    if abs(
                        date.fromisoformat(candidate["week_date"]) - incoming_date
                    ) <= timedelta(days=3)
                ),
                key=lambda candidate: abs(
                    date.fromisoformat(candidate["week_date"]) - incoming_date
                ),
            )
            if nearby:
                current = nearby[0]
                if current["rank"] == row["rank"]:
                    unchanged += 1
                    date_aliases.append({
                        "api_week_date": row["week_date"],
                        "csv_week_date": current["week_date"],
                        "id": row["id"],
                        "player": row["player"],
                        "rank": row["rank"],
                    })
                    continue
            elif row["week_date"] not in existing_dates and not allow_new_weeks:
                skipped_new_weeks.append(row)
                continue
            else:
                additions.append(row)
                by_key[key] = row
                existing_dates.add(row["week_date"])
                rows_by_player.setdefault(row["id"], []).append(row)
                continue
        if current["rank"] == row["rank"]:
            unchanged += 1
            continue
        conflicts.append({
            "api_week_date": row["week_date"],
            "csv_week_date": current["week_date"],
            "id": row["id"],
            "player": row["player"],
            "existing_rank": current["rank"],
            "wta_api_rank": row["rank"],
        })

    merged = [*existing, *additions]
    merged.sort(key=lambda row: (row["week_date"], int(row["rank"]), row["id"]))
    return MergeResult(
        rows=merged,
        additions=additions,
        conflicts=conflicts,
        date_aliases=date_aliases,
        skipped_new_weeks=skipped_new_weeks,
        unchanged=unchanged,
    )


def _read_player_values(path: Path) -> list[str]:
    values = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            value = line.strip()
            if value and not value.startswith("#"):
                values.append(value)
    return values


def _player_ids(args: argparse.Namespace) -> list[str]:
    values = [*(args.player_id or []), *(args.player_url or [])]
    if args.players_file:
        values.extend(_read_player_values(args.players_file))
    player_ids = []
    for value in values:
        player_id = parse_player_id(value)
        if player_id not in player_ids:
            player_ids.append(player_id)
    if not player_ids:
        raise ValueError("provide --player-id, --player-url, or --players-file")
    return player_ids


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill historical rankings from the official WTA player-history API."
    )
    parser.add_argument(
        "--player-id",
        action="append",
        help="Numeric WTA player ID; repeatable.",
    )
    parser.add_argument(
        "--player-url",
        action="append",
        help="WTA player profile or ranking-API URL; repeatable.",
    )
    parser.add_argument(
        "--players-file",
        type=Path,
        help="Text file containing one WTA player ID or profile URL per line.",
    )
    parser.add_argument("--from-year", type=int, default=1983)
    parser.add_argument("--to-year", type=int, default=1999)
    parser.add_argument("--output", type=Path, default=Path(WTA_RANKINGS_CSV_83_99))
    parser.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Seconds between player-history requests (default: 0.5).",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        help=(
            "Cache each validated official WTA response so interrupted batches can "
            "resume without repeating completed requests."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Atomically update the ranking CSV. Without this flag, only preview changes.",
    )
    parser.add_argument(
        "--allow-new-weeks",
        action="store_true",
        help="Allow dates absent from the CSV; normally they are reported and skipped.",
    )
    parser.add_argument(
        "--keep-existing-conflicts",
        action="store_true",
        help=(
            "Apply non-conflicting additions while preserving and reporting existing "
            "player/week ranks that disagree with the API."
        ),
    )
    return parser


def run(args: argparse.Namespace) -> int:
    if args.from_year > args.to_year:
        raise ValueError("--from-year cannot be later than --to-year")
    if args.delay < 0:
        raise ValueError("--delay cannot be negative")
    player_ids = _player_ids(args)
    incoming_rows: list[dict[str, str]] = []
    for index, player_id in enumerate(player_ids):
        was_cached = bool(
            args.cache_dir
            and _player_cache_path(
                args.cache_dir,
                player_id,
                args.from_year,
                args.to_year,
            ).exists()
        )
        rows = fetch_player_ranking_rows(
            player_id,
            from_year=args.from_year,
            to_year=args.to_year,
            cache_dir=args.cache_dir,
        )
        incoming_rows.extend(rows)
        logger.info(f"Fetched {len(rows)} historical singles ranks for WTA player {player_id}.")
        if not was_cached and index + 1 < len(player_ids) and args.delay:
            time.sleep(args.delay)

    result = merge_ranking_rows(
        load_ranking_rows(args.output),
        incoming_rows,
        allow_new_weeks=args.allow_new_weeks,
    )
    logger.info(
        "Historical ranking preview: "
        f"{len(result.additions)} additions, {result.unchanged} unchanged, "
        f"{len(result.conflicts)} conflicts, {len(result.date_aliases)} date aliases, "
        f"{len(result.skipped_new_weeks)} rows on unknown weeks."
    )
    for addition in result.additions[:20]:
        logger.info(
            f"Would add {addition['week_date']} {addition['player']} "
            f"(WTA {addition['id']}) at #{addition['rank']}."
        )
    if len(result.additions) > 20:
        logger.info(f"...and {len(result.additions) - 20} more additions.")
    for conflict in result.conflicts[:20]:
        logger.warning(
            f"Conflict API {conflict['api_week_date']} / CSV "
            f"{conflict['csv_week_date']} {conflict['player']} "
            f"(WTA {conflict['id']}): CSV #{conflict['existing_rank']}, "
            f"WTA API #{conflict['wta_api_rank']}."
        )
    for skipped in result.skipped_new_weeks[:20]:
        logger.warning(
            f"Skipped unknown week {skipped['week_date']} for {skipped['player']} "
            f"(WTA {skipped['id']}) at #{skipped['rank']}."
        )
    if result.conflicts and args.apply and not args.keep_existing_conflicts:
        logger.error(
            "No files changed because conflicts must be reviewed first; use "
            "--keep-existing-conflicts to preserve them and apply only safe additions."
        )
        return 2
    if not args.apply:
        logger.info("Dry run only; use --apply to write the additions.")
        return 0
    if not result.additions:
        logger.info("No missing player/week rows to add.")
        return 0

    added_players = sync_wta_players(Path(PLAYER_ALIASES_WTA_ITF_FILE), result.additions)
    atomic_write_csv(args.output, CSV_FIELDNAMES, result.rows, encoding="utf-8")
    logger.info(
        f"Added {len(result.additions)} ranking rows and {added_players} canonical players "
        f"to {args.output}."
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except (OSError, UnicodeError, ValueError, csv.Error) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
