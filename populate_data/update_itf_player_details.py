"""Cache ITF profile details for Argentine players with a recorded match since 2014."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import itf  # noqa: E402
from lazy_browser import LazyBrowserSession  # noqa: E402
from populate_data.itf_load_new import create_driver  # noqa: E402
from runtime_paths import DATA_DIR as RUNTIME_DATA_DIR  # noqa: E402
from utils import (  # noqa: E402
    normalize_player_name,
    save_json_array_one_line_per_item,
)

DATA_DIR = Path(RUNTIME_DATA_DIR)
ALIASES_PATH = DATA_DIR / "player_aliases_wta_itf.json"
OUTPUT_PATH = DATA_DIR / "itf_player_details.json"
MATCH_START_DATE = "2014-01-01"
DETAILS_URL = (
    "https://www.itftennis.com/tennis/api/PlayerApi/"
    "GetHeadToHeadPlayerDetails?circuitCode=WT&playerId={player_id}"
)


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default


def _arg_identity_indexes() -> tuple[dict[str, str], dict[str, str]]:
    by_id: dict[str, str] = {}
    by_name: dict[str, str] = {}
    for identity in _load_json(ALIASES_PATH, []):
        if not isinstance(identity, dict) or str(identity.get("country", "")).upper() != "ARG":
            continue
        display_name = str(
            identity.get("display_name") or identity.get("itf_name") or identity.get("wta_name") or ""
        ).strip()
        for field in ("wta_id", "itf_id", "bjkc_id"):
            player_id = str(identity.get(field, "")).strip()
            if player_id:
                by_id[player_id] = display_name
        for field in ("additional_wta_ids", "additional_itf_ids", "additional_bjkc_ids"):
            for player_id in identity.get(field) or []:
                if str(player_id).strip():
                    by_id[str(player_id).strip()] = display_name
        names = [
            identity.get("display_name"),
            identity.get("wta_name"),
            identity.get("itf_name"),
            identity.get("bjkc_name"),
            *(identity.get("aliases") or []),
        ]
        for name in names:
            key = normalize_player_name(str(name or ""))
            if key:
                by_name[key] = display_name
    return by_id, by_name


def players_with_recent_matches() -> list[tuple[str, str]]:
    """Return ARG players on either side of a match dated 2014 or later."""
    arg_by_id, arg_by_name = _arg_identity_indexes()
    players: dict[str, str] = {}
    paths = sorted(DATA_DIR.glob("*_matches_arg.csv"))
    manual_path = DATA_DIR / "manually_added_matches.csv"
    if manual_path.exists():
        paths.append(manual_path)
    for path in paths:
        with path.open(encoding="utf-8-sig", newline="") as source:
            for row in csv.DictReader(source):
                if str(row.get("date", ""))[:10] < MATCH_START_DATE:
                    continue
                for side in ("winner", "loser"):
                    player_id = str(row.get(f"{side}Id", "")).strip()
                    raw_name = str(row.get(f"{side}Name", "")).strip()
                    canonical_name = arg_by_id.get(player_id) or arg_by_name.get(normalize_player_name(raw_name))
                    country = str(row.get(f"{side}Country", "")).upper()
                    if not player_id.startswith("800") or (country != "ARG" and not canonical_name):
                        continue
                    players[player_id] = canonical_name or raw_name
    roster = sorted(
        ((display_name, player_id) for player_id, display_name in players.items()),
        key=lambda item: (normalize_player_name(item[0]), item[1]),
    )
    return roster


def _save_profiles(existing: dict[str, dict[str, Any]]) -> None:
    rows = sorted(existing.values(), key=lambda row: normalize_player_name(str(row.get("displayName", ""))))
    save_json_array_one_line_per_item(OUTPUT_PATH, rows)


def _profile_row(browser: LazyBrowserSession, player_id: str, fallback_name: str) -> dict[str, Any]:
    payload = itf._fetch_itf_json(
        browser,
        DETAILS_URL.format(player_id=player_id),
        timeout_ms=20_000,
        retries=2,
        failure_severity="partial",
    )
    if not isinstance(payload, dict):
        raise ValueError(f"Unexpected ITF profile response for {player_id}")
    returned_id = str(payload.get("playerId", "")).strip()
    if returned_id and returned_id != player_id:
        raise ValueError(f"ITF returned player {returned_id} for requested player {player_id}")
    return {
        "playerId": player_id,
        "displayName": str(payload.get("FullName") or fallback_name).strip(),
        "birthYear": payload.get("birthYear"),
        "playHand": str(payload.get("playHand") or "").strip(),
        "backHandStyle": str(payload.get("backHandStyle") or "").strip(),
    }


def _unavailable_profile_row(player_id: str, fallback_name: str) -> dict[str, Any]:
    return {
        "playerId": player_id,
        "displayName": fallback_name,
        "birthYear": None,
        "playHand": "",
        "backHandStyle": "",
        "fetchStatus": "unavailable",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="Refetch profiles already present in the cache.")
    parser.add_argument("--limit", type=int, default=0, help="Fetch at most this many profiles (0 means all).")
    parser.add_argument("--delay", type=float, default=0.1, help="Seconds between ITF requests.")
    parser.add_argument(
        "--api-interval",
        type=float,
        default=None,
        help="Override the shared ITF minimum request interval for a controlled backfill.",
    )
    parser.add_argument(
        "--api-jitter",
        type=float,
        default=None,
        help="Override the shared ITF request jitter for a controlled backfill.",
    )
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=20,
        help="Save progress after this many successful fetches (0 only saves at the end).",
    )
    parser.add_argument(
        "--record-missing-unavailable",
        action="store_true",
        help="Record missing targets as unavailable without requesting the ITF API.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit unsuccessfully when any ITF profile request fails.",
    )
    args = parser.parse_args()
    if args.api_interval is not None:
        itf._ITF_MIN_REQUEST_INTERVAL = max(0.0, args.api_interval)
    if args.api_jitter is not None:
        itf._ITF_REQUEST_JITTER_MAX = max(0.0, args.api_jitter)

    existing = {
        str(row.get("playerId", "")): row
        for row in _load_json(OUTPUT_PATH, [])
        if isinstance(row, dict) and str(row.get("playerId", ""))
    }
    roster = players_with_recent_matches()
    roster_ids = {player_id for _, player_id in roster}
    existing = {player_id: row for player_id, row in existing.items() if player_id in roster_ids}
    print(f"Targeting {len(roster)} ARG players with matches on or after {MATCH_START_DATE}.", flush=True)

    targets = [
        (name, player_id)
        for name, player_id in roster
        if player_id and (args.refresh or player_id not in existing)
    ]
    if args.limit > 0:
        targets = targets[: args.limit]
    if args.record_missing_unavailable:
        for name, player_id in targets:
            existing[player_id] = _unavailable_profile_row(player_id, name)
        _save_profiles(existing)
        print(
            f"Saved {len(existing)} ITF player profiles to {OUTPUT_PATH} "
            f"({len(targets)} marked unavailable)",
            flush=True,
        )
        return

    browser = LazyBrowserSession(create_driver)
    failures: list[tuple[str, str, str]] = []
    successes = 0
    try:
        for index, (name, player_id) in enumerate(targets, start=1):
            print(f"[{index}/{len(targets)}] {name} ({player_id})", flush=True)
            try:
                existing[player_id] = _profile_row(browser, player_id, name)
            except (RuntimeError, ValueError) as exc:
                existing[player_id] = _unavailable_profile_row(player_id, name)
                failures.append((name, player_id, str(exc)))
                print(f"  Failed: {exc}", flush=True)
                continue
            successes += 1
            if args.checkpoint_every > 0 and successes % args.checkpoint_every == 0:
                _save_profiles(existing)
            if args.delay > 0 and index < len(targets):
                time.sleep(args.delay)
    finally:
        browser.quit()

    _save_profiles(existing)
    print(f"Saved {len(existing)} ITF player profiles to {OUTPUT_PATH}", flush=True)
    if failures:
        print(f"Failed to fetch {len(failures)} profiles:", flush=True)
        for name, player_id, error in failures:
            print(f"- {name} ({player_id}): {error}", flush=True)
        if args.strict:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
