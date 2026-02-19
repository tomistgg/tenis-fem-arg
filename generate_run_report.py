import argparse
import csv
import json
import os
from datetime import datetime, timezone

MAX_MATCH_LINES_PER_FILE = 50


def load_json(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def normalize_name(value):
    return (value or "").strip().upper()


def normalize_country(value):
    return (value or "").strip().upper()


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
        return str(row.get("matchId", "")).strip()
    if "MATCHID" in headers:
        return str(row.get("MATCHID", "")).strip()

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

    before_entry = load_json(os.path.join(before_dir, "entry_lists_cache.json")) or {}
    after_entry = load_json(os.path.join(after_dir, "entry_lists_cache.json")) or {}
    before_tourney = load_json(os.path.join(before_dir, "tournament_snapshot.json")) or {}
    after_tourney = load_json(os.path.join(after_dir, "tournament_snapshot.json")) or {}

    if before_entry and after_entry:
        for t_key in sorted(set(before_entry.keys()) | set(after_entry.keys())):
            old_entries = before_entry.get(t_key, [])
            new_entries = after_entry.get(t_key, [])

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
                added.append(format_match_line(row))

        if added:
            report["added_matches"][csv_name] = {
                "count": len(added),
                "items": added[:MAX_MATCH_LINES_PER_FILE],
                "truncated": len(added) > MAX_MATCH_LINES_PER_FILE,
            }

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

    return report


def render_markdown(report):
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = []
    lines.append(f"# Website Update Report ({now_utc})")
    lines.append("")
    lines.append("## 1) Argentine Withdrawals (WTA/ITF)")
    if report["withdrawals"]:
        for item in report["withdrawals"]:
            players = ", ".join(item["players"])
            lines.append(f"- {item['tournament_name']}: {players}")
    else:
        lines.append("- None detected.")

    lines.append("")
    lines.append("## 2) Tournaments that now have an Entry List")
    if report["new_entry_lists"]:
        for item in report["new_entry_lists"]:
            lines.append(f"- {item['tournament_name']} ({item['entries_count']} entries)")
    else:
        lines.append("- None detected.")

    lines.append("")
    lines.append("## 3) Matches Added to CSV Files")
    if report["added_matches"]:
        for csv_name, payload in report["added_matches"].items():
            entries = payload["items"]
            lines.append(f"- {csv_name}: {payload['count']} new match(es)")
            for line in entries:
                lines.append(f"  - {line}")
            if payload["truncated"]:
                lines.append(f"  - ... and {payload['count'] - len(entries)} more")
    else:
        lines.append("- None detected.")

    lines.append("")
    lines.append("## 4) Tournaments Added to Calendar")
    if report["added_calendar_tournaments"]:
        for item in report["added_calendar_tournaments"]:
            lines.append(
                f"- {item.get('week_label', '')} | {item.get('name', '')} | "
                f"{item.get('level', '')} | {item.get('column', '')} | {item.get('continent', '')}"
            )
    else:
        lines.append("- None detected.")

    lines.append("")
    lines.append("## Notes")
    lines.append("- Diff compares pre-run snapshots vs post-run `data/` files in this workflow run.")
    lines.append("- On first run (or when a snapshot file is missing), sections may show `None detected` because no baseline exists.")
    lines.append("- Match lists are capped per CSV to keep email size manageable.")
    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--before", required=True, help="Directory with pre-run snapshot files")
    parser.add_argument("--after", required=True, help="Directory with post-run data files")
    parser.add_argument("--output", required=True, help="Output report markdown file")
    args = parser.parse_args()

    report = compute_report(args.before, args.after)
    markdown = render_markdown(report)

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(markdown)

    print(markdown)


if __name__ == "__main__":
    main()
