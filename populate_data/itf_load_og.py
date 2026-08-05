import json
import time
import pandas as pd
import os
import sys
import random
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from datetime import datetime, timedelta
from pathlib import Path

# Allow imports from the project root when invoked as `python populate_data/itf_load_og.py`.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from utils import expand_gs_calendar_cache
from canonical_data import source_match_key, sync_itf_players
from http_client import get_with_retry
from transactional_io import atomic_write_dataframe
from pipeline_errors import DataValidationError, PipelineError
from run_state import report_run_issue

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
from runtime_paths import DATA_DIR as RUNTIME_DATA_DIR

DATA_DIR = str(RUNTIME_DATA_DIR)
TOURNAMENT_LINK_PREFIX = "/en/tournament/"

def create_tournament_df(tournament_list):
    tournament_list = expand_gs_calendar_cache(tournament_list)
    if not tournament_list:
        print("No data provided.")
        return None

    rows = []
    for item in tournament_list:
        link = TOURNAMENT_LINK_PREFIX + item.get("tournamentLink", "")
        t_key = link.rstrip('/').split('/')[-1] if link else None

        rows.append({
            "startDate": item.get("startDate"),
            "tournamentName": item.get("tournamentName"),
            "hostNation": item.get("hostNation"),
            "category": item.get("category"),
            "surfaceDesc": item.get("surfaceDesc"),
            "indoorOrOutDoor": item.get("indoorOrOutDoor"),
            "tournamentKey": t_key
        })

    return pd.DataFrame(rows)

def fetch_itf_ids_to_json(keys_list):
    if not keys_list:
        return "[]"

    chrome_options = Options()
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)

    results = []
    try:
        driver.get("https://www.itftennis.com/en/tournament-calendar/womens-world-tennis-tour-calendar/")
        time.sleep(5)

        for key in keys_list:
            api_url = f"https://www.itftennis.com/tennis/api/TournamentApi/GetEventFilters?tournamentKey={key}"
            print(f"Fetching ID for {key}...")
            last_error = None
            for attempt in range(3):
                try:
                    driver.get(api_url)
                    time.sleep(1 + random.uniform(0.0, 0.5))
                    raw_content = driver.find_element("tag name", "body").text.strip()
                    data = json.loads(raw_content)
                    if data and "tournamentId" in data:
                        results.append({
                            "tournamentKey": key,
                            "tournamentId": data["tournamentId"]
                        })
                        last_error = None
                        break
                    raise ValueError("response did not contain tournamentId")
                except Exception as exc:
                    last_error = exc
                    if attempt < 2:
                        time.sleep((2 ** attempt) + random.uniform(0.0, 0.75))
            if last_error is not None:
                report_run_issue(
                    "olympics-loader", "fetch tournament ID", last_error,
                    severity="partial", context={"tournament_key": str(key)},
                )
    finally:
        driver.quit()

    return json.dumps(results)

def merge_ids_with_pandas(calendar_df, json_ids_string):
    try:
        ids_list = json.loads(json_ids_string)
        ids_df = pd.DataFrame(ids_list)
        final_df = pd.merge(calendar_df, ids_df, on="tournamentKey", how="left")
        return final_df
    except Exception as e:
        raise DataValidationError(
            component="olympics-loader",
            operation="merge tournament IDs",
            message="could not merge Olympics calendar with tournament IDs",
            context={"cause": str(e)},
        ) from e


def fetch_api_data(tId, classification, week_number=0):
    url = "https://www.itftennis.com/tennis/api/TournamentApi/GetDrawsheet"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
        "Referer": f"https://www.itftennis.com/en/tournament/draws-and-results/print/?tournamentId={tId}&circuitCode=WT",
        "Origin": "https://www.itftennis.com",
        "Accept": "application/json, text/plain, */*",
    }
    
    payload = {
        "circuitCode": "WT",
        "eventClassificationCode": classification,
        "matchTypeCode": "S",
        "tourType": "WT",
        "tournamentId": f"{tId}",
        "weekNumber": week_number
    }
    
    try:
        response = get_with_retry(
            url,
            component="olympics-loader",
            params=payload,
            headers=headers,
        )
        return response.json()
    except PipelineError:
        return None
    except Exception as exc:
        report_run_issue("olympics-loader", "parse drawsheet", exc, severity="partial")
        return None

def parse_drawsheet(data, tourney_meta, draw_type, week_offset=0):
    if not data or not isinstance(data, dict): return []
    rows = []
    
    t_id = tourney_meta.get('tournamentId')
    t_name = tourney_meta.get('tournamentName')
    t_cat = tourney_meta.get('category')
    t_surf = tourney_meta.get('surfaceDesc')
    t_indoor = tourney_meta.get('indoorOrOutDoor', '')
    t_io = 'I' if t_indoor == 'Indoor' else 'O'
    t_nation = tourney_meta.get('hostNation')
    
    base_date = tourney_meta.get('startDate')
    
    if base_date and "T" in base_date:
        base_date = base_date.split("T")[0]

    t_date = base_date 

    if base_date and week_offset != 0: 
        try:
            date_obj = datetime.strptime(base_date, '%Y-%m-%d')
            adjusted_date_obj = date_obj + timedelta(days=7 * week_offset)
            t_date = adjusted_date_obj.strftime('%Y-%m-%d')
        except Exception as e:
            raise DataValidationError(
                component="olympics-loader",
                operation="parse tournament date",
                message=f"invalid Olympics tournament date: {base_date}",
                context={"value": str(base_date), "cause": str(e)},
            ) from e

    ko_groups = data.get("koGroups", [])

    q_map = {}
    if draw_type == "Q":
        seen_q = {}
        for group in ko_groups:
            for rnd in group.get("rounds", []):
                rd = rnd.get("roundDesc")
                rn = rnd.get("roundNumber")
                if rd and rd not in seen_q:
                    seen_q[rd] = int(rn) if isinstance(rn, (int, float)) else 9999
        q_map = {rd: f"QR{i + 1}" for i, (rd, _) in enumerate(sorted(seen_q.items(), key=lambda x: x[1]))}

    for group in ko_groups:
        rounds = group.get("rounds", [])
        for rnd in rounds:
            r_ds = rnd.get("roundDesc")
            matches = rnd.get("matches", [])
            for match in matches:
                try:
                    if match.get("playStatusCode") != "PC" and match.get("resultStatusCode") not in ("WO", "BYE"): continue
                    
                    matchId = match.get("matchId")
                    teams = match.get("teams", [])
                    if len(teams) < 2: continue
                    
                    is_winner_0 = str(teams[0].get("isWinner")).lower() == "true"
                    
                    if is_winner_0:
                        winner, loser = teams[0], teams[1]
                    else:
                        winner, loser = teams[1], teams[0]
                    
                    def get_p(t):
                        ps = t.get("players", [])
                        if not ps or not isinstance(ps[0], dict): return "Unknown", "", ""
                        p = ps[0]
                        return p.get('playerId',''), f"{p.get('givenName','')} {p.get('familyName','')}".strip(), p.get('nationality','')

                    w_id, w_n, w_c = get_p(winner)
                    l_id, l_n, l_c = get_p(loser)
                    
                    w_en = winner.get('entryStatus') or ""
                    w_sd = winner.get('seeding') or ""
                    l_en = loser.get('entryStatus') or ""
                    l_sd = loser.get('seeding') or ""
                    
                    # Score Parsing
                    w_s, l_s = winner.get("scores", []), loser.get("scores", [])
                    parts = []
                    for i in range(max(len(w_s), len(l_s))):
                        ws = w_s[i] if i < len(w_s) else {}
                        ls = l_s[i] if i < len(l_s) else {}
                        if isinstance(ws, dict) and isinstance(ls, dict):
                            sc_w = ws.get("score")
                            sc_l = ls.get("score")
                            if sc_w is not None and sc_l is not None:
                                s = f"{sc_w}-{sc_l}"
                                tb = ws.get("losingScore") or ls.get("losingScore")
                                if tb: s += f"({tb})"
                                parts.append(s)
                                
                    res = " ".join(parts)
                    status_desc = match.get("resultStatusDesc", "Completed")
                    if status_desc:
                        if "Retired" in status_desc:
                            res += " ret."
                        elif "Defaulted" in status_desc or "Default" in status_desc:
                            res += " def."

                    if match.get("resultStatusCode") == "BYE":
                        res = "-"
                        status_desc = "Bye"
                        l_n = "Bye"
                        l_c = "-"
                    elif match.get("resultStatusCode") == "WO":
                        res = "W/O"
                        status_desc = "Walkover"
                    elif not any(char.isdigit() for char in res):
                        res = ""
                        status_desc = "Walkover"

                    if w_c != "ARG" and l_c != "ARG":
                        continue

                    rows.append({
                        "matchType": "OG",
                        "matchId": matchId,
                        "date": t_date,
                        "tournamentId": t_id,
                        "tournamentName": t_name,
                        "tournamentCategory": t_cat,
                        "surface": t_surf,
                        "inOrOutdoor": t_io,
                        "tournamentCountry": t_nation,
                        "roundName": q_map.get(r_ds, r_ds) if draw_type == "Q" else r_ds,
                        "draw": draw_type,
                        "result": res,
                        "resultStatusDesc": status_desc,
                        "winnerId": w_id,
                        "winnerEntry": w_en,
                        "winnerSeed": w_sd,
                        "winnerName": w_n,
                        "winnerCountry": w_c,
                        "loserId": l_id,
                        "loserEntry": l_en,
                        "loserSeed": l_sd,
                        "loserName": l_n,
                        "loserCountry": l_c
                    })
                except Exception as e:
                    report_run_issue(
                        "olympics-loader", "parse drawsheet match", e,
                        severity="partial",
                        context={
                            "tournament_id": str(t_id),
                            "draw": str(draw_type),
                            "match_id": str(match.get("matchId") or ""),
                        },
                    )
                    continue
    return rows

if __name__ == "__main__":
    from pipeline_transaction import run_current_script_transaction, transaction_is_active

    if not transaction_is_active():
        raise SystemExit(run_current_script_transaction(__file__))

    gs_files = ['olympic_games.json']
    all_matches = []

    for file_name in gs_files:

        with open(os.path.join(DATA_DIR, file_name), 'r', encoding='utf-8') as f:
            raw_data = json.load(f)

        tournaments_df = create_tournament_df(raw_data)

        if tournaments_df is None or tournaments_df.empty:
            print(f"DataFrame creation failed for {file_name}.")
            continue

        print("Step 2: Fetching Tournament IDs...")
        keys_list = tournaments_df["tournamentKey"].dropna().unique().tolist()
        json_ids_string = fetch_itf_ids_to_json(keys_list)

        print("Step 3: Merging Data...")
        final_df = merge_ids_with_pandas(tournaments_df, json_ids_string)
        final_df['tournamentId'] = final_df['tournamentId'].fillna(0).astype(int).astype(str).replace('0', '')

        print(f"Step 4: Fetching Match Details for {len(final_df)} tournaments...")

        tournaments_list = final_df.to_dict('records')

        for tourney in tournaments_list:
            tId = tourney.get("tournamentId")
            tName = tourney.get("tournamentName")
            tCategory = tourney.get("category", "")

            if tCategory and str(tCategory).strip().startswith("Tier"):
                print(f"Skipping {tName} (Excluded Category: {tCategory})")
                continue

            if not tId or pd.isna(tId):
                print(f"Skipping {tName} (No ID found)")
                continue

            print(f"Processing: {tName} (ID: {int(tId)})")

            for code in ["Q", "M"]:
                json_data = fetch_api_data(int(tId), code, week_number=0)

                if json_data:
                    offset = -1 if code == "Q" else 0
                    parsed = parse_drawsheet(json_data, tourney, code, week_offset=offset)
                    all_matches.extend(parsed)
                    print(f"   -> {code}: Found {len(parsed)} ARG matches")

                time.sleep(0.2)

            time.sleep(0.5)

    if all_matches:
        added_players = sync_itf_players(
            Path(DATA_DIR) / "player_aliases_wta_itf.json", all_matches
        )
        if added_players:
            print(f"Added {added_players} new ITF identities to the canonical player table.")
        final_matches_df = pd.DataFrame(all_matches)

        file_path = os.path.join(DATA_DIR, "og_matches_arg.csv")

        final_matches_df['_canonical_key'] = final_matches_df.apply(
            lambda row: source_match_key(row.to_dict(), "olympics"), axis=1
        )
        final_matches_df = final_matches_df.drop_duplicates(
            subset=['_canonical_key'], keep='last'
        ).drop(columns=['_canonical_key'])
        atomic_write_dataframe(final_matches_df, file_path, index=False, encoding='utf-8-sig')
        print(f"\nSUCCESS! Saved {len(final_matches_df)} ARG matches to:\n{file_path}")
    else:
        print(f"\nFinished processing files, but no ARG matches were found.")
