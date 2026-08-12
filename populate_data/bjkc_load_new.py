import csv
import json
import os
import sys

import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(BASE_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from canonical_data import source_match_key
from http_client import get_with_retry
from pipeline_errors import PipelineError
from run_state import report_run_issue
from runtime_logging import get_logger
from runtime_paths import DATA_DIR as RUNTIME_DATA_DIR
from time_utils import madrid_today
from transactional_io import atomic_write_dataframe

logger = get_logger("bjkc-loader")
DATA_DIR = str(RUNTIME_DATA_DIR)

# --- CONFIGURATION ---
_CURRENT_YEAR = madrid_today().year
START_YEAR = _CURRENT_YEAR
END_YEAR = _CURRENT_YEAR
SERIES_BASE_URL = "https://api.itf-production.sports-data.stadion.io/custom/wcotDrawsModeled/bjkc/"
TIE_BASE_URL = "https://api.itf-production.sports-data.stadion.io/custom/tieCentre/"

HEADERS = {
    "accept": "*/*",
    "referer": "https://www.billiejeankingcup.com/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
    ),
}


def get_score_string(side1_sets, side2_sets, winner_is_s1):
    if not side1_sets or not side2_sets:
        return ""
    res_parts = []
    s1_sorted = sorted(side1_sets, key=lambda x: x.get("setNumber", 0))
    s2_sorted = sorted(side2_sets, key=lambda x: x.get("setNumber", 0))
    for s1, s2 in zip(s1_sorted, s2_sorted, strict=False):
        s1_s, s2_s = s1.get("setScore", 0), s2.get("setScore", 0)
        tb1, tb2 = s1.get("setTieBreakScore", 0), s2.get("setTieBreakScore", 0)
        if winner_is_s1:
            part = f"{s1_s}-{s2_s}"
            if s1_s == 7 and s2_s == 6:
                part += f"({tb2})"
            elif s1_s == 6 and s2_s == 7:
                part += f"({tb1})"
        else:
            part = f"{s2_s}-{s1_s}"
            if s2_s == 7 and s1_s == 6:
                part += f"({tb1})"
            elif s2_s == 6 and s1_s == 7:
                part += f"({tb2})"
        res_parts.append(part)
    return " ".join(res_parts)


def check_nation(nation_obj, target_country="Argentina", target_iso="ARG"):
    """Helper to check a single nation object."""
    if not nation_obj:
        return False
    return nation_obj.get("nation") == target_country or nation_obj.get("nationISO") == target_iso


def is_target_involved(content, target_country="Argentina", target_iso="ARG"):
    """
    Scans the entire content block of a draw to see if Argentina is involved
    in either the participants list (tables) or specific ties.
    """
    # 1. Check participants in Pools/Round Robin tables
    if isinstance(content, dict) and "tables" in content:
        for entry in content["tables"]:
            country = entry.get("country", {})
            if country.get("name") == target_country or country.get("ISOcode") == target_iso:
                return True

    # 2. Check specific ties in Pools 'recent' list
    if isinstance(content, dict) and "recent" in content:
        for tie in content["recent"]:
            if check_nation(tie.get("homeNation")) or check_nation(tie.get("awayNation")):
                return True

    # 3. Check specific ties in Tree structures (List of rounds)
    if isinstance(content, list):
        for round_item in content:
            for tie in round_item.get("ties", []):
                if check_nation(tie.get("homeNation")) or check_nation(tie.get("awayNation")):
                    return True

    return False


def parse_tie_matches(tie_data, tie_id, current_year=None):
    """Parse one saved BJKC tie response without performing network I/O."""
    current_year = START_YEAR if current_year is None else current_year
    raw_date = tie_data.get("endDate", "") or tie_data.get("startDate", "")
    formatted_date = raw_date.split("T")[0] if raw_date else ""
    venue_country = tie_data.get("venue", {}).get("country", {}).get("name", "")
    surface = (tie_data.get("surfaceFriendlyName") or "").strip()
    if not surface and formatted_date.startswith(f"{current_year}-"):
        surface = "Clay"

    def get_player_names(side):
        return " / ".join(
            player.get("player", {}).get("_admin_name", "")
            for player in side.get("sidePlayer", [])
            if player.get("player")
        )

    def get_country(side):
        players = side.get("sidePlayer", [])
        if not players:
            return ""
        return players[0].get("player", {}).get("person", {}).get("country", {}).get("ISOcode", "")

    parsed = []
    for match in tie_data.get("matches", []):
        sides = match.get("sides", [])
        if len(sides) < 2:
            continue
        winner_side_id = match.get("winnerSideId")
        side1, side2 = sides[0], sides[1]
        winner_is_side1 = side1.get("id") == winner_side_id
        winner, loser = (side1, side2) if winner_is_side1 else (side2, side1)
        parsed.append(
            {
                "tieId": tie_id,
                "matchType": "Fed/BJK Cup",
                "matchId": match.get("id"),
                "date": formatted_date,
                "tournamentId": tie_data.get("_name"),
                "tournamentCategory": "Fed/BJK Cup",
                "surface": surface,
                "inOrOutdoor": "I" if surface.startswith("I.") else "O",
                "tournamentCountry": venue_country,
                "resultStatusDesc": match.get("resultStatusDesc", ""),
                "matchOrder": int(match["orderInRound"]) if match.get("orderInRound") is not None else None,
                "result": get_score_string(side1.get("sideSets"), side2.get("sideSets"), winner_is_side1),
                "winnerId": winner.get("id"),
                "winnerEntry": "",
                "winnerSeed": "",
                "winnerName": get_player_names(winner),
                "winnerCountry": get_country(winner),
                "loserId": loser.get("id"),
                "loserEntry": "",
                "loserSeed": "",
                "loserName": get_player_names(loser),
                "loserCountry": get_country(loser),
            }
        )
    return parsed


def main():
    all_ties = []
    logger.info(f"--- Phase 1: Fetching Draws {START_YEAR} to {END_YEAR} (Argentina Filter) ---")

    for year in range(START_YEAR, END_YEAR + 1):
        logger.debug(f"Processing year: {year}")
        try:
            response = get_with_retry(
                f"{SERIES_BASE_URL}{year}",
                component="bjkc-series",
                headers=HEADERS,
            )
            if response.status_code != 200:
                continue

            data = response.json().get("data", [])
            for block in data:
                for event in block.get("events", []):
                    base_info = {"year": year, "eventName": event.get("name")}
                    for draw in event.get("draws", []):
                        content = draw.get("content")
                        if isinstance(content, str):
                            content = content.strip()
                            if not content:
                                continue
                            content = json.loads(content)

                        if not content:
                            continue

                        # Comprehensive check: If Argentina is anywhere in this draw's content
                        if is_target_involved(content):
                            draw_info = {**base_info, "drawName": draw.get("name"), "drawId": draw.get("id")}

                            # Extract all ties from this specific draw
                            if isinstance(content, list):  # Tree
                                for r in content:
                                    for tie in r.get("ties", []):
                                        if check_nation(tie.get("homeNation")) or check_nation(tie.get("awayNation")):
                                            all_ties.append(
                                                {**draw_info, "tieId": tie.get("id"), "roundName": r.get("name")}
                                            )
                            elif isinstance(content, dict):  # Pool
                                for tie in content.get("recent", []):
                                    if check_nation(tie.get("homeNation")) or check_nation(tie.get("awayNation")):
                                        all_ties.append(
                                            {**draw_info, "tieId": tie.get("id"), "roundName": tie.get("round")}
                                        )
        except PipelineError as e:
            logger.error(f"Error fetching {year}: {e}")
        except Exception as e:
            report_run_issue(
                "bjkc-series",
                "parse series",
                e,
                severity="partial",
                context={"year": year},
            )
            logger.debug(f"Error parsing {year}: {e}")

    if not all_ties:
        logger.info("No ties found for Argentina.")
        return

    df_ties = pd.DataFrame(all_ties).drop_duplicates(subset=["tieId"])
    unique_ids = df_ties["tieId"].dropna().unique().tolist()

    logger.info(f"Phase 2: fetching matches for {len(unique_ids)} Argentina ties")
    match_results = []
    for i, tid in enumerate(unique_ids):
        logger.debug(f"Ties: {i + 1}/{len(unique_ids)}")
        try:
            r = get_with_retry(
                f"{TIE_BASE_URL}{tid}",
                component="bjkc-tie",
                headers=HEADERS,
            )
            if r.status_code != 200:
                continue
            tie_data = r.json().get("data", {}).get("tie", {})

            match_results.extend(parse_tie_matches(tie_data, tid))
        except PipelineError:
            continue
        except Exception as exc:
            report_run_issue(
                "bjkc-tie",
                "parse tie",
                exc,
                severity="partial",
                context={"tie_id": str(tid)},
            )
            continue

    # Phase 3: Final Merge and Column Order
    if not match_results:
        logger.info("No match results found.")
        return

    final_df = pd.merge(df_ties, pd.DataFrame(match_results), on="tieId", how="inner")
    final_df = final_df.rename(columns={"eventName": "tournamentName", "drawName": "draw"})

    cols = [
        "matchType",
        "matchId",
        "date",
        "tournamentId",
        "tournamentName",
        "tournamentCategory",
        "surface",
        "inOrOutdoor",
        "tournamentCountry",
        "roundName",
        "draw",
        "matchOrder",
        "result",
        "resultStatusDesc",
        "winnerId",
        "winnerEntry",
        "winnerSeed",
        "winnerName",
        "winnerCountry",
        "loserId",
        "loserEntry",
        "loserSeed",
        "loserName",
        "loserCountry",
    ]

    # Filter final dataframe to only include the required columns
    final_df = final_df[cols].copy()
    final_df["matchOrder"] = pd.to_numeric(final_df["matchOrder"], errors="coerce").astype("Int64")

    csv_filename = os.path.join(DATA_DIR, "bjkc_matches_arg.csv")
    final_df["_canonical_key"] = final_df.apply(lambda row: source_match_key(row.to_dict(), "bjkc"), axis=1)
    final_df = final_df.drop_duplicates(subset=["_canonical_key"], keep="last")

    # Upsert by the BJKC natural key. Match IDs alone are not guaranteed to be
    # globally unique across ties/seasons.
    if os.path.exists(csv_filename):
        existing_df = pd.read_csv(csv_filename)

        for col in cols:
            if col not in existing_df.columns:
                existing_df[col] = pd.NA
        existing_df = existing_df[cols].copy()
        existing_df["matchOrder"] = pd.to_numeric(existing_df["matchOrder"], errors="coerce").astype("Int64")

        existing_df["_canonical_key"] = existing_df.apply(lambda row: source_match_key(row.to_dict(), "bjkc"), axis=1)
        existing_ids = set(existing_df["_canonical_key"])
        incoming_ids = set(final_df["_canonical_key"])

        inserted_count = len(incoming_ids - existing_ids)
        updated_count = len(incoming_ids & existing_ids)

        combined_df = pd.concat(
            [existing_df[~existing_df["_canonical_key"].isin(incoming_ids)], final_df], ignore_index=True
        )
        combined_df = combined_df.drop(columns=["_canonical_key"])
        combined_df["matchOrder"] = pd.to_numeric(combined_df["matchOrder"], errors="coerce").astype("Int64")
        atomic_write_dataframe(
            combined_df,
            csv_filename,
            index=False,
            quoting=csv.QUOTE_ALL,
            lineterminator="\r\n",
        )

        logger.info(
            f"Upserted {len(final_df)} matches ({inserted_count} inserted, "
            f"{updated_count} refreshed) into '{csv_filename}'."
        )
    else:
        # File doesn't exist, create it and write headers
        atomic_write_dataframe(
            final_df.drop(columns=["_canonical_key"]),
            csv_filename,
            index=False,
            quoting=csv.QUOTE_ALL,
            lineterminator="\r\n",
        )
        logger.info(f"Saved {len(final_df)} rows to a new file '{csv_filename}'.")


if __name__ == "__main__":
    from pipeline_transaction import run_current_script_transaction, transaction_is_active

    if transaction_is_active():
        main()
    else:
        raise SystemExit(run_current_script_transaction(__file__))
