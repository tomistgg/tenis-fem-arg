import csv
import hashlib
import json
import os
import sys
from datetime import date, time, timedelta
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config import PLAYER_ALIASES_WTA_ITF_FILE, WTA_RANKINGS_CSV
from canonical_data import sync_wta_players
from time_utils import new_york_now, new_york_today
from wta import get_rankings
from transactional_io import atomic_write_csv
from utils import save_json_file
from pipeline_errors import PipelineError
from run_state import report_run_issue

RANKINGS_CSV = WTA_RANKINGS_CSV
CSV_FIELDNAMES = ["week_date", "id", "rank", "points", "player", "country", "dob"]
MIN_CURRENT_WEEK_ROWS = 1000
RANKING_STATUS_FILE = os.path.join(os.path.dirname(RANKINGS_CSV), "wta_ranking_refresh_status.json")
PUBLICATION_CUTOFF = time(10, 0)


def to_title_case(name):
    return name.title() if name else ""


def get_this_weeks_monday():
    today = new_york_today()
    return today - timedelta(days=today.weekday())


def load_csv_by_date():
    """Load CSV into a dict: date_str -> list of row dicts."""
    by_date = {}
    if not os.path.exists(RANKINGS_CSV):
        return by_date
    with open(RANKINGS_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            d = row["week_date"]
            if d not in by_date:
                by_date[d] = []
            by_date[d].append(row)
    return by_date


def csv_date_is_complete(rows):
    """Return True when every row is populated and the natural key is unique."""
    if not rows:
        return False
    ids = [str(row.get("id") or "").strip() for row in rows]
    return (
        all(ids)
        and len(ids) == len(set(ids))
        and all(str(row.get("points") or "").strip() for row in rows)
        and all(str(row.get("dob") or "").strip() for row in rows)
    )


def csv_is_sorted(by_date):
    return sorted(by_date.keys()) == list(by_date.keys())


def ranking_signature(rows):
    """Return a stable signature for ranking content, ignoring week_date."""
    content = []
    for row in rows or []:
        content.append(tuple(str(row.get(field) or "").strip() for field in CSV_FIELDNAMES[1:]))
    content.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    encoded = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def ranking_is_valid(rows):
    """Reject empty/partial API responses before they can replace a ranking."""
    if len(rows or []) < MIN_CURRENT_WEEK_ROWS:
        return False
    ranks = {str(row.get("rank") or "").strip() for row in rows}
    ids = [str(row.get("id") or "").strip() for row in rows]
    return "1" in ranks and all(ids) and len(ids) == len(set(ids))


def load_status():
    try:
        with open(RANKING_STATUS_FILE, encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def save_status(status):
    current = load_status()
    if current == status:
        return False
    save_json_file(RANKING_STATUS_FILE, status)
    return True


def now_eastern():
    return new_york_now()


def rewrite_csv(by_date):
    """Rewrite the entire CSV sorted by (week_date, rank)."""
    output_rows = []
    for d in sorted(by_date.keys()):
        rows = by_date[d]
        try:
            rows_sorted = sorted(rows, key=lambda r: int(r.get("rank") or 0))
        except (ValueError, TypeError):
            rows_sorted = rows
        output_rows.extend(
            {key: row.get(key, "") for key in CSV_FIELDNAMES}
            for row in rows_sorted
        )
    atomic_write_csv(
        RANKINGS_CSV,
        CSV_FIELDNAMES,
        output_rows,
        encoding="utf-8",
    )


def fetch_from_api(date_str):
    """Fetch rankings from API and return as CSV-format row dicts."""
    print(f"  Fetching from API for {date_str}...")
    try:
        data = get_rankings(date_str)
    except Exception as e:
        report_run_issue(
            "wta-rankings", "fetch weekly ranking", e, severity="degraded",
            context={"week_date": date_str, "fallback": "existing ranking"},
        )
        print(f"  Error fetching rankings for {date_str}: {e}")
        return []
    if not data:
        report_run_issue(
            "wta-rankings",
            "fetch weekly ranking",
            PipelineError(
                component="wta-rankings",
                operation="fetch weekly ranking",
                message="WTA ranking source returned no rows",
                context={"week_date": date_str, "fallback": "existing ranking"},
            ),
            severity="degraded",
        )
        print(f"  Could not fetch rankings for {date_str}.")
        return []
    print(f"  Fetched {len(data)} players.")
    return [{
        "week_date": date_str,
        "id":        p.get("Id", ""),
        "rank":      p.get("Rank", ""),
        "points":    p.get("Points", ""),
        "player":    (p.get("OfficialPlayer") or to_title_case(p.get("Player", "")) or "").strip(),
        "country":   p.get("Country", ""),
        "dob":       p.get("DOB", ""),
    } for p in data]


def main():
    by_date = load_csv_by_date()
    eastern_now = now_eastern()
    this_monday = str(eastern_now.date() - timedelta(days=eastern_now.weekday()))
    previous_monday = str(date.fromisoformat(this_monday) - timedelta(days=7))
    status_before = load_status()
    needs_rewrite = False

    # --- Step 1: re-fetch CSV dates missing points/dob ---
    for date_str in sorted(by_date.keys()):
        if csv_date_is_complete(by_date[date_str]):
            continue
        print(f"CSV for {date_str} is incomplete. Re-fetching...")
        rows = fetch_from_api(date_str)
        if rows and ranking_is_valid(rows):
            added = sync_wta_players(Path(PLAYER_ALIASES_WTA_ITF_FILE), rows)
            if added:
                print(f"  Added {added} new WTA identities to the canonical player table.")
            by_date[date_str] = rows
            needs_rewrite = True

    # --- Step 2: validate this week's ranking against last week ---
    # Once a ranking is accepted, do not hit the API again on every 2-hour run.
    accepted_status = {"confirmed_changed", "confirmed_frozen"}
    status_is_accepted = (
        status_before.get("requested_date") == this_monday
        and status_before.get("status") in accepted_status
    )
    if not status_is_accepted:
        print(f"Fetching rankings for this week ({this_monday})...")
        rows = fetch_from_api(this_monday)
        current_rows = by_date.get(this_monday) or []
        previous_rows = by_date.get(previous_monday) or []
        cutoff_passed = eastern_now.timetz().replace(tzinfo=None) >= PUBLICATION_CUTOFF

        if rows and not ranking_is_valid(rows):
            print(f"Rejected incomplete/invalid ranking response for {this_monday}.")
            rows = []

        if rows:
            added = sync_wta_players(Path(PLAYER_ALIASES_WTA_ITF_FILE), rows)
            if added:
                print(f"Added {added} new WTA identities to the canonical player table.")
            same_as_previous = bool(previous_rows) and ranking_signature(rows) == ranking_signature(previous_rows)
            if same_as_previous and not cutoff_passed:
                # Remove a stale copy that may have been written by an older run.
                if this_monday in by_date:
                    by_date.pop(this_monday, None)
                    needs_rewrite = True
                status = {
                    "requested_date": this_monday,
                    "previous_date": previous_monday,
                    "status": "pending_publication",
                    "comparison": "same_as_previous_week",
                    "cutoff": "10:00 America/New_York",
                    "message": "The WTA response still matches last week; waiting until after the publication cutoff.",
                }
                print(status["message"])
            else:
                new_status = "confirmed_frozen" if same_as_previous else "confirmed_changed"
                if by_date.get(this_monday) != rows:
                    by_date[this_monday] = rows
                    needs_rewrite = True
                status = {
                    "requested_date": this_monday,
                    "previous_date": previous_monday,
                    "status": new_status,
                    "comparison": "same_as_previous_week" if same_as_previous else "different_from_previous_week",
                    "cutoff": "10:00 America/New_York",
                    "message": (
                        "Ranking accepted as frozen after the publication cutoff."
                        if same_as_previous else
                        "New WTA ranking accepted."
                    ),
                }
                print(status["message"])
        else:
            # If an old run left an exact copy of last week's ranking under the
            # current date, do not present it as current while waiting.
            if (
                this_monday in by_date
                and previous_rows
                and ranking_signature(current_rows) == ranking_signature(previous_rows)
                and not cutoff_passed
            ):
                by_date.pop(this_monday, None)
                needs_rewrite = True
            status = {
                "requested_date": this_monday,
                "previous_date": previous_monday,
                "status": "pending_publication",
                "comparison": "unavailable",
                "cutoff": "10:00 America/New_York",
                "message": "No valid current-week WTA ranking was returned; retaining last week's ranking.",
            }
            print(status["message"])
        save_status(status)
    else:
        status = status_before
        print(f"This week's ranking already accepted as {status.get('status')}.")

    # --- Step 3: check CSV is sorted ---
    if not needs_rewrite and not csv_is_sorted(by_date):
        print("CSV is out of order. Rewriting to sort.")
        needs_rewrite = True

    # --- Step 4: rewrite CSV if anything changed ---
    if needs_rewrite:
        print("Rewriting CSV...")
        rewrite_csv(by_date)
        print("Done. CSV rewritten sorted by date and rank.")
    else:
        print("CSV is up to date.")


if __name__ == "__main__":
    from pipeline_transaction import run_current_script_transaction, transaction_is_active

    if transaction_is_active():
        main()
    else:
        raise SystemExit(run_current_script_transaction(__file__))
