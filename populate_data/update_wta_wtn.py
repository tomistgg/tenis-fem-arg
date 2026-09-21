"""Fill a small batch of live WTN values for players in current WTA entry lists."""

import argparse
import os
import sys
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from config import ENTRY_LISTS_CACHE_FILE, PLAYER_IDENTITY_INDEX  # noqa: E402
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
    dumps_draws_store_cache,
    dumps_entry_lists_cache,
    expand_draws_store_cache,
    expand_entry_lists_cache,
    expand_tournament_snapshot,
    load_cache,
    save_cache,
)

WTN_CACHE_FILE = str(Path(DATA_DIR) / ITF_WTN_CACHE_FILENAME)
TOURNAMENT_SNAPSHOT_FILE = str(Path(DATA_DIR) / "tournament_snapshot.json")
DRAWS_STORE_FILE = str(Path(DATA_DIR) / "draws_store_cache.json")


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


def _draw_profile_players(draws_store):
    players_by_id = {}
    for tournament in (draws_store or {}).values():
        draws = tournament.get("draws", tournament) if isinstance(tournament, dict) else {}
        for draw_type in ("MDS", "QS"):
            for player in (draws.get(draw_type) or {}).get("players", []):
                name = str(player.get("name") or "").strip()
                if "," in name:
                    last_name, first_name = name.split(",", 1)
                    name = f"{first_name} {last_name}".strip()
                identity = PLAYER_IDENTITY_INDEX.resolve("wta", name=name)
                player_id = str(player.get("itf_id") or (identity.itf_id if identity else "")).strip()
                if not player_id:
                    continue
                players_by_id[player_id] = {
                    "player_id": player_id,
                    "name": name,
                    "country": str(player.get("country") or (identity.country if identity else "")),
                    "type": "MAIN",
                }
    return players_by_id


def refresh_draw_wtn(driver, draws_store, limit):
    players_by_id = _draw_profile_players(draws_store)
    if not players_by_id:
        return 0
    profile_entries = {"https://draws.local": list(players_by_id.values())}
    refresh_entry_list_wtn(
        driver,
        profile_entries,
        WTN_CACHE_FILE,
        today=madrid_today(),
        resolve_itf_player=lambda player: player,
        fetch_profiles=True,
        max_profile_fetches=limit,
    )
    values = {
        player_id: player.get("wtn")
        for player_id, player in players_by_id.items()
        if player.get("wtn") not in (None, "", "-")
    }
    updated = 0
    for tournament in (draws_store or {}).values():
        draws = tournament.get("draws", tournament) if isinstance(tournament, dict) else {}
        for draw_type in ("MDS", "QS"):
            for player in (draws.get(draw_type) or {}).get("players", []):
                value = values.get(str(player.get("itf_id") or ""))
                if value is not None and player.get("wtn") != str(value):
                    player["wtn"] = str(value)
                    updated += 1
    return updated


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true", help="Report coverage without fetching profiles.")
    parser.add_argument("--draws", action="store_true", help="Fetch and fill WTN values in singles draws.")
    parser.add_argument("--limit", type=int, default=8, help="Maximum profiles to fetch (default: 8).")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be at least 1")

    entries, weeks = load_current_data()
    if args.status:
        print_status(entries, weeks)
        return

    draws_store = expand_draws_store_cache(load_cache(DRAWS_STORE_FILE)) if args.draws else None
    driver = LazyBrowserSession(create_driver)
    try:
        if args.draws:
            draw_updates = refresh_draw_wtn(driver, draws_store, args.limit)
        else:
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
    if args.draws:
        save_cache(DRAWS_STORE_FILE, draws_store, formatter=dumps_draws_store_cache)
        print(f"Draw WTN rows updated: {draw_updates}")
    print_status(entries, weeks)


if __name__ == "__main__":
    main()
