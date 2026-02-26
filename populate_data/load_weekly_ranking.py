import csv
import json
import os
import sys
from datetime import datetime, date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config import WTA_CACHE_FILE, DATA_DIR
from wta import get_rankings

RANKINGS_CSV = os.path.join(DATA_DIR, "wta_rankings_20_29.csv")
CSV_FIELDNAMES = ["week_date", "rank", "points", "player", "country", "dob"]


def to_title_case(name):
    return name.title() if name else ""


def is_complete(entry):
    """Return True if a cache entry has Points and DOB populated."""
    return bool(entry.get("Points")) and bool(entry.get("DOB"))


def get_this_weeks_monday():
    today = date.today()
    return today - timedelta(days=today.weekday())


def load_cache():
    if os.path.exists(WTA_CACHE_FILE):
        with open(WTA_CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache):
    with open(WTA_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


def load_csv_dates():
    """Return the set of week_date values already in the CSV."""
    if not os.path.exists(RANKINGS_CSV):
        return set()
    with open(RANKINGS_CSV, encoding="utf-8") as f:
        return {row["week_date"] for row in csv.DictReader(f)}


def csv_is_sorted():
    """Return True if the CSV rows are in ascending date order."""
    if not os.path.exists(RANKINGS_CSV):
        return True
    prev = ""
    with open(RANKINGS_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            d = row["week_date"]
            if d < prev:
                return False
            prev = d
    return True


def rewrite_csv(cache):
    """
    Rewrite the entire CSV from cache, sorted by (week_date, rank).
    Only includes dates whose cache entries are complete (have Points + DOB).
    Preserves rows for dates not in cache (already-written historical data).
    """
    # Load existing rows for dates not in cache (historical, won't be rewritten from cache)
    existing_rows = {}
    if os.path.exists(RANKINGS_CSV):
        with open(RANKINGS_CSV, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                d = row["week_date"]
                if d not in existing_rows:
                    existing_rows[d] = []
                existing_rows[d].append(row)

    # Build the complete row set: cache-sourced dates override existing rows
    all_rows = {}
    for d, players in cache.items():
        if not players or not is_complete(players[0]):
            # Keep existing rows if cache is incomplete
            if d in existing_rows:
                all_rows[d] = existing_rows[d]
            continue
        all_rows[d] = [{
            "week_date": d,
            "rank":      p.get("Rank", ""),
            "points":    p.get("Points", ""),
            "player":    to_title_case(p.get("Player", "")),
            "country":   p.get("Country", ""),
            "dob":       p.get("DOB", ""),
        } for p in players]

    # Also include historical dates not in cache at all
    for d, rows in existing_rows.items():
        if d not in all_rows:
            all_rows[d] = rows

    # Write sorted by date, then by rank
    tmp = RANKINGS_CSV + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        for d in sorted(all_rows.keys()):
            rows = all_rows[d]
            try:
                rows_sorted = sorted(rows, key=lambda r: int(r.get("rank") or 0))
            except (ValueError, TypeError):
                rows_sorted = rows
            for row in rows_sorted:
                writer.writerow({k: row.get(k, "") for k in CSV_FIELDNAMES})
    os.replace(tmp, RANKINGS_CSV)


def fetch_and_cache(date_str, cache):
    """Fetch rankings for date_str, update cache in-place, return data."""
    print(f"  Fetching from API for {date_str}...")
    data = get_rankings(date_str)
    if data:
        cache[date_str] = data
        print(f"  Fetched {len(data)} players.")
    else:
        print(f"  Could not fetch rankings for {date_str}.")
    return data


def main():
    cache = load_cache()
    csv_dates = load_csv_dates()
    this_monday = str(get_this_weeks_monday())
    cache_updated = False
    needs_rewrite = False

    # --- Step 1: re-fetch cache entries missing Points/DOB ---
    for date_str in sorted(cache.keys()):
        if not cache[date_str] or is_complete(cache[date_str][0]):
            continue
        print(f"Cache for {date_str} is incomplete. Re-fetching...")
        if fetch_and_cache(date_str, cache):
            cache_updated = True
            needs_rewrite = True

    # --- Step 2: fetch this week if not yet cached ---
    if this_monday not in cache:
        print(f"Fetching rankings for this week ({this_monday})...")
        if fetch_and_cache(this_monday, cache):
            cache_updated = True
            needs_rewrite = True
    else:
        print(f"This week ({this_monday}) already cached ({len(cache[this_monday])} players).")

    # --- Step 3: check for any cached dates not yet in CSV ---
    missing = sorted(d for d in cache if d not in csv_dates and is_complete(cache[d][0] if cache[d] else {}))
    if missing:
        print(f"Dates in cache but not in CSV: {missing}")
        needs_rewrite = True
    else:
        print("No dates missing from CSV.")

    # --- Step 4: check CSV is sorted (guard against previous out-of-order writes) ---
    if not needs_rewrite and not csv_is_sorted():
        print("CSV is out of order. Rewriting to sort.")
        needs_rewrite = True

    # --- Step 5: rewrite CSV if anything changed ---
    if needs_rewrite:
        print("Rewriting CSV...")
        rewrite_csv(cache)
        print(f"Done. CSV rewritten sorted by date and rank.")
    else:
        print("CSV is up to date.")

    if cache_updated:
        save_cache(cache)
        print("Cache saved.")


if __name__ == "__main__":
    main()
