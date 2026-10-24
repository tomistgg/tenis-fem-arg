"""Withdrawal history kept separately from the current acceptance lists."""

import json
import re
from datetime import date, timedelta
from pathlib import Path

from config import resolve_player_presentation_name
from utils import normalize_player_name

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


def remove_wta_draw_absences(history, published_date):
    """A roster replacement after qualifying publication cannot prove withdrawal."""
    if published_date:
        history["withdrawals"] = [
            p for p in history.get("withdrawals", []) if p.get("date", "") < published_date
        ]


def promote_wta_qualifying_entries(previous, current, qualifying_draw, history, published_date):
    """Retain missing qualifying entrants ranked above the published qualifying field."""
    draw_players = qualifying_draw.get("players") or []
    if not draw_players:
        return current

    def name_key(player):
        name = str(player.get("name") or "")
        if "," in name:
            surname, given = name.split(",", 1)
            name = f"{given.strip()} {surname.strip()}"
        return normalize_player_name(resolve_player_presentation_name(
            "wta", player_id=player.get("player_id", ""), name=name,
        ))

    def entry_rank(player):
        value = str(player.get("rank") or "").strip()
        return int(value) if value.isdigit() and int(value) > 0 else None

    known = [*history.get("last_seen", {}).values(), *previous, *current]
    by_name = {name_key(player): player for player in known if name_key(player)}
    by_id = {str(player["player_id"]): player for player in known if player.get("player_id")}
    draw_names = {name_key(player) for player in draw_players}
    draw_ids = {str(player["player_id"]) for player in draw_players if player.get("player_id")}
    ranks = []
    for player in draw_players:
        entry = by_id.get(str(player.get("player_id") or "")) or by_name.get(name_key(player))
        rank = entry_rank(entry or {})
        if str(player.get("seed") or "").strip() == "1" and rank is None:
            return current
        if rank is not None:
            ranks.append(rank)
    if not ranks:
        return current
    best_entry_rank = min(ranks)

    # Recover only absences first recorded when the draw appeared, never older withdrawals.
    candidates = {_player_key(player): player for player in previous if player.get("type") == "QUAL"}
    earlier_withdrawals = {
        _player_key(player) for player in history.get("withdrawals", [])
        if not published_date or player.get("date", "") < published_date
    }
    if published_date:
        for player in history.get("last_seen", {}).values():
            if player.get("type") == "QUAL" and _player_key(player) not in earlier_withdrawals:
                candidates.setdefault(_player_key(player), player)
    for player in history.get("withdrawals", []):
        if player.get("type") == "QUAL" and published_date and player.get("date", "") >= published_date:
            candidates.setdefault(_player_key(player), player)
    main_names = {name_key(player) for player in current if player.get("type") == "MAIN"}
    promoted = {}
    for key, player in candidates.items():
        rank = entry_rank(player)
        if (rank is None or rank >= best_entry_rank or name_key(player) in draw_names
                or str(player.get("player_id") or "") in draw_ids or name_key(player) in main_names):
            continue
        promoted[key] = {**player, "type": "MAIN", "entry": "", "seed": ""}
        promoted[key].pop("date", None)
    if not promoted:
        return current

    result = [player for player in current if _player_key(player) not in promoted]
    result.extend(promoted.values())
    main = sorted((p for p in result if p.get("type") == "MAIN"), key=lambda p: entry_rank(p) or 99999)
    for position, player in enumerate(main, 1):
        player["pos"], player["pos_num"] = str(position), position
    history["withdrawals"] = [p for p in history.get("withdrawals", []) if _player_key(p) not in promoted]
    for key, player in promoted.items():
        history.setdefault("last_seen", {})[key] = _snapshot(player)
    return main + [player for player in result if player.get("type") != "MAIN"]


def record_wta_withdrawals(state, key, previous, current, observation, observed_date, *, qualifying_draw_date=None):
    """Infer absence only in successfully parsed, nonempty provider sections."""
    tournament = state.setdefault(key, {"withdrawals": [], "last_seen": {}})
    last_seen = tournament["last_seen"]
    for player in _real_players(previous):
        last_seen.setdefault(_player_key(player), _snapshot(player))
    if qualifying_draw_date and observed_date >= qualifying_draw_date:
        remove_wta_draw_absences(tournament, qualifying_draw_date)
        return
    sections = set(observation.get("complete_sections", []))
    if not sections:
        return
    active_ids = set(observation.get("player_ids", []))
    section_ids = {
        section: set(ids)
        for section, ids in observation.get("section_player_ids", {}).items()
    }
    active_names = {
        section: {_name_key(p) for p in current if p.get("type") == section}
        for section in WITHDRAWAL_DRAW_TYPES
    }

    def is_present(player, section):
        ids = section_ids.get(section, active_ids)
        return str(player.get("player_id") or "") in ids or _name_key(player) in active_names[section]

    def is_active(player):
        return is_present(player, "MAIN") or (
            player.get("type") == "QUAL" and is_present(player, "QUAL")
        )

    records = {
        _player_key(p): p
        for p in tournament["withdrawals"]
        if p.get("type") in WITHDRAWAL_DRAW_TYPES and not is_active(p)
    }
    # Keep the last trusted roster even if an intervening partial response
    # changed the regular entry cache. That response must not erase history.
    for player in last_seen.values():
        if player.get("type") in sections and not is_active(player):
            records.setdefault(_player_key(player), {**_snapshot(player), "date": observed_date})
    for player in _real_players(current):
        if player.get("type") in sections:
            last_seen[_player_key(player)] = _snapshot(player)
    tournament["withdrawals"] = list(records.values())


def _itf_withdrawal_date(information):
    match = re.search(r"\b(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})\b", str(information or ""))
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
                source = "wta" if key.startswith("http") else "itf"
                rows.append(
                    {
                        "position": position,
                        "name": resolve_player_presentation_name(
                            source,
                            player_id=row.get("player_id", ""),
                            name=row.get("name", ""),
                        ),
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
