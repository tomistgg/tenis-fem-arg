# NOTE: This script runs after main.py in CI (see hourly-update.yml).
# It imports from config and utils — keep those modules free of heavy
# runtime dependencies (Selenium, pandas, etc.) so this script stays lightweight.
import argparse
import csv
import json
import os
import re
from datetime import datetime, timezone, timedelta

from config import repair_name_text
from utils import fix_encoding, save_json_array_one_line_per_item

MAX_MATCH_LINES_PER_FILE = 50
RANKINGS_CSV_FILES = ["wta_rankings_83_99.csv", "wta_rankings_00_09.csv", "wta_rankings_10_19.csv", "wta_rankings_20_29.csv"]
ALIASES_JSON_FILE = "player_aliases_wta_itf.json"


def repair_nested_strings(value):
    if isinstance(value, dict):
        return {k: repair_nested_strings(v) for k, v in value.items()}
    if isinstance(value, list):
        return [repair_nested_strings(v) for v in value]
    if isinstance(value, str):
        return repair_name_text(value)
    return value



def load_json(path):
    if not os.path.exists(path):
        return None
    try:
        # Use utf-8-sig to tolerate BOM-prefixed JSON files (common on Windows).
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return None


def normalize_name(value):
    return repair_name_text(value).strip().upper()


def normalize_exact_name(value):
    return " ".join(repair_name_text(value).strip().upper().split())


def normalize_country(value):
    return (value or "").strip().upper()


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


def load_rankings_name_set(dir_path):
    names = set()
    for fname in RANKINGS_CSV_FILES:
        path = os.path.join(dir_path, fname)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    raw = row.get("player") or row.get("Player") or row.get("PLAYER") or ""
                    for v in name_variants(raw):
                        if v:
                            names.add(v)
        except Exception:
            continue
    return names


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


def is_itf_id(value):
    s = (value or "").strip()
    return s.isdigit() and (len(s) >= 9 or s.startswith("800"))


def is_wta_id(value):
    s = (value or "").strip()
    return s.isdigit() and not is_itf_id(s)


def load_aliases(path):
    items = load_json(path) or []
    if not isinstance(items, list):
        return []
    return [repair_nested_strings(it) for it in items if isinstance(it, dict)]


def build_alias_indexes(items):
    by_wta = {}
    by_itf = {}
    by_name = {}

    def _index_name(ent, name):
        for k in name_variants(name or ""):
            by_name.setdefault(k, [])
            if ent not in by_name[k]:
                by_name[k].append(ent)

    for it in items:
        wid = (it.get("wta_id") or "").strip()
        iid = (it.get("itf_id") or "").strip()
        if wid and wid not in by_wta:
            by_wta[wid] = it
        if iid and iid not in by_itf:
            by_itf[iid] = it
        _index_name(it, it.get("wta_name") or "")
        _index_name(it, it.get("display_name") or "")
        _index_name(it, it.get("itf_name") or "")

    return by_wta, by_itf, by_name


def load_rankings_by_week(dir_path, weeks):
    """Return (by_week, variant_name_to_id, exact_name_to_id)."""
    weeks = {w for w in (weeks or set()) if w}
    if not weeks:
        return {}, {}, {}

    needed_files = set()
    for w in weeks:
        try:
            year = int(w[:4])
        except Exception:
            continue
        if year <= 2000:
            needed_files.add("wta_rankings_83_99.csv")
            if year == 2000:
                needed_files.add("wta_rankings_00_09.csv")
        elif 2001 <= year <= 2009:
            needed_files.add("wta_rankings_00_09.csv")
        elif 2010 <= year <= 2019:
            needed_files.add("wta_rankings_10_19.csv")
        else:
            needed_files.add("wta_rankings_20_29.csv")

    by_week = {w: {} for w in weeks}
    name_to_id = {w: {} for w in weeks}
    exact_name_to_id = {w: {} for w in weeks}

    for fname in RANKINGS_CSV_FILES:
        if fname not in needed_files:
            continue
        path = os.path.join(dir_path, fname)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    week = (row.get("week_date") or row.get("week") or "").strip()
                    if week not in weeks:
                        continue
                    pid = (row.get("id") or row.get("player_id") or row.get("playerId") or "").strip()
                    rank = (row.get("rank") or row.get("Rank") or "").strip()
                    player = (row.get("player") or row.get("Player") or "").strip()
                    if not pid:
                        continue
                    by_week[week][pid] = {"rank": rank, "player": player}
                    for v in name_variants(player):
                        name_to_id[week].setdefault(v, pid)
                    exact = normalize_exact_name(player)
                    if exact and exact not in exact_name_to_id[week]:
                        exact_name_to_id[week][exact] = pid
        except Exception:
            continue

    return by_week, name_to_id, exact_name_to_id


def get_tournament_label(t_key, before_snapshot, after_snapshot):
    if isinstance(after_snapshot, dict) and t_key in after_snapshot:
        return after_snapshot[t_key].get("name") or t_key
    if isinstance(before_snapshot, dict) and t_key in before_snapshot:
        return before_snapshot[t_key].get("name") or t_key
    return t_key


def get_arg_players(entries):
    players = set()
    for row in entries or []:
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


def get_match_players(row):
    winner = (row.get("winnerName") or row.get("_winnerName") or row.get("WINNERNAME") or row.get("WINNER_NAME") or "").strip()
    loser = (row.get("loserName") or row.get("_loserName") or row.get("LOSERNAME") or row.get("LOSER_NAME") or "").strip()
    out = []
    if winner:
        out.append(winner)
    if loser:
        out.append(loser)
    # Avoid duplicates while keeping order.
    seen = set()
    uniq = []
    for n in out:
        k = normalize_rank_key(n)
        if not k or k in seen:
            continue
        seen.add(k)
        uniq.append(n)
    return uniq


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


def build_row_key(row, headers):
    if "matchId" in headers:
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
        "added_matches": {},
        "added_calendar_tournaments": [],
    }

    aliases_path = os.path.join(after_dir, ALIASES_JSON_FILE)
    aliases_items = load_aliases(aliases_path)
    by_wta, by_itf, by_alias_name = build_alias_indexes(aliases_items)
    aliases_changed = False

    added_rows_by_csv = {}

    before_entry = load_json(os.path.join(before_dir, "entry_lists_cache.json")) or {}
    after_entry = load_json(os.path.join(after_dir, "entry_lists_cache.json")) or {}
    before_tourney = load_json(os.path.join(before_dir, "tournament_snapshot.json")) or {}
    after_tourney = load_json(os.path.join(after_dir, "tournament_snapshot.json")) or {}

    if before_entry and after_entry:
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
            key = build_row_key(row, before_headers)
            if key:
                before_map[key] = row

        added = []
        for row in after_rows:
            key = build_row_key(row, after_headers)
            if not key:
                continue
            if key not in before_map:
                match_line = format_match_line(row)
                added.append({"line": match_line, "row": row})

        if added:
            added_rows_by_csv[csv_name] = added

    if added_rows_by_csv:
        needed_weeks = set()
        for rows in added_rows_by_csv.values():
            for item in rows:
                row = item.get("row") or {}
                week = monday_from_date_str(row.get("date") or row.get("DATE") or "")
                if week:
                    needed_weeks.add(week)

        rankings_by_week, _, ranking_exact_name_to_id = load_rankings_by_week(after_dir, needed_weeks)

        def index_alias_entry(ent):
            wid = (ent.get("wta_id") or "").strip()
            iid = (ent.get("itf_id") or "").strip()
            if wid:
                by_wta[wid] = ent
            if iid:
                by_itf[iid] = ent
            for field in ("wta_name", "display_name", "itf_name"):
                for k in name_variants(ent.get(field) or ""):
                    by_alias_name.setdefault(k, [])
                    if ent not in by_alias_name[k]:
                        by_alias_name[k].append(ent)

        def maybe_fill_wta_names(ent, wta_id, week):
            nonlocal aliases_changed
            if not wta_id or not week:
                return
            info = (rankings_by_week.get(week) or {}).get(wta_id) or {}
            player_name = (info.get("player") or "").strip()
            if not player_name:
                return
            if not (ent.get("wta_name") or "").strip():
                ent["wta_name"] = player_name
                aliases_changed = True
            if not (ent.get("display_name") or "").strip():
                ent["display_name"] = player_name
                aliases_changed = True
            if aliases_changed:
                index_alias_entry(ent)

        def ensure_player_and_collect_issues(pid, name, week):
            nonlocal aliases_changed
            pid = (pid or "").strip()
            name = (name or "").strip()
            week = (week or "").strip()

            ent = None
            alias_missing = False
            alias_incomplete = False
            if is_wta_id(pid):
                ent = by_wta.get(pid)
                if ent is None:
                    info = (rankings_by_week.get(week) or {}).get(pid) if week else None
                    wta_name = ((info or {}).get("player") or "").strip() or name
                    ent = {
                        "display_name": wta_name,
                        "wta_id": pid,
                        "wta_name": wta_name,
                        "itf_id": "",
                        "itf_name": "",
                        "bjkc_name": "",
                    }
                    aliases_items.append(ent)
                    aliases_changed = True
                    index_alias_entry(ent)
                maybe_fill_wta_names(ent, pid, week)
                # Do not emit email issues for WTA-id players.
                return []
            elif is_itf_id(pid):
                ent = by_itf.get(pid)
                if ent is None:
                    alias_missing = True
                    ent = {
                        "display_name": "",
                        "wta_id": "",
                        "wta_name": "",
                        "itf_id": pid,
                        "itf_name": name,
                        "bjkc_name": "",
                    }
                    aliases_items.append(ent)
                    aliases_changed = True
                    index_alias_entry(ent)
                elif name and not (ent.get("itf_name") or "").strip():
                    ent["itf_name"] = name
                    aliases_changed = True
                    index_alias_entry(ent)

                wta_id_existing = (ent.get("wta_id") or "").strip() if isinstance(ent, dict) else ""
                if wta_id_existing and week:
                    maybe_fill_wta_names(ent, wta_id_existing, week)

                wta_id_existing = (ent.get("wta_id") or "").strip() if isinstance(ent, dict) else ""
                wta_name_existing = (ent.get("wta_name") or "").strip() if isinstance(ent, dict) else ""
                if not wta_id_existing or not wta_name_existing:
                    alias_incomplete = True

                # For ITF entries missing WTA mapping, try exact name lookup in rankings CSV for this week.
                if ent is not None and week and name and alias_incomplete:
                    hit = (ranking_exact_name_to_id.get(week) or {}).get(normalize_exact_name(name))
                    if hit:
                        ent["wta_id"] = hit
                        info = (rankings_by_week.get(week) or {}).get(hit) or {}
                        player_name = (info.get("player") or "").strip()
                        if player_name:
                            ent["wta_name"] = player_name
                            if not (ent.get("display_name") or "").strip():
                                ent["display_name"] = player_name
                        aliases_changed = True
                        index_alias_entry(ent)
                        maybe_fill_wta_names(ent, hit, week)
            else:
                # Non-numeric ids are rare; try to match by name to avoid duplicates.
                for k in name_variants(name):
                    hits = by_alias_name.get(k) or []
                    if hits:
                        ent = hits[0]
                        break
                if ent is None and name:
                    ent = {
                        "display_name": "",
                        "wta_id": "",
                        "wta_name": "",
                        "itf_id": "",
                        "itf_name": name,
                        "bjkc_name": "",
                    }
                    aliases_items.append(ent)
                    aliases_changed = True
                    index_alias_entry(ent)
            issues = []
            # Only emit issues for unresolved ITF alias mapping gaps.
            if is_itf_id(pid):
                itf_id = (ent.get("itf_id") or "").strip() if isinstance(ent, dict) else pid
                itf_name = (ent.get("itf_name") or "").strip() if isinstance(ent, dict) else name
                wta_id = (ent.get("wta_id") or "").strip() if isinstance(ent, dict) else ""
                wta_name = (ent.get("wta_name") or "").strip() if isinstance(ent, dict) else ""
                if (alias_missing or alias_incomplete) and (not wta_id or not wta_name):
                    if week:
                        issues.append(
                            f"{itf_name or name} (itf_id {itf_id}) unresolved in aliases after exact rankings lookup for week {week}."
                        )
                    else:
                        issues.append(
                            f"{itf_name or name} (itf_id {itf_id}) unresolved in aliases after exact rankings lookup."
                        )
            return issues

        for csv_name, rows in added_rows_by_csv.items():
            processed = []
            for item in rows:
                row = item.get("row") or {}
                week = monday_from_date_str(row.get("date") or row.get("DATE") or "")
                issues = []
                for _, pid, name in iter_match_sides(row):
                    issues.extend(ensure_player_and_collect_issues(pid, name, week))
                processed.append({"line": item.get("line", ""), "issues": issues})

            report["added_matches"][csv_name] = {
                "count": len(processed),
                "items": processed[:MAX_MATCH_LINES_PER_FILE],
                "truncated": len(processed) > MAX_MATCH_LINES_PER_FILE,
            }

        if aliases_changed:
            def _sort_key(ent):
                return normalize_rank_key(
                    (ent.get("display_name") or ent.get("wta_name") or ent.get("itf_name") or "")
                )

            save_json_array_one_line_per_item(aliases_path, sorted(aliases_items, key=_sort_key), transform=repair_nested_strings)

    before_calendar = load_json(os.path.join(before_dir, "calendar_snapshot.json")) or []
    after_calendar = load_json(os.path.join(after_dir, "calendar_snapshot.json")) or []

    if before_calendar and after_calendar:
        before_keys = {
            (
                row.get("week_label", ""),
                row.get("name", ""),
                row.get("level", ""),
                row.get("column", ""),
                row.get("continent", ""),
            )
            for row in before_calendar if isinstance(row, dict)
        }
        added = []
        for row in after_calendar:
            if not isinstance(row, dict):
                continue
            key = (
                row.get("week_label", ""),
                row.get("name", ""),
                row.get("level", ""),
                row.get("column", ""),
                row.get("continent", ""),
            )
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

    now_utc = datetime.now(timezone.utc)
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
        fetched_at_str = entry.get("fetchedAt")
        if not fetched_at_str:
            continue  # no timestamp yet (pre-existing cache entries)
        try:
            fetched_at = datetime.strptime(fetched_at_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
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


def render_email_markdown(report):
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = []
    lines.append(f"# Website Update Alerts ({now_utc})")
    lines.append("")

    has_any = any(
        [
            bool(report.get("withdrawals")),
            bool(report.get("new_entry_lists")),
            bool(report.get("added_matches")),
            bool(report.get("new_draws")),
            bool(report.get("added_calendar_tournaments")),
            bool(report.get("failed_draw_fetches")),
            bool(report.get("blocked_itf_responses")),
            bool(report.get("bad_draw_scores")),
        ]
    )
    if not has_any:
        lines.append("None detected.")
        return "\n".join(lines).rstrip() + "\n"

    if report.get("withdrawals"):
        lines.append("## 1) Argentine Withdrawals (WTA/ITF)")
        for item in report["withdrawals"]:
            players = ", ".join(item["players"])
            lines.append(f"- {item['tournament_name']}: {players}")
        lines.append("")

    if report.get("new_entry_lists"):
        lines.append("## 2) Tournaments that now have an Entry List")
        for item in report["new_entry_lists"]:
            lines.append(f"- {item['tournament_name']} ({item['entries_count']} entries)")
        lines.append("")

    if report.get("new_draws"):
        lines.append("## 3) New Tournament Draws Available")
        for item in report["new_draws"]:
            types_str = ", ".join(item["types"])
            lines.append(f"- {item['name']}: {types_str}")
        lines.append("")

    if report.get("added_calendar_tournaments"):
        lines.append("## 4) Tournaments Added to Calendar")
        for item in report["added_calendar_tournaments"]:
            lines.append(
                f"- {item.get('week_label', '')} | {item.get('name', '')} | "
                f"{item.get('level', '')} | {item.get('column', '')} | {item.get('continent', '')}"
            )
        lines.append("")

    for csv_name, payload in report["added_matches"].items():
        lines.append(f"## 5) Matches Added ({csv_name})")
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
        lines.append("## 6) Draw Fetch Failures")
        for item in report["failed_draw_fetches"]:
            name = item.get("name") or item.get("key", "")
            key = item.get("key", "")
            lines.append(f"- Could not load Drawsheet for {name} ({key})")
        lines.append("")

    if report.get("blocked_itf_responses"):
        lines.append("## 7) ITF Blocked Responses")
        for item in report["blocked_itf_responses"]:
            endpoint = item.get("endpoint") or "itf"
            tournament_name = item.get("tournament_name") or ""
            tournament_id = item.get("tournament_id") or ""
            code = item.get("code") or ""
            week_number = item.get("week_number") or ""
            context_parts = [p for p in [
                tournament_name,
                f"id={tournament_id}" if tournament_id else "",
                f"code={code}" if code else "",
                f"week={week_number}" if week_number not in ("", None) else "",
            ] if p]
            context = " | ".join(context_parts) if context_parts else "general ITF request"
            lines.append(f"- Block page detected on {endpoint}: {context}")
        lines.append("")
    if report.get("bad_draw_scores"):
        lines.append("## 8) Draw Matches with Invalid Scores")
        for item in report["bad_draw_scores"]:
            lines.append(
                f"- {item['tournament_name']} ({item['draw_label']}) "
                f"R{item['round']}M{item['match_num']}: "
                f"winner={item['winner_name']} score=\"{item['score']}\""
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_markdown(report):
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = []
    lines.append(f"# Website Update Report ({now_utc})")
    lines.append("")

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

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--before", required=True, help="Directory with pre-run snapshot files")
    parser.add_argument("--after", required=True, help="Directory with post-run data files")
    parser.add_argument("--output", required=True, help="Output report markdown file")
    parser.add_argument("--email-output", help="Optional output markdown file for email alerts")
    args = parser.parse_args()

    report = compute_report(args.before, args.after)
    markdown = render_markdown(report)
    email_markdown = render_email_markdown(report) if args.email_output else None

    out_dir = os.path.dirname(args.output)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(markdown)

    if args.email_output and email_markdown is not None:
        email_dir = os.path.dirname(args.email_output)
        if email_dir:
            os.makedirs(email_dir, exist_ok=True)
        with open(args.email_output, "w", encoding="utf-8") as f:
            f.write(email_markdown)

    print(markdown)


if __name__ == "__main__":
    main()
