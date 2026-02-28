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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "..", "data")
TOURNAMENT_LINK_PREFIX = "/en/tournament/"

def get_itf_calendar_by_month(target_year):
    chrome_options = Options()
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("window-size=1920,1080")
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36")

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)

    all_tournaments = []
    seen_ids = set()

    months = []
    for month in range(1, 13):
        # monthrange returns (first_day_weekday, number_of_days)
        last_day = calendar.monthrange(target_year, month)[1]
        
        # Cap December at 25th
        if month == 12:
            last_day = 25
        
        start_date = f"{target_year}-{month:02d}-01"
        end_date = f"{target_year}-{month:02d}-{last_day}"
        
        months.append((start_date, end_date))

    try:
        print("Establishing session...")
        driver.get("https://www.itftennis.com/en/tournament-calendar/womens-world-tennis-tour-calendar/")
        time.sleep(5)

        for start_date, end_date in months:
            print(f"Fetching data for {start_date} to {end_date}...")
            
            api_url = (
                f"https://www.itftennis.com/tennis/api/TournamentApi/GetCalendar?"
                f"circuitCode=WT&searchString=&skip=0&take=1000&dateFrom={start_date}&dateTo={end_date}"
                f"&isOrderAscending=true&orderField=startDate"
            )
            
            driver.get(api_url)
            time.sleep(2) 
            
            raw_content = driver.find_element("tag name", "body").text.strip()
            
            if not raw_content:
                continue

            try:
                month_data = json.loads(raw_content)
            except json.JSONDecodeError:
                continue

            if isinstance(month_data, dict):
                month_data = month_data.get('items') or month_data.get('data') or []

            if not isinstance(month_data, list):
                continue

            for tournament in month_data:
                if isinstance(tournament, dict):
                    t_id = tournament.get('tournamentKey')
                    if t_id and t_id not in seen_ids:
                        all_tournaments.append(tournament)
                        seen_ids.add(t_id)

        all_tournaments.sort(key=lambda x: x.get('startDate', ''))
        return all_tournaments

    except Exception as e:
        print(f"An error occurred: {e}")
        return []
    finally:
        driver.quit()

def create_tournament_df(tournament_list):
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
    t_indoor = tourney_meta.get('indoorOrOutDoor', '')
    t_io = 'I' if t_indoor == 'Indoor' else 'O'
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
            r_id = rnd.get("roundNumber")
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
                    w_sd = int(winner['seeding']) if winner.get('seeding') else ""
                    l_en = loser.get('entryStatus') or ""
                    l_sd = int(loser['seeding']) if loser.get('seeding') else ""
                    
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
                        "matchType": "ITF",
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
                    continue
    return rows

if __name__ == "__main__":
    current_year = datetime.now().year
    output_path = os.path.join(DATA_DIR, "itf_matches_arg.csv")

    # Detect years already saved so we can skip them on rerun
    existing_years = set()
    if os.path.exists(output_path):
        try:
            existing_df = pd.read_csv(output_path, usecols=["date"], dtype=str)
            existing_years = set(existing_df["date"].str[:4].dropna().unique())
            print(f"Found existing data for years: {sorted(existing_years)}")
        except Exception:
            pass

    write_header = len(existing_years) == 0

    for year in range(1994, current_year + 1):
        if str(year) in existing_years:
            print(f"SKIPPING YEAR {year} (already in CSV)")
            continue
        print(f"PROCESSING YEAR: {year}")

        print("Step 1: Fetching Calendar...")
        raw_data = get_itf_calendar_by_month(year)

        if not raw_data:
            print(f"No calendar data found for {year}.")
            continue

        tournaments_df = create_tournament_df(raw_data)

        if tournaments_df is None or tournaments_df.empty:
            print(f"DataFrame creation failed for {year}.")
            continue

        print("Step 2: Fetching Tournament IDs...")
        keys_list = tournaments_df["tournamentKey"].dropna().unique().tolist()
        json_ids_string = fetch_itf_ids_to_json(keys_list)

        print("Step 3: Merging Data...")
        final_df = merge_ids_with_pandas(tournaments_df, json_ids_string)
        final_df['tournamentId'] = final_df['tournamentId'].fillna(0).astype(int).astype(str).replace('0', '')

        print(f"Step 4: Fetching Match Details for {len(final_df)} tournaments...")

        all_matches = []
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

            is_multiweek = tCategory == "ITF Womens Multi-Week Circuit"

            if is_multiweek:
                week = 1
                while True:
                    has_data_this_week = False

                    for code in ["Q", "M"]:
                        json_data = fetch_api_data(int(tId), code, week_number=week)

                        if json_data:
                            has_data_this_week = True
                            parsed = parse_drawsheet(json_data, tourney, code, week_offset=(week - 1))
                            all_matches.extend(parsed)
                            print(f"   -> Week {week}, {code}: Found {len(parsed)} ARG matches")

                        time.sleep(0.2)

                    if not has_data_this_week:
                        break

                    week += 1
                    if week > 10:
                        break
            else:
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
            final_matches_df.to_csv(output_path, mode='a', header=write_header, index=False, encoding='utf-8-sig')
            write_header = False
            print(f"\nSUCCESS! Appended {len(final_matches_df)} ARG matches from {year} to:\n{output_path}")
        else:
            print(f"\nFinished {year}, but no ARG matches were found.")

    print(f"\nDone! All years written to {output_path}")
