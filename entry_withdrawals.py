"""Withdrawal history kept separately from the current acceptance lists."""

import json
import re
from datetime import date, timedelta
from pathlib import Path

from config import resolve_player_presentation_name

WITHDRAWALS_FILENAME = "entry_withdrawals.json"
DRAW_LABELS = {"MAIN": "MD", "QUAL": "Q", "ALT": "ALT"}
WITHDRAWAL_DRAW_TYPES = {"MAIN", "QUAL"}
MONTHS = {
    name: number
    for number, name in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1
    )
}


def withdrawal_deadline(start_date):
    """Wednesday two weeks before the tournament's Monday (September 28 -> 16)."""
    try:
        start = date.fromisoformat(str(start_date)[:10])
    except ValueError:
        return None
    return start - timedelta(days=start.weekday() + 12)


def load_withdrawals(path):
    try:
        with Path(path).open(encoding="utf-8") as handle:
            state = json.load(handle)
    except FileNotFoundError:
        return {}
    if not isinstance(state, dict):
        raise ValueError("withdrawal history must be an object")
    return state


def _name_key(player):
    return " ".join(str(player.get("name") or "").split()).casefold()


def _player_key(player):
    identifier = str(player.get("player_id") or "").strip()
    return f"id:{identifier}" if identifier else f"name:{_name_key(player)}"


def _snapshot(player):
    return {
        key: str(player.get(key) or "")
        for key in ("player_id", "name", "country", "pos", "type", "rank")
    }


def _real_players(players):
    return [p for p in players if p.get("name") and not p["name"].startswith("(")]


def record_wta_withdrawals(state, key, previous, current, observation, observed_date):
    """Infer absence only in successfully parsed, nonempty provider sections."""
    tournament = state.setdefault(key, {"withdrawals": [], "last_seen": {}})
    last_seen = tournament["last_seen"]
    for player in _real_players(previous):
        last_seen.setdefault(_player_key(player), _snapshot(player))
    sections = set(observation.get("complete_sections", []))
    if not sections:
        return
    active_ids = set(observation.get("player_ids", []))
    active_names = {_name_key(p) for p in current}

    def is_present(player):
        return str(player.get("player_id") or "") in active_ids or _name_key(player) in active_names

    records = {
        _player_key(p): p
        for p in tournament["withdrawals"]
        if p.get("type") in WITHDRAWAL_DRAW_TYPES and not is_present(p)
    }
    # Keep the last trusted roster even if an intervening partial response
    # changed the regular entry cache. That response must not erase history.
    for player in last_seen.values():
        if player.get("type") in sections and not is_present(player):
            records.setdefault(_player_key(player), {**_snapshot(player), "date": observed_date})
    for player in _real_players(current):
        if player.get("type") in sections:
            last_seen[_player_key(player)] = _snapshot(player)
    tournament["withdrawals"] = list(records.values())


def _itf_withdrawal_date(information):
    match = re.search(r"\bW\s+(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})\b", str(information or ""))
    if not match:
        return None
    day, month, year = match.groups()
    try:
        return date(int(year), MONTHS[month.title()], int(day))
    except (KeyError, ValueError):
        return None


def _itf_entry_rank(player):
    wta_rank = str(player.get("atpWtaRank") or "").strip()
    if wta_rank:
        return wta_rank
    itf_rank = str(player.get("itfWorldTennisRanking") or "").strip()
    return f"ITF {itf_rank}" if itf_rank else "-"


def record_itf_withdrawals(state, key, previous, current, classifications, start_date, observed_date):
    """Use ITF's dated W entries; W-list numbering is never a former draw position."""
    deadline = withdrawal_deadline(start_date)
    if deadline is None:
        return
    classifications = [item for item in classifications if isinstance(item, dict)]
    tournament = state.setdefault(key, {"withdrawals": [], "last_seen": {}})
    last_seen = tournament["last_seen"]
    for player in _real_players(previous):
        last_seen[_player_key(player)] = _snapshot(player)
    records = {
        _player_key(p): p
        for p in tournament["withdrawals"]
        if p["date"] >= deadline.isoformat()
    }
    if observed_date >= deadline.isoformat():
        for classification in classifications:
            if classification.get("entryClassificationCode") != "W":
                continue
            for entry in classification.get("entries") or []:
                withdrawn = _itf_withdrawal_date(entry.get("information"))
                if withdrawn is None or withdrawn < deadline or withdrawn.isoformat() > observed_date:
                    continue
                for player in entry.get("players") or []:
                    if player.get("hiddenPlayer"):
                        continue
                    identifier = str(player.get("playerId") or "")
                    name = resolve_player_presentation_name(
                        "itf",
                        player_id=identifier,
                        name=f"{player.get('givenName') or ''} {player.get('familyName') or ''}".strip(),
                    )
                    if not name:
                        continue
                    record = {"player_id": identifier, "name": name, "country": player.get("nationalityCode") or "-"}
                    identity = _player_key(record)
                    previous_record = records.get(identity)
                    position = last_seen.get(identity, {})
                    if previous_record and previous_record["date"] == withdrawn.isoformat():
                        position = previous_record
                    rank = str(position.get("rank") or "").strip()
                    if not rank or rank == "-":
                        rank = _itf_entry_rank(player)
                    record.update(
                        pos=position.get("pos", ""),
                        type=position.get("type", ""),
                        rank=rank,
                        date=withdrawn.isoformat(),
                    )
                    records[identity] = record
    for player in _real_players(current):
        last_seen[_player_key(player)] = _snapshot(player)
    tournament["withdrawals"] = list(records.values())
    if classifications:
        tournament["last_checked_date"] = observed_date


def public_withdrawals(state, tournament_groups):
    """Expose rows and deadlines to the browser, without internal position history."""
    result = {}
    for tournaments in tournament_groups.values():
        for key, info in tournaments.items():
            deadline = None if key.startswith("http") else withdrawal_deadline(info.get("startDate"))
            rows = []
            for row in state.get(key, {}).get("withdrawals", []):
                if deadline is not None and row["date"] < deadline.isoformat():
                    continue
                if key.startswith("http") and row.get("type") not in WITHDRAWAL_DRAW_TYPES:
                    continue
                draw = DRAW_LABELS.get(row.get("type"))
                position = f"{row['pos']}-{draw}" if row.get("pos") and draw else "—"
                rows.append(
                    {
                        "position": position,
                        "name": row["name"],
                        "country": row.get("country", "-"),
                        "rank": row.get("rank") or "-",
                        "date": row["date"],
                    }
                )
            result[key] = {
                "deadline": deadline.isoformat() if deadline else None,
                "rows": sorted(rows, key=lambda row: (row["date"], row["name"]), reverse=True),
            }
    return result
