"""Build the data shown on the Milestones page from local ranking and match history."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from utils import fix_encoding_keep_accents, format_player_name, normalize_player_name

POINT_FIELDS = ("category", "draw", "W", "F", "SF", "QF", "R16", "R32", "R64", "R128", "QLFR", "QR3", "QR2", "QR1")
ROUND_ORDER = {
    "QR1": 1,
    "QR2": 2,
    "QR3": 3,
    "QR4": 4,
    "1st Round": 5,
    "2nd Round": 6,
    "3rd Round": 7,
    "4th Round": 8,
    "5th Round": 9,
    "Quarter Finals": 10,
    "Quarter-finals": 10,
    "Semi-finals": 11,
    "Final": 12,
}
ITF_CATEGORIES = {"W10", "W15", "W25", "W35", "W40", "W50", "W60", "W75", "W80", "W100"}
WTA125_CATEGORIES = {"WTA 125", "125K", "125K Series"}


def _name_key(value: Any) -> str:
    return normalize_player_name(str(value or ""))


def _display_name(value: Any) -> str:
    return format_player_name(fix_encoding_keep_accents(str(value or "")).strip())


def _monday(value: str) -> date:
    parsed = datetime.strptime(value[:10], "%Y-%m-%d").date()
    return parsed - timedelta(days=parsed.weekday())


def _event_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    match_date = str(row.get("DATE", ""))
    tournament_id = str(row.get("TOURNAMENT_ID", "")).strip()
    name = str(row.get("TOURNAMENT", "")).strip()
    match_type = str(row.get("MATCH_TYPE", "")).strip()
    year = match_date[:4]
    if tournament_id:
        return tournament_id, year, name, match_type
    return name, year, match_type, _monday(match_date).isoformat()


def _load_schedules(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8-sig") as source:
            raw = json.load(source)
    except (OSError, json.JSONDecodeError):
        return {"periods": [], "profiles": {}, "exceptions": []}
    fields = tuple(raw.get("fields") or POINT_FIELDS)
    profiles = {}
    for profile_name, rows in (raw.get("profiles") or {}).items():
        profiles[profile_name] = [dict(zip(fields, row, strict=False)) for row in rows]
    raw["profiles"] = profiles
    return raw


def _schedule_period(schedules: dict[str, Any], event_date: str) -> dict[str, Any] | None:
    return next(
        (
            period
            for period in schedules.get("periods", [])
            if str(period.get("from", "")) <= event_date <= str(period.get("to", ""))
        ),
        None,
    )


def _schedule_tour(event: dict[str, Any]) -> str:
    category = event["category"]
    if category in WTA125_CATEGORIES:
        return "wta125"
    if category in ITF_CATEGORIES or event["matchType"].upper() == "ITF":
        return "itf"
    return "wta"


def _schedule_category(event: dict[str, Any]) -> str:
    category = event["category"]
    if category in WTA125_CATEGORIES:
        return "WTA 125"
    if event["matchType"].upper() == "GS":
        return "GS"
    if event["matchType"].upper() == "OG":
        return "OG"
    if "+H" in event["tournament"].upper():
        return f"{category}+H"
    return category


def _draw_size_from_rounds(event: dict[str, Any]) -> int:
    rounds = event["mainRounds"]
    category = _schedule_category(event)
    if "1st Round" in rounds and any(round_name in rounds for round_name in ("4th Round", "5th Round")):
        return 128
    if "1st Round" in rounds and "3rd Round" in rounds:
        return 64
    if category == "GS":
        return 128
    if category in {"Premier Mandatory", "WTA 1000"}:
        return 64
    return 32


def _draw_size_lookup(draw_sizes: list[dict[str, Any]]) -> tuple[dict[str, int], dict[str, int]]:
    by_wta_id: dict[str, int] = {}
    by_itf_event: dict[str, int] = {}
    for row in draw_sizes:
        size = row.get("mainDrawSize")
        if not isinstance(size, int) or size <= 0:
            continue
        source = str(row.get("source", "")).upper()
        if source == "WTA":
            tournament_id = str(row.get("tournamentId", "")).lstrip("0") or "0"
            by_wta_id[tournament_id] = size
        elif source == "ITF":
            key = f"{row.get('tournamentName', '')}|{row.get('date', '')}"
            by_itf_event[key] = size
    return by_wta_id, by_itf_event


def _point_table(
    event: dict[str, Any], schedules: dict[str, Any], by_wta_id: dict[str, int], by_itf_event: dict[str, int]
) -> tuple[dict[str, Any] | None, int]:
    period = _schedule_period(schedules, event["date"])
    if not period:
        return None, _draw_size_from_rounds(event)
    profile = schedules["profiles"].get(period.get(_schedule_tour(event)), [])
    category = _schedule_category(event)
    candidates = [row for row in profile if row.get("category") == category]
    if not candidates and category.endswith("+H"):
        candidates = [row for row in profile if row.get("category") == category[:-2]]
    if not candidates:
        return None, _draw_size_from_rounds(event)

    inferred = _draw_size_from_rounds(event)
    tournament_id = str(event.get("tournamentId", "")).lstrip("0") or "0"
    exact_itf_key = f"{event['tournament']}|{event['date']}"
    actual = by_itf_event.get(exact_itf_key) if _schedule_tour(event) == "itf" else by_wta_id.get(tournament_id)
    wanted = actual or inferred
    selected = min(candidates, key=lambda row: (abs(int(row.get("draw") or 32) - wanted), -int(row.get("draw") or 32)))
    return selected, 128 if wanted >= 96 else 64 if wanted > 32 else 32


def _main_point_key(round_name: str, result: str, draw_size: int) -> str | None:
    if round_name == "Final":
        return "W" if result == "W" else "F"
    if result == "W":
        next_rounds = {
            32: {
                "1st Round": "2nd Round",
                "2nd Round": "Quarter-finals",
                "Quarter Finals": "Semi-finals",
                "Quarter-finals": "Semi-finals",
                "Semi-finals": "Final",
            },
            64: {
                "1st Round": "2nd Round",
                "2nd Round": "3rd Round",
                "3rd Round": "Quarter-finals",
                "Quarter Finals": "Semi-finals",
                "Quarter-finals": "Semi-finals",
                "Semi-finals": "Final",
            },
            128: {
                "1st Round": "2nd Round",
                "2nd Round": "3rd Round",
                "3rd Round": "4th Round",
                "4th Round": "Quarter-finals",
                "Quarter Finals": "Semi-finals",
                "Quarter-finals": "Semi-finals",
                "Semi-finals": "Final",
            },
        }
        next_round = next_rounds[draw_size].get(round_name)
        if next_round:
            return _main_point_key(next_round, "L", draw_size)
    if round_name == "Semi-finals":
        return "SF"
    if round_name in {"Quarter Finals", "Quarter-finals"}:
        return "QF"
    maps = {
        32: {"2nd Round": "R16", "1st Round": "R32"},
        64: {"3rd Round": "R16", "2nd Round": "R32", "1st Round": "R64"},
        128: {"4th Round": "R16", "3rd Round": "R32", "2nd Round": "R64", "1st Round": "R128"},
    }
    return maps[draw_size].get(round_name)


def _final_qualifying_round(table: dict[str, Any] | None) -> str:
    if table:
        for key in ("QR3", "QR2", "QR1"):
            if table.get(key) is not None:
                return key
    return "QR1"


def _qualifying_point_key(player: dict[str, Any], table: dict[str, Any]) -> str | None:
    round_name = player["bestQualRound"]
    if not round_name:
        return None
    if player["bestMainRound"]:
        return round_name if player["bestQualResult"] == "L" else "QLFR"
    if player["bestQualResult"] == "W":
        if round_name == _final_qualifying_round(table):
            return "QLFR"
        return {"QR1": "QR2", "QR2": "QR3"}.get(round_name)
    return round_name


def _round_display(player: dict[str, Any]) -> str:
    main = player["bestMainRound"]
    if main == "Final" and player["bestMainResult"] == "W":
        main = "W"
    replacements = {
        "Final": "F",
        "Semi-finals": "SF",
        "Quarter Finals": "QF",
        "Quarter-finals": "QF",
        "4th Round": "4th",
        "3rd Round": "3rd",
        "2nd Round": "2nd",
        "1st Round": "1st",
    }
    main = replacements.get(main, main)
    qual = player["bestQualRound"]
    return " + ".join(value for value in (main, qual) if value)


def _event_is_exception(event: dict[str, Any], schedules: dict[str, Any]) -> bool:
    return any(
        str(rule.get("from", "")) <= event["date"] <= str(rule.get("to", ""))
        and str(rule.get("tournament", "")).casefold() in event["tournament"].casefold()
        and int(rule.get("points", 0)) == 0
        for rule in schedules.get("exceptions", [])
    )


def _price_player_result(
    event: dict[str, Any], player: dict[str, Any], table: dict[str, Any] | None, draw_size: int
) -> int:
    if not table:
        return 0
    points = 0
    if player["bestMainRound"]:
        qualified_first_round_loss = (
            player["bestQualRound"] and player["bestMainRound"] == "1st Round" and player["bestMainResult"] == "L"
        )
        if not qualified_first_round_loss:
            key = _main_point_key(player["bestMainRound"], player["bestMainResult"], draw_size)
            if key and table.get(key) is not None:
                points += int(table[key])
    key = _qualifying_point_key(player, table)
    if key and table.get(key) is not None:
        points += int(table[key])
    return points


def _arg_identity_index(data_dir: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Map every known ARG alias and source ID to its canonical display name."""
    try:
        identities = json.loads((data_dir / "player_aliases_wta_itf.json").read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}, {}
    names: dict[str, str] = {}
    source_ids: dict[str, str] = {}
    for identity in identities if isinstance(identities, list) else []:
        if not isinstance(identity, dict) or str(identity.get("country", "")).upper() != "ARG":
            continue
        display_name = _display_name(
            identity.get("display_name") or identity.get("wta_name") or identity.get("itf_name")
        )
        if not display_name:
            continue
        identity_names = [
            identity.get("display_name"),
            identity.get("wta_name"),
            identity.get("itf_name"),
            identity.get("bjkc_name"),
            *(identity.get("aliases") or []),
        ]
        for value in identity_names:
            key = _name_key(value)
            if key:
                names[key] = display_name
        for field in ("wta_id", "itf_id", "bjkc_id"):
            source_id = str(identity.get(field, "")).strip()
            if source_id:
                source_ids[source_id] = display_name
        for field in ("additional_wta_ids", "additional_itf_ids", "additional_bjkc_ids"):
            for source_id in identity.get(field) or []:
                if str(source_id).strip():
                    source_ids[str(source_id).strip()] = display_name
    return names, source_ids


def _group_events(
    history: list[dict[str, Any]],
    arg_names: dict[str, str],
    arg_source_ids: dict[str, str],
) -> tuple[dict[tuple[str, ...], dict[str, Any]], dict[str, dict[str, str]]]:
    events: dict[tuple[str, ...], dict[str, Any]] = {}
    facts: dict[str, dict[str, str]] = defaultdict(dict)
    for row in history:
        match_type = str(row.get("MATCH_TYPE", "")).strip()
        match_date = str(row.get("DATE", ""))[:10]
        if not match_date or match_type.casefold() == "fed/bjk cup":
            continue
        winner = str(row.get("_winnerName", "")).strip()
        loser = str(row.get("_loserName", "")).strip()
        if "/" in winner or "/" in loser:
            continue
        draw = str(row.get("DRAW", "")).upper()
        round_name = str(row.get("ROUND", ""))
        sides = (
            (winner, str(row.get("_winnerCountry", "")).upper(), str(row.get("_winnerId", "")), "W"),
            (loser, str(row.get("_loserCountry", "")).upper(), str(row.get("_loserId", "")), "L"),
        )
        key = _event_key(row)
        event = events.setdefault(
            key,
            {
                "date": _monday(match_date).isoformat(),
                "firstMatchDate": match_date,
                "tournament": str(row.get("TOURNAMENT", "")),
                "tournamentId": str(row.get("TOURNAMENT_ID", "")),
                "category": str(row.get("CATEGORY", "")),
                "matchType": match_type,
                "mainRounds": set(),
                "mainMonday": "",
                "qualMonday": "",
                "players": {},
            },
        )
        event["firstMatchDate"] = min(event["firstMatchDate"], match_date)
        match_monday = _monday(match_date).isoformat()
        if draw == "Q":
            event["qualMonday"] = min(event["qualMonday"] or match_monday, match_monday)
        else:
            event["mainRounds"].add(round_name)
            event["mainMonday"] = min(event["mainMonday"] or match_monday, match_monday)
        for raw_name, country, source_id, result in sides:
            raw_key = _name_key(raw_name)
            canonical_name = arg_source_ids.get(source_id) or arg_names.get(raw_key)
            if not raw_name or (country != "ARG" and not canonical_name):
                continue
            display_name = canonical_name or _display_name(raw_name)
            player_key = _name_key(display_name)
            player_facts = facts[player_key]
            player_facts["displayName"] = display_name
            player_facts["firstProMatch"] = min(player_facts.get("firstProMatch", match_date), match_date)
            if result == "W":
                player_facts["firstProWin"] = min(player_facts.get("firstProWin", match_date), match_date)
                if draw != "Q":
                    player_facts["firstMainDrawWin"] = min(player_facts.get("firstMainDrawWin", match_date), match_date)
            participant = event["players"].setdefault(
                player_key,
                {
                    "displayName": display_name,
                    "bestMainRound": "",
                    "bestMainOrder": 0,
                    "bestMainResult": "",
                    "bestQualRound": "",
                    "bestQualOrder": 0,
                    "bestQualResult": "",
                    "bestQualDate": "",
                },
            )
            order = ROUND_ORDER.get(round_name, 0)
            prefix = "bestQual" if draw == "Q" else "bestMain"
            if order > participant[f"{prefix}Order"] or (order == participant[f"{prefix}Order"] and result == "L"):
                participant[f"{prefix}Round"] = round_name
                participant[f"{prefix}Order"] = order
                participant[f"{prefix}Result"] = result
                if draw == "Q":
                    participant["bestQualDate"] = match_date
    for event in events.values():
        if event["mainMonday"]:
            event["date"] = event["mainMonday"]
        elif event["qualMonday"] and event["matchType"].upper() == "GS":
            event["date"] = (_monday(event["qualMonday"]) + timedelta(days=7)).isoformat()
        elif event["qualMonday"]:
            event["date"] = event["qualMonday"]
    return events, facts


def _ranked_player_history(
    ranking_weeks: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    first: dict[str, str] = {}
    last: dict[str, str] = {}
    dobs: dict[str, str] = {}
    for week in sorted(ranking_weeks):
        for player in ranking_weeks[week]:
            if str(player.get("Country", "")).upper() != "ARG":
                continue
            key = _name_key(player.get("Player"))
            if not key:
                continue
            first.setdefault(key, week)
            last[key] = week
            dob = str(player.get("DOB", ""))[:10]
            if dob:
                dobs[key] = dob
    return first, last, dobs


def _alias_dobs(data_dir: Path, dobs: dict[str, str]) -> None:
    try:
        identities = json.loads((data_dir / "player_aliases_wta_itf.json").read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return
    for identity in identities if isinstance(identities, list) else []:
        if not isinstance(identity, dict) or str(identity.get("country", "")).upper() != "ARG":
            continue
        dob = str(identity.get("dob", ""))[:10]
        if not dob:
            continue
        for field in ("display_name", "wta_name", "itf_name", "bjkc_name"):
            key = _name_key(identity.get(field))
            if key:
                dobs.setdefault(key, dob)


def _itf_profile_birth_years(
    data_dir: Path,
    dobs: dict[str, str],
    arg_names: dict[str, str],
    arg_source_ids: dict[str, str],
) -> None:
    """Apply cached ITF birth years for Argentine players with recent matches."""
    try:
        profiles = json.loads((data_dir / "itf_player_details.json").read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return
    for profile in profiles if isinstance(profiles, list) else []:
        if not isinstance(profile, dict):
            continue
        try:
            birth_year = int(profile.get("birthYear"))
        except (TypeError, ValueError):
            continue
        source_id = str(profile.get("playerId", "")).strip()
        raw_name = str(profile.get("displayName") or profile.get("fullName") or "").strip()
        canonical_name = arg_source_ids.get(source_id) or arg_names.get(_name_key(raw_name)) or raw_name
        key = _name_key(canonical_name)
        if key:
            dobs[key] = str(birth_year)


def _dense_top_three(items: list[tuple[str, str]]) -> list[dict[str, Any]]:
    dates: list[str] = []
    selected_count = 0
    for event_date in sorted({item_date for item_date, _ in items}):
        if selected_count >= 3:
            break
        dates.append(event_date)
        selected_count += sum(item_date == event_date for item_date, _ in items)
    positions = {event_date: index + 1 for index, event_date in enumerate(dates)}
    return [
        {"position": positions[event_date], "name": name, "date": event_date}
        for event_date, name in sorted(items, key=lambda item: (item[0], item[1]))
        if event_date in positions
    ]


def build_milestones_data(
    *,
    history: list[dict[str, Any]],
    ranking_weeks: dict[str, list[dict[str, Any]]],
    active_names: list[str],
    current_wta_names: set[str],
    draw_sizes: list[dict[str, Any]],
    data_dir: str | Path,
    today: date,
) -> dict[str, Any]:
    data_dir = Path(data_dir)
    schedules = _load_schedules(data_dir / "points_distribution_history.json")
    arg_names, arg_source_ids = _arg_identity_index(data_dir)
    events, facts = _group_events(history, arg_names, arg_source_ids)
    first_ranked, last_ranked, dobs = _ranked_player_history(ranking_weeks)
    _alias_dobs(data_dir, dobs)
    _itf_profile_birth_years(data_dir, dobs, arg_names, arg_source_ids)
    by_wta_id, by_itf_event = _draw_size_lookup(draw_sizes)

    point_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    first_point: dict[str, str] = {}
    passed_qualifying: dict[str, str] = {}
    two_week_names = (
        "Australian Open",
        "Roland Garros",
        "Wimbledon",
        "US Open",
        "Indian Wells",
        "Miami",
        "Madrid",
        "Internazionali",
        "Rome",
    )
    freeze_mondays: set[date] = set()
    for event in events.values():
        category = _schedule_category(event)
        genuine_two_week = category in {"GS", "WTA 1000", "Premier Mandatory"} and any(
            name.casefold() in event["tournament"].casefold() for name in two_week_names
        )
        if genuine_two_week:
            start = _monday(event["date"])
            freeze_mondays.update({start, start + timedelta(days=7)})
    for event in events.values():
        table, draw_size = _point_table(event, schedules, by_wta_id, by_itf_event)
        exception = _event_is_exception(event, schedules)
        for player_key, result in event["players"].items():
            points = 0 if exception else _price_player_result(event, result, table, draw_size)
            effective = _monday(event["date"])
            if event["category"] in {"W15", "W35"}:
                effective += timedelta(days=7)
            category = _schedule_category(event)
            two_week = category in {"GS", "WTA 1000", "Premier Mandatory"} and any(
                name.casefold() in event["tournament"].casefold() for name in two_week_names
            )
            if two_week:
                drop_date = effective + timedelta(weeks=54)
            elif effective in freeze_mondays:
                previous = effective - timedelta(days=7)
                freeze_start = previous if previous in freeze_mondays else effective
                drop_date = freeze_start + timedelta(weeks=54)
            else:
                drop_date = effective + timedelta(weeks=53)
            row = {
                "date": event["date"],
                "tournament": event["tournament"],
                "category": event["category"],
                "round": _round_display(result),
                "points": points,
                "dropDate": drop_date.isoformat(),
            }
            point_rows[player_key].append(row)
            if points > 0:
                first_point[player_key] = min(
                    first_point.get(player_key, event["firstMatchDate"]), event["firstMatchDate"]
                )
            if result["bestQualRound"]:
                qualified = bool(result["bestMainRound"] and result["bestQualResult"] == "W")
                won_final = result["bestQualResult"] == "W" and result["bestQualRound"] == _final_qualifying_round(
                    table
                )
                if qualified or won_final:
                    qual_date = result["bestQualDate"] or event["firstMatchDate"]
                    passed_qualifying[player_key] = min(passed_qualifying.get(player_key, qual_date), qual_date)

    milestone_sources = {
        "ranked": first_ranked,
        "point": first_point,
        "mainDrawWin": {
            key: value["firstMainDrawWin"] for key, value in facts.items() if value.get("firstMainDrawWin")
        },
        "proWin": {key: value["firstProWin"] for key, value in facts.items() if value.get("firstProWin")},
        "qualified": passed_qualifying,
    }
    display_names = {key: value.get("displayName", key.title()) for key, value in facts.items()}
    display_names.update(arg_names)
    for week_rows in ranking_weeks.values():
        for player in week_rows:
            key = _name_key(player.get("Player"))
            if key:
                display_names.setdefault(key, _display_name(player.get("Player")))

    historical = []
    for birth_year in range(2000, today.year - 13):
        cohort = {key for key, dob in dobs.items() if str(dob).startswith(str(birth_year))}
        row = {"year": birth_year}
        for field, source in milestone_sources.items():
            row[field] = _dense_top_three(
                [(source[key], display_names.get(key, key.title())) for key in cohort if key in source]
            )
        historical.append(row)

    current_wta_keys = {_name_key(name) for name in current_wta_names}
    active = []
    seen = set()
    for name in sorted(active_names, key=_name_key):
        key = _name_key(name)
        if not key or key in current_wta_keys or key in seen:
            continue
        seen.add(key)
        rows = sorted(point_rows.get(key, []), key=lambda row: (row["date"], row["tournament"]), reverse=True)
        live_rows = [row for row in rows if row["dropDate"] > today.isoformat()]
        expired_rows = [
            {**row, "dropDate": "-"}
            for row in rows
            if row["dropDate"] <= today.isoformat() and int(row["points"]) > 0
        ]
        active.append(
            {
                "name": _display_name(name),
                "lastRankedWeek": last_ranked.get(key, ""),
                "totalEverPoints": sum(int(row["points"]) for row in rows),
                "livePoints": sum(int(row["points"]) for row in live_rows),
                "liveRows": live_rows,
                "expiredRows": expired_rows,
            }
        )
    return {"historical": historical, "active": active}
