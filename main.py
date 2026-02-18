import pandas as pd
import csv
from datetime import datetime, timedelta
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager

from config import ENTRY_LISTS_CACHE_FILE
from utils import (
    fix_encoding, fix_encoding_keep_accents,
    load_cache, save_cache, merge_entry_list
)
from calendar_builder import (
    get_monday_offset, generate_dynamic_monday_map,
    build_calendar_data, get_sheety_matches
)
from wta import (
    build_tournament_groups, get_full_wta_calendar,
    get_wta_rankings_cached, scrape_tournament_players
)
from itf import (
    get_full_itf_calendar, get_itf_players,
    get_dynamic_itf_calendar, get_itf_rankings_cached
)
from html_generator import generate_html

COUNTRY_OVERRIDES = {
    "FRANCESCA MATTIOLI": "ARG",
}


def override_country_for_player(player_name, country_code):
    key = (player_name or "").strip().upper()
    if key in COUNTRY_OVERRIDES:
        return COUNTRY_OVERRIDES[key]
    return country_code


def normalize_country_overrides(rows, name_key, country_key):
    for row in rows or []:
        row[country_key] = override_country_for_player(row.get(name_key, ""), row.get(country_key, ""))
    return rows


def main():
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)

    try:
        # 1. Build tournament groups and maps
        tournament_groups = build_tournament_groups()
        monday_map = generate_dynamic_monday_map(num_weeks=4)
        itf_monday_map = generate_dynamic_monday_map(num_weeks=3)

        # Fetch ITF calendar and merge into tournament_groups
        itf_items = get_dynamic_itf_calendar(driver, num_weeks=3)

        for label in monday_map.values():
            if label not in tournament_groups:
                tournament_groups[label] = {}

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

                tournament_groups[week_label][item['tournamentKey'].lower()] = {
                    "name": t_name,
                    "level": level,
                    "startDate": item['startDate'],
                    "endDate": item.get('endDate', None)
                }

        # 2. Fetch Players & Rankings
        today = datetime.now()
        ranking_monday = (today - timedelta(days=today.weekday())).strftime("%Y-%m-%d")
        all_wta_players = get_wta_rankings_cached(ranking_monday, nationality=None)
        normalize_country_overrides(all_wta_players, "Player", "Country")

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

        for week, tourneys in tournament_groups.items():
            print(f"Processing {week}...")
            week_monday = next((k for k, v in monday_map.items() if v == week), None)
            if week_monday is None: continue

            md_date = get_monday_offset(week_monday, 4)
            q_date = get_monday_offset(week_monday, 3)

            today_date = datetime.now()
            md_datetime = datetime.strptime(md_date, "%Y-%m-%d")
            q_datetime = datetime.strptime(q_date, "%Y-%m-%d")

            # Fetch rankings for this week (cached, so fast after first call)
            if md_datetime > today_date: md_date = (today_date - timedelta(days=today_date.weekday())).strftime("%Y-%m-%d")
            if q_datetime > today_date: q_date = (today_date - timedelta(days=today_date.weekday())).strftime("%Y-%m-%d")

            if md_date not in ranking_cache: ranking_cache[md_date] = get_wta_rankings_cached(md_date, nationality=None)
            if q_date not in ranking_cache: ranking_cache[q_date] = get_wta_rankings_cached(q_date, nationality=None)
            normalize_country_overrides(ranking_cache[md_date], "Player", "Country")
            normalize_country_overrides(ranking_cache[q_date], "Player", "Country")

            # Process WTA tournaments (single pass per tournament)
            for key, t_info in tourneys.items():
                t_name = t_info["name"]
                if key.startswith("http"):
                    t_list, status_dict = scrape_tournament_players(key, ranking_cache[md_date], ranking_cache[q_date], entry_cache.get(key, []))
                    t_list = merge_entry_list(entry_cache.get(key, []), t_list)
                    normalize_country_overrides(t_list, "name", "country")
                    entry_cache[key] = t_list
                    tournament_store[key] = t_list
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

                    tourney_players_list.sort(key=lambda x: (x["pos_num"], x["name"]))
                    tourney_players_list = merge_entry_list(entry_cache.get(key, []), tourney_players_list)
                    normalize_country_overrides(tourney_players_list, "name", "country")
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
        for tourneys in tournament_groups.values():
            active_keys.update(tourneys.keys())
        entry_cache = {k: v for k, v in entry_cache.items() if k in active_keys}

        save_cache(ENTRY_LISTS_CACHE_FILE, entry_cache)

        # 4. Fetch match history
        match_history_data = []
        matches_files = ['itf_fill/itf_matches_arg.csv', 'itf_fill/wta_matches_arg.csv', 'itf_fill/gs_matches_arg.csv']
        for file_path in matches_files:
            try:
                with open(file_path, 'r', encoding='utf-8-sig') as file_obj:
                    reader = csv.DictReader(file_obj, delimiter=',')
                    for row in reader:
                        match_history_data.append(row)
            except Exception as e:
                print(f"Error reading matches data from {file_path}: {e}")

        cleaned_history = []
        for m in match_history_data:
            fecha = (m.get('date') or m.get('Date') or m.get('matchDate') or
                    m.get('match_date') or m.get('FECHA') or '')

            winner_entry = m.get('winnerEntry') or m.get('winner_entry') or m.get('WinnerEntry') or ''
            loser_entry = m.get('loserEntry') or m.get('loser_entry') or m.get('LoserEntry') or ''
            winner_entry = '' if winner_entry == 'DA' else winner_entry
            loser_entry = '' if loser_entry == 'DA' else loser_entry

            raw_round = m.get('roundName') or m.get('round_name') or m.get('RoundName') or ''
            draw_type = m.get('draw') or m.get('Draw') or m.get('DRAW') or ''
            
            if draw_type == 'Q':
                qr_mapping = {
                    '1st Round': 'QR1',
                    '2nd Round': 'QR2',
                    '3rd Round': 'QR3',
                    '4th Round': 'QR4'
                }
                final_round = qr_mapping.get(raw_round, raw_round) 
            else:
                final_round = raw_round

            new_match = {
                'DATE': fecha,
                'TOURNAMENT': fix_encoding(m.get('tournamentName') or m.get('tournament_name') or m.get('TournamentName') or ''),
                'SURFACE': m.get('surface') or m.get('Surface') or '',
                'ROUND': final_round,
                'PLAYER': '',
                'ENTRY': '',
                'SEED': '',
                'RESULT': '',
                'SCORE': m.get('result') or m.get('Result') or '',
                'RIVAL_ENTRY': '',
                'RIVAL_SEED': '',
                'RIVAL': '',
                'RIVAL_COUNTRY': '',
                '_winnerName': fix_encoding_keep_accents(m.get('winnerName') or m.get('winner_name') or m.get('WinnerName') or ''),
                '_loserName': fix_encoding_keep_accents(m.get('loserName') or m.get('loser_name') or m.get('LoserName') or ''),
                '_winnerCountry': m.get('winnerCountry') or m.get('winner_country') or m.get('WinnerCountry') or '',
                '_loserCountry': m.get('loserCountry') or m.get('loser_country') or m.get('LoserCountry') or '',
                '_winnerEntry': winner_entry,
                '_loserEntry': loser_entry,
                '_winnerSeed': m.get('winnerSeed') or m.get('winner_seed') or m.get('WinnerSeed') or '',
                '_loserSeed': m.get('loserSeed') or m.get('loser_seed') or m.get('LoserSeed') or ''
            }
            cleaned_history.append(new_match)

        def parse_match_date(item):
            d = item.get('DATE') or "1900-01-01"
            try:
                return pd.to_datetime(d, dayfirst=False)
            except:
                return pd.to_datetime("1900-01-01")

        cleaned_history.sort(key=parse_match_date, reverse=True)

        # 5. Fetch full-year calendars (ITF needs Selenium)
        full_itf = get_full_itf_calendar(driver)

    finally:
        driver.quit()

    # 6. Build calendar data
    full_wta = get_full_wta_calendar()
    all_calendar_tournaments = full_wta + full_itf
    calendar_data = build_calendar_data(all_calendar_tournaments)

    national_team_data = []
    try:
        with open('national_team_order.csv', 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f, delimiter=';')
            for row in reader:
                national_team_data.append(row)
    except Exception as e:
        print(f"Error reading national team data: {e}")

    # 7. Generate HTML
    generate_html(
        tournament_groups, tournament_store, players_data, schedule_map,
        cleaned_history, calendar_data, match_history_data, all_wta_players,
        national_team_data=national_team_data
    )


if __name__ == "__main__":
    main()
