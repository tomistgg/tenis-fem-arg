"""Prepare the offline singles tournament planner from existing source caches."""

from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path

from calendar_builder import get_calendar_tournament_key
from canonical_data import MATCH_SOURCES, PlayerIdentityIndex, normalized_name
from draws import _is_completed_score, _player_name_matches_winner
from itf import _is_cancelled_itf_calendar_item
from ranking_publication import accepted_ranking_dates, load_ranking_status
from time_utils import NEW_YORK, madrid_today, new_york_now
from utils import (
    compact_tournament_name,
    expand_calendar_snapshot,
    expand_draws_store_cache,
    expand_entry_lists_cache,
    expand_itf_calendar_cache,
    expand_points_distribution,
    expand_tournament_draw_sizes,
    expand_tstrength_cache,
    expand_wta_calendar_cache,
    fix_encoding_keep_accents,
    get_continent,
)
from wta_calendar_cache import filter_wta_calendar


def _load(data_dir, filename, default):
    path = data_dir / filename
    if not path.exists():
        return default
    with path.open(encoding="utf-8-sig") as source:
        return json.load(source)


def _date(value):
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _number(value):
    try:
        number = float(value)
        return number if number > 0 and number < float("inf") else None
    except (TypeError, ValueError):
        return None


def _rank(value):
    text = str(value or "").strip()
    return int(text) if text.isdigit() and int(text) > 0 else None


def _entry_identity(key):
    match = re.search(r"/tournaments/(\d+)/[^/]+/(\d{4})/", key)
    if match:
        return "WTA", match[1], match[2]
    match = re.fullmatch(r"w-itf-[a-z]+-(\d{4})-\d+", key.lower())
    return ("ITF", key.lower(), match[1]) if match else None


def _event_id(source, source_id, season):
    return f"{source.lower()}:{str(source_id).lower()}:{season}"


def entry_deadlines(event, today):
    """Conservative date-only windows; published routes close new entry."""
    start = _date(event.get("startDate"))
    level = str(event.get("level") or "")
    main = qual = None
    basis = "unknown"
    if start:
        monday = start - timedelta(days=start.weekday())
        if event.get("source") == "ITF" and re.fullmatch(r"W(?:15|35|50|75|100)", level):
            main = qual = monday - timedelta(days=18)
            basis = "estimated"
        elif re.fullmatch(r"WTA (?:125|250|500|1000)", level):
            main, qual = monday - timedelta(days=28), monday - timedelta(days=21)
            basis = "estimated"
        elif level in {"GS", "Grand Slam"}:
            main, qual = monday - timedelta(days=42), monday - timedelta(days=28)
            basis = "estimated"
    stored_main = _date(event.get("entryDeadline") or event.get("entryDeadlineMain"))
    stored_qual = _date(event.get("qualifyingEntryDeadline") or event.get("entryDeadlineQual"))
    if stored_main or stored_qual:
        if event.get("source") == "ITF":
            main = qual = stored_main or stored_qual
            basis = "stored"
        else:
            main, qual = stored_main or main, stored_qual or qual
            basis = "stored" if stored_main and stored_qual else "mixed"
    open_routes = [route for route, deadline in (("MAIN", main), ("QUAL", qual)) if deadline and today < deadline]
    if event.get("entryPublished"):
        qualifying_unpublished = event.get("source") == "WTA" and not any(
            entry.get("type") == "QUAL" for entry in event.get("entries", [])
        )
        open_routes = [route for route in open_routes if route == "QUAL" and qualifying_unpublished]
    latest = max(value for value in (main, qual) if value) if main or qual else None
    return {
        "deadlineMain": main.isoformat() if main else None,
        "deadlineQual": qual.isoformat() if qual else None,
        "deadline": latest.isoformat() if latest else None,
        "deadlineBasis": basis,
        "openRoutes": open_routes,
    }


def event_available_to(event, player_key):
    """Existing singles entries remain assessable; new entries need an open window."""
    return player_key in event.get("listedPlayerIds", []) or bool(event.get("openRoutes"))


def planner_slam_deadlines(slams):
    """Share main-draw and qualifying entry cutoffs with Points Breakdown."""
    deadlines = []
    for slam in slams:
        main, qualifying = _date(slam.get("mdCutoff")), _date(slam.get("qCutoff"))
        if main and qualifying:
            deadlines.append({
                "id": slam["id"], "name": slam["name"], "year": slam["year"],
                "mdCutoff": main.isoformat(), "qCutoff": qualifying.isoformat(),
            })
    return deadlines


def _calendar(data_dir):
    events = {}
    for row in expand_calendar_snapshot(_load(data_dir, "calendar_snapshot.json", [])):
        source = str(row.get("source") or "").upper()
        source_id = str(row.get("tournamentKey") or row.get("tournamentId") or "")
        if source and source_id:
            item = dict(row)
            item["sourceId"] = source_id.lower()
            item["season"] = str(row.get("startDate") or "")[:4]
            events[_event_id(source, source_id, item["season"])] = item
    wta = expand_wta_calendar_cache(_load(data_dir, "wta_full_calendar_cache.json", {}))
    for row in wta.get("items", []):
        group = row.get("tournamentGroup") or {}
        source_id = str(group.get("id") or "")
        season = str(row.get("year") or row.get("startDate", "")[:4])
        if not source_id:
            continue
        event_id = _event_id("WTA", source_id, season)
        if not filter_wta_calendar([row], "0000-01-01", "9999-12-31"):
            events.pop(event_id, None)
            continue
        item = {
            "name": f"{row.get('level', '')} {row.get('city') or group.get('name') or ''}".strip(),
            "source": "WTA",
            "sourceId": source_id,
            "season": season,
            "level": row.get("level", ""),
            "surface": row.get("surface", ""),
            "country": row.get("country", ""),
            "startDate": row.get("startDate", "")[:10],
            "endDate": row.get("endDate", "")[:10],
            "drawSize": row.get("singlesDrawSize"),
        }
        item.update({key: value for key, value in row.items() if "deadline" in key.lower()})
        events[event_id] = {**item, **events.get(event_id, {})}
    itf = expand_itf_calendar_cache(_load(data_dir, "itf_calendar_cache.json", {}))
    for row in itf.get("items", []):
        source_id = str(row.get("tournamentKey") or "").lower()
        identity = _entry_identity(source_id)
        if not identity:
            continue
        event_id = _event_id(*identity)
        if _is_cancelled_itf_calendar_item(row):
            events.pop(event_id, None)
            continue
        item = {
            "name": row.get("tournamentName") or row.get("name", ""),
            "source": "ITF",
            "sourceId": source_id,
            "season": identity[2],
            "level": row.get("category", ""),
            "surface": row.get("surfaceDesc", ""),
            "country": row.get("hostNationCode", ""),
            "startDate": row.get("startDate", "")[:10],
            "endDate": row.get("endDate", "")[:10],
        }
        item.update({key: value for key, value in row.items() if "deadline" in key.lower()})
        events[event_id] = {**item, **events.get(event_id, {})}
    return events


def _player(index, source, player_id, name):
    identifier = str(player_id or "").strip()
    record = index.resolve(source.lower(), player_id=identifier, name=name)
    if record:
        return record.player_key, record.presentation_name
    key = f"{source.lower()}:{identifier}" if identifier else f"name:{normalized_name(name)}"
    return key, fix_encoding_keep_accents(str(name or ""))


def _field(entries):
    ranks = sorted(row["rank"] for row in entries if row["rank"] and row["rankType"] == "WTA")
    wtns = sorted(row["wtn"] for row in entries if row["wtn"] is not None)
    return {"count": len(entries), "ranks": ranks, "wtns": wtns, "wtnCount": len(wtns)}


def _historical_field(event, strength, draw="MD"):
    candidates = [
        row
        for row in strength
        if event["source"] == "WTA"
        and str(row.get("id")) == event["sourceId"]
        and str(row.get("year")) < str(event["season"])
        and row.get("draw") == draw
        and row.get("level") == event["level"]
        and row.get("rankings")
    ]
    if not candidates:
        return None
    row = max(candidates, key=lambda item: (str(item.get("year", "")), str(item.get("startDate", ""))))
    ranks = sorted(rank for value in row["rankings"] if (rank := _rank(value)) and rank != 2000)
    return {
        "year": str(row.get("year")),
        "startDate": row.get("startDate"),
        # T-Strength stores 2000 for an unranked player; it is not an observed ranking.
        "ranks": ranks,
        "rankedCount": len(ranks),
        "drawSize": row.get("playerCount"),
        "meanRank": round(sum(ranks) / len(ranks), 1) if ranks else None,
        "source": "WTA",
    }


def _rankings(data_dir, index, today, *, explicit_today):
    weeks = defaultdict(list)
    # Ranking archives use decade names, e.g. 20_29 rather than 2020_2029.
    decade = (today.year % 100) // 10 * 10
    path = data_dir / f"wta_rankings_{decade:02d}_{decade + 9:02d}.csv"
    if not path.exists():
        return None, {}
    cutoff = (today - timedelta(days=60)).isoformat()
    with path.open(encoding="utf-8-sig", newline="") as source:
        for row in csv.DictReader(source):
            if cutoff <= row.get("week_date", "") <= today.isoformat():
                weeks[row["week_date"]].append(
                    {
                        "Id": row.get("id"),
                        "Player": row.get("player"),
                        "Rank": row.get("rank"),
                        "Points": row.get("points"),
                        "Country": row.get("country"),
                        "DOB": row.get("dob"),
                    }
                )
    instant = datetime.combine(today, time(23, 59), NEW_YORK) if explicit_today else new_york_now()
    accepted = accepted_ranking_dates(weeks, instant, load_ranking_status(data_dir))
    latest = max(accepted, default=None)
    ranks = {}
    for row in weeks.get(latest, []):
        if row.get("Country") == "ARG":
            key, _ = _player(index, "wta", row.get("Id"), row.get("Player"))
            ranks[key] = {"rank": _rank(row.get("Rank")), "points": _rank(row.get("Points")) or 0}
    return latest, ranks


def _surface(value):
    value = str(value or "").casefold()
    return next((name for name in ("Clay", "Hard", "Grass", "Carpet") if name.casefold() in value), "Unknown")


def _player_results(data_dir, index, today, live_matches=None):
    results = defaultdict(list)
    cutoff = (today - timedelta(days=365)).isoformat()
    seen = set()
    for filename, source_name in MATCH_SOURCES.items():
        path = data_dir / filename
        if not path.exists():
            continue
        with path.open(encoding="utf-8-sig", newline="") as source:
            for row in csv.DictReader(source):
                played = str(row.get("date") or "")[:10]
                if not cutoff <= played <= today.isoformat():
                    continue
                status = str(row.get("resultStatusDesc") or "").casefold()
                score = re.sub(r"[^a-z]", "", str(row.get("result") or "").casefold())
                if str(row.get("draw", "")).upper().startswith("D") or "double" in status:
                    continue
                if live_matches is not None and played >= (today - timedelta(days=21)).isoformat():
                    live_matches.append(row)
                if "walkover" in status or score in {"wo", "walkover", "bye"}:
                    continue
                if any(
                    str(row.get(f"{side}Name") or "").casefold() in {"bye", "unknown", ""}
                    or "/" in str(row.get(f"{side}Name") or "")
                    for side in ("winner", "loser")
                ):
                    continue
                match_key = (source_name, row.get("tournamentId"), played, row.get("matchId"), row.get("roundName"))
                if match_key in seen:
                    continue
                seen.add(match_key)
                for side in ("winner", "loser"):
                    if row.get(f"{side}Country") != "ARG":
                        continue
                    identifier = row.get(f"{side}Id")
                    name = row.get(f"{side}Name")
                    record = index.resolve_any_id(identifier)
                    if source_name == "bjkc":
                        record = record or index.resolve("bjkc", name=name)
                    key = (
                        record.player_key
                        if record
                        else _player(
                            index,
                            "itf" if str(identifier).startswith("800") else "wta",
                            identifier,
                            row.get(f"{side}Name"),
                        )[0]
                    )
                    results[key].append((played, side == "winner", _surface(row.get("surface")), name))
    return results


_PROGRESS_ROUNDS = ["QR1", "QR2", "QR3", "R128", "R64", "R32", "R16", "QF", "SF", "F", "W"]


def _main_round(draw_size, number):
    remaining = (1 << (int(draw_size or 32) - 1).bit_length()) // (2 ** (number - 1))
    return {1: "W", 2: "F", 4: "SF", 8: "QF"}.get(remaining, f"R{remaining}")


def _qualifying_rounds(draw, event=None, point_tables=()):
    labels = [label for label in draw.get("round_labels", []) if str(label).casefold() != "qualifier"]
    known = len(labels) or _rank(draw.get("num_rounds"))
    if known or event is None:
        return known
    description = event.get("pointsDescription")
    table = next((row for row in point_tables if row.get("Description") == description), None)
    if table is None:
        table = next((row for row in point_tables
                      if str(row.get("Description") or "").startswith(event["level"] + " (")), {})
    for number in (3, 2, 1):
        if table.get(f"QR{number}") not in (None, ""):
            return number
    if event.get("level") in {"GS", "Grand Slam"}:
        return 3
    qualifying_size = event.get("qualifyingSize") or 32
    if event["level"] == "WTA 125" and qualifying_size <= 8:
        return 1
    return 3 if qualifying_size > 32 else 2


def _update_progress(progress, key, value):
    previous = progress.get(key)
    if previous:
        if value["route"] == "QUAL":
            previous["route"] = "QUAL"
            if value["round"].startswith("QR") and value["completed"] and not previous["round"].startswith("QR"):
                previous["qualifyingRound"] = value["round"]
        previous["played"] = previous["played"] or value["played"]
        old_round = _PROGRESS_ROUNDS.index(previous["round"])
        new_round = _PROGRESS_ROUNDS.index(value["round"])
        if old_round > new_round or (old_round == new_round and previous["completed"]):
            return
        if previous["route"] == "QUAL":
            value["route"] = "QUAL"
            if previous["round"].startswith("QR") and previous["completed"]:
                value["qualifyingRound"] = previous["round"]
        if previous.get("qualifyingRound"):
            value["qualifyingRound"] = previous["qualifyingRound"]
        value["played"] = value["played"] or previous["played"]
    progress[key] = value


def _draw_progress(event, draws, index):
    progress, participants = {}, {}
    for draw_type in ("QS", "MDS"):
        draw = draws.get(draw_type) or {}
        qualifying = draw_type == "QS"
        size = _rank(draw.get("draw_size")) or event.get("drawSize") or 32
        rounds = _qualifying_rounds(draw) if qualifying else (int(size) - 1).bit_length()
        if not rounds:
            continue
        players = [row for row in draw.get("players", []) if _rank(row.get("pos"))]
        matches = {(row.get("round"), row.get("match_num")): row for row in draw.get("matches", [])}
        byes = set(draw.get("byes", []))
        for player in players:
            raw_name = str(player.get("name") or "")
            if not raw_name or "/" in raw_name:
                continue
            family, comma, given = raw_name.partition(",")
            name = f"{given.strip()} {family.strip()}" if comma else raw_name
            source = "ITF" if player.get("itf_id") else event["source"]
            identifier = player.get("itf_id") or player.get("wta_id") or ""
            key, name = _player(index, source, identifier, name)
            record = index.by_key.get(key)
            if str(player.get("country") or (record.country if record else "")).upper() != "ARG":
                continue
            participants[key] = {"name": name, "source": source, "player_id": identifier}
            pos = int(player["pos"])
            route = "QUAL" if qualifying or player.get("entry") in {"Q", "LL"} else "MAIN"
            value = {"round": "QR1" if qualifying else _main_round(size, 1),
                     "route": route, "completed": False, "played": not qualifying and player.get("entry") == "Q"}
            for number in range(1, rounds + 1):
                value["round"] = f"QR{number}" if qualifying else _main_round(size, number)
                match_number = (pos - 1) // (2 ** number)
                match = matches.get((number, match_number), {})
                bye = number == 1 and (pos + 1 if pos % 2 else pos - 1) in byes
                if not bye:
                    if not _is_completed_score(str(match.get("score") or "")):
                        break
                    candidates = [row for row in players
                                  if (int(row["pos"]) - 1) // (2 ** number) == match_number
                                  and _player_name_matches_winner(row["name"], match.get("winner_name", ""))]
                    if len(candidates) != 1:
                        break
                    value["played"] = True
                    if int(candidates[0]["pos"]) != pos:
                        value["completed"] = True
                        break
                if number == rounds:
                    value["round"] = _main_round(event.get("drawSize"), 1) if qualifying else "W"
                    value["completed"] = not qualifying
            _update_progress(progress, key, value)
    return progress, participants


def _match_progress(event, matches, index, progress, participants, draws, point_tables):
    start = _date(event["startDate"])
    end = _date(event.get("endDate")) or start
    for row in sorted(matches, key=lambda row: str(row.get("date") or "")):
        played = _date(row.get("date"))
        if (str(row.get("tournamentId")) != str(event.get("tournamentId") or event["sourceId"])
                or not played or not start - timedelta(days=7) <= played <= end):
            continue
        if not (row.get("result") or row.get("resultStatusDesc")) or any(
            str(row.get(f"{side}Name") or "").casefold() in {"bye", "unknown", ""}
            for side in ("winner", "loser")
        ):
            continue
        raw_round = str(row.get("roundName") or "")
        qualifying = str(row.get("draw") or "").upper() == "Q"
        number = re.search(r"\d+", raw_round)
        if qualifying:
            if not number or int(number[0]) > 3:
                continue
            number = int(number[0])
            current = f"QR{number}"
            final_qualifying = _qualifying_rounds(draws.get("QS") or {}, event, point_tables)
            following = _main_round(event.get("drawSize"), 1) if number >= final_qualifying else f"QR{number + 1}"
        else:
            named = {"quarter": "QF", "semi": "SF", "final": "F"}
            current = next((value for label, value in named.items() if label in raw_round.casefold()), None)
            if current is None and number:
                current = _main_round(event.get("drawSize"), int(number[0]))
            if current not in _PROGRESS_ROUNDS:
                continue
            following = _PROGRESS_ROUNDS[_PROGRESS_ROUNDS.index(current) + 1]
        for side in ("winner", "loser"):
            if row.get(f"{side}Country") != "ARG" or "/" in str(row.get(f"{side}Name") or ""):
                continue
            identifier, name = row.get(f"{side}Id"), row.get(f"{side}Name")
            source = "ITF" if str(identifier).startswith("800") else event["source"]
            key, name = _player(index, source, identifier, name)
            participants[key] = {"name": name, "source": source, "player_id": identifier}
            won = side == "winner"
            _update_progress(progress, key, {"round": following if won else current,
                                           "route": "QUAL" if qualifying or row.get(f"{side}Entry") in {"Q", "LL"}
                                           else "MAIN",
                                           "completed": not won or following == "W", "played": True})


def build_planner_data(data_dir, *, today=None):
    """Build a compact planner payload without fetching or changing stored data."""
    explicit_today = today is not None
    today = today or madrid_today()
    data_dir = Path(data_dir)
    index = PlayerIdentityIndex(_load(data_dir, "player_aliases_wta_itf.json", []))
    events = _calendar(data_dir)
    entry_lists = expand_entry_lists_cache(_load(data_dir, "entry_lists_cache.json", {}))
    entry_keys = {_event_id(*identity): key for key in entry_lists if (identity := _entry_identity(key))}
    strength = expand_tstrength_cache(_load(data_dir, "tstrength_cache.json", []))
    draw_sizes = expand_tournament_draw_sizes(_load(data_dir, "tournament_draw_sizes.json", []))
    point_tables = expand_points_distribution(_load(data_dir, "points_distribution.json", []))
    draw_cache = expand_draws_store_cache(_load(data_dir, "draws_store_cache.json", {}))
    draw_keys = {_event_id(*identity): key for key in draw_cache if (identity := _entry_identity(key))}
    itf_ids = _load(data_dir, "itf_event_filters_cache.json", {})
    wtn_cache = _load(data_dir, "itf_wtn_cache.json", {})
    live_matches = []
    results = _player_results(data_dir, index, today, live_matches)
    ranking_date, ranks = _rankings(data_dir, index, today, explicit_today=explicit_today)
    arg_entries = {}
    output_events = []
    current_week = today - timedelta(days=today.weekday())
    for event_id, raw_event in events.items():
        start = _date(raw_event.get("startDate"))
        end = _date(raw_event.get("endDate")) or start
        if (
            not start
            or not end
            or end < current_week
            or raw_event.get("level") in {"WTAFinals", "BJKC"}
            or "cancel" in str(raw_event.get("name", "")).casefold()
        ):
            continue
        event = dict(raw_event)
        if event["source"] == "ITF" and event["sourceId"] in itf_ids:
            event["tournamentId"] = str(itf_ids[event["sourceId"]])
        event["id"] = event_id
        event["calendarKey"] = get_calendar_tournament_key(event)
        event["name"] = fix_encoding_keep_accents(str(event.get("name") or ""))
        event["displayName"] = compact_tournament_name(event["name"])
        event["week"] = (start - timedelta(days=start.weekday())).isoformat()
        event["continent"] = event.get("continent") or get_continent(event.get("country", ""))
        entry_key = entry_keys.get(event_id, "")
        event["entryKey"] = entry_key
        event["entries"] = []
        for row in entry_lists.get(entry_key, []):
            if row.get("type") not in {"MAIN", "QUAL", "ALT"} or not row.get("name"):
                continue
            key, name = _player(index, event["source"], row.get("player_id"), row.get("name"))
            rank_value = row.get("rank")
            numeric_rank = _rank(rank_value)
            entry = {
                "playerKey": key,
                "name": name,
                "country": str(row.get("country") or "").upper(),
                "type": row["type"],
                "position": _rank(row.get("pos_num") or row.get("pos")),
                "priority": _rank(row.get("priority")),
                "rank": numeric_rank,
                "rankType": "WTA" if numeric_rank else str(rank_value or ""),
                "wtn": _number(row.get("wtn")),
            }
            event["entries"].append(entry)
            if str(row.get("country") or "").upper() == "ARG":
                arg_entries[key] = {"name": name, "source": event["source"], "player_id": row.get("player_id")}
        event["entryPublished"] = bool(event["entries"])
        event.update(entry_deadlines(event, today))
        event["field"] = {
            "main": _field([row for row in event["entries"] if row["type"] == "MAIN"]),
            "qual": _field([row for row in event["entries"] if row["type"] == "QUAL"]),
        }
        event["entries"] = [entry for entry in event["entries"] if entry["country"] == "ARG"]
        sizes = [
            row
            for row in draw_sizes
            if str(row.get("source", "")).upper() == event["source"]
            and str(row.get("tournamentId", "")).lower() in {event["sourceId"], str(event.get("tournamentId") or "")}
            and str(row.get("date", ""))[:4] == event["season"]
        ]
        event["drawSize"] = event.get("drawSize") or (sizes[-1].get("mainDrawSize") if sizes else None)
        if sizes:
            event["pointsDescription"] = sizes[-1].get("description")
            event["qualifyingSize"] = sizes[-1].get("qualifyingSize")
        draws = (draw_cache.get(draw_keys.get(event_id)) or {}).get("draws") or {}
        event["drawSize"] = event.get("drawSize") or (draws.get("MDS") or {}).get("draw_size")
        progress, participants = _draw_progress(event, draws, index)
        _match_progress(event, live_matches, index, progress, participants, draws, point_tables)
        event["progress"] = progress
        listed = {entry["playerKey"] for entry in event["entries"]}
        for key, participant in participants.items():
            arg_entries[key] = participant
            if key not in listed:
                event["entries"].append({
                    "playerKey": key, "name": participant["name"], "country": "ARG",
                    "type": "QUAL" if progress[key]["round"].startswith("QR") else "MAIN",
                    "position": None, "priority": None, "rank": None, "rankType": "", "wtn": None,
                })
        event["listedPlayerIds"] = sorted(listed | set(participants))
        event["entryPublished"] = event["entryPublished"] or bool(participants)
        event["historical"] = _historical_field(event, strength)
        event["historicalQual"] = _historical_field(event, strength, draw="Q")
        output_events.append(event)
    players = []
    player_keys = set(ranks) | set(results) | set(arg_entries)
    for key in player_keys:
        record = index.by_key.get(key)
        if record and record.country != "ARG":
            continue
        fallback = arg_entries.get(key, {})
        history = sorted(results.get(key, []), reverse=True)
        surfaces = {}
        for surface in ("Clay", "Hard", "Grass", "Carpet"):
            matches = [won for _, won, played_surface, _ in history if surface == played_surface]
            surfaces[surface] = {"wins": sum(matches), "losses": len(matches) - sum(matches)}
        itf_id = record.itf_id if record else (fallback.get("player_id", "") if fallback.get("source") == "ITF" else "")
        wtn = wtn_cache.get(itf_id, {})
        recent = history[:20]
        players.append(
            {
                "id": key,
                "name": record.presentation_name
                if record
                else fallback.get("name") or (history[0][3] if history else key),
                "country": "ARG",
                "wtaId": record.wta_id if record else "",
                "itfId": itf_id,
                "rank": ranks.get(key, {}).get("rank"),
                "points": ranks.get(key, {}).get("points"),
                "wtn": _number(wtn.get("wtn")),
                "wtnDate": wtn.get("retrieved_at") or wtn.get("observed_on"),
                "surfaces": surfaces,
                "recent": {
                    "wins": sum(row[1] for row in recent),
                    "losses": sum(not row[1] for row in recent),
                    "matches": len(recent),
                },
                "lastMatchDate": history[0][0] if history else None,
                "listedEventIds": [event["id"] for event in output_events if key in event["listedPlayerIds"]],
            }
        )
    return {
        "asOf": today.isoformat(),
        "asOfRanking": ranking_date,
        "events": sorted(output_events, key=lambda event: (event["startDate"], event["name"])),
        "players": sorted(players, key=lambda player: (player["rank"] or 100000, player["name"])),
        "surfacePeriodDays": 365,
    }
