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
    months_es = {
        1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril",
        5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto",
        9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre"
    }
    return f"Semana {monday_date.day} {months_es[monday_date.month]}"

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
                        tournament_store[key] = t_list
                        for p_name, suffix in status_dict.items():
                            p_key = p_name.upper()
                            if p_key not in arg_names_set: continue
                            if p_key not in schedule_map: schedule_map[p_key] = {}
                            if week in schedule_map[p_key]: schedule_map[p_key][week] += f'<div style="margin-top: 3px;">{t_name}{suffix}</div>'
                            else: schedule_map[p_key][week] = f"{t_name}{suffix}"
            else:
                for key, (t_list, status_dict) in temp_wta_results.items():
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
                    tournament_store[key] = tourney_players_list

                    for p_name, suffix in itf_name_map.items():
                        if p_name not in arg_names_set: continue
                        if p_name not in schedule_map: schedule_map[p_name] = {}
                        if week in schedule_map[p_name]:
                            if t_name not in schedule_map[p_name][week]: schedule_map[p_name][week] += f"<br>{t_name}{suffix}"
                        else: schedule_map[p_name][week] = f"{t_name}{suffix}"

        match_history_data = get_sheety_matches()

        cleaned_history = []
        for m in match_history_data:
            # Try different possible field names for date
            fecha = (m.get('date') or m.get('Date') or m.get('matchDate') or 
                    m.get('match_date') or m.get('FECHA') or '')
            
            # Create new structure with Spanish column names
            new_match = {
                'FECHA': fecha,
                'TORNEO': m.get('tournamentName') or m.get('tournament_name') or m.get('TournamentName') or '',
                'SUPERFICIE': m.get('surface') or m.get('Surface') or '',
                'RONDA': m.get('roundName') or m.get('round_name') or m.get('RoundName') or '',
                'TENISTA': '',  # Will be filled by JavaScript
                'RESULTADO': '',  # Will be filled by JavaScript (W or L)
                'SCORE': m.get('result') or m.get('Result') or '',
                'RIVAL': '',  # Will be filled by JavaScript
                'PAIS_RIVAL': '',  # Will be filled by JavaScript
                # Keep original names for filtering
                '_winnerName': m.get('winnerName') or m.get('winner_name') or m.get('WinnerName') or '',
                '_loserName': m.get('loserName') or m.get('loser_name') or m.get('LoserName') or '',
                '_winnerCountry': m.get('winnerCountry') or m.get('winner_country') or m.get('WinnerCountry') or '',
                '_loserCountry': m.get('loserCountry') or m.get('loser_country') or m.get('LoserCountry') or ''
            }
            cleaned_history.append(new_match)

        # Sort logic: latest date first
        def parse_match_date(item):
            d = item.get('FECHA') or "1900-01-01"
            try:
                return pd.to_datetime(d)
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
            if name: history_arg_players.add(name.strip().title())
        # Check losers
        if m.get('loserCountry') == 'ARG' or m.get('loser_country') == 'ARG':
            name = m.get('loserName') or m.get('loser_name')
            if name: history_arg_players.add(name.strip().title())

    history_players_sorted = sorted(list(history_arg_players))

    html_template = f"""
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <title>Próximos Torneos</title>
        <style>
            @font-face {{ font-family: 'Montserrat'; src: url('Montserrat-SemiBold.ttf'); }}
            body {{ font-family: 'Montserrat', sans-serif; background: #f0f4f8; margin: 0; display: flex; height: auto; min-height: 100vh; overflow-y: auto; }}
            .app-container {{ display: flex; width: 100%; height: 100%; }}
            .sidebar {{ width: 180px; background: #1e293b; color: white; display: flex; flex-direction: column; flex-shrink: 0; }}
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

            .column-entry thead th {{ position: sticky; top: 0; background: #75AADB !important; color: white; z-index: 10; border-bottom: 2px solid #1e293b; }}
            .column-entry .content-card {{ overflow-y: visible; max-height: none; border: 1px solid black; background: white; box-shadow: 0 4px 20px rgba(0,0,0,0.05); width: 100%; }}
            
            #history-table th {{ background: #75AADB !important; position: sticky; top: 0; z-index: 10; }}
        </style>
    </head>
    <body onload="updateEntryList(); renderHistoryTable();">
        <div class="app-container">
            <div class="sidebar">
                <div class="sidebar-header">Tenistas Argentinas</div>
                <div class="menu-item active" id="btn-upcoming" onclick="switchTab('upcoming')">Próximos Torneos</div>
                <div class="menu-item" id="btn-history" onclick="switchTab('history')">Historial de Partidos</div>
            </div>
            
            <div class="main-content">
                <div id="view-upcoming" class="dual-layout">
                    <div class="column-main">
                        <div class="header-row">
                            <div class="search-container">
                                <input type="text" id="s" placeholder="Buscar tenista..." oninput="filter()">
                            </div>
                            <h1>Próximos Torneos</h1>
                        </div>
                        <div class="content-card">
                            <div class="table-wrapper">
                                <table>
                                    <thead>
                                        <tr>
                                            <th class="sticky-col col-rank">Rank</th>
                                            <th class="sticky-col col-name">Jugadora</th>
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
                                        <th>Jugadora</th>
                                        <th style="width:35px">País</th>
                                        <th style="width:70px">E-Rank</th> 
                                    </tr>
                                </thead>
                                <tbody id="entry-body"></tbody>
                            </table>
                        </div>
                    </div>
                </div>

                <div id="view-history" class="single-layout" style="display: none;">
                    <div class="header-row">
                        <div class="search-container">
                            <select id="playerHistorySelect" onchange="filterHistoryByPlayer()" style="width: 300px;">
                                <option value="">Seleccionar Tenista...</option>
                                {"".join([f'<option value="{name}">{name}</option>' for name in history_players_sorted])}
                            </select>
                        </div>
                        <h1>Historial de Partidos</h1>
                    </div>
                    <div class="content-card">
                        <div class="table-wrapper">
                            <table id="history-table">
                                <thead id="history-head"></thead>
                                <tbody id="history-body">
                                    <tr><td colspan="100%" style="padding: 20px; color: #64748b;">Selecciona una jugadora para ver sus partidos</td></tr>
                                </tbody>
                            </table>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        <script>
            const tournamentData = {json.dumps(tournament_store)};
            const historyData = {json.dumps(cleaned_history)};

            function switchTab(tabName) {{
                document.querySelectorAll('.menu-item').forEach(el => el.classList.remove('active'));
                document.getElementById('btn-' + tabName).classList.add('active');
                
                document.getElementById('view-upcoming').style.display = (tabName === 'upcoming') ? 'flex' : 'none';
                document.getElementById('view-history').style.display = (tabName === 'history') ? 'flex' : 'none';
            }}

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
                const displayColumns = ['FECHA', 'TORNEO', 'SUPERFICIE', 'RONDA', 'TENISTA', 'RESULTADO', 'SCORE', 'RIVAL', 'PAIS_RIVAL'];
                let headHtml = '<tr>';
                displayColumns.forEach(col => {{
                    headHtml += `<th>${{col.replace('_', ' ')}}</th>`;
                }});
                headHtml += '</tr>';
                thead.innerHTML = headHtml;
                
                // Set initial placeholder message
                tbody.innerHTML = `<tr><td colspan="${{displayColumns.length}}" style="padding: 20px; color: #64748b;">Selecciona una jugadora para ver sus partidos</td></tr>`;
            }}

            function filterHistoryByPlayer() {{
                const selectedPlayer = document.getElementById('playerHistorySelect').value.toUpperCase();
                const tbody = document.getElementById('history-body');
                const displayColumns = ['FECHA', 'TORNEO', 'SUPERFICIE', 'RONDA', 'TENISTA', 'RESULTADO', 'SCORE', 'RIVAL', 'PAIS_RIVAL'];
                
                if (!selectedPlayer) {{
                    tbody.innerHTML = `<tr><td colspan="${{displayColumns.length}}" style="padding: 20px;">Selecciona una jugadora...</td></tr>`;
                    return;
                }}

                const filtered = historyData.filter(row => {{
                    const wName = (row['_winnerName'] || "").toString().toUpperCase();
                    const lName = (row['_loserName'] || "").toString().toUpperCase();
                    return wName === selectedPlayer || lName === selectedPlayer;
                }});

                if (filtered.length === 0) {{
                    tbody.innerHTML = `<tr><td colspan="${{displayColumns.length}}" style="padding: 20px;">No se encontraron partidos para esta jugadora.</td></tr>`;
                    return;
                }}

                // Sort by date descending (most recent first)
                filtered.sort((a, b) => {{
                    const dateA = new Date(a['FECHA'] || '1900-01-01');
                    const dateB = new Date(b['FECHA'] || '1900-01-01');
                    return dateB - dateA;
                }});

                let bodyHtml = '';
                filtered.forEach(row => {{
                    const isWinner = (row['_winnerName'] || "").toString().toUpperCase() === selectedPlayer;
                    
                    // Fill in the dynamic columns
                    const rowData = {{
                        'FECHA': row['FECHA'] || '',
                        'TORNEO': row['TORNEO'] || '',
                        'SUPERFICIE': row['SUPERFICIE'] || '',
                        'RONDA': row['RONDA'] || '',
                        'TENISTA': selectedPlayer,
                        'RESULTADO': isWinner ? 'W' : 'L',
                        'SCORE': row['SCORE'] || '',
                        'RIVAL': isWinner ? (row['_loserName'] || '') : (row['_winnerName'] || ''),
                        'PAIS_RIVAL': isWinner ? (row['_loserCountry'] || '') : (row['_winnerCountry'] || '')
                    }};
                    
                    bodyHtml += '<tr>';
                    displayColumns.forEach(col => {{
                        bodyHtml += `<td>${{rowData[col] ?? ''}}</td>`;
                    }});
                    bodyHtml += '</tr>';
                }});
                tbody.innerHTML = bodyHtml;
            }}
        </script>
    </body>
    </html>
    """
    with open("index.html", "w", encoding="utf-8") as f: f.write(html_template)

if __name__ == "__main__":
    main()