import csv
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config import WTA_RANKINGS_CSV, DATA_DIR
from wta import get_rankings

RANKINGS_CSV = WTA_RANKINGS_CSV
CSV_FIELDNAMES = ["week_date", "rank", "points", "player", "country", "dob"]


def to_title_case(name):
    return name.title() if name else ""


def get_this_weeks_monday():
    today = date.today()
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
    """Return True if the CSV rows for a date have points and dob populated."""
    return bool(rows) and bool(rows[0].get("points")) and bool(rows[0].get("dob"))


def csv_is_sorted(by_date):
    return sorted(by_date.keys()) == list(by_date.keys())


def rewrite_csv(by_date):
    """Rewrite the entire CSV sorted by (week_date, rank)."""
    tmp = RANKINGS_CSV + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        for d in sorted(by_date.keys()):
            rows = by_date[d]
            try:
                rows_sorted = sorted(rows, key=lambda r: int(r.get("rank") or 0))
            except (ValueError, TypeError):
                rows_sorted = rows
            for row in rows_sorted:
                writer.writerow({k: row.get(k, "") for k in CSV_FIELDNAMES})
    os.replace(tmp, RANKINGS_CSV)


def fetch_from_api(date_str):
    """Fetch rankings from API and return as CSV-format row dicts."""
    print(f"  Fetching from API for {date_str}...")
    data = get_rankings(date_str)
    if not data:
        print(f"  Could not fetch rankings for {date_str}.")
        return []
    print(f"  Fetched {len(data)} players.")
    return [{
        "week_date": date_str,
        "rank":      p.get("Rank", ""),
        "points":    p.get("Points", ""),
        "player":    to_title_case(p.get("Player", "")),
        "country":   p.get("Country", ""),
        "dob":       p.get("DOB", ""),
    } for p in data]


def main():
    by_date = load_csv_by_date()
    this_monday = str(get_this_weeks_monday())
    needs_rewrite = False

    # --- Step 1: re-fetch CSV dates missing points/dob ---
    for date_str in sorted(by_date.keys()):
        if csv_date_is_complete(by_date[date_str]):
            continue
        print(f"CSV for {date_str} is incomplete. Re-fetching...")
        rows = fetch_from_api(date_str)
        if rows:
            by_date[date_str] = rows
            needs_rewrite = True

    # --- Step 2: fetch this week if not in CSV ---
    if this_monday not in by_date:
        print(f"Fetching rankings for this week ({this_monday})...")
        rows = fetch_from_api(this_monday)
        if rows:
            by_date[this_monday] = rows
            needs_rewrite = True
    else:
        print(f"This week ({this_monday}) already in CSV ({len(by_date[this_monday])} players).")

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
    main()
