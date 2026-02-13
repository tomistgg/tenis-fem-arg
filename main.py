import requests
import time
import json
import random
import pandas as pd
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
import os
import unicodedata

def format_player_name(text):
    if not text:
        return ""
    return text.title()

def fix_encoding(text):
    """Fix encoding issues and normalize special characters"""
    if not text:
        return ""

    # Try to fix mojibake (common UTF-8 misinterpreted as Latin-1)
    try:
        # If text contains mojibake, this will fix it
        if 'Ã' in text or 'Ã¡' in text or 'Ã©' in text or 'Ã­' in text or 'Ã³' in text or 'Ãº' in text:
            text = text.encode('latin-1').decode('utf-8')
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass

    # Normalize Unicode characters (remove accents)
    try:
        nfkd_form = unicodedata.normalize('NFKD', text)
        text_without_accents = "".join([c for c in nfkd_form if not unicodedata.combining(c)])
        return text_without_accents
    except:
        return text

def load_player_mapping(filename="player_aliases.json"):
    if not os.path.exists(filename):
        print(f"Alerta: No se encontró {filename}.")
        return {}
    with open(filename, "r", encoding="utf-8") as f:
        return json.load(f)

PLAYER_MAPPING = load_player_mapping()

NAME_LOOKUP = {}
for display_name, aliases in PLAYER_MAPPING.items():
    for alias in aliases:
        NAME_LOOKUP[alias.strip().upper()] = display_name.upper()

WTA_CACHE_FILE = "wta_rankings_cache.json"
ITF_CACHE_FILE = "itf_rankings_cache.json"
ENTRY_LISTS_CACHE_FILE = "entry_lists_cache.json"

def load_cache(cache_file):
    """Load rankings cache from JSON file"""
    if os.path.exists(cache_file):
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_cache(cache_file, cache_data):
    """Save rankings cache to JSON file"""
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(cache_data, f, indent=2, ensure_ascii=False)

def merge_entry_list(cached_players, new_players):
    """Merge new scraped players with cached players, preserving sections that disappeared."""
    new_main = [p for p in new_players if p.get("type") == "MAIN"]
    new_qual = [p for p in new_players if p.get("type") == "QUAL"]
    cached_main = [p for p in cached_players if p.get("type") == "MAIN"]
    cached_qual = [p for p in cached_players if p.get("type") == "QUAL"]

    final_main = new_main if new_main else cached_main
    final_qual = new_qual if new_qual else cached_qual
    return final_main + final_qual

def get_cached_rankings(date_str, cache_file, fetch_func, nationality=None):
    """
    Get rankings from cache or fetch if needed.
    """
    cache = load_cache(cache_file)
    
    if date_str in cache:
        return cache[date_str]
    
    new_data = fetch_func(date_str, nationality=nationality)
    if new_data:
        cache[date_str] = new_data
        save_cache(cache_file, cache)
    return new_data


def get_next_monday():
    today = datetime.now()
    days_until_monday = (7 - today.weekday()) % 7
    if days_until_monday == 0:
        days_until_monday = 7
    next_mon = today + timedelta(days=days_until_monday)
    return next_mon.replace(hour=0, minute=0, second=0, microsecond=0)

def get_monday_from_date(date_str):
    date = datetime.strptime(date_str, "%Y-%m-%d")
    weekday = date.weekday()
    
    if weekday >= 5:
        days_until_monday = 7 - weekday
        monday = date + timedelta(days=days_until_monday)
    else:
        days_since_monday = weekday
        monday = date - timedelta(days=days_since_monday)
    
    return monday

def format_week_label(monday_date):
    months_en = {
        1: "January", 2: "February", 3: "March", 4: "April",
        5: "May", 6: "June", 7: "July", 8: "August",
        9: "September", 10: "October", 11: "November", 12: "December"
    }
    return f"Week of {months_en[monday_date.month]} {monday_date.day}"

def build_tournament_groups():
    next_monday = get_next_monday()
    four_weeks_later = next_monday + timedelta(weeks=4)
    
    from_date = (next_monday - timedelta(days=7)).strftime("%Y-%m-%d")
    to_date = four_weeks_later.strftime("%Y-%m-%d")
    
    url = "https://api.wtatennis.com/tennis/tournaments/"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
        "referer": "https://www.wtatennis.com/",
        "account": "wta"
    }
    
    params = {
        "page": 0,
        "pageSize": 30,
        "excludeLevels": "ITF",
        "from": from_date,
        "to": to_date
    }
    
    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        data = response.json()
    except Exception as e:
        print(f"Error fetching tournaments: {e}")
        return {}
    
    tournament_groups = {}
    
    for tournament in data.get("content", []):
        tournament_id = tournament["tournamentGroup"]["id"]
        raw_name = tournament["tournamentGroup"]["name"]
        
        nfkd_form = unicodedata.normalize('NFKD', raw_name)
        clean_name = "".join([c for c in nfkd_form if not unicodedata.combining(c)])

        suffix = ""
        if "#" in clean_name:
            parts = clean_name.split("#")
            clean_name = parts[0].strip()
            suffix = " " + parts[1].strip()
        
        name = clean_name.lower().replace(" ", "-").replace("'", "-")
        if suffix:
            name += "-" + suffix.strip()
        
        year = tournament["year"]
        level = tournament["level"]
        city = tournament["city"].title()
        start_date = tournament["startDate"]
        
        monday = get_monday_from_date(start_date)
        
        if not (next_monday <= monday < four_weeks_later):
            continue
        
        week_label = format_week_label(monday)
        
        url = f"https://www.wtatennis.com/tournaments/{tournament_id}/{name}/{year}/player-list"
        display_name = f"{level} {city}{suffix}"
        
        if week_label not in tournament_groups:
            tournament_groups[week_label] = {}
        
        # Store tournament with its level for sorting later
        tournament_groups[week_label][url] = {
            "name": display_name,
            "level": level
        }
    
    return tournament_groups

TOURNAMENT_GROUPS = build_tournament_groups()

API_URL = "https://api.wtatennis.com/tennis/players/ranked"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"}

def get_monday_offset(date_str, weeks_back):
    dt = pd.to_datetime(date_str)
    monday = dt - timedelta(days=dt.weekday())
    return (monday - timedelta(weeks=weeks_back)).strftime('%Y-%m-%d')

def get_itf_players(tournament_key, driver):
    url = f"https://www.itftennis.com/tennis/api/TournamentApi/GetAcceptanceList?tournamentKey={tournament_key}&circuitCode=WT"
    try:
        driver.get(url)
        time.sleep(random.uniform(4, 6))
        raw_content = driver.find_element("tag name", "body").text
        start = raw_content.find('[')
        end = raw_content.rfind(']') + 1
        if start == -1: return [], {}
        
        data = json.loads(raw_content[start:end])
        
        root_data = data[0].get("entryClassifications", []) if data else []
        
        name_map = {}
        for classification in root_data:
            desc = classification.get("entryClassification", "").upper()
            code = classification.get("entryClassificationCode", "")
            if "WITHDRAWAL" in desc: continue 
            
            for entry in classification.get("entries") or []:
                pos = entry.get("positionDisplay", "")
                suffix = "" if code == "MDA" else (f" (ALT {pos})" if code == "ALT" or "ALTERNATE" in desc else " (Q)")
                players = entry.get("players") or []
                for p in players:
                    full_name = f"{p.get('givenName', '')} {p.get('familyName', '')}".strip().upper()
                    matched_name = NAME_LOOKUP.get(full_name, full_name)
                    name_map[matched_name] = suffix
                        
        return root_data, name_map 
    except Exception as e:
        print(f"Error en {tournament_key}: {e}")
        return [], {}

    final_list = {}
    if not isinstance(data, list): return {}
    for item in data:
        if not isinstance(item, dict): continue
        for classification in item.get("entryClassifications") or []:
            desc = classification.get("entryClassification", "").upper()
            code = classification.get("entryClassificationCode", "")
            if "WITHDRAWAL" in desc: break 
            for entry in classification.get("entries") or []:
                pos = entry.get("positionDisplay", "")
                suffix = "" if code == "MDA" else (f" (ALT {pos})" if code == "ALT" or "ALTERNATE" in desc else " (Q)")
                players = entry.get("players")
                if not players or not isinstance(players, list): continue
                for player in players:
                    full_name = f"{player.get('givenName', '')} {player.get('familyName', '')}".strip().upper()
                    if full_name:
                        matched_name = NAME_LOOKUP.get(full_name, full_name)
                        final_list[matched_name] = suffix
    return final_list

def get_dynamic_itf_calendar(driver, num_weeks=3):
    """
    Fetch ITF calendar dynamically for the next N weeks
    """
    try:
        next_monday = get_next_monday()
        date_from = next_monday.strftime("%Y-%m-%d")
        date_to = (next_monday + timedelta(weeks=num_weeks)).strftime("%Y-%m-%d")
        
        url = f"https://www.itftennis.com/tennis/api/TournamentApi/GetCalendar?circuitCode=WT&dateFrom={date_from}&dateTo={date_to}&skip=0&take=500"
        driver.get(url)
        time.sleep(5)
        raw_content = driver.find_element("tag name", "body").text
        data = json.loads(raw_content)
        return data.get('items', [])
    except Exception as e:
        print(f"Error calendario: {e}")
        return []

def get_rankings(date_str, nationality=None):
    all_players, page = [], 0
    while True:
        params = {
            "page": page,
            "pageSize": 100,
            "type": "rankSingles",
            "sort": "asc",
            "metric": "SINGLES",
            "at": date_str
        }
        
        if nationality:
            params["nationality"] = nationality

        try:
            r = requests.get(API_URL, params=params, headers=HEADERS, timeout=10)
            data = r.json()
            items = data.get('content', []) if isinstance(data, dict) else data
            if not items: break
            all_players.extend(items)
            page += 1
            time.sleep(0.1)
        except: break
    
    ranking_results = []
    for p in all_players:
        if not p.get('player'): continue
        wta_name = p.get('player', {}).get('fullName').strip().upper()
        display_name = NAME_LOOKUP.get(wta_name, wta_name)
        ranking_results.append({
            "Player": display_name, 
            "Rank": p.get('ranking'), 
            "Country": p.get('player', {}).get('countryCode', ''), 
            "Key": display_name 
        })
    return ranking_results

def get_itf_rankings(nationality="ARG"):
    all_players = []
    skip = 0
    take = 50
    
    while True:
        url = "https://www.itftennis.com/tennis/api/PlayerRankApi/GetPlayerRankings"
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.itftennis.com/en/rankings/",
            "Sec-Ch-Ua": '"Not(A:Brand";v="8", "Chromium";v="144", "Google Chrome";v="144"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin"
        }
        
        params = {
            "circuitCode": "WT",
            "matchTypeCode": "S",
            "ageCategoryCode": "",
            "nationCode": nationality,
            "take": take,
            "skip": skip,
            "isOrderAscending": "true"
        }
        
        try:
            r = requests.get(url, headers=headers, params=params, timeout=10)
            data = r.json()
            items = data.get('items', []) if isinstance(data, dict) else []
            if not items: break
            all_players.extend(items)
            
            total_items = data.get("totalItems", 0)
            if skip + take >= total_items: break
            
            skip += take
            time.sleep(0.1)
        except:
            break
    
    ranking_results = []
    for p in all_players:
        if not p.get('playerId'): continue
        itf_name = f"{p.get('playerGivenName', '')} {p.get('playerFamilyName', '')}".strip().upper()
        display_name = NAME_LOOKUP.get(itf_name, itf_name)
        ranking_results.append({
            "Player": display_name,
            "Rank": f"ITF {p.get('rank')}",
            "Country": p.get('playerNationalityCode', ''),
            "Key": display_name
        })
    return ranking_results


def get_wta_rankings_cached(date_str, nationality=None):
    """Get WTA rankings with caching"""
    return get_cached_rankings(
        date_str,
        WTA_CACHE_FILE,
        get_rankings,
        nationality=nationality
    )

def get_itf_rankings_cached(date_str, nationality="ARG"):
    """Get ITF rankings with caching"""
    return get_cached_rankings(
        date_str,
        ITF_CACHE_FILE,
        lambda d, **kw: get_itf_rankings(nationality=kw.get('nationality', 'ARG')),
        nationality=nationality
    )

def scrape_tournament_players(url, md_rankings, qual_rankings):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        soup = BeautifulSoup(r.text, 'html.parser')
    except Exception as e:
        print(f"Error scraping {url}: {e}")
        return [], {}
    
    main_draw_names = set()
    qualifying_names = set()
    
    current_state = "MAIN" 
    
    all_elements = soup.find_all(['div', 'span', 'button', 'a'], recursive=True)
    
    for tag in all_elements:
        text = tag.get_text().strip()
        ui_tab = tag.get('data-ui-tab', '').lower()
        
        if "qualifying" in text.lower() or "qualifying" in ui_tab:
            current_state = "QUAL"
        elif "main draw" in text.lower() or "main draw" in ui_tab:
            current_state = "MAIN"
        elif "doubles" in text.lower() or "doubles" in ui_tab:
            current_state = "IGNORE"
            
        p_name = tag.get('data-tracking-player-name')
        if p_name:
            name_key = p_name.strip().upper()
            matched_name = NAME_LOOKUP.get(name_key, name_key)
            
            if current_state == "MAIN":
                main_draw_names.add(matched_name)
            elif current_state == "QUAL":
                qualifying_names.add(matched_name)

    def get_p_rank(name, rank_list):
        return next((item for item in rank_list if item["Player"] == name), {"Rank": 9999, "Country": "-"})

    md_list = []
    for name in main_draw_names:
        info = get_p_rank(name, md_rankings)
        md_list.append({
            "name": format_player_name(name),
            "country": info["Country"],
            "rank_num": info["Rank"],
            "rank": f"{info['Rank']}" if info['Rank'] < 9999 else "-",
            "type": "MAIN"
        })
    md_list.sort(key=lambda x: x["rank_num"])
    for idx, p in enumerate(md_list, 1):
        p["pos"] = str(idx)

    qual_list = []
    for name in qualifying_names:
        info = get_p_rank(name, qual_rankings)
        qual_list.append({
            "name": format_player_name(name),
            "country": info["Country"],
            "rank_num": info["Rank"],
            "rank": f"{info['Rank']}" if info['Rank'] < 9999 else "-",
            "type": "QUAL"
        })
    qual_list.sort(key=lambda x: x["rank_num"])
    for idx, p in enumerate(qual_list, 1):
        p["pos"] = str(idx)

    final_tourney_list = md_list + qual_list
    
    suffix_map = {p: "" for p in main_draw_names}
    suffix_map.update({p: " (Q)" for p in qualifying_names})
    
    return final_tourney_list, suffix_map

def get_tournament_sort_order(level):
    level_order = {
        "WTA1000": 1, "WTA 1000": 1,
        "WTA500": 2, "WTA 500": 2,
        "WTA250": 3, "WTA 250": 3,
        "WTA125": 4, "WTA 125": 4,
        "W100": 5, "W75": 6, "W60": 7,
        "W50": 8, "W35": 9, "W25": 10, "W15": 11
    }
    return level_order.get(level, 99)

def generate_dynamic_monday_map(num_weeks=4):
    next_monday = get_next_monday()
    monday_map = {}
    
    for week_offset in range(num_weeks):
        monday = next_monday + timedelta(weeks=week_offset)
        monday_str = monday.strftime("%Y-%m-%d")
        week_label = format_week_label(monday)
        monday_map[monday_str] = week_label
    
    return monday_map

def get_sheety_matches():
    """Fetch match history from Sheety API"""
    url = "https://api.sheety.co/6db57031b06f3dea3029e25e8bc924b9/wtaMatches/matches"
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        if 'matches' in data:
            return data['matches']
        return []
    except Exception as e:
        print(f"Error fetching matches from Sheety: {e}")
        return []

def main():
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)

    try:
        monday_map = generate_dynamic_monday_map(num_weeks=4)
        itf_monday_map = generate_dynamic_monday_map(num_weeks=3)
        
        # 1. Fetch Calendar & Tournaments
        itf_items = get_dynamic_itf_calendar(driver, num_weeks=3)

        for label in monday_map.values():
            if label not in TOURNAMENT_GROUPS:
                TOURNAMENT_GROUPS[label] = {}

        for item in itf_items:
            s_date = pd.to_datetime(item['startDate'])
            monday_date = (s_date - timedelta(days=s_date.weekday())).strftime('%Y-%m-%d')
            if monday_date in itf_monday_map:
                week_label = itf_monday_map[monday_date]
                t_name = item['tournamentName']
                level = "W15"
                if "W100" in t_name or "100k" in t_name: level = "W100"
                elif "W75" in t_name or "75k" in t_name: level = "W75"
                elif "W50" in t_name or "50k" in t_name: level = "W50"
                elif "W35" in t_name or "35k" in t_name: level = "W35"
                elif "W15" in t_name or "15k" in t_name: level = "W15"
                
                TOURNAMENT_GROUPS[week_label][item['tournamentKey'].lower()] = {
                    "name": t_name,
                    "level": level
                }     
        
        # 2. Fetch Players & Rankings
        today = datetime.now()
        ranking_monday = (today - timedelta(days=today.weekday())).strftime("%Y-%m-%d")
        all_wta_players = get_wta_rankings_cached(ranking_monday, nationality=None)
        
        wta_players_arg = [p for p in all_wta_players if p['Country'] == 'ARG']
        itf_players_arg = get_itf_rankings_cached(ranking_monday, nationality="ARG")
        
        wta_names_arg = {p['Player'] for p in wta_players_arg}
        itf_only_arg = [p for p in itf_players_arg if p['Player'] not in wta_names_arg]
        
        players_data = wta_players_arg + itf_only_arg
        arg_names_set = {p['Player'] for p in players_data}

        # 3. Process Tournaments (WTA & ITF)
        schedule_map = {}
        tournament_store = {}
        ranking_cache = {}
        entry_cache = load_cache(ENTRY_LISTS_CACHE_FILE)

        for week, tourneys in TOURNAMENT_GROUPS.items():
            print(f"Procesando {week}...")
            week_monday = next((k for k, v in monday_map.items() if v == week), None)
            if week_monday is None: continue
            
            md_date = get_monday_offset(week_monday, 4)
            q_date = get_monday_offset(week_monday, 3)
            
            today_date = datetime.now()
            md_datetime = datetime.strptime(md_date, "%Y-%m-%d")
            q_datetime = datetime.strptime(q_date, "%Y-%m-%d")
            
            temp_wta_results = {}
            has_wta_players = False
            
            # Pre-scan WTA
            for key, t_info in tourneys.items():
                if key.startswith("http"):
                    t_list, status_dict = scrape_tournament_players(key, [], [])
                    temp_wta_results[key] = (t_list, status_dict)
                    if t_list and len(t_list) > 0: has_wta_players = True
            
            # Process WTA with Rankings
            if has_wta_players:
                if md_datetime > today_date: md_date = (today_date - timedelta(days=today_date.weekday())).strftime("%Y-%m-%d")
                if q_datetime > today_date: q_date = (today_date - timedelta(days=today_date.weekday())).strftime("%Y-%m-%d")

                if md_date not in ranking_cache: ranking_cache[md_date] = get_wta_rankings_cached(md_date, nationality=None)
                if q_date not in ranking_cache: ranking_cache[q_date] = get_wta_rankings_cached(q_date, nationality=None)
                
                for key, t_info in tourneys.items():
                    t_name = t_info["name"]
                    if key.startswith("http"):
                        t_list, status_dict = scrape_tournament_players(key, ranking_cache[md_date], ranking_cache[q_date])
                        t_list = merge_entry_list(entry_cache.get(key, []), t_list)
                        entry_cache[key] = t_list
                        tournament_store[key] = t_list
                        for p_name, suffix in status_dict.items():
                            p_key = p_name.upper()
                            if p_key not in arg_names_set: continue
                            if p_key not in schedule_map: schedule_map[p_key] = {}
                            if week in schedule_map[p_key]: schedule_map[p_key][week] += f'<div style="margin-top: 3px;">{t_name}{suffix}</div>'
                            else: schedule_map[p_key][week] = f"{t_name}{suffix}"
            else:
                for key, (t_list, status_dict) in temp_wta_results.items():
                    t_list = merge_entry_list(entry_cache.get(key, []), t_list)
                    entry_cache[key] = t_list
                    tournament_store[key] = t_list
                    t_name = tourneys[key]["name"]
                    for p_name, suffix in status_dict.items():
                        p_key = p_name.upper()
                        if p_key not in arg_names_set: continue
                        if p_key not in schedule_map: schedule_map[p_key] = {}
                        if week in schedule_map[p_key]: schedule_map[p_key][week] += f'<div style="margin-top: 3px;">{t_name}{suffix}</div>'
                        else: schedule_map[p_key][week] = f"{t_name}{suffix}"
            
            # Process ITF
            for key, t_info in tourneys.items():
                t_name = t_info["name"]
                if not key.startswith("http"):
                    itf_entries, itf_name_map = get_itf_players(key, driver)
                    tourney_players_list = []
                    
                    for classification in itf_entries:
                        class_code = classification.get("entryClassificationCode", "")
                        if class_code in ["MDA", "JR"]: section_type = "MAIN"
                        elif class_code == "Q": section_type = "QUAL"
                        else: continue
                        
                        for entry in classification.get("entries") or []:
                            pos = entry.get("positionDisplay", "-")
                            players = entry.get("players") or []
                            if not players: continue
                            p_node = players[0]
                            raw_f_name = f"{p_node.get('givenName', '')} {p_node.get('familyName', '')}".strip()
                            
                            wta = p_node.get("atpWtaRank", "")
                            itf_rank = p_node.get("itfBTRank")
                            wtn = p_node.get("worldRating", "")
                            
                            if class_code == "JR": erank_str = "JE"
                            else:
                                erank_str = "-"
                                if wta and str(wta).strip() != "": erank_str = f"{wta}"
                                elif itf_rank is not None and str(itf_rank).strip() != "": erank_str = f"ITF {itf_rank}"
                                elif wtn and str(wtn).strip() != "": erank_str = f"WTN {wtn}"
                            
                            try:
                                pos_digits = ''.join(filter(str.isdigit, str(pos)))
                                pos_num = int(pos_digits) if pos_digits else 999
                            except: pos_num = 999

                            tourney_players_list.append({
                                "pos": pos, "name": raw_f_name, "country": p_node.get("nationalityCode", "-"),
                                "rank": erank_str, "type": section_type, "pos_num": pos_num
                            })

                    tourney_players_list.sort(key=lambda x: x["pos_num"])
                    tourney_players_list = merge_entry_list(entry_cache.get(key, []), tourney_players_list)
                    entry_cache[key] = tourney_players_list
                    tournament_store[key] = tourney_players_list

                    for p_name, suffix in itf_name_map.items():
                        if p_name not in arg_names_set: continue
                        if p_name not in schedule_map: schedule_map[p_name] = {}
                        if week in schedule_map[p_name]:
                            if t_name not in schedule_map[p_name][week]: schedule_map[p_name][week] += f"<br>{t_name}{suffix}"
                        else: schedule_map[p_name][week] = f"{t_name}{suffix}"

        # Remove tournaments no longer in the next 4 weeks
        active_keys = set()
        for tourneys in TOURNAMENT_GROUPS.values():
            active_keys.update(tourneys.keys())
        entry_cache = {k: v for k, v in entry_cache.items() if k in active_keys}

        save_cache(ENTRY_LISTS_CACHE_FILE, entry_cache)

        match_history_data = get_sheety_matches()

        cleaned_history = []
        for m in match_history_data:
            # Try different possible field names for date
            fecha = (m.get('date') or m.get('Date') or m.get('matchDate') or
                    m.get('match_date') or m.get('FECHA') or '')

            # Create new structure with Spanish column names
            # Get entry values and replace "DA" with empty string
            winner_entry = m.get('winnerEntry') or m.get('winner_entry') or m.get('WinnerEntry') or ''
            loser_entry = m.get('loserEntry') or m.get('loser_entry') or m.get('LoserEntry') or ''
            winner_entry = '' if winner_entry == 'DA' else winner_entry
            loser_entry = '' if loser_entry == 'DA' else loser_entry

            new_match = {
                'DATE': fecha,
                'TOURNAMENT': fix_encoding(m.get('tournamentName') or m.get('tournament_name') or m.get('TournamentName') or ''),
                'SURFACE': m.get('surface') or m.get('Surface') or '',
                'ROUND': m.get('roundName') or m.get('round_name') or m.get('RoundName') or '',
                'PLAYER': '',
                'ENTRY': '',
                'SEED': '',
                'RESULT': '',
                'SCORE': m.get('result') or m.get('Result') or '',
                'RIVAL_ENTRY': '',
                'RIVAL_SEED': '',
                'RIVAL': '',
                'RIVAL_COUNTRY': '',
                # Keep original names for filtering
                '_winnerName': m.get('winnerName') or m.get('winner_name') or m.get('WinnerName') or '',
                '_loserName': m.get('loserName') or m.get('loser_name') or m.get('LoserName') or '',
                '_winnerCountry': m.get('winnerCountry') or m.get('winner_country') or m.get('WinnerCountry') or '',
                '_loserCountry': m.get('loserCountry') or m.get('loser_country') or m.get('LoserCountry') or '',
                '_winnerEntry': winner_entry,
                '_loserEntry': loser_entry,
                '_winnerSeed': m.get('winnerSeed') or m.get('winner_seed') or m.get('WinnerSeed') or '',
                '_loserSeed': m.get('loserSeed') or m.get('loser_seed') or m.get('LoserSeed') or ''
            }
            cleaned_history.append(new_match)

        # Sort logic: latest date first
        def parse_match_date(item):
            d = item.get('DATE') or "1900-01-01"
            try:
                return pd.to_datetime(d, dayfirst=True)
            except:
                return pd.to_datetime("1900-01-01")

        cleaned_history.sort(key=parse_match_date, reverse=True)

    finally:
        driver.quit()

    dropdown_html = ""
    for week, tourneys in TOURNAMENT_GROUPS.items():
        week_has_data = False
        for t_key in tourneys.keys():
            if t_key in tournament_store and tournament_store[t_key]:
                week_has_data = True
                break
        
        if not week_has_data: continue
            
        dropdown_html += f'<option disabled class="dropdown-header">{week.upper()}</option>'
        sorted_tourneys = sorted(tourneys.items(), key=lambda x: get_tournament_sort_order(x[1]["level"]))
        
        for t_key, t_info in sorted_tourneys:
            if t_key in tournament_store and tournament_store[t_key]:
                t_name = t_info["name"]
                dropdown_html += f'<option value="{t_key}" class="dropdown-item">{t_name}</option>'
        dropdown_html += '</optgroup>'

    table_rows = ""
    week_keys = list(TOURNAMENT_GROUPS.keys())
    
    def get_sort_key(player_name):
        p = next(item for item in players_data if item["Player"] == player_name)
        rank = p['Rank']
        if isinstance(rank, int): return (0, rank)
        else:
            itf_rank = int(rank.replace("ITF ", "")) if isinstance(rank, str) and "ITF" in rank else 999999
            return (1, itf_rank)
    
    for p_name in sorted([p['Player'] for p in players_data], key=get_sort_key):
        p = next(item for item in players_data if item["Player"] == p_name)
        player_display = format_player_name(p['Player'])
        row = f'<tr data-name="{player_display.lower()}">'
        row += f'<td class="sticky-col col-rank">{p["Rank"]}</td>'
        row += f'<td class="sticky-col col-name">{player_display}</td>'
        for week in week_keys:
            val = schedule_map.get(p['Key'], {}).get(week, "—")
            is_main = "(Q)" not in val and val != "—"
            row += f'<td class="col-week">{"<b>" if is_main else ""}{val}{"</b>" if is_main else ""}</td>'
        table_rows += row + "</tr>"

    history_arg_players = set()
    for m in match_history_data:
        # Check winners
        if m.get('winnerCountry') == 'ARG' or m.get('winner_country') == 'ARG':
            name = m.get('winnerName') or m.get('winner_name')
            if name:
                name_upper = name.strip().upper()
                display_name = NAME_LOOKUP.get(name_upper, name_upper)
                history_arg_players.add(format_player_name(display_name))
        # Check losers
        if m.get('loserCountry') == 'ARG' or m.get('loser_country') == 'ARG':
            name = m.get('loserName') or m.get('loser_name')
            if name:
                name_upper = name.strip().upper()
                display_name = NAME_LOOKUP.get(name_upper, name_upper)
                history_arg_players.add(format_player_name(display_name))

    history_players_sorted = sorted(list(history_arg_players))

    html_template = f"""
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <title>WT Argentina</title>
        <link href="https://cdn.jsdelivr.net/npm/select2@4.1.0-rc.0/dist/css/select2.min.css" rel="stylesheet" />
        <script src="https://code.jquery.com/jquery-3.6.0.min.js"></script>
        <script src="https://cdn.jsdelivr.net/npm/select2@4.1.0-rc.0/dist/js/select2.min.js"></script>
        <style>
            @font-face {{ font-family: 'Montserrat'; src: url('Montserrat-SemiBold.ttf'); }}
            body {{ font-family: 'Montserrat', sans-serif; background: #f0f4f8; margin: 0; display: flex; min-height: 100vh; overflow-y: auto; }}
            .app-container {{ display: flex; width: 100%; min-height: 100vh; }}
            .sidebar {{ width: 180px; background: #1e293b; color: white; display: flex; flex-direction: column; flex-shrink: 0; min-height: 100vh; }}
            .sidebar-header {{ padding: 25px 15px; font-size: 15px; font-weight: 800; color: #75AADB; border-bottom: 1px solid #475569; }}
            .menu-item {{ padding: 15px 20px; cursor: pointer; color: #cbd5e1; font-size: 14px; border-bottom: 1px solid #334155; transition: 0.2s; }}
            .menu-item:hover {{ background: #334155; color: white; }}
            .menu-item.active {{ background: #75AADB; color: white; font-weight: bold; }}
            .main-content {{ flex: 1; overflow-y: visible; background: #f8fafc; padding: 20px; display: flex; flex-direction: column; }}
            
            /* Layout Views */
            .dual-layout {{ display: flex; min-height: 80vh; gap: 40px; position: relative; width: 100%; }}
            .single-layout {{ width: 100%; display: flex; flex-direction: column; }}
            
            .column-main {{ flex: 0 0 70%; display: flex; flex-direction: column; align-items: flex-start; position: relative; min-width: 0; }}
            .column-main table {{ table-layout: fixed; width: 100%; }}
            .column-entry {{ flex: 1; display: flex; flex-direction: column; align-items: flex-start; min-width: 0; }}
            .column-main::after {{ content: ""; position: absolute; right: -20px; top: 50px; bottom: 20px; width: 1px; background: #94a3b8; }}
            .header-row {{ width: 100%; margin-bottom: 20px; display: flex; flex-direction: column; align-items: center; position: relative; gap: 10px; }}
            h1 {{ margin: 0; font-size: 22px; color: #1e293b; }}
            .search-container {{ position: absolute; left: 0; top: 50%; transform: translateY(-50%); }}
            input, select {{ padding: 8px 12px; border-radius: 8px; border: 2px solid #94a3b8; font-family: inherit; font-size: 13px; width: 250px; box-sizing: border-box; }}
            select {{ background: white; font-weight: bold; cursor: pointer; appearance: none; background-image: url("data:image/svg+xml;charset=UTF-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%23475569' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'%3E%3C/polyline%3E%3C/svg%3E"); background-repeat: no-repeat; background-position: right 10px center; }}
            .content-card {{ background: white; box-shadow: 0 4px 20px rgba(0,0,0,0.05); width: 100%; border: 1px solid black; }}
            .table-wrapper {{ overflow-x: auto; width: 100%; }}
            table {{ border-collapse: separate; border-spacing: 0; width: 100%; table-layout: fixed; border: 1px solid black; }}
            th {{ position: sticky; top: 0; background: #75AADB !important; color: white; padding: 10px 15px; font-size: 11px; font-weight: bold; border-bottom: 2px solid #1e293b; border-right: 1px solid #1e293b; z-index: 10; text-transform: uppercase; text-align: center; }}
            td {{ padding: 8px 12px; border-bottom: 1px solid #94a3b8; text-align: center; font-size: 13px; border-right: 1px solid #94a3b8; }}
            .column-entry td {{ font-size: 12px; padding: 6px 10px; }}
            .sticky-col {{ position: sticky; background: white !important; z-index: 2; }}
            .row-arg {{ background-color: #e0f2fe !important; }}
            td.col-week {{ width: 150px; font-size: 11px; line-height: 1.2; overflow: hidden; text-overflow: ellipsis; }}
            th.sticky-col {{ z-index: 11; background: #75AADB !important; color: white; }}
            .col-rank {{ left: 0; width: 32px; min-width: 45px; max-width: 45px; }}
            .col-name {{ left: 45px; width: 140px; min-width: 140px; max-width: 140px; text-align: left; font-weight: bold; }}
            .col-week {{ width: 130px; font-size: 11px; font-weight: bold; line-height: 1.2; overflow: hidden; text-overflow: ellipsis; }}
            .divider-row td {{ background: #e2e8f0; font-weight: bold; text-align: center; padding: 5px 15px; font-size: 11px; border-right: none; }}
            tr.hidden {{ display: none; }}
            tr:hover td {{ background: #f1f5f9; }}
            tr:hover td.sticky-col {{ background: #f1f5f9 !important; }}
            .dropdown-header {{ background-color: #e2e8f0 !important; font-weight: bold !important; text-align: center !important; padding: 12px 0 !important; font-size: 11px; display: block; }}
            .dropdown-item {{ padding: 8px 15px; text-align: left; background-color: #ffffff; }}
            
            #tSelect {{ appearance: none; padding: 10px 30px 10px 12px; line-height: 1.5; background-color: white; }}
            #tSelect optgroup {{ background-color: #babdc2; color: #ffffff; text-align: center; font-style: normal; font-weight: 800; padding: 10px 0; }}
            #tSelect option {{ background-color: #ffffff; color: #1e293b; text-align: left; padding: 8px 12px; cursor: pointer; }}
            #tSelect option {{ margin-left: -15px; }}
            #tSelect option:hover, #tSelect option:focus, #tSelect option:checked {{ background-color: #75AADB !important; color: white !important; }}

            .select2-container--default .select2-selection--single {{
                border: 2px solid #94a3b8;
                border-radius: 8px;
                height: 38px;
                padding: 4px 12px;
                font-family: inherit;
                font-size: 13px;
            }}
            .select2-container--default .select2-selection--single .select2-selection__rendered {{
                color: #1e293b;
                line-height: 28px;
                padding-left: 0;
            }}
            .select2-container--default .select2-selection--single .select2-selection__arrow {{
                height: 36px;
            }}
            .select2-container--default.select2-container--open .select2-selection--single {{
                border-color: #75AADB;
            }}
            .select2-dropdown {{
                border: 2px solid #94a3b8;
                border-radius: 8px;
                font-family: inherit;
            }}
            .select2-search--dropdown .select2-search__field {{
                border: 1px solid #94a3b8;
                border-radius: 4px;
                padding: 4px 8px;
                font-family: inherit;
            }}
            .select2-results__option {{
                padding: 8px 12px;
                font-size: 13px;
            }}
            .select2-results__option--highlighted {{
                background-color: #75AADB !important;
                color: white !important;
            }}
            .select2-container {{
                width: 250px !important;
            }}

            .column-entry thead th {{ position: sticky; top: 0; background: #75AADB !important; color: white; z-index: 10; border-bottom: 2px solid #1e293b; }}
            .column-entry .content-card {{ overflow-y: visible; max-height: none; border: 1px solid black; background: white; box-shadow: 0 4px 20px rgba(0,0,0,0.05); width: 100%; }}
            
            #history-table th {{ background: #75AADB !important; position: sticky; top: 0; z-index: 10; }}
            #history-table {{ table-layout: fixed; width: 100%; }}
            #history-table th:nth-child(1) {{ width: 80px; }} /* DATE */
            #history-table th:nth-child(2) {{ width: auto; }} /* TOURNAMENT */
            #history-table th:nth-child(3) {{ width: 70px; }} /* SURFACE */
            #history-table th:nth-child(4) {{ width: 100px; }} /* ROUND */
            #history-table th:nth-child(5) {{ width: auto; }} /* PLAYER */
            #history-table th:nth-child(6) {{ width: 50px; }} /* RESULT */
            #history-table th:nth-child(7) {{ width: 120px; }} /* SCORE */
            #history-table th:nth-child(8) {{ width: auto; min-width: 200px; }} /* OPPONENT */
            #history-table td {{ font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
            #history-table td:nth-child(8) {{ white-space: normal; overflow: visible; text-overflow: clip; }} /* Allow OPPONENT to wrap */

            /* Filter Panel Styles */
            .history-layout {{ display: flex; gap: 20px; width: 100%; }}
            .filter-panel {{ width: 250px; padding: 15px; flex-shrink: 0; border: 2px solid black; background: white; }}
            .filter-panel h3 {{ margin: -15px -15px 15px -15px; font-size: 16px; color: white; text-align: center; font-weight: bold; background: #75AADB; border: none; padding: 12px; border-radius: 0; }}
            .filter-group {{ margin-bottom: 20px; text-align: left; }}
            .filter-group-title {{ font-size: 13px; font-weight: bold; color: #475569; margin-bottom: 8px; cursor: pointer; user-select: none; display: flex; justify-content: center; align-items: center; text-align: center; position: relative; }}
            .filter-group-title:hover {{ color: #75AADB; }}
            .filter-options {{ border: 1px solid #e2e8f0; border-radius: 4px; padding: 8px; background: #f8fafc; text-align: left; }}
            .filter-option {{ padding: 6px 10px; margin-bottom: 4px; font-size: 12px; text-align: left; cursor: pointer; user-select: none; border-radius: 3px; transition: background 0.15s; }}
            .filter-option:hover {{ background: #e2e8f0; }}
            .filter-option.selected {{ font-weight: bold; background: #dbeafe; color: #1e40af; }}
            .filter-actions {{ margin-top: 20px; display: flex; justify-content: space-between; align-items: center; gap: 10px; }}
            .filter-instructions {{ font-size: 10px; color: #64748b; flex: 1; line-height: 1.3; padding-left: 15px; }}
            .filter-btn {{ padding: 8px 16px; border: none; border-radius: 4px; cursor: pointer; font-family: inherit; font-size: 12px; font-weight: bold; white-space: nowrap; }}
            .filter-btn-clear {{ background: #e2e8f0; color: #475569; }}
            .filter-btn-clear:hover {{ background: #cbd5e1; }}
            #filter-opponent-select {{ font-size: 11px; }}
            .history-content {{ flex: 1; display: flex; flex-direction: column; min-width: 0; }}
            .collapse-icon {{ font-size: 14px; position: absolute; right: 0; }}
            .filter-group.collapsed .filter-options {{ display: none; }}
            .filter-group.collapsed .opponent-select-container {{ display: none; }}
            .filter-group.collapsed .collapse-icon::before {{ content: '▼'; }}
            .filter-group:not(.collapsed) .collapse-icon::before {{ content: '▲'; }}
            .filter-search {{ width: 100%; padding: 6px 8px; border: 1px solid #cbd5e1; border-radius: 4px; font-family: inherit; font-size: 11px; margin-bottom: 8px; box-sizing: border-box; }}
            .filter-search:focus {{ outline: none; border-color: #75AADB; }}
            .table-header-section {{ margin-bottom: 15px; display: flex; align-items: center; justify-content: space-between; }}
            .table-title {{ margin: 0; font-size: 22px; color: #1e293b; flex: 1; text-align: center; }}
            .player-select-container {{ width: 250px; }}

            /* Mobile Menu Toggle */
            .mobile-menu-toggle {{ display: none; position: fixed; top: 15px; left: 15px; z-index: 1000; background: #1e293b; color: white; border: none; padding: 10px 15px; border-radius: 4px; cursor: pointer; font-size: 18px; }}
            .sidebar.mobile-hidden {{ transform: translateX(-100%); }}

            /* Responsive Styles */
            @media (max-width: 1024px) {{
                /* Tablet adjustments */
                .dual-layout {{ gap: 20px; }}
                .column-main {{ flex: 0 0 60%; }}
                .column-entry {{ flex: 1; }}
                input, select {{ width: 200px; }}
                .select2-container {{ width: 200px !important; }}
            }}

            @media (max-width: 768px) {{
                /* Mobile styles */
                body {{ overflow-x: hidden; }}
                .mobile-menu-toggle {{ display: block; }}

                .app-container {{ flex-direction: column; }}

                .sidebar {{
                    position: fixed;
                    left: 0;
                    top: 0;
                    height: 100vh;
                    z-index: 999;
                    transition: transform 0.3s ease;
                    box-shadow: 2px 0 10px rgba(0,0,0,0.3);
                }}

                .main-content {{
                    padding: 60px 10px 10px 10px;
                    width: 100%;
                }}

                /* Stack dual layout vertically */
                .dual-layout {{
                    flex-direction: column;
                    gap: 20px;
                }}

                .column-main {{
                    flex: 1;
                    width: 100%;
                }}

                .column-main::after {{ display: none; }}

                .column-entry {{
                    flex: 1;
                    width: 100%;
                }}

                /* Adjust header rows */
                .header-row {{
                    flex-direction: column;
                    gap: 10px;
                    align-items: stretch;
                }}

                .search-container {{
                    position: static;
                    transform: none;
                    width: 100%;
                }}

                h1 {{
                    font-size: 18px;
                    text-align: center;
                }}

                input, select {{
                    width: 100%;
                    max-width: 100%;
                }}

                .select2-container {{
                    width: 100% !important;
                }}

                /* Table adjustments */
                .table-wrapper {{
                    overflow-x: auto;
                    -webkit-overflow-scrolling: touch;
                }}

                table {{
                    font-size: 11px;
                    min-width: 600px;
                }}

                th, td {{
                    padding: 6px 8px;
                    font-size: 10px;
                }}

                .col-name {{
                    min-width: 120px;
                    max-width: 120px;
                }}

                .col-week {{
                    font-size: 9px;
                }}

                /* History layout - stack vertically */
                .history-layout {{
                    flex-direction: column;
                }}

                .filter-panel {{
                    width: 100%;
                    padding: 15px;
                    margin-bottom: 20px;
                    border: 2px solid black;
                }}

                .filter-panel h3 {{
                    font-size: 14px;
                    padding: 10px;
                }}

                .history-content {{
                    width: 100%;
                }}

                .table-header-section {{
                    flex-direction: column;
                    gap: 10px;
                }}

                .player-select-container {{
                    width: 100%;
                }}

                .table-title {{
                    font-size: 18px;
                }}

                /* History table */
                #history-table {{
                    min-width: 700px;
                }}

                #history-table th:nth-child(1) {{ width: 70px; }}
                #history-table th:nth-child(2) {{ width: auto; min-width: 120px; }}
                #history-table th:nth-child(3) {{ width: 60px; }}
                #history-table th:nth-child(4) {{ width: 80px; }}
                #history-table th:nth-child(5) {{ width: auto; min-width: 120px; }}
                #history-table th:nth-child(6) {{ width: 40px; }}
                #history-table th:nth-child(7) {{ width: 100px; }}
                #history-table th:nth-child(8) {{ width: auto; min-width: 150px; }}

                .filter-actions {{
                    flex-direction: column;
                    gap: 10px;
                }}

                .filter-instructions {{
                    padding-left: 0;
                    text-align: center;
                }}
            }}

            @media (max-width: 480px) {{
                /* Extra small mobile */
                h1 {{
                    font-size: 16px;
                }}

                .sidebar-header {{
                    font-size: 14px;
                    padding: 20px 10px;
                }}

                .menu-item {{
                    font-size: 13px;
                    padding: 12px 15px;
                }}

                th, td {{
                    padding: 4px 6px;
                    font-size: 9px;
                }}

                .col-name {{
                    min-width: 100px;
                    max-width: 100px;
                }}

                .filter-panel h3 {{
                    font-size: 13px;
                }}

                .filter-group-title {{
                    font-size: 12px;
                }}

                .filter-option {{
                    font-size: 11px;
                }}
            }}
        </style>
    </head>
    <body onload="updateEntryList(); renderHistoryTable();">
        <button class="mobile-menu-toggle" onclick="toggleMobileMenu()">☰</button>
        <div class="app-container">
            <div class="sidebar" id="sidebar">
                <div class="sidebar-header">WT Argentina</div>
                <div class="menu-item active" id="btn-upcoming" onclick="switchTab('upcoming')">Upcoming Tournaments</div>
                <div class="menu-item" id="btn-history" onclick="switchTab('history')">Match History</div>
            </div>
            
            <div class="main-content">
                <div id="view-upcoming" class="dual-layout">
                    <div class="column-main">
                        <div class="header-row">
                            <div class="search-container">
                                <input type="text" id="s" placeholder="Search player..." oninput="filter()">
                            </div>
                            <h1>Upcoming Tournaments</h1>
                        </div>
                        <div class="content-card">
                            <div class="table-wrapper">
                                <table>
                                    <thead>
                                        <tr>
                                            <th class="sticky-col col-rank">Rank</th>
                                            <th class="sticky-col col-name">Player</th>
                                            {"".join([f'<th class="col-week">{w}</th>' for w in week_keys])}
                                        </tr>
                                    </thead>
                                    <tbody id="tb">{table_rows}</tbody>
                                </table>
                            </div>
                        </div>
                    </div>

                    <div class="column-entry">
                        <div class="header-row">
                            <h1>Entry List</h1> 
                            <div class="search-container-static"> 
                                <select id="tSelect" onchange="updateEntryList()">
                                    {dropdown_html}
                                </select>
                            </div>
                        </div>
                        <div class="content-card">
                            <table>
                                <thead>
                                    <tr>
                                        <th style="width:15px">#</th>
                                        <th>PLAYER</th>
                                        <th style="width:35px">NAT</th>
                                        <th style="width:70px">E-Rank</th> 
                                    </tr>
                                </thead>
                                <tbody id="entry-body"></tbody>
                            </table>
                        </div>
                    </div>
                </div>

                <div id="view-history" class="single-layout" style="display: none;">
                    <div class="history-layout">
                        <div class="filter-panel">
                            <h3>Filters</h3>

                            <div class="filter-group collapsed">
                                <div class="filter-group-title" onclick="toggleFilterGroup(this)">
                                    Surface <span class="collapse-icon"></span>
                                </div>
                                <div class="filter-options" id="filter-surface"></div>
                            </div>

                            <div class="filter-group collapsed">
                                <div class="filter-group-title" onclick="toggleFilterGroup(this)">
                                    Round <span class="collapse-icon"></span>
                                </div>
                                <div class="filter-options" id="filter-round"></div>
                            </div>

                            <div class="filter-group collapsed">
                                <div class="filter-group-title" onclick="toggleFilterGroup(this)">
                                    Result <span class="collapse-icon"></span>
                                </div>
                                <div class="filter-options" id="filter-result"></div>
                            </div>

                            <div class="filter-group collapsed">
                                <div class="filter-group-title" onclick="toggleFilterGroup(this)">
                                    Opponent <span class="collapse-icon"></span>
                                </div>
                                <div class="opponent-select-container" style="padding: 8px; overflow: visible;">
                                    <select id="filter-opponent-select" style="width: 100%;">
                                        <option value="">All Opponents</option>
                                    </select>
                                </div>
                            </div>

                            <div class="filter-group collapsed">
                                <div class="filter-group-title" onclick="toggleFilterGroup(this)">
                                    Opponent Country <span class="collapse-icon"></span>
                                </div>
                                <div class="filter-options" id="filter-opponent-country"></div>
                            </div>

                            <div class="filter-group collapsed">
                                <div class="filter-group-title" onclick="toggleFilterGroup(this)">
                                    Player Entry <span class="collapse-icon"></span>
                                </div>
                                <div class="filter-options" id="filter-player-entry"></div>
                            </div>

                            <div class="filter-group collapsed">
                                <div class="filter-group-title" onclick="toggleFilterGroup(this)">
                                    Seed <span class="collapse-icon"></span>
                                </div>
                                <div class="filter-options" id="filter-seed"></div>
                            </div>

                            <div class="filter-group collapsed">
                                <div class="filter-group-title" onclick="toggleFilterGroup(this)">
                                    Match Type <span class="collapse-icon"></span>
                                </div>
                                <div class="filter-options" id="filter-match-type"></div>
                            </div>

                            <div class="filter-actions">
                                <div class="filter-instructions">Ctrl+Click to select multiple options.</div>
                                <button class="filter-btn filter-btn-clear" onclick="clearHistoryFilters()">Reset Filters</button>
                            </div>
                        </div>

                        <div class="history-content">
                            <div class="table-header-section">
                                <div class="player-select-container">
                                    <select id="playerHistorySelect">
                                        <option value="">Select Player...</option>
                                        {"".join([f'<option value="{name}">{name}</option>' for name in history_players_sorted])}
                                    </select>
                                </div>
                                <h1 class="table-title">Match History</h1>
                                <div style="width: 250px;"></div>
                            </div>

                            <div class="content-card">
                                <div class="table-wrapper">
                                    <table id="history-table">
                                        <thead id="history-head"></thead>
                                        <tbody id="history-body">
                                            <tr><td colspan="100%" style="padding: 20px; color: #64748b;">Select a player to view their matches</td></tr>
                                        </tbody>
                                    </table>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        <script>
            const tournamentData = {json.dumps(tournament_store)};
            const historyData = {json.dumps(cleaned_history)};
            const playerMapping = {json.dumps(PLAYER_MAPPING)};

            function toggleMobileMenu() {{
                const sidebar = document.getElementById('sidebar');
                sidebar.classList.toggle('mobile-hidden');
            }}

            // Close mobile menu when clicking outside
            document.addEventListener('click', function(event) {{
                const sidebar = document.getElementById('sidebar');
                const menuToggle = document.querySelector('.mobile-menu-toggle');

                if (window.innerWidth <= 768) {{
                    if (!sidebar.contains(event.target) && !menuToggle.contains(event.target)) {{
                        sidebar.classList.add('mobile-hidden');
                    }}
                }}
            }});

            // Close mobile menu when tab is clicked
            function switchTab(tabName) {{
                document.querySelectorAll('.menu-item').forEach(el => el.classList.remove('active'));
                document.getElementById('btn-' + tabName).classList.add('active');

                document.getElementById('view-upcoming').style.display = (tabName === 'upcoming') ? 'flex' : 'none';
                document.getElementById('view-history').style.display = (tabName === 'history') ? 'flex' : 'none';

                // Close mobile menu after selecting
                if (window.innerWidth <= 768) {{
                    document.getElementById('sidebar').classList.add('mobile-hidden');
                }}
            }}

            function reverseScore(score) {{
                if (!score) return '';
                return score.split(' ').map(set => {{
                    const m = set.match(/^(\\d+)-(\\d+)(.*)$/);
                    if (!m) return set;
                    return m[2] + '-' + m[1] + m[3];
                }}).join(' ');
            }}

            function buildPrefix(seed, entry) {{
                const parts = [];
                if (seed) parts.push(seed);
                if (entry) parts.push(entry);
                if (parts.length === 0) return '';
                return '(' + parts.join('/') + ') ';
            }}

            // Find the last qualifying round per tournament and rename it to QRF
            function getQRFinalMap() {{
                const maxQR = {{}};
                historyData.forEach(row => {{
                    const t = row['TOURNAMENT'] || '';
                    const r = row['ROUND'] || '';
                    const m = r.match(/^QR(\\d+)$/);
                    if (m) {{
                        const num = parseInt(m[1]);
                        if (!maxQR[t] || num > maxQR[t]) maxQR[t] = num;
                    }}
                }});
                return maxQR;
            }}
            const qrFinalMap = getQRFinalMap();

            function displayRound(round, tournament) {{
                const m = (round || '').match(/^QR(\\d+)$/);
                if (m && qrFinalMap[tournament] === parseInt(m[1])) return 'QRF';
                return round;
            }}

            // Format date string to yyyy-MM-dd
            function formatDate(dateStr) {{
                if (!dateStr) return '';
                const parts = dateStr.split('/');
                if (parts.length === 3) {{
                    return parts[2] + '-' + parts[1].padStart(2, '0') + '-' + parts[0].padStart(2, '0');
                }}
                const d = new Date(dateStr);
                if (isNaN(d)) return dateStr;
                return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
            }}

            // Helper function to get display name from player mapping
            function getDisplayName(upperCaseName) {{
                // Try to find the display name in playerMapping
                for (const [displayName, aliases] of Object.entries(playerMapping)) {{
                    for (const alias of aliases) {{
                        if (alias.toUpperCase() === upperCaseName) {{
                            return displayName; // Return proper capitalization from mapping
                        }}
                    }}
                }}
                // If not found, convert to title case
                return upperCaseName.split(' ').map(word => 
                    word.charAt(0).toUpperCase() + word.slice(1).toLowerCase()
                ).join(' ');
            }}

            $(document).ready(function() {{
                // Initialize sidebar state for mobile
                if (window.innerWidth <= 768) {{
                    document.getElementById('sidebar').classList.add('mobile-hidden');
                }}

                $('#playerHistorySelect').select2({{
                    placeholder: 'Select a player...',
                    allowClear: true,
                    width: '250px'
                }});

                $('#playerHistorySelect').on('change', function() {{
                    filterHistoryByPlayer();
                }});

                renderHistoryTable();

                // Handle window resize
                window.addEventListener('resize', function() {{
                    if (window.innerWidth > 768) {{
                        document.getElementById('sidebar').classList.remove('mobile-hidden');
                    }} else {{
                        document.getElementById('sidebar').classList.add('mobile-hidden');
                    }}
                }});
            }});

            function filter() {{
                const q = document.getElementById('s').value.toLowerCase();
                document.querySelectorAll('#tb tr').forEach(row => {{
                    const matches = row.getAttribute('data-name').includes(q);
                    row.classList.toggle('hidden', !matches);
                }});
            }}
            function updateEntryList() {{
                const sel = document.getElementById('tSelect').value;
                const body = document.getElementById('entry-body');
                if (!tournamentData[sel]) return;
                const players = tournamentData[sel];
                let html = '';
                const main = players.filter(p => p.type === 'MAIN');
                const qual = players.filter(p => p.type === 'QUAL');
                
                main.forEach(p => {{
                    html += `<tr class="${{p.country==='ARG'?'row-arg':''}}"><td>${{p.pos}}</td><td style="text-align:left;font-weight:bold;">${{p.name}}</td><td>${{p.country}}</td><td>${{p.rank}}</td></tr>`;
                }});
                if (qual.length > 0) {{
                    html += `<tr class="divider-row"><td colspan="4">QUALIFYING</td></tr>`;
                    qual.forEach(p => {{
                        html += `<tr class="${{p.country==='ARG'?'row-arg':''}}"><td>${{p.pos}}</td><td style="text-align:left;">${{p.name}}</td><td>${{p.country}}</td><td>${{p.rank}}</td></tr>`;
                    }});
                }}
                body.innerHTML = html;
            }}


            function renderHistoryTable() {{
                const thead = document.getElementById('history-head');
                const tbody = document.getElementById('history-body');
                
                if (!historyData || historyData.length === 0) return;

                // Define column headers (excluding hidden _ columns)
                const displayColumns = ['DATE', 'TOURNAMENT', 'SURFACE', 'ROUND', 'PLAYER', 'RESULT', 'SCORE', 'OPPONENT'];
                let headHtml = '<tr>';
                displayColumns.forEach(col => {{
                    headHtml += `<th>${{col.replace('_', ' ')}}</th>`;
                }});
                headHtml += '</tr>';
                thead.innerHTML = headHtml;
                
                // Set initial placeholder message
                tbody.innerHTML = `<tr><td colspan="${{displayColumns.length}}" style="padding: 20px; color: #64748b;">Select a player to view their matches</td></tr>`;
            }}

            let currentPlayerData = [];

            function toggleFilterGroup(element) {{
                element.parentElement.classList.toggle('collapsed');
            }}

            function populateFilters(playerMatches) {{
                // Extract unique values for each filter
                const surfaces = new Set();
                const rounds = new Set();
                const results = new Set(['W', 'L']);
                const opponents = new Set();
                const opponentCountries = new Set();
                const playerEntries = new Set();
                const seeds = new Set(['Yes', 'No']);
                const matchTypes = new Set();

                const selectedPlayer = document.getElementById('playerHistorySelect').value.toUpperCase();

                playerMatches.forEach(row => {{
                    const wName = (row['_winnerName'] || "").toString().toUpperCase();
                    const wNameNormalized = getDisplayName(wName).toUpperCase();
                    const isWinner = wNameNormalized === selectedPlayer;

                    // Surface
                    if (row['SURFACE']) surfaces.add(row['SURFACE']);

                    // Round
                    if (row['ROUND']) rounds.add(row['ROUND']);

                    // Opponent
                    const opponentName = isWinner ? (row['_loserName'] || '') : (row['_winnerName'] || '');
                    if (opponentName) opponents.add(getDisplayName(opponentName.toUpperCase()));

                    // Opponent Country
                    const opponentCountry = isWinner ? (row['_loserCountry'] || '') : (row['_winnerCountry'] || '');
                    if (opponentCountry) opponentCountries.add(opponentCountry);

                    // Player Entry
                    const playerEntry = isWinner ? (row['_winnerEntry'] || '') : (row['_loserEntry'] || '');
                    if (playerEntry) playerEntries.add(playerEntry);

                    // Match Type (determine from tournament)
                    const tournament = row['TOURNAMENT'] || '';
                    if (tournament.includes('ITF') || tournament.includes('W15') || tournament.includes('W25') ||
                        tournament.includes('W35') || tournament.includes('W50') || tournament.includes('W60') ||
                        tournament.includes('W75') || tournament.includes('W100')) {{
                        matchTypes.add('ITF');
                    }} else {{
                        matchTypes.add('WTA');
                    }}
                }});

                // Populate filter options
                populateFilterOptions('filter-surface', Array.from(surfaces).sort());
                populateFilterOptions('filter-round', Array.from(rounds).sort());
                populateFilterOptions('filter-result', Array.from(results));
                populateOpponentSelect(Array.from(opponents).sort());
                populateFilterOptions('filter-opponent-country', Array.from(opponentCountries).sort());
                populateFilterOptions('filter-player-entry', Array.from(playerEntries).sort());
                populateFilterOptions('filter-seed', Array.from(seeds));
                populateFilterOptions('filter-match-type', Array.from(matchTypes).sort());
            }}

            function populateFilterOptions(filterId, values) {{
                const container = document.getElementById(filterId);
                let html = '';
                values.forEach(value => {{
                    if (value) {{
                        html += `<div class="filter-option" data-value="${{value}}" onclick="toggleFilterOption(event, this)">${{value}}</div>`;
                    }}
                }});
                container.innerHTML = html || '<div style="padding: 5px; color: #94a3b8; font-size: 11px;">No options</div>';
            }}

            function populateOpponentSelect(opponents) {{
                const select = document.getElementById('filter-opponent-select');

                // Destroy existing Select2 if it exists
                if ($(select).data('select2')) {{
                    $(select).select2('destroy');
                }}

                // Clear and populate options
                let html = '<option value="">All Opponents</option>';
                opponents.forEach(opponent => {{
                    if (opponent) {{
                        html += `<option value="${{opponent}}">${{opponent}}</option>`;
                    }}
                }});
                select.innerHTML = html;

                // Initialize Select2 with search
                $(select).select2({{
                    placeholder: 'All Opponents',
                    allowClear: true,
                    width: '100%'
                }});

                // Auto-apply filters when selection changes
                $(select).off('change').on('change', function() {{
                    applyHistoryFilters();
                }});
            }}

            function toggleFilterOption(event, element) {{
                // Support Ctrl+Click for multi-select
                if (!event.ctrlKey && !event.metaKey) {{
                    // Single click without Ctrl - deselect all others in this group first
                    const siblings = element.parentElement.querySelectorAll('.filter-option');
                    siblings.forEach(sib => {{
                        if (sib !== element) sib.classList.remove('selected');
                    }});
                }}

                // Toggle this option
                element.classList.toggle('selected');

                // Auto-apply filters
                applyHistoryFilters();
            }}

            function getSelectedFilterValues(filterId) {{
                const container = document.getElementById(filterId);
                const selectedOptions = container.querySelectorAll('.filter-option.selected');
                return Array.from(selectedOptions).map(option => option.getAttribute('data-value'));
            }}

            function applyHistoryFilters() {{
                const selectedPlayer = document.getElementById('playerHistorySelect').value.toUpperCase();
                if (!selectedPlayer) return;

                // Get selected filter values
                const selectedSurfaces = getSelectedFilterValues('filter-surface');
                const selectedRounds = getSelectedFilterValues('filter-round');
                const selectedResults = getSelectedFilterValues('filter-result');
                const selectedOpponent = document.getElementById('filter-opponent-select').value;
                const selectedOpponentCountries = getSelectedFilterValues('filter-opponent-country');
                const selectedPlayerEntries = getSelectedFilterValues('filter-player-entry');
                const selectedSeeds = getSelectedFilterValues('filter-seed');
                const selectedMatchTypes = getSelectedFilterValues('filter-match-type');

                // Filter the data (if nothing selected in a category, show all)
                const filtered = currentPlayerData.filter(row => {{
                    const wName = (row['_winnerName'] || "").toString().toUpperCase();
                    const lName = (row['_loserName'] || "").toString().toUpperCase();
                    const wNameNormalized = getDisplayName(wName).toUpperCase();
                    const lNameNormalized = getDisplayName(lName).toUpperCase();
                    const isWinner = wNameNormalized === selectedPlayer;

                    // Surface filter
                    if (selectedSurfaces.length > 0 && !selectedSurfaces.includes(row['SURFACE'] || '')) return false;

                    // Round filter
                    if (selectedRounds.length > 0 && !selectedRounds.includes(row['ROUND'] || '')) return false;

                    // Result filter
                    const result = isWinner ? 'W' : 'L';
                    if (selectedResults.length > 0 && !selectedResults.includes(result)) return false;

                    // Opponent filter (single select from dropdown)
                    if (selectedOpponent) {{
                        const opponentName = isWinner ? (row['_loserName'] || '') : (row['_winnerName'] || '');
                        const opponentDisplay = opponentName ? getDisplayName(opponentName.toUpperCase()) : '';
                        if (opponentDisplay !== selectedOpponent) return false;
                    }}

                    // Opponent Country filter
                    const opponentCountry = isWinner ? (row['_loserCountry'] || '') : (row['_winnerCountry'] || '');
                    if (selectedOpponentCountries.length > 0 && !selectedOpponentCountries.includes(opponentCountry)) return false;

                    // Player Entry filter
                    const playerEntry = isWinner ? (row['_winnerEntry'] || '') : (row['_loserEntry'] || '');
                    if (selectedPlayerEntries.length > 0 && !selectedPlayerEntries.includes(playerEntry)) return false;

                    // Seed filter
                    const playerSeed = isWinner ? (row['_winnerSeed'] || '') : (row['_loserSeed'] || '');
                    const hasSeed = playerSeed ? 'Yes' : 'No';
                    if (selectedSeeds.length > 0 && !selectedSeeds.includes(hasSeed)) return false;

                    // Match Type filter
                    const tournament = row['TOURNAMENT'] || '';
                    const isITF = tournament.includes('ITF') || tournament.includes('W15') || tournament.includes('W25') ||
                                  tournament.includes('W35') || tournament.includes('W50') || tournament.includes('W60') ||
                                  tournament.includes('W75') || tournament.includes('W100');
                    const matchType = isITF ? 'ITF' : 'WTA';
                    if (selectedMatchTypes.length > 0 && !selectedMatchTypes.includes(matchType)) return false;

                    return true;
                }});

                renderFilteredMatches(filtered, selectedPlayer);
            }}

            function clearHistoryFilters() {{
                // Remove selected class from all filter options
                document.querySelectorAll('.filter-option.selected').forEach(option => {{
                    option.classList.remove('selected');
                }});
                // Reset opponent select dropdown
                $('#filter-opponent-select').val('').trigger('change');
                // Auto-apply filters (which will show all matches since nothing is selected)
                applyHistoryFilters();
            }}

            function renderFilteredMatches(matches, selectedPlayer) {{
                const tbody = document.getElementById('history-body');
                const displayColumns = ['DATE', 'TOURNAMENT', 'SURFACE', 'ROUND', 'PLAYER', 'RESULT', 'SCORE', 'OPPONENT'];

                if (matches.length === 0) {{
                    tbody.innerHTML = `<tr><td colspan="${{displayColumns.length}}" style="padding: 20px;">No matches found with the selected filters.</td></tr>`;
                    return;
                }}

                // Round priority (lower = higher in table)
                const roundOrder = {{
                    'Final': 1, 'Semi-finals': 2, 'Quarter-finals': 3,
                    '4th Round': 4, '3rd Round': 5, '2nd Round': 6, '1st Round': 7,
                    'QRF': 8, 'QR4': 9, 'QR3': 10, 'QR2': 11, 'QR1': 12,
                    'Semi Finals': 13, 'Quarter Finals': 14,
                    'Last 16': 15, 'Last 32': 16, 'Round Robin': 17
                }};
                function getRoundOrder(round) {{
                    return roundOrder[round] || 99;
                }}

                // Sort by date descending, then by round order ascending
                matches.sort((a, b) => {{
                    const dateA = formatDate(a['DATE'] || '1900-01-01');
                    const dateB = formatDate(b['DATE'] || '1900-01-01');
                    if (dateA !== dateB) return dateB.localeCompare(dateA);
                    return getRoundOrder(displayRound(a['ROUND'], a['TOURNAMENT'])) - getRoundOrder(displayRound(b['ROUND'], b['TOURNAMENT']));
                }});

                let bodyHtml = '';
                matches.forEach(row => {{
                    const wName = (row['_winnerName'] || "").toString().toUpperCase();
                    const wNameNormalized = getDisplayName(wName).toUpperCase();
                    const isWinner = wNameNormalized === selectedPlayer;

                    const playerDisplayName = getDisplayName(selectedPlayer);
                    const rivalName = isWinner ? (row['_loserName'] || '') : (row['_winnerName'] || '');
                    const rivalDisplayName = rivalName ? getDisplayName(rivalName.toUpperCase()) : '';

                    // Fill in the dynamic columns
                    const pSeed = isWinner ? (row['_winnerSeed'] || '') : (row['_loserSeed'] || '');
                    const pEntry = isWinner ? (row['_winnerEntry'] || '') : (row['_loserEntry'] || '');
                    const rSeed = isWinner ? (row['_loserSeed'] || '') : (row['_winnerSeed'] || '');
                    const rEntry = isWinner ? (row['_loserEntry'] || '') : (row['_winnerEntry'] || '');

                    const rivalCountry = isWinner ? (row['_loserCountry'] || '') : (row['_winnerCountry'] || '');
                    const opponentName = rivalDisplayName + (rivalCountry ? ` [${{rivalCountry}}]` : '');

                    const rowData = {{
                        'DATE': formatDate(row['DATE'] || ''),
                        'TOURNAMENT': row['TOURNAMENT'] || '',
                        'SURFACE': row['SURFACE'] || '',
                        'ROUND': displayRound(row['ROUND'] || '', row['TOURNAMENT'] || ''),
                        'PLAYER': buildPrefix(pSeed, pEntry) + playerDisplayName,
                        'RESULT': isWinner ? 'W' : 'L',
                        'SCORE': isWinner ? (row['SCORE'] || '') : reverseScore(row['SCORE'] || ''),
                        'OPPONENT': buildPrefix(rSeed, rEntry) + opponentName
                    }};

                    bodyHtml += '<tr>';
                    displayColumns.forEach(col => {{
                        bodyHtml += `<td>${{rowData[col] ?? ''}}</td>`;
                    }});
                    bodyHtml += '</tr>';
                }});
                tbody.innerHTML = bodyHtml;
            }}

            function filterHistoryByPlayer() {{
                const selectedPlayer = document.getElementById('playerHistorySelect').value.toUpperCase();
                const tbody = document.getElementById('history-body');
                const displayColumns = ['DATE', 'TOURNAMENT', 'SURFACE', 'ROUND', 'PLAYER', 'RESULT', 'SCORE', 'OPPONENT'];

                if (!selectedPlayer) {{
                    tbody.innerHTML = `<tr><td colspan="${{displayColumns.length}}" style="padding: 20px;">Select a player...</td></tr>`;
                    return;
                }}

                const filtered = historyData.filter(row => {{
                    const wName = (row['_winnerName'] || "").toString().toUpperCase();
                    const lName = (row['_loserName'] || "").toString().toUpperCase();
                    // Normalize names using the player mapping to match aliases
                    const wNameNormalized = getDisplayName(wName).toUpperCase();
                    const lNameNormalized = getDisplayName(lName).toUpperCase();
                    return wNameNormalized === selectedPlayer || lNameNormalized === selectedPlayer;
                }});

                if (filtered.length === 0) {{
                    tbody.innerHTML = `<tr><td colspan="${{displayColumns.length}}" style="padding: 20px;">No matches found for this player.</td></tr>`;
                    return;
                }}

                // Store current player data for filtering
                currentPlayerData = filtered;

                // Populate filters with this player's data
                populateFilters(filtered);

                // Render all matches (filters start with all checked)
                renderFilteredMatches(filtered, selectedPlayer);
            }}
        </script>
    </body>
    </html>
    """
    with open("index.html", "w", encoding="utf-8") as f: f.write(html_template)

if __name__ == "__main__":
    main()