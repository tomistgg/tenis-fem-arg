import argparse
import csv
import json
import os
import unicodedata
from datetime import datetime, timezone, timedelta

MAX_MATCH_LINES_PER_FILE = 50
RANKINGS_CSV_FILES = ["wta_rankings_00_09.csv", "wta_rankings_10_19.csv", "wta_rankings_20_29.csv"]
ALIASES_JSON_FILE = "player_aliases_wta_itf.json"


def save_json_array_one_line_per_item(path, items):
    """Write a JSON array with one compact object per line (easy to diff/edit)."""
    with open(path, "w", encoding="utf-8") as f:
        f.write("[\n")
        for i, item in enumerate(items or []):
            if i:
                f.write(",\n")
            f.write("  ")
            f.write(json.dumps(item, ensure_ascii=False))
        f.write("\n]\n")


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
    return (value or "").strip().upper()


def normalize_country(value):
    return (value or "").strip().upper()


def strip_accents(text):
    s = (text or "").strip()
    if not s:
        return ""
    return "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))


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
    return [it for it in items if isinstance(it, dict)]


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
    """Return ({week: {wta_id: {rank, player}}}, {week: {norm_name: wta_id}})."""
    weeks = {w for w in (weeks or set()) if w}
    if not weeks:
        return {}, {}

    needed_files = set()
    for w in weeks:
        try:
            year = int(w[:4])
        except Exception:
            continue
        if 2000 <= year <= 2009:
            needed_files.add("wta_rankings_00_09.csv")
        elif 2010 <= year <= 2019:
            needed_files.add("wta_rankings_10_19.csv")
        else:
            needed_files.add("wta_rankings_20_29.csv")

    by_week = {w: {} for w in weeks}
    name_to_id = {w: {} for w in weeks}

    for fname in sorted(needed_files):
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
        except Exception:
            continue

    return by_week, name_to_id


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

        rankings_by_week, ranking_name_to_id = load_rankings_by_week(after_dir, needed_weeks)

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
            elif is_itf_id(pid):
                ent = by_itf.get(pid)
                if ent is None:
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

            wta_id = ""
            if is_wta_id(pid):
                wta_id = pid
            elif ent is not None:
                wta_id = (ent.get("wta_id") or "").strip()

            # Try to map ITF-only entries by name from rankings for that week.
            if ent is not None and not wta_id and week and name:
                name_map = ranking_name_to_id.get(week) or {}
                for k in name_variants(name):
                    hit = name_map.get(k)
                    if hit:
                        ent["wta_id"] = hit
                        aliases_changed = True
                        index_alias_entry(ent)
                        maybe_fill_wta_names(ent, hit, week)
                        wta_id = hit
                        break

            rank = ""
            if wta_id and week:
                rank = ((rankings_by_week.get(week) or {}).get(wta_id) or {}).get("rank") or ""

            issues = []
            if wta_id and week and not rank:
                issues.append(f"{name} (wta_id {wta_id}) not found in rankings for week {week}.")
            if not wta_id:
                itf_only_id = (ent.get("itf_id") or "").strip() if isinstance(ent, dict) else ""
                if itf_only_id:
                    issues.append(f"{name} only has itf_id {itf_only_id} (no wta_id in aliases).")
                elif pid:
                    issues.append(f"{name} has id {pid} (cannot map to WTA rankings).")
                else:
                    issues.append(f"{name} has no id (cannot map to WTA rankings).")
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

            save_json_array_one_line_per_item(aliases_path, sorted(aliases_items, key=_sort_key))

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

    return report


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
