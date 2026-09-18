"""Fill a small batch of live WTN values for players in current WTA entry lists."""

import argparse
import os
import sys
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from config import ENTRY_LISTS_CACHE_FILE  # noqa: E402
from itf_wtn import (  # noqa: E402
    ITF_WTN_CACHE_FILENAME,
    entry_list_wtn_status,
    refresh_entry_list_wtn,
)
from lazy_browser import LazyBrowserSession  # noqa: E402
from main import create_driver  # noqa: E402
from runtime_paths import DATA_DIR  # noqa: E402
from time_utils import madrid_today  # noqa: E402
from utils import (  # noqa: E402
    dumps_entry_lists_cache,
    expand_entry_lists_cache,
    expand_tournament_snapshot,
    load_cache,
    save_cache,
)

WTN_CACHE_FILE = str(Path(DATA_DIR) / ITF_WTN_CACHE_FILENAME)
TOURNAMENT_SNAPSHOT_FILE = str(Path(DATA_DIR) / "tournament_snapshot.json")


def load_current_data():
    entries = expand_entry_lists_cache(load_cache(ENTRY_LISTS_CACHE_FILE))
    snapshot = expand_tournament_snapshot(load_cache(TOURNAMENT_SNAPSHOT_FILE))
    weeks = {str(key): record.get("startDate") for key, record in snapshot.items()}
    return entries, weeks


def print_status(entries, weeks):
    counts = entry_list_wtn_status(entries, WTN_CACHE_FILE, tournament_weeks=weeks)
    old = counts["previous_week"] + counts["other_week"]
    print(f"WTA rows (MAIN/QUAL): {counts['total']}")
    print(f"Current-week WTN:    {counts['current_week']}")
    print(f"Old-week WTN:        {old}")
    print(f"  Previous week:     {counts['previous_week']}")
    print(f"  Other week:        {counts['other_week']}")
    print(f"Missing WTN:         {counts['missing']}")
    print(f"  No ITF ID mapping: {counts['unmapped']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true", help="Report coverage without fetching profiles.")
    parser.add_argument("--limit", type=int, default=8, help="Maximum profiles to fetch (default: 8).")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be at least 1")

    entries, weeks = load_current_data()
    if args.status:
        print_status(entries, weeks)
        return

    driver = LazyBrowserSession(create_driver)
    try:
        refresh_entry_list_wtn(
            driver,
            entries,
            WTN_CACHE_FILE,
            today=madrid_today(),
            fetch_profiles=True,
            tournament_weeks=weeks,
            allow_cross_week_fallback=True,
            max_profile_fetches=args.limit,
        )
    finally:
        driver.quit()
    save_cache(ENTRY_LISTS_CACHE_FILE, entries, formatter=dumps_entry_lists_cache)
    print_status(entries, weeks)


if __name__ == "__main__":
    main()
