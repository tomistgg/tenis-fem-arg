"""Render the deployable site entirely from canonical tracked data.

The refresh pipeline owns CSV/JSON caches under ``data/``.  Browser-specific
HTML, CSS, JavaScript, and data bundles are derived output and are written only
to the requested site directory.
"""

from __future__ import annotations

import contextlib
import json
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from calendar_builder import get_monday_from_date
from config import CONTINENT_KEYS
from html_generator import generate_html
from main import (
    _append_schedule_label,
    _filter_itf_draws_for_website,
    _is_excluded_entry_list_tournament,
    _schedule_tournament_name,
    enrich_history_with_wta_ranks,
    load_match_history,
)
from utils import (
    expand_calendar_snapshot,
    expand_draws_store_cache,
    expand_entry_lists_cache,
    expand_itf_rankings_cache,
    expand_tournament_snapshot,
    expand_tstrength_cache,
    load_csv_rows,
)
from wta import _load_wta_csv

CALENDAR_COLUMNS = ("gs", "wta_tour", "wta_125", "itf")
SCHEDULE_WEEK_COUNT = 4
TournamentInfo = dict[str, Any]
TournamentGroups = OrderedDict[str, OrderedDict[str, TournamentInfo]]
ScheduleMap = dict[str, dict[str, str]]


def _load_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8-sig") as source:
            return json.load(source)
    except (OSError, json.JSONDecodeError):
        return default


def _monday_from_date(value: str) -> str:
    parsed = datetime.strptime(str(value or "")[:10], "%Y-%m-%d")
    return (parsed - timedelta(days=parsed.weekday())).strftime("%Y-%m-%d")


def _tournament_inputs(data_dir: Path) -> tuple[TournamentGroups, OrderedDict[str, str]]:
    snapshot = expand_tournament_snapshot(_load_json(data_dir / "tournament_snapshot.json", {}))
    tournament_groups: TournamentGroups = OrderedDict()
    for tournament_key, raw_info in (snapshot or {}).items():
        info = dict(raw_info)
        week = str(info.pop("week", "") or "").strip()
        if not week:
            continue
        tournament_groups.setdefault(week, OrderedDict())[tournament_key] = info

    week_dates: dict[str, str] = {}
    for week, tournaments in tournament_groups.items():
        for info in tournaments.values():
            try:
                monday = get_monday_from_date(str(info.get("startDate", "")))
            except ValueError:
                continue
            week_dates[week] = monday.strftime("%Y-%m-%d")
            break
    monday_map = OrderedDict((monday, week) for week, monday in sorted(week_dates.items(), key=lambda item: item[1]))
    return tournament_groups, monday_map


def _visible_schedule_monday_map(monday_map: OrderedDict[str, str]) -> OrderedDict[str, str]:
    """Return the four chronological weeks shown in Schedule."""
    return OrderedDict(list(monday_map.items())[:SCHEDULE_WEEK_COUNT])


def _entry_inputs(
    data_dir: Path,
    tournament_groups: TournamentGroups,
) -> tuple[dict[str, list[dict[str, Any]]], ScheduleMap, set[str]]:
    entry_cache = expand_entry_lists_cache(_load_json(data_dir / "entry_lists_cache.json", {})) or {}
    active_keys = {
        key
        for tournaments in tournament_groups.values()
        for key, info in tournaments.items()
        if not _is_excluded_entry_list_tournament(key, info)
    }
    active_keys.update(f"{key}#qual" for key in tuple(active_keys))
    tournament_store = {
        key: players for key, players in entry_cache.items() if key in active_keys and isinstance(players, list)
    }

    key_to_week = {key: week for week, tournaments in tournament_groups.items() for key in tournaments}
    for key in tournament_store:
        if key.endswith("#qual") and key not in key_to_week:
            key_to_week[key] = key_to_week.get(key[:-5], "")

    schedule_map: ScheduleMap = {}
    schedule_entries: dict[str, dict[str, list[dict[str, Any]]]] = {}
    unranked_arg_names: set[str] = set()
    for tournament_key, players in tournament_store.items():
        week = key_to_week.get(tournament_key, "")
        base_key = tournament_key[:-5] if tournament_key.endswith("#qual") else tournament_key
        tournament_info: TournamentInfo = next(
            (
                tournaments.get(tournament_key) or tournaments.get(base_key) or {}
                for tournaments in tournament_groups.values()
                if tournament_key in tournaments or base_key in tournaments
            ),
            {},
        )
        tournament_name = _schedule_tournament_name(tournament_key, tournament_info.get("name", base_key))
        for player in players:
            if not isinstance(player, dict) or str(player.get("country", "")).upper() != "ARG":
                continue
            player_key = str(player.get("name", "")).strip().upper()
            if not player_key:
                continue
            entry_type = str(player.get("type", "MAIN")).upper()
            if entry_type == "QUAL":
                suffix = " (Q)"
            elif entry_type == "ALT":
                position = str(player.get("pos", "")).strip()
                suffix = f" (ALT {position})" if position else " (ALT)"
            else:
                suffix = ""
            schedule_entries.setdefault(player_key, {}).setdefault(week, []).append(
                {
                    "label": f"{tournament_name}{suffix}",
                    "priority": player.get("priority", ""),
                    "entry_type": entry_type,
                    "pos_num": player.get("pos_num", 9999),
                }
            )
            unranked_arg_names.add(player_key)

    def numeric_sort_value(value: Any) -> int:
        text = str(value or "").strip()
        return int(text) if text.isdigit() else 9999

    entry_type_order = {"MAIN": 0, "QUAL": 1, "ALT": 2}
    for player_key, weeks in schedule_entries.items():
        for week, entries in weeks.items():
            entries.sort(
                key=lambda entry: (
                    numeric_sort_value(entry["priority"]),
                    entry_type_order.get(entry["entry_type"], 3),
                    numeric_sort_value(entry["pos_num"]),
                    entry["label"].lower(),
                )
            )
            for entry in entries:
                _append_schedule_label(
                    schedule_map,
                    player_key,
                    week,
                    entry["label"],
                    style="append_br",
                )
    return tournament_store, schedule_map, unranked_arg_names


def _entry_list_hidden_keys(data_dir: Path) -> set[str]:
    """Return tournaments whose main draw has replaced their Entry List."""
    state = _load_json(data_dir / "itf_acceptance_state.json", {})
    if not isinstance(state, dict):
        return set()
    return {
        str(key)
        for key, entry in state.items()
        if isinstance(entry, dict)
        and (
            entry.get("main_draw_available_date")
            or entry.get("argless_entry_list_removed_date")
        )
    }


def _ranking_inputs(
    data_dir: Path,
    entry_arg_names: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rankings_by_date = _load_wta_csv(data_dir) or {}
    latest_date = max(rankings_by_date, default="")
    all_wta_players = list(rankings_by_date.get(latest_date, []))
    players_data = [dict(player) for player in all_wta_players if str(player.get("Country", "")).upper() == "ARG"]
    known_names = {str(player.get("Player", "")).strip().upper() for player in players_data}

    itf_by_date = expand_itf_rankings_cache(_load_json(data_dir / "itf_rankings_cache.json", {})) or {}
    latest_itf_date = max(itf_by_date, default="")
    for player in itf_by_date.get(latest_itf_date, []):
        if not isinstance(player, dict):
            continue
        name = str(player.get("Player", "")).strip().upper()
        if str(player.get("Country", "")).upper() == "ARG" and name not in known_names:
            players_data.append(dict(player))
            known_names.add(name)

    for name in sorted(entry_arg_names - known_names):
        players_data.append({"Player": name, "Key": name, "Rank": "-", "Country": "ARG"})
    return players_data, all_wta_players


def _calendar_inputs(data_dir: Path) -> list[dict[str, Any]]:
    rows = expand_calendar_snapshot(_load_json(data_dir / "calendar_snapshot.json", [])) or []
    weeks: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for row in rows:
        if not isinstance(row, dict):
            continue
        week_label = str(row.get("week_label", "") or "")
        column = str(row.get("column", "") or "")
        continent = str(row.get("continent", "") or "")
        if not week_label or column not in CALENDAR_COLUMNS or continent not in CONTINENT_KEYS:
            continue
        week = weeks.setdefault(
            week_label,
            {
                "week_label": week_label,
                "monday_date": "",
                "has_any": False,
                "columns": {name: {key: [] for key in CONTINENT_KEYS} for name in CALENDAR_COLUMNS},
            },
        )
        tournament = {
            key: row.get(key, "")
            for key in (
                "name",
                "level",
                "surface",
                "country",
                "startDate",
                "endDate",
                "source",
                "tournamentKey",
                "tournamentId",
                "calendarKey",
            )
        }
        week["columns"][column][continent].append(tournament)
        week["has_any"] = True
        if not week["monday_date"]:
            with contextlib.suppress(ValueError):
                week["monday_date"] = _monday_from_date(row.get("startDate", ""))
    return [week for week in weeks.values() if week.get("monday_date")]


def render_site_from_data(data_dir: str | Path, site_root: str | Path) -> None:
    """Generate a complete browser site without network access."""

    data_dir = Path(data_dir).resolve()
    site_root = Path(site_root).resolve()
    site_root.mkdir(parents=True, exist_ok=True)

    tournament_groups, monday_map = _tournament_inputs(data_dir)
    tournament_store, schedule_map, entry_arg_names = _entry_inputs(data_dir, tournament_groups)
    monday_map = _visible_schedule_monday_map(monday_map)
    players_data, all_wta_players = _ranking_inputs(data_dir, entry_arg_names)
    match_history_data, cleaned_history = load_match_history(data_dir)
    enrich_history_with_wta_ranks(cleaned_history, data_dir)
    draws_store = expand_draws_store_cache(_load_json(data_dir / "draws_store_cache.json", {})) or {}
    calendar_change_history = _load_json(data_dir / "calendar_change_history.json", []) or []
    tstrength_data = expand_tstrength_cache(_load_json(data_dir / "tstrength_cache.json", [])) or []

    generate_html(
        tournament_groups,
        tournament_store,
        players_data,
        schedule_map,
        cleaned_history,
        _calendar_inputs(data_dir),
        match_history_data,
        all_wta_players,
        national_team_data=load_csv_rows(data_dir / "national_team_order.csv", delimiter=";"),
        captains_data=load_csv_rows(data_dir / "captains.csv"),
        draws_data=_filter_itf_draws_for_website(draws_store),
        calendar_changes=calendar_change_history,
        tstrength_data=tstrength_data,
        monday_map=monday_map,
        entry_list_hidden_keys=_entry_list_hidden_keys(data_dir),
        data_dir=data_dir,
        site_root=site_root,
    )
