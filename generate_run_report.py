import argparse
import csv
import json
import os
import unicodedata
from datetime import datetime, timezone

MAX_MATCH_LINES_PER_FILE = 50
RANKINGS_CSV_FILES = ["wta_rankings_00_09.csv", "wta_rankings_10_19.csv", "wta_rankings_20_29.csv"]


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

    rankings_names = None

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
                if rankings_names is None:
                    rankings_names = load_rankings_name_set(after_dir)

                match_line = format_match_line(row)
                missing = []
                for player in get_match_players(row):
                    has_any = any(v in rankings_names for v in name_variants(player))
                    if not has_any:
                        missing.append(player)

                added.append({
                    "line": match_line,
                    "missing_players": missing,
                })

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
            missing = item.get("missing_players") if isinstance(item, dict) else []
            for name in (missing or []):
                lines.append(f"  {name} not found in rankings.")
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
