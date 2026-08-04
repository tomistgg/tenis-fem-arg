# NOTE: This script runs after main.py in CI (see hourly-update.yml).
# It imports from config and utils — keep those modules free of heavy
# runtime dependencies (Selenium, pandas, etc.) so this script stays lightweight.
import argparse
import csv
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from canonical_data import (
    MATCH_SOURCES,
    PlayerIdentityIndex,
    canonical_match_key,
    load_player_rows,
    sync_itf_players,
    sync_wta_match_players,
)
from config import repair_name_text
from execution_analysis import analyze_execution, effective_run_status
from time_utils import parse_utc_timestamp, utc_now
from html_generator import country_flag_html
from utils import (
    fix_encoding,
    expand_calendar_snapshot,
    expand_draws_store_cache,
    expand_entry_lists_cache,
    expand_tournament_snapshot,
    expand_draws_snapshot,
    get_cache_timestamp,
    write_text_if_changed,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAX_MATCH_LINES_PER_FILE = 50
RANKINGS_CSV_FILES = ["wta_rankings_83_99.csv", "wta_rankings_00_09.csv", "wta_rankings_10_19.csv", "wta_rankings_20_29.csv"]
ALIASES_JSON_FILE = "player_aliases_wta_itf.json"


def load_json(path):
    if not os.path.exists(path):
        return None
    try:
        # Use utf-8-sig to tolerate BOM-prefixed JSON files (common on Windows).
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if os.path.basename(path) == "calendar_snapshot.json":
            return expand_calendar_snapshot(data)
        if os.path.basename(path) == "tournament_snapshot.json":
            return expand_tournament_snapshot(data)
        if os.path.basename(path) == "draws_snapshot.json":
            return expand_draws_snapshot(data)
        if os.path.basename(path) == "draws_store_cache.json":
            return expand_draws_store_cache(data)
        if os.path.basename(path) == "entry_lists_cache.json":
            return expand_entry_lists_cache(data)
        return data
    except Exception:
        return None


def normalize_name(value):
    return repair_name_text(value).strip().upper()


def normalize_exact_name(value):
    return " ".join(repair_name_text(value).strip().upper().split())


_ITF_CALENDAR_SEQUENCE_SUFFIX_RE = re.compile(r"\s+\d+$")


def normalize_calendar_name(value, column=""):
    """Normalize calendar names for diffing.

    ITF display names are numbered during calendar assembly (for example
    "W15 Monastir 19"), and that suffix can shift between runs when the
    visible window changes. Strip the synthetic sequence number so calendar
    diffs compare the underlying event instead of the run-dependent label.
    """
    name = normalize_exact_name(value)
    if (column or "").strip().lower() == "itf":
        name = _ITF_CALENDAR_SEQUENCE_SUFFIX_RE.sub("", name)
    return name


def normalize_country(value):
    return (value or "").strip().upper()


def _has_country_flag(code):
    if not code or code == "-":
        return False
    return str(country_flag_html(code, show_code=False)).startswith("<img")


def _record_flagless_country(bucket, code, player_name):
    code = normalize_country(code)
    if not code or code == "-":
        return
    if _has_country_flag(code):
        return
    entry = bucket.setdefault(code, {"country": code, "players": []})
    if player_name and player_name not in entry["players"]:
        entry["players"].append(player_name)


def strip_accents(text):
    return fix_encoding(repair_name_text(text))


def normalize_rank_key(value):
    s = strip_accents(value).upper()
    s = " ".join(s.split())
    return s


def name_variants(value):
    base = normalize_rank_key(value)
    if not base:
        return set()
    out = {base}
    if "-" in base:
        out.add(" ".join(base.replace("-", " ").split()))
    return out


def monday_from_date_str(value):
    s = (value or "").strip()
    if not s:
        return ""
    if len(s) >= 10:
        s = s[:10]
    try:
        d = datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return ""
    monday = d - timedelta(days=d.weekday())
    return monday.strftime("%Y-%m-%d")


def _safe_int(value, default=9999):
    try:
        return int(value)
    except Exception:
        return default


def _acceptance_list_fingerprint(players):
    """Return a stable fingerprint for acceptance-list changes.

    We intentionally ignore derived fields like seed_rank and seed so the
    fingerprint only changes when the list itself changes, not when ranking
    lookups get refreshed.
    """
    if not players:
        return ""
    key_fields = sorted(
        (
            normalize_exact_name((row or {}).get("name") or ""),
            str((row or {}).get("type") or "").strip().upper(),
            _safe_int((row or {}).get("pos_num"), 9999),
        )
        for row in players
        if isinstance(row, dict)
    )
    return json.dumps(key_fields, ensure_ascii=False)


def is_itf_id(value):
    s = (value or "").strip()
    return s.isdigit() and (len(s) >= 9 or s.startswith("800"))


def get_tournament_label(t_key, before_snapshot, after_snapshot):
    if isinstance(after_snapshot, dict) and t_key in after_snapshot:
        return after_snapshot[t_key].get("name") or t_key
    if isinstance(before_snapshot, dict) and t_key in before_snapshot:
        return before_snapshot[t_key].get("name") or t_key
    return t_key


def format_tournament_key_label(t_key, name):
    key = (t_key or "").strip()
    tournament_name = (name or "").strip()
    if key and tournament_name:
        return f"{key} ({tournament_name})"
    return tournament_name or key


def get_arg_players(entries):
    players = set()
    for row in entries or []:
        if not isinstance(row, dict):
            continue
        country = normalize_country(row.get("country") or row.get("Country"))
        if country != "ARG":
            continue
        name = normalize_name(row.get("name") or row.get("player") or row.get("Player"))
        if name:
            players.add(name)
    return players


def format_match_line(row):
    date = row.get("date") or row.get("DATE") or ""
    tournament = row.get("tournamentName") or row.get("TOURNAMENT") or row.get("tournament") or ""
    winner = row.get("winnerName") or row.get("_winnerName") or ""
    loser = row.get("loserName") or row.get("_loserName") or ""
    result = row.get("result") or row.get("SCORE") or ""
    round_name = row.get("roundName") or row.get("ROUND") or ""

    matchup = ""
    if winner or loser:
        matchup = f"{winner} def. {loser}".strip()

    parts = [p for p in [date, tournament, round_name, matchup, result] if p]
    return " | ".join(parts)


def iter_match_sides(row):
    """Yield (side, player_id, player_name) for winner+loser."""
    w_id = (row.get("winnerId") or row.get("WINNERID") or row.get("winner_id") or "").strip()
    w_name = (row.get("winnerName") or row.get("_winnerName") or row.get("WINNERNAME") or row.get("WINNER_NAME") or "").strip()
    l_id = (row.get("loserId") or row.get("LOSERID") or row.get("loser_id") or "").strip()
    l_name = (row.get("loserName") or row.get("_loserName") or row.get("LOSERNAME") or row.get("LOSER_NAME") or "").strip()
    if w_name:
        yield "winner", w_id, w_name
    if l_name:
        yield "loser", l_id, l_name


def load_csv_rows(path):
    if not os.path.exists(path):
        return [], []
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            return reader.fieldnames or [], list(reader)
    except Exception:
        return [], []


def build_row_key(row, headers, source=None):
    if "matchId" in headers:
        if source:
            try:
                return "|".join(canonical_match_key(row, source))
            except ValueError:
                pass
        mid = str(row.get("matchId", "")).strip()
        tid = str(row.get("tournamentId", "")).strip()
        return f"{tid}|{mid}" if tid else mid
    if "MATCHID" in headers:
        mid = str(row.get("MATCHID", "")).strip()
        tid = str(row.get("TOURNAMENT_ID", "") or row.get("tournamentId", "")).strip()
        return f"{tid}|{mid}" if tid else mid

    required = ["date", "tournamentName", "winnerName", "loserName", "roundName", "draw"]
    if all(k in row for k in required):
        return "||".join([(row.get(k) or "").strip() for k in required])
    return None



def compute_report(before_dir, after_dir):
    report = {
        "withdrawals": [],
        "new_entry_lists": [],
        "itf_seed_missing_rankings": [],
        "added_matches": {},
        "added_calendar_tournaments": [],
        "flagless_player_countries": [],
        "wta_ranking_status": {},
    }

    aliases_path = os.path.join(after_dir, ALIASES_JSON_FILE)

    added_rows_by_csv = {}

    before_entry = load_json(os.path.join(before_dir, "entry_lists_cache.json")) or {}
    after_entry = load_json(os.path.join(after_dir, "entry_lists_cache.json")) or {}
    before_ranking_status = load_json(os.path.join(before_dir, "wta_ranking_refresh_status.json")) or {}
    ranking_status = load_json(os.path.join(after_dir, "wta_ranking_refresh_status.json")) or {}
    ranking_status_is_accepted = (
        isinstance(ranking_status, dict)
        and ranking_status.get("status") in {"confirmed_changed", "confirmed_frozen"}
    )
    ranking_was_already_accepted_this_week = (
        isinstance(ranking_status, dict)
        and
        isinstance(before_ranking_status, dict)
        and before_ranking_status.get("requested_date") == ranking_status.get("requested_date")
        and before_ranking_status.get("status") in {"confirmed_changed", "confirmed_frozen"}
    )
    if ranking_status_is_accepted and not ranking_was_already_accepted_this_week:
        report["wta_ranking_status"] = ranking_status
    before_tourney = load_json(os.path.join(before_dir, "tournament_snapshot.json")) or {}
    after_tourney = load_json(os.path.join(after_dir, "tournament_snapshot.json")) or {}

    flagless_player_countries = {}
    for entries in after_entry.values():
        if not isinstance(entries, list):
            continue
        for row in entries:
            if not isinstance(row, dict):
                continue
            player_name = repair_name_text((row.get("name") or row.get("player") or "")).strip()
            _record_flagless_country(flagless_player_countries, row.get("country") or row.get("Country") or "", player_name)

    for fname in RANKINGS_CSV_FILES:
        path = os.path.join(after_dir, fname)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    player_name = repair_name_text((row.get("player") or row.get("Player") or "")).strip()
                    _record_flagless_country(
                        flagless_player_countries,
                        row.get("country") or row.get("Country") or "",
                        player_name,
                    )
        except Exception:
            continue

    report["flagless_player_countries"] = [
        {"country": country, "players": sorted(entry["players"])}
        for country, entry in sorted(flagless_player_countries.items())
    ]

    for t_key in sorted(set(before_entry.keys()) | set(after_entry.keys())):
        old_entries = before_entry.get(t_key, [])
        new_entries = after_entry.get(t_key, [])

        # Only report withdrawals for tournaments present in both snapshots.
        # If a tournament was pruned (no longer in the active week window),
        # its disappearance is not a withdrawal.
        if t_key in before_entry and t_key in after_entry:
            old_arg = get_arg_players(old_entries)
            new_arg = get_arg_players(new_entries)

            withdrew = sorted(old_arg - new_arg)
            if withdrew:
                report["withdrawals"].append({
                    "tournament_key": t_key,
                    "tournament_name": get_tournament_label(t_key, before_tourney, after_tourney),
                    "players": withdrew,
                })

        old_has = len(old_entries) > 0
        new_has = len(new_entries) > 0
        if (not old_has) and new_has:
            report["new_entry_lists"].append({
                "tournament_key": t_key,
                "tournament_name": get_tournament_label(t_key, before_tourney, after_tourney),
                "entries_count": len(new_entries),
            })

        # Only alert on seed gaps when the acceptance list itself changed.
        # Seed_rank/seed can change later as rankings are refreshed, and we do
        # not want those follow-up updates to trigger a new email.
        if _acceptance_list_fingerprint(old_entries) == _acceptance_list_fingerprint(new_entries):
            continue
        if not new_has:
            continue

        main_entries = [
            row for row in new_entries
            if isinstance(row, dict) and str(row.get("type") or "").upper() == "MAIN"
        ]
        if not main_entries:
            continue

        main_count = len(main_entries)
        seed_count = 8 if main_count <= 24 else 16
        placeholder_names = {"(AVAILABLE SLOT)", "(SPECIAL EXEMPT)"}
        missing_seed_rank_players = []
        for row in sorted(main_entries, key=lambda item: (_safe_int((item or {}).get("pos_num"), 9999), normalize_exact_name((item or {}).get("name") or ""))):
            name = repair_name_text((row or {}).get("name") or "").strip()
            if not name:
                continue
            if name.upper() in placeholder_names:
                continue
            pos_num = _safe_int((row or {}).get("pos_num"), 9999)
            if pos_num > seed_count:
                continue
            if str((row or {}).get("seed_rank") or "").strip():
                continue
            missing_seed_rank_players.append({
                "name": name,
                "pos_num": pos_num,
                "entry_rank": str((row or {}).get("rank") or "").strip(),
            })

        if missing_seed_rank_players:
            report["itf_seed_missing_rankings"].append({
                "tournament_key": t_key,
                "tournament_name": get_tournament_label(t_key, before_tourney, after_tourney),
                "seed_count": seed_count,
                "players": missing_seed_rank_players,
            })

    before_files = set()
    after_files = set()
    if os.path.isdir(before_dir):
        before_files = {f for f in os.listdir(before_dir) if f.lower().endswith(".csv")}
    if os.path.isdir(after_dir):
        after_files = {f for f in os.listdir(after_dir) if f.lower().endswith(".csv")}

    for csv_name in sorted(after_files):
        after_path = os.path.join(after_dir, csv_name)
        before_path = os.path.join(before_dir, csv_name)

        after_headers, after_rows = load_csv_rows(after_path)
        if not after_rows:
            continue

        has_match_shape = (
            "matchId" in after_headers or
            ("winnerName" in after_headers and "loserName" in after_headers)
        )
        if not has_match_shape:
            continue

        if csv_name not in before_files:
            continue
        before_headers, before_rows = load_csv_rows(before_path)

        before_map = {}
        for row in before_rows:
            key = build_row_key(row, before_headers, MATCH_SOURCES.get(csv_name))
            if key:
                before_map[key] = row

        added = []
        for row in after_rows:
            key = build_row_key(row, after_headers, MATCH_SOURCES.get(csv_name))
            if not key:
                continue
            if key not in before_map:
                match_line = format_match_line(row)
                added.append({"line": match_line, "row": row})

        if added:
            added_rows_by_csv[csv_name] = added

    if added_rows_by_csv:
        new_match_rows = [
            item.get("row") or {}
            for rows in added_rows_by_csv.values()
            for item in rows
        ]
        player_table_path = Path(aliases_path)
        sync_itf_players(player_table_path, new_match_rows)
        sync_wta_match_players(player_table_path, new_match_rows)
        player_index = PlayerIdentityIndex(load_player_rows(player_table_path))

        for csv_name, rows in added_rows_by_csv.items():
            processed = []
            for item in rows:
                row = item.get("row") or {}
                week = monday_from_date_str(row.get("date") or row.get("DATE") or "")
                issues = []
                for _, player_id, player_name in iter_match_sides(row):
                    if not is_itf_id(player_id):
                        continue
                    record = player_index.by_itf_id.get(player_id)
                    if record is None:
                        issues.append(
                            f"{player_name} (itf_id {player_id}) is missing from the canonical player table."
                        )
                        continue
                    if not record.wta_id or not record.wta_name:
                        suffix = f" for week {week}" if week else ""
                        issues.append(
                            f"{record.itf_name or player_name} (itf_id {player_id}) "
                            f"has no verified WTA crosswalk{suffix}."
                        )
                processed.append({"line": item.get("line", ""), "issues": issues})

            report["added_matches"][csv_name] = {
                "count": len(processed),
                "items": processed[:MAX_MATCH_LINES_PER_FILE],
                "truncated": len(processed) > MAX_MATCH_LINES_PER_FILE,
            }

    before_calendar = load_json(os.path.join(before_dir, "calendar_snapshot.json")) or []
    after_calendar = load_json(os.path.join(after_dir, "calendar_snapshot.json")) or []

    if before_calendar and after_calendar:
        def _calendar_compare_key(row):
            week_label = row.get("week_label", "")
            column = row.get("column", "")
            continent = row.get("continent", "")
            calendar_key = normalize_exact_name(row.get("calendarKey", ""))
            if calendar_key:
                return (week_label, column, continent, "calendarKey", calendar_key)

            tournament_id = normalize_exact_name(row.get("tournamentId", ""))
            if tournament_id:
                source = normalize_exact_name(row.get("source", ""))
                stable_id = f"{source}:{tournament_id}" if source else tournament_id
                return (week_label, column, continent, "tournamentId", stable_id)

            tournament_key = normalize_exact_name(row.get("tournamentKey", ""))
            if tournament_key:
                source = normalize_exact_name(row.get("source", ""))
                stable_key = f"{source}:{tournament_key}" if source else tournament_key
                return (week_label, column, continent, "tournamentKey", stable_key)

            # Legacy fallback for older snapshots that do not carry source IDs.
            return (
                week_label,
                column,
                continent,
                "legacy",
                normalize_calendar_name(row.get("name", ""), column),
                row.get("level", ""),
            )

        before_keys = {
            _calendar_compare_key(row)
            for row in before_calendar if isinstance(row, dict)
        }
        added = []
        for row in after_calendar:
            if not isinstance(row, dict):
                continue
            key = _calendar_compare_key(row)
            if key not in before_keys:
                added.append(row)
        report["added_calendar_tournaments"] = added

    # Detect new tournament draws
    before_draws = load_json(os.path.join(before_dir, "draws_snapshot.json")) or {}
    after_draws = load_json(os.path.join(after_dir, "draws_snapshot.json")) or {}

    new_draws = []
    for t_key, info in after_draws.items():
        t_name = info.get("name", t_key)
        after_types = set(info.get("types", []))
        before_types = set()
        if t_key in before_draws:
            before_types = set(before_draws[t_key].get("types", []))
        new_types = after_types - before_types
        if new_types:
            type_labels = {"MDS": "Main Draw", "QS": "Qualifying"}
            labels = [type_labels.get(t, t) for t in sorted(new_types)]
            new_draws.append({"name": t_name, "types": labels})
    report["new_draws"] = new_draws

    now_utc = utc_now()
    today_str = now_utc.strftime("%Y-%m-%d")

    # Draw fetch failures (tournaments where ITF returned no data and no cache exists).
    # Exclude future-dated tournaments — an empty draw before the event starts is
    # expected (draws aren't published yet), not a real failure.
    failed_draw_fetches = load_json(os.path.join(after_dir, "draw_fetch_errors.json")) or []
    report["failed_draw_fetches"] = [
        item for item in failed_draw_fetches
        if isinstance(item, dict) and (item.get("startDate") or "9999") <= today_str
    ]

    blocked_itf_responses = load_json(os.path.join(after_dir, "itf_blocked_responses.json")) or []
    report["blocked_itf_responses"] = [
        item for item in blocked_itf_responses if isinstance(item, dict)
    ]

    # Stale draws: active tournaments whose draw hasn't been refreshed in >24h
    draws_store = load_json(os.path.join(after_dir, "draws_store_cache.json")) or {}
    stale_draws = []
    for t_key, entry in draws_store.items():
        if not isinstance(entry, dict):
            continue
        end_date = (entry.get("endDate") or "")[:10]
        if end_date and end_date < today_str:
            continue  # tournament is over
        fetched_at_str = get_cache_timestamp(os.path.join(after_dir, "draws_store_cache.json"), t_key, entry)
        if not fetched_at_str:
            continue  # no timestamp yet (pre-existing cache entries)
        try:
            fetched_at = parse_utc_timestamp(fetched_at_str)
        except Exception:
            continue
        age_hours = (now_utc - fetched_at).total_seconds() / 3600
        if age_hours > 24:
            stale_draws.append({
                "name": entry.get("name", t_key),
                "key": t_key,
                "fetched_at": fetched_at_str,
                "age_hours": round(age_hours, 1),
            })
    report["stale_draws"] = stale_draws

    # Bad draw scores: concluded matches whose score violates tennis rules
    bad_draw_scores = []
    for t_key, entry in draws_store.items():
        if not isinstance(entry, dict):
            continue
        t_name = entry.get("name", t_key)
        for draw_type, draw in (entry.get("draws") or {}).items():
            if not isinstance(draw, dict):
                continue
            draw_label = {"MDS": "Main Draw", "QS": "Qualifying"}.get(draw_type, draw_type)
            for m in (draw.get("matches") or []):
                if not isinstance(m, dict):
                    continue
                winner = m.get("winner_name") or ""
                score = m.get("score") or ""
                if winner and score and _is_bad_draw_score(score):
                    bad_draw_scores.append({
                        "tournament_name": t_name,
                        "draw_label": draw_label,
                        "round": m.get("round", "?"),
                        "match_num": m.get("match_num", "?"),
                        "winner_name": winner,
                        "score": score,
                    })
    report["bad_draw_scores"] = bad_draw_scores

    return report


def _parse_compact_set(token):
    """Parse a compact set token (e.g. '64', '76(4)') into (winner_games, loser_games).
    Returns None if not a recognisable set token."""
    mc = re.match(r'^\[?(\d+)[-:/](\d+)\]?(?:\(\d+\))?$', token)
    if mc:
        return int(mc.group(1)), int(mc.group(2))
    mc2 = re.match(r'^(\d+)(?:\(\d+\))?$', token)
    if mc2:
        d = mc2.group(1)
        if len(d) == 2:
            return int(d[0]), int(d[1])
        if len(d) == 3:
            return int(d[:2]), int(d[2])
        if len(d) == 4:
            return int(d[:2]), int(d[2:])
        mid = len(d) // 2
        return int(d[:mid]), int(d[mid:])
    return None


def _is_bad_draw_score(score_str):
    """Return True if a concluded match score violates basic tennis rules.

    A score is bad when every parsed set has both players below 6 games
    AND the match is not a walkover or retirement — e.g. '22 22' or '44'.
    Retirements ('65 RET') and walkovers ('W/O') are always valid.
    """
    if not score_str or not score_str.strip():
        return False
    parts = score_str.strip().upper().split()
    non_score_tokens = {'RET', 'DEF', 'W/O', 'WO', 'W.O.'}
    if any(p in non_score_tokens for p in parts):
        return False  # retirement or walkover — valid regardless of game counts
    for p in parts:
        parsed = _parse_compact_set(p)
        if parsed and max(parsed) >= 6:
            return False  # at least one complete set found — valid
    # Every set token has max games < 6 (or nothing was parseable)
    return True


def _format_itf_empty_draw_status(draw_types):
    types = [t for t in (draw_types or []) if t in {"MDS", "QS"}]
    short = []
    if "MDS" in types:
        short.append("MD")
    if "QS" in types:
        short.append("QS")
    if short == ["MD"]:
        return "MD is empty"
    if short == ["QS"]:
        return "QS is empty"
    if set(short) == {"MD", "QS"}:
        return "MD and QS are empty"
    if short:
        return f"{' / '.join(short)} is empty"
    return "MD and QS are empty"


def _append_execution_summary(lines, run_status, *, include_technical=True):
    if not run_status:
        return
    analysis = analyze_execution(run_status)
    lines.append("## Update result")
    lines.append(f"- **Result:** {analysis['outcome']}")
    lines.append(f"- **Website:** {analysis['website']}")
    lines.append(f"- **Publishing:** {analysis['publishing']}")
    lines.append(f"- **What happened:** {analysis['summary']}")
    lines.append("")

    if analysis["issues"]:
        lines.append("## What went wrong")
        for issue in analysis["issues"]:
            count = f" This happened {issue['count']} times." if issue["count"] > 1 else ""
            lines.append(f"- {issue['reason']}{count} {issue['impact']}")
        lines.append("")

    lines.append("## What happens next")
    lines.append(f"- **Next step:** {analysis['next_step']}")
    lines.append("")

    if not include_technical:
        return
    technical = [detail for issue in analysis["issues"] for detail in issue["technical"]]
    if run_status:
        lines.append("## Technical details")
        lines.append(f"- Internal status: {analysis['raw_status']}")
        if run_status.get("run_id"):
            lines.append(f"- Run reference: {run_status['run_id']}")
        for detail in technical:
            count = f" ({detail['count']} occurrences)" if detail["count"] > 1 else ""
            lines.append(
                f"- {detail['component']} / {detail['operation']}: {detail['message']}{count}"
            )
        lines.append("")


def render_email_markdown(report):
    now_utc = utc_now().strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = []
    lines.append(f"# Website Update Alerts ({now_utc})")
    lines.append("")

    run_status = report.get("run_status") or {}
    status_name = effective_run_status(run_status)
    draw_update_warning = bool(
        report.get("failed_draw_fetches")
        or report.get("blocked_itf_responses")
        or any(
            isinstance(issue, dict)
            and issue.get("component") == "itf-loader"
            and issue.get("operation") == "fetch drawsheet"
            for issue in (run_status.get("issues") or [])
        )
    )

    has_any = any(
        [
            bool(report.get("withdrawals")),
            bool(report.get("new_entry_lists")),
            bool(report.get("itf_seed_missing_rankings")),
            bool(report.get("added_matches")),
            bool(report.get("new_draws")),
            bool(report.get("added_calendar_tournaments")),
            bool(report.get("failed_draw_fetches")),
            bool(report.get("blocked_itf_responses")),
            bool(report.get("bad_draw_scores")),
            bool(report.get("flagless_player_countries")),
            bool(report.get("wta_ranking_status")),
            status_name in {"failed", "partial", "degraded"},
        ]
    )
    if not has_any:
        lines.append("None detected.")
        return "\n".join(lines).rstrip() + "\n"

    _append_execution_summary(lines, run_status)

    if draw_update_warning:
        lines.append("## Tournament draws that may be out of date")
        if status_name in {"success", "degraded"}:
            lines.append(
                "- The rest of the update passed its checks, but one or more ITF draws could not be refreshed. "
                "The affected draws may be older or missing."
            )
        else:
            lines.append(
                "- One or more ITF draws could not be refreshed. The affected draws may be older or missing."
            )
        lines.append("")

    if report.get("wta_ranking_status"):
        status = report["wta_ranking_status"]
        lines.append("## WTA Ranking Status")
        lines.append(
            f"- {status.get('requested_date', 'current week')}: "
            f"{status.get('message', status.get('status', 'unknown'))}"
        )
        lines.append("")

    if report.get("withdrawals"):
        lines.append("## 1) Argentine Withdrawals (WTA/ITF)")
        for item in report["withdrawals"]:
            players = ", ".join(item["players"])
            lines.append(
                f"- {format_tournament_key_label(item.get('tournament_key'), item.get('tournament_name'))}: {players}"
            )
        lines.append("")

    if report.get("new_entry_lists"):
        lines.append("## 2) Tournaments that now have an Entry List")
        for item in report["new_entry_lists"]:
            lines.append(f"- {item['tournament_name']} ({item['entries_count']} entries)")
        lines.append("")

    if report.get("itf_seed_missing_rankings"):
        lines.append("## 3) ITF Seed Alerts (missing WTA rankings)")
        for item in report["itf_seed_missing_rankings"]:
            players = item.get("players") or []
            players_text = "; ".join(
                (
                    f"{repair_name_text((player or {}).get('name') or '').strip()}"
                    f" (main pos {(_safe_int((player or {}).get('pos_num'), 9999))}, "
                    f"entry rank {((player or {}).get('entry_rank') or 'unavailable')})"
                )
                for player in players
            )
            seed_count = item.get("seed_count", "")
            label = repair_name_text(item.get("tournament_name") or "").strip() or (item.get("tournament_key") or "")
            seed_phrase = "this seed slot" if len(players) == 1 else "these seed slots"
            lines.append(
                f"- {label} (top {seed_count} seeds): {players_text}. "
                f"WTA ranking not found for {seed_phrase}."
            )
        lines.append("")

    if report.get("new_draws"):
        lines.append("## 4) New Tournament Draws Available")
        for item in report["new_draws"]:
            types_str = ", ".join(item["types"])
            lines.append(f"- {item['name']}: {types_str}")
        lines.append("")

    if report.get("added_calendar_tournaments"):
        lines.append("## 5) Tournaments Added to Calendar")
        for item in report["added_calendar_tournaments"]:
            lines.append(
                f"- {item.get('week_label', '')} | {item.get('name', '')} | "
                f"{item.get('level', '')} | {item.get('column', '')} | {item.get('continent', '')}"
            )
        lines.append("")

    if report.get("flagless_player_countries"):
        lines.append("## 6) Player Countries Without a Flag")
        for item in report["flagless_player_countries"]:
            players = "; ".join(item.get("players") or [])
            lines.append(f"- {item.get('country', '')}: {players}")
        lines.append("")

    for csv_name, payload in (report.get("added_matches") or {}).items():
        lines.append(f"## 7) Matches Added ({csv_name})")
        for item in payload.get("items") or []:
            match_line = item.get("line") if isinstance(item, dict) else str(item)
            lines.append(f"- {match_line}")
            issues = item.get("issues") if isinstance(item, dict) else []
            for msg in (issues or []):
                lines.append(f"  {msg}")
        if payload.get("truncated"):
            lines.append(f"- ... and {payload['count'] - len(payload.get('items') or [])} more")
        lines.append("")

    if report.get("failed_draw_fetches"):
        lines.append("## 8) Draw Fetch Failures")
        for item in report["failed_draw_fetches"]:
            name = item.get("name") or item.get("key", "")
            key = item.get("key", "")
            reason = (item.get("reason") or "").strip()
            if not reason:
                reason = _format_itf_empty_draw_status(item.get("drawTypes"))
            lines.append(f"- Could not refresh the draw for {format_tournament_key_label(key, name)}: {reason}")
        lines.append("")

    if report.get("blocked_itf_responses"):
        lines.append("## 9) ITF Blocked Responses")
        grouped_blocks = {}
        for item in report["blocked_itf_responses"]:
            endpoint = item.get("endpoint") or "itf"
            tournament_name = item.get("tournament_name") or ""
            tournament_id = item.get("tournament_id") or ""
            key = (endpoint, tournament_id, tournament_name)
            bucket = grouped_blocks.setdefault(key, {
                "codes": [],
                "weeks": [],
            })
            code = (item.get("code") or "").strip()
            week_number = (item.get("week_number") or "").strip()
            if code and code not in bucket["codes"]:
                bucket["codes"].append(code)
            if week_number and week_number not in bucket["weeks"]:
                bucket["weeks"].append(week_number)

        for (endpoint, tournament_id, tournament_name), bucket in grouped_blocks.items():
            context_parts = [p for p in [
                tournament_name,
                f"id={tournament_id}" if tournament_id else "",
            ] if p]
            context = " | ".join(context_parts) if context_parts else "general ITF request"
            lines.append(f"- The ITF website blocked the request to {endpoint}: {context}")
            if bucket["codes"]:
                lines.append(f"  - codes: {', '.join(sorted(bucket['codes']))}")
            if bucket["weeks"]:
                lines.append(f"  - weeks: {', '.join(sorted(bucket['weeks'], key=lambda v: int(v) if str(v).isdigit() else str(v)))}")
        lines.append("")

    if report.get("bad_draw_scores"):
        lines.append("## 10) Draw Matches with Invalid Scores")
        for item in report["bad_draw_scores"]:
            lines.append(
                f"- {item['tournament_name']} ({item['draw_label']}) "
                f"R{item['round']}M{item['match_num']}: "
                f"winner={item['winner_name']} score=\"{item['score']}\""
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_markdown(report):
    now_utc = utc_now().strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = []
    lines.append(f"# Website Update Report ({now_utc})")
    lines.append("")

    run_status = report.get("run_status") or {}
    _append_execution_summary(lines, run_status, include_technical=False)

    if report["withdrawals"]:
        lines.append("## 1) Argentine Withdrawals (WTA/ITF)")
        for item in report["withdrawals"]:
            players = ", ".join(item["players"])
            lines.append(f"- {item['tournament_name']}: {players}")
        lines.append("")

    if report["new_entry_lists"]:
        lines.append("## 2) Tournaments that now have an Entry List")
        for item in report["new_entry_lists"]:
            lines.append(f"- {item['tournament_name']} ({item['entries_count']} entries)")
        lines.append("")

    if report["added_matches"]:
        lines.append("## 3) Matches Added to CSV Files")
        for csv_name, payload in report["added_matches"].items():
            entries = payload["items"]
            lines.append(f"- {csv_name}: {payload['count']} new match(es)")
            for line in entries:
                if isinstance(line, dict):
                    lines.append(f"  - {line.get('line', '')}")
                else:
                    lines.append(f"  - {line}")
            if payload["truncated"]:
                lines.append(f"  - ... and {payload['count'] - len(entries)} more")
        lines.append("")

    if report.get("new_draws"):
        lines.append("## 4) New Tournament Draws Available")
        for item in report["new_draws"]:
            types_str = ", ".join(item["types"])
            lines.append(f"- {item['name']}: {types_str}")
        lines.append("")

    if report["added_calendar_tournaments"]:
        lines.append("## 5) Tournaments Added to Calendar")
        for item in report["added_calendar_tournaments"]:
            lines.append(
                f"- {item.get('week_label', '')} | {item.get('name', '')} | "
                f"{item.get('level', '')} | {item.get('column', '')} | {item.get('continent', '')}"
            )
        lines.append("")

    if report.get("flagless_player_countries"):
        lines.append("## 6) Player Countries Without a Flag")
        for item in report["flagless_player_countries"]:
            players = "; ".join(item.get("players") or [])
            lines.append(f"- {item.get('country', '')}: {players}")
        lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--before", required=True, help="Directory with pre-run snapshot files")
    parser.add_argument("--after", required=True, help="Directory with post-run data files")
    parser.add_argument("--output", required=True, help="Output report markdown file")
    parser.add_argument("--email-output", help="Optional output markdown file for email alerts")
    parser.add_argument("--run-status", help="Optional transactional run-state JSON file")
    args = parser.parse_args()

    report = compute_report(args.before, args.after)
    report["run_status"] = load_json(args.run_status) if args.run_status else None
    markdown = render_markdown(report)
    email_markdown = render_email_markdown(report) if args.email_output else None

    out_dir = os.path.dirname(args.output)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    write_text_if_changed(args.output, markdown, encoding="utf-8")

    if args.email_output and email_markdown is not None:
        email_dir = os.path.dirname(args.email_output)
        if email_dir:
            os.makedirs(email_dir, exist_ok=True)
        write_text_if_changed(args.email_output, email_markdown, encoding="utf-8")

    print(markdown)


if __name__ == "__main__":
    main()
