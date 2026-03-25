import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config import HEADERS, PLAYER_ALIASES_WTA_ITF_FILE, repair_name_text


CSV_FIELDNAMES = ["week_date", "id", "rank", "points", "player", "country", "dob"]
DEFAULT_FROM_DATE = "1983-01-01"
DEFAULT_TO_DATE = "2000-12-31"
DEFAULT_SLEEP_SECONDS = 15.0
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_MAX_RETRIES = 4
COMPLETED_STATUSES = {"done", "no_valid_weeks", "no_rankings", "not_found"}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "..", "data")
DEFAULT_OUTPUT_CSV = os.path.join(DATA_DIR, "wta_rankings_83_99.csv")
DEFAULT_PROGRESS_FILE = os.path.join(DATA_DIR, "wta_rankings_83_99_progress.jsonl")


class RequestPacer:
    def __init__(self, min_seconds_between_requests):
        self.min_seconds = max(0.0, float(min_seconds_between_requests))
        self._last_request_started_at = None

    def wait(self):
        if self._last_request_started_at is None:
            return
        elapsed = time.monotonic() - self._last_request_started_at
        remaining = self.min_seconds - elapsed
        if remaining > 0:
            print(f"  Waiting {remaining:.1f}s before the next API hit...")
            time.sleep(remaining)

    def mark_request_start(self):
        self._last_request_started_at = time.monotonic()


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Fetch weekly WTA player rankings into a resumable CSV. "
            "Defaults match the date range from the provided endpoint."
        )
    )
    parser.add_argument("--from-date", default=DEFAULT_FROM_DATE, help="Ranking start date (YYYY-MM-DD).")
    parser.add_argument("--to-date", default=DEFAULT_TO_DATE, help="Ranking end date (YYYY-MM-DD).")
    parser.add_argument("--sleep-seconds", type=float, default=DEFAULT_SLEEP_SECONDS, help="Minimum seconds between API requests.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Request timeout in seconds.")
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES, help="Retries per player for transient failures.")
    parser.add_argument("--output-csv", default=DEFAULT_OUTPUT_CSV, help="Output CSV path.")
    parser.add_argument("--progress-file", default=DEFAULT_PROGRESS_FILE, help="Append-only JSONL progress path.")
    parser.add_argument("--aliases-file", default=PLAYER_ALIASES_WTA_ITF_FILE, help="Path to player_aliases_wta_itf.json.")
    parser.add_argument("--limit", type=int, default=0, help="Optional cap on how many players to process.")
    parser.add_argument("--start-at-id", default="", help="Optional WTA id to start from.")
    parser.add_argument("--only-id", default="", help="Only process one WTA id.")
    return parser.parse_args()


def ensure_parent_dir(path):
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def load_players(aliases_file):
    with open(aliases_file, "r", encoding="utf-8-sig") as f:
        raw = json.load(f)

    players = []
    seen_ids = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        wta_id = str(item.get("wta_id") or "").strip()
        if not wta_id or wta_id in seen_ids:
            continue
        seen_ids.add(wta_id)
        players.append({
            "wta_id": wta_id,
            "display_name": repair_name_text(item.get("display_name")).strip(),
            "wta_name": repair_name_text(item.get("wta_name")).strip(),
            "itf_name": repair_name_text(item.get("itf_name")).strip(),
        })
    return players


def select_players(players, start_at_id="", only_id="", limit=0):
    selected = players

    if only_id:
        selected = [player for player in players if player["wta_id"] == only_id]
        return selected[:limit] if limit > 0 else selected

    if start_at_id:
        for index, player in enumerate(players):
            if player["wta_id"] == start_at_id:
                selected = players[index:]
                break

    if limit > 0:
        selected = selected[:limit]

    return selected


def load_progress(progress_file):
    progress_by_id = {}
    if not os.path.exists(progress_file):
        return progress_by_id

    with open(progress_file, "r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                print(f"Warning: skipping malformed progress line {line_number} in {progress_file}.")
                continue
            wta_id = str(entry.get("wta_id") or "").strip()
            if not wta_id:
                continue
            progress_by_id[wta_id] = entry

    return progress_by_id


def load_csv_player_ids(output_csv):
    player_ids = set()
    if not os.path.exists(output_csv):
        return player_ids

    with open(output_csv, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            player_id = str(row.get("id") or "").strip()
            if player_id:
                player_ids.add(player_id)

    return player_ids


def ensure_output_csv(output_csv):
    ensure_parent_dir(output_csv)
    if os.path.exists(output_csv) and os.path.getsize(output_csv) > 0:
        return

    with open(output_csv, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()


def purge_player_rows(output_csv, player_id):
    if not os.path.exists(output_csv):
        return False

    tmp_path = output_csv + ".tmp"
    removed_any = False
    with open(output_csv, "r", encoding="utf-8", newline="") as src, open(tmp_path, "w", encoding="utf-8", newline="") as dst:
        reader = csv.DictReader(src)
        writer = csv.DictWriter(dst, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        for row in reader:
            current_id = str(row.get("id") or "").strip()
            if current_id == player_id:
                removed_any = True
                continue
            writer.writerow({field: row.get(field, "") for field in CSV_FIELDNAMES})

    os.replace(tmp_path, output_csv)
    return removed_any


def append_progress(progress_file, entry):
    ensure_parent_dir(progress_file)
    with open(progress_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def progress_entry_matches_range(entry, from_date, to_date):
    return (
        str(entry.get("from_date") or "").strip() == from_date
        and str(entry.get("to_date") or "").strip() == to_date
    )


def make_request_headers():
    headers = dict(HEADERS or {})
    headers.setdefault("Accept", "application/json, text/plain, */*")
    headers.setdefault("Accept-Language", "en-US,en;q=0.9")
    headers.setdefault("Origin", "https://www.wtatennis.com")
    headers.setdefault("Referer", "https://www.wtatennis.com/")
    headers.setdefault("account", "wta")
    return headers


def rate_limited_get(session, url, headers, params, timeout, pacer):
    pacer.wait()
    pacer.mark_request_start()
    return session.get(url, headers=headers, params=params, timeout=timeout)


def fetch_player_rankings(session, wta_id, from_date, to_date, timeout, max_retries, pacer):
    url = f"https://api.wtatennis.com/tennis/players/{wta_id}/ranking"
    params = {
        "from": from_date,
        "to": to_date,
        "aggregation-method": "weekly",
    }
    headers = make_request_headers()
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            response = rate_limited_get(session, url, headers, params, timeout, pacer)
            if response.status_code == 404:
                return "not_found", None
            if response.status_code in (429, 500, 502, 503, 504):
                last_error = RuntimeError(f"HTTP {response.status_code}")
                print(f"  Transient response {response.status_code} for WTA id {wta_id} (attempt {attempt}/{max_retries}).")
            else:
                response.raise_for_status()
                payload = response.json()
                return "ok", payload
        except requests.RequestException as exc:
            last_error = exc
            print(f"  Request failed for WTA id {wta_id} (attempt {attempt}/{max_retries}): {exc}")
        except ValueError as exc:
            last_error = exc
            print(f"  Could not decode JSON for WTA id {wta_id} (attempt {attempt}/{max_retries}): {exc}")

        if attempt < max_retries:
            backoff = min(60, 5 * attempt)
            print(f"  Backing off for {backoff}s before retrying WTA id {wta_id}...")
            time.sleep(backoff)

    raise RuntimeError(f"Failed to fetch rankings for WTA id {wta_id}: {last_error}")


def get_week_date(ranked_at):
    value = str(ranked_at or "").strip()
    if not value:
        return ""
    if "T" in value:
        return value.split("T", 1)[0]
    return value[:10]


def build_player_name(player_info):
    full_name = repair_name_text(player_info.get("fullName")).strip()
    if full_name:
        return full_name

    first_name = repair_name_text(player_info.get("firstName")).strip()
    last_name = repair_name_text(player_info.get("lastName")).strip()
    return " ".join(part for part in (first_name, last_name) if part)


def parse_weekly_rows(payload, fallback_wta_id):
    player_info = payload.get("player") or {}
    weekly_rankings = payload.get("weeklyRankings") or []
    player_name = build_player_name(player_info)
    player_id = str(player_info.get("id") or fallback_wta_id).strip()
    country = str(player_info.get("countryCode") or "").strip()
    dob = str(player_info.get("dateOfBirth") or "").strip()

    rows = []
    seen_week_dates = set()

    def sort_key(item):
        return get_week_date(item.get("rankedAt"))

    for item in sorted(weekly_rankings, key=sort_key):
        week_date = get_week_date(item.get("rankedAt"))
        if not week_date or week_date in seen_week_dates:
            continue

        rank_value = item.get("singlesRanking")
        if rank_value is None:
            rank_value = item.get("singleRanking")

        try:
            rank_int = int(rank_value)
        except (TypeError, ValueError):
            continue

        if rank_int in (0, 9999):
            continue

        seen_week_dates.add(week_date)
        rows.append({
            "week_date": week_date,
            "id": player_id,
            "rank": str(rank_int),
            "points": "",
            "player": player_name,
            "country": country,
            "dob": dob,
        })

    return rows, len(weekly_rankings), player_name


def append_rows(output_csv, rows):
    count = 0
    with open(output_csv, "a", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDNAMES)
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in CSV_FIELDNAMES})
            count += 1
        if count:
            csv_file.flush()
            os.fsync(csv_file.fileno())
    return count


def build_progress_entry(player, from_date, to_date, status, rows_seen, rows_written, error=""):
    return {
        "wta_id": player["wta_id"],
        "display_name": player.get("display_name") or player.get("wta_name") or player.get("itf_name") or "",
        "status": status,
        "raw_weeks_seen": rows_seen,
        "rows_written": rows_written,
        "from_date": from_date,
        "to_date": to_date,
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "error": error,
    }


def main():
    args = parse_args()
    players = load_players(args.aliases_file)
    players = select_players(
        players,
        start_at_id=str(args.start_at_id or "").strip(),
        only_id=str(args.only_id or "").strip(),
        limit=max(0, int(args.limit or 0)),
    )

    progress_by_id = load_progress(args.progress_file)
    completed_ids = {
        wta_id
        for wta_id, entry in progress_by_id.items()
        if str(entry.get("status") or "").strip() in COMPLETED_STATUSES
        and progress_entry_matches_range(entry, args.from_date, args.to_date)
    }
    csv_player_ids = load_csv_player_ids(args.output_csv)

    ensure_output_csv(args.output_csv)

    print(f"Players with WTA ids loaded: {len(players)}")
    print(f"Completed players already recorded: {len(completed_ids)}")
    print(f"Output CSV: {args.output_csv}")
    print(f"Progress file: {args.progress_file}")
    print(f"Date range: {args.from_date} -> {args.to_date}")
    print(f"Minimum seconds between API requests: {args.sleep_seconds}")

    pacer = RequestPacer(args.sleep_seconds)
    session = requests.Session()

    processed_now = 0
    try:
        for index, player in enumerate(players, start=1):
            wta_id = player["wta_id"]
            if wta_id in completed_ids:
                continue

            if wta_id in csv_player_ids:
                print(f"[{index}/{len(players)}] Removing stale rows for WTA id {wta_id} before retrying.")
                if purge_player_rows(args.output_csv, wta_id):
                    csv_player_ids.discard(wta_id)

            label = player.get("display_name") or player.get("wta_name") or player.get("itf_name") or wta_id
            print(f"[{index}/{len(players)}] Fetching {label} ({wta_id})...")

            status = ""
            raw_weeks_seen = 0
            rows_written = 0
            error_message = ""

            try:
                fetch_status, payload = fetch_player_rankings(
                    session=session,
                    wta_id=wta_id,
                    from_date=args.from_date,
                    to_date=args.to_date,
                    timeout=args.timeout,
                    max_retries=max(1, args.max_retries),
                    pacer=pacer,
                )

                if fetch_status == "not_found":
                    status = "not_found"
                    print(f"  WTA id {wta_id} returned 404.")
                else:
                    rows, raw_weeks_seen, player_name = parse_weekly_rows(payload or {}, wta_id)
                    if not payload or not (payload.get("weeklyRankings") or []):
                        status = "no_rankings"
                        print(f"  No weeklyRankings payload for WTA id {wta_id}.")
                    elif not rows:
                        status = "no_valid_weeks"
                        print(f"  {player_name or wta_id}: {raw_weeks_seen} weeks seen, 0 valid singles rows after filtering.")
                    else:
                        rows_written = append_rows(args.output_csv, rows)
                        status = "done"
                        csv_player_ids.add(wta_id)
                        print(f"  {player_name or wta_id}: wrote {rows_written} rows from {raw_weeks_seen} weekly entries.")
            except Exception as exc:
                status = "error"
                error_message = str(exc)
                print(f"  Error for WTA id {wta_id}: {error_message}")

            progress_entry = build_progress_entry(
                player=player,
                from_date=args.from_date,
                to_date=args.to_date,
                status=status,
                rows_seen=raw_weeks_seen,
                rows_written=rows_written,
                error=error_message,
            )
            append_progress(args.progress_file, progress_entry)
            progress_by_id[wta_id] = progress_entry

            if status in COMPLETED_STATUSES:
                completed_ids.add(wta_id)

            processed_now += 1
    finally:
        session.close()
    print(f"Run finished. Players attempted in this run: {processed_now}")


if __name__ == "__main__":
    main()
