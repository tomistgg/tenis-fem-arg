import requests
import json
from datetime import datetime
import os
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "..", "data")

# --- CONFIGURATION ---
START_YEAR = datetime.now().year
END_YEAR = datetime.now().year
SERIES_BASE_URL = "https://api.itf-production.sports-data.stadion.io/custom/wcotDrawsModeled/bjkc/"
TIE_BASE_URL = "https://api.itf-production.sports-data.stadion.io/custom/tieCentre/"

HEADERS = {
    "accept": "*/*",
    "referer": "https://www.billiejeankingcup.com/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
}

def get_score_string(side1_sets, side2_sets, winner_is_s1):
    if not side1_sets or not side2_sets: return ""
    res_parts = []
    s1_sorted = sorted(side1_sets, key=lambda x: x.get('setNumber', 0))
    s2_sorted = sorted(side2_sets, key=lambda x: x.get('setNumber', 0))
    for s1, s2 in zip(s1_sorted, s2_sorted):
        s1_s, s2_s = s1.get('setScore', 0), s2.get('setScore', 0)
        tb1, tb2 = s1.get('setTieBreakScore', 0), s2.get('setTieBreakScore', 0)
        if winner_is_s1:
            part = f"{s1_s}-{s2_s}"
            if s1_s == 7 and s2_s == 6: part += f"({tb2})"
            elif s1_s == 6 and s2_s == 7: part += f"({tb1})"
        else:
            part = f"{s2_s}-{s1_s}"
            if s2_s == 7 and s1_s == 6: part += f"({tb1})"
            elif s2_s == 6 and s1_s == 7: part += f"({tb2})"
        res_parts.append(part)
    return " ".join(res_parts)

def check_nation(nation_obj, target_country="Argentina", target_iso="ARG"):
    """Helper to check a single nation object."""
    if not nation_obj: return False
    return nation_obj.get('nation') == target_country or nation_obj.get('nationISO') == target_iso

def is_target_involved(content, target_country="Argentina", target_iso="ARG"):
    """
    Scans the entire content block of a draw to see if Argentina is involved
    in either the participants list (tables) or specific ties.
    """
    # 1. Check participants in Pools/Round Robin tables
    if isinstance(content, dict) and 'tables' in content:
        for entry in content['tables']:
            country = entry.get('country', {})
            if country.get('name') == target_country or country.get('ISOcode') == target_iso:
                return True
    
    # 2. Check specific ties in Pools 'recent' list
    if isinstance(content, dict) and 'recent' in content:
        for tie in content['recent']:
            if check_nation(tie.get('homeNation')) or check_nation(tie.get('awayNation')):
                return True
                
    # 3. Check specific ties in Tree structures (List of rounds)
    if isinstance(content, list):
        for round_item in content:
            for tie in round_item.get('ties', []):
                if check_nation(tie.get('homeNation')) or check_nation(tie.get('awayNation')):
                    return True
                    
    return False

def main():
    all_ties = []
    print(f"--- Phase 1: Fetching Draws {START_YEAR} to {END_YEAR} (Argentina Filter) ---")
    
    for year in range(START_YEAR, END_YEAR + 1):
        print(f"Processing Year: {year}...", end="\r")
        try:
            response = requests.get(f"{SERIES_BASE_URL}{year}", headers=HEADERS, timeout=15)
            if response.status_code != 200: continue
            
            data = response.json().get('data', [])
            for block in data:
                for event in block.get('events', []):
                    base_info = {"year": year, "eventName": event.get('name')}
                    for draw in event.get('draws', []):
                        content = draw.get('content')
                        if isinstance(content, str):
                            content = content.strip()
                            if not content: continue
                            content = json.loads(content)
                        
                        if not content: continue

                        # Comprehensive check: If Argentina is anywhere in this draw's content
                        if is_target_involved(content):
                            draw_info = {**base_info, "drawName": draw.get('name'), "drawId": draw.get('id')}
                            
                            # Extract all ties from this specific draw
                            if isinstance(content, list): # Tree
                                for r in content:
                                    for tie in r.get('ties', []):
                                        if check_nation(tie.get('homeNation')) or check_nation(tie.get('awayNation')):
                                            all_ties.append({**draw_info, "tieId": tie.get('id'), "roundName": r.get('name')})
                            elif isinstance(content, dict): # Pool
                                for tie in content.get('recent', []):
                                    if check_nation(tie.get('homeNation')) or check_nation(tie.get('awayNation')):
                                        all_ties.append({**draw_info, "tieId": tie.get('id'), "roundName": tie.get('round')})
        except Exception as e:
            print(f"\nError fetching {year}: {e}")

    if not all_ties:
        print("\nNo ties found for Argentina.")
        return

    df_ties = pd.DataFrame(all_ties).drop_duplicates(subset=['tieId'])
    unique_ids = df_ties['tieId'].dropna().unique().tolist()
    
    print(f"\n--- Phase 2: Fetching Matches for {len(unique_ids)} Argentina Ties ---")
    match_results = []
    for i, tid in enumerate(unique_ids):
        print(f"Ties: {i+1}/{len(unique_ids)}", end="\r")
        try:
            r = requests.get(f"{TIE_BASE_URL}{tid}", headers=HEADERS, timeout=15)
            if r.status_code != 200: continue
            tie_data = r.json().get('data', {}).get('tie', {})
            
            raw_date = tie_data.get('endDate', '')
            if not raw_date:
                raw_date = tie_data.get('startDate', '')
                
            formatted_date = raw_date.split('T')[0] if raw_date else ""

            venue_country = tie_data.get('venue', {}).get('country', {}).get('name', '')
            surface = tie_data.get('surfaceFriendlyName', '')

            for m in tie_data.get('matches', []):
                sides = m.get('sides', [])
                if len(sides) < 2: continue
                w_id = m.get('winnerSideId')
                s1, s2 = sides[0], sides[1]
                is_s1 = s1.get('id') == w_id
                win, los = (s1, s2) if is_s1 else (s2, s1)
                
                def get_p(side): return " / ".join([p.get('player', {}).get('_admin_name', '') for p in side.get('sidePlayer', []) if p.get('player')])
                def get_c(side): return side.get('sidePlayer', [{}])[0].get('player', {}).get('person', {}).get('country', {}).get('ISOcode')

                match_results.append({
                    "tieId": tid, "matchType": "Fed/BJK Cup", "matchId": m.get('id'), "date": formatted_date,
                    "tournamentId": tie_data.get('_name'), "tournamentCategory": "Fed/BJK Cup",
                    "surface": surface, "tournamentCountry": venue_country, "resultStatusDesc": m.get('resultStatusDesc', ''),
                    "result": get_score_string(s1.get('sideSets'), s2.get('sideSets'), is_s1),
                    "winnerId": win.get('id'), "winnerEntry": "", "winnerSeed": "", "winnerName": get_p(win), "winnerCountry": get_c(win),
                    "loserId": los.get('id'), "loserEntry": "", "loserSeed": "", "loserName": get_p(los), "loserCountry": get_c(los)
                })
        except: continue

    # Phase 3: Final Merge and Column Order
    if not match_results:
        print("\nNo match results found.")
        return

    final_df = pd.merge(df_ties, pd.DataFrame(match_results), on="tieId", how="inner")
    final_df = final_df.rename(columns={"eventName": "tournamentName", "drawName": "draw"})
    
    cols = ["matchType", "matchId", "date", "tournamentId", "tournamentName", "tournamentCategory", "surface", 
            "tournamentCountry", "roundName", "draw", "result", "resultStatusDesc", "winnerId", "winnerEntry", 
            "winnerSeed", "winnerName", "winnerCountry", "loserId", "loserEntry", "loserSeed", "loserName", "loserCountry"]
    
    # Filter final dataframe to only include the required columns
    final_df = final_df[cols]
    
    csv_filename = os.path.join(DATA_DIR, "bjkc_matches_arg.csv")
    
    # Check if file exists to only append new records
    if os.path.exists(csv_filename):
        existing_df = pd.read_csv(csv_filename)
        
        # Filter final_df to only include matchIds that are not in the existing CSV
        new_matches = final_df[~final_df['matchId'].isin(existing_df['matchId'])]
        
        if not new_matches.empty:
            # Append without writing the header again
            new_matches.to_csv(csv_filename, mode='a', header=False, index=False)
            print(f"\nDone! Appended {len(new_matches)} new matches to '{csv_filename}'.")
        else:
            print(f"\nDone! No new matches found. '{csv_filename}' is already up to date.")
    else:
        # File doesn't exist, create it and write headers
        final_df.to_csv(csv_filename, index=False)
        print(f"\nDone! Saved {len(final_df)} rows to a new file '{csv_filename}'.")

if __name__ == "__main__":
    main()
