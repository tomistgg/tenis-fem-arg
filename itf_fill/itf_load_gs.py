import json
import time
import pandas as pd
import os
import requests
import calendar
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from datetime import datetime, timedelta

def create_tournament_df(tournament_list):
    if not tournament_list:
        print("No data provided.")
        return None

    rows = []
    for item in tournament_list:
        link = item.get("tournamentLink", "")
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
            driver.get(api_url)
            time.sleep(1) 
            
            try:
                raw_content = driver.find_element("tag name", "body").text.strip()
                data = json.loads(raw_content)
                
                if data and "tournamentId" in data:
                    results.append({
                        "tournamentKey": key,
                        "tournamentId": data["tournamentId"]
                    })
            except Exception as e:
                print(f"Failed for {key}: {e}")
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
        print(f"Error merging DataFrames: {e}")
        return calendar_df


def fetch_api_data(tId, classification, week_number=0):
    url = "https://www.itftennis.com/tennis/api/TournamentApi/GetDrawsheet"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
        "Referer": f"https://www.itftennis.com/en/tournament/draws-and-results/print/?tournamentId={tId}&circuitCode=WT",
        "Origin": "https://www.itftennis.com",
        "Content-Type": "application/json"
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
        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        return None

def parse_drawsheet(data, tourney_meta, draw_type, week_offset=0):
    if not data or not isinstance(data, dict): return []
    rows = []
    
    t_id = tourney_meta.get('tournamentId')
    t_name = tourney_meta.get('tournamentName')
    t_cat = tourney_meta.get('category')
    t_surf = tourney_meta.get('surfaceDesc')
    t_nation = tourney_meta.get('hostNation')
    
    base_date = tourney_meta.get('startDate')
    
    if base_date and "T" in base_date:
        base_date = base_date.split("T")[0]

    t_date = base_date 

    if base_date and week_offset > 0:
        try:
            date_obj = datetime.strptime(base_date, '%Y-%m-%d')
            adjusted_date_obj = date_obj + timedelta(days=7 * week_offset)
            t_date = adjusted_date_obj.strftime('%Y-%m-%d')
        except Exception as e:
            print(f"Date parsing failed for {base_date}: {e}")
            t_date = base_date

    ko_groups = data.get("koGroups", [])
    for group in ko_groups:
        rounds = group.get("rounds", [])
        for rnd in rounds:
            r_id = rnd.get("roundNumber")
            r_ds = rnd.get("roundDesc")
            matches = rnd.get("matches", [])
            for match in matches:
                try:
                    if match.get("playStatusCode") != "PC": continue
                    
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

                    if not any(char.isdigit() for char in res):
                        res = ""
                        status_desc = "Walkover"

                    if w_c != "ARG" and l_c != "ARG":
                        continue

                    rows.append({
                        "matchType": "GS",
                        "matchId": matchId,
                        "date": t_date,
                        "tournamentId": t_id,
                        "tournamentName": t_name,
                        "tournamentCategory": t_cat,
                        "surface": t_surf,
                        "tournamentCountry": t_nation,
                        "roundDesc": r_ds,
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
                    continue
    return rows

if __name__ == "__main__":
    gs_files = ['australian_open.json', 'roland_garros.json', 'wimbledon.json', 'us_open.json']
    all_matches = []

    for f in gs_files:

        with open(f, 'r', encoding='utf-8') as f:
            raw_data = json.load(f)

        tournaments_df = create_tournament_df(raw_data)

        if tournaments_df is None or tournaments_df.empty:
            print(f"DataFrame creation failed for {year}.")
            raise SystemExit(0)

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
                    parsed = parse_drawsheet(json_data, tourney, code, week_offset=0)
                    all_matches.extend(parsed)
                    print(f"   -> {code}: Found {len(parsed)} ARG matches")

                time.sleep(0.2)

            time.sleep(0.5)

    if all_matches:
        final_matches_df = pd.DataFrame(all_matches)

        script_dir = os.path.dirname(os.path.abspath(__file__))
        file_path = os.path.join(script_dir, f"gs_matches_arg.csv")

        final_matches_df.to_csv(file_path, index=False, encoding='utf-8-sig')
        print(f"\nSUCCESS! Saved {len(final_matches_df)} ARG matches to:\n{file_path}")
    else:
        print(f"\nFinished {year}, but no ARG matches were found.")
