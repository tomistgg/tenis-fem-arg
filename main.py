import os
import pandas as pd
import csv
from datetime import datetime, timedelta
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager

from config import ENTRY_LISTS_CACHE_FILE, NAME_LOOKUP
from utils import (
    fix_encoding, fix_encoding_keep_accents,
    load_cache, save_cache, merge_entry_list,
    save_json_file, normalize_country_overrides, load_csv_rows
)
from calendar_builder import (
    get_monday_offset, generate_dynamic_monday_map,
    build_calendar_data
)
from wta import (
    build_tournament_groups, get_full_wta_calendar,
    get_wta_rankings_cached, scrape_tournament_players
)
from itf import (
    get_full_itf_calendar, get_itf_players,
    get_dynamic_itf_calendar, get_itf_rankings_cached,
    get_itf_level, parse_itf_entry_list
)
from html_generator import generate_html

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
TOURNAMENT_SNAPSHOT_FILE = os.path.join(DATA_DIR, "tournament_snapshot.json")
CALENDAR_SNAPSHOT_FILE = os.path.join(DATA_DIR, "calendar_snapshot.json")


def create_driver():
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)


def build_all_tournament_groups(driver):
    """Merge WTA tournament groups with ITF calendar and save snapshot."""
    tournament_groups = build_tournament_groups()
    monday_map = generate_dynamic_monday_map(num_weeks=4)
    itf_monday_map = generate_dynamic_monday_map(num_weeks=3)

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
            tournament_groups[week_label][item['tournamentKey'].lower()] = {
                "name": t_name,
                "level": get_itf_level(t_name),
                "startDate": item['startDate'],
                "endDate": item.get('endDate', None)
            }

    tournament_snapshot = {}
    for week, tourneys in tournament_groups.items():
        for key, info in tourneys.items():
            tournament_snapshot[key] = {
                "name": info.get("name", key),
                "level": info.get("level", ""),
                "startDate": info.get("startDate"),
                "endDate": info.get("endDate"),
                "week": week,
            }
    save_json_file(TOURNAMENT_SNAPSHOT_FILE, tournament_snapshot)

    return tournament_groups, monday_map


def fetch_arg_players():
    """Fetch WTA+ITF rankings and return deduplicated ARG player list."""
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

    return players_data, arg_names_set, all_wta_players


def process_tournaments(driver, tournament_groups, monday_map, arg_names_set, entry_cache):
    """Process WTA & ITF tournaments: scrape entry lists, build schedule map."""
    schedule_map = {}
    tournament_store = {}
    ranking_cache = {}
    unranked_schedule = {}

    for week, tourneys in tournament_groups.items():
        print(f"Processing {week}...")
        week_monday = next((k for k, v in monday_map.items() if v == week), None)
        if week_monday is None:
            continue

        md_date = get_monday_offset(week_monday, 4)
        q_date = get_monday_offset(week_monday, 3)

        today_date = datetime.now()
        md_datetime = datetime.strptime(md_date, "%Y-%m-%d")
        q_datetime = datetime.strptime(q_date, "%Y-%m-%d")

        if md_datetime > today_date:
            md_date = (today_date - timedelta(days=today_date.weekday())).strftime("%Y-%m-%d")
        if q_datetime > today_date:
            q_date = (today_date - timedelta(days=today_date.weekday())).strftime("%Y-%m-%d")

        if md_date not in ranking_cache:
            ranking_cache[md_date] = get_wta_rankings_cached(md_date, nationality=None)
        if q_date not in ranking_cache:
            ranking_cache[q_date] = get_wta_rankings_cached(q_date, nationality=None)
        normalize_country_overrides(ranking_cache[md_date], "Player", "Country")
        normalize_country_overrides(ranking_cache[q_date], "Player", "Country")

        # WTA tournaments
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
                    if p_key not in arg_names_set:
                        continue
                    if p_key not in schedule_map:
                        schedule_map[p_key] = {}
                    if week in schedule_map[p_key]:
                        schedule_map[p_key][week] += f'<div style="margin-top: 3px;">{t_name}{suffix}</div>'
                    else:
                        schedule_map[p_key][week] = f"{t_name}{suffix}"
                for p in t_list:
                    p_upper = p['name'].upper()
                    if p_upper in arg_names_set:
                        continue
                    if p.get('country', '') != 'ARG':
                        continue
                    suffix = '' if p.get('type') == 'MAIN' else ' (Q)'
                    if p_upper not in unranked_schedule:
                        unranked_schedule[p_upper] = {}
                    if week in unranked_schedule[p_upper]:
                        if t_name not in unranked_schedule[p_upper][week]:
                            unranked_schedule[p_upper][week] += f'<div style="margin-top: 3px;">{t_name}{suffix}</div>'
                    else:
                        unranked_schedule[p_upper][week] = f"{t_name}{suffix}"

        # ITF tournaments
        for key, t_info in tourneys.items():
            t_name = t_info["name"]
            if not key.startswith("http"):
                itf_entries, itf_name_map = get_itf_players(key, driver)
                tourney_players_list = parse_itf_entry_list(itf_entries)
                tourney_players_list = merge_entry_list(entry_cache.get(key, []), tourney_players_list)
                normalize_country_overrides(tourney_players_list, "name", "country")
                entry_cache[key] = tourney_players_list
                tournament_store[key] = tourney_players_list

                for p_name, suffix in itf_name_map.items():
                    if p_name not in arg_names_set:
                        continue
                    if p_name not in schedule_map:
                        schedule_map[p_name] = {}
                    if week in schedule_map[p_name]:
                        if t_name not in schedule_map[p_name][week]:
                            schedule_map[p_name][week] += f"<br>{t_name}{suffix}"
                    else:
                        schedule_map[p_name][week] = f"{t_name}{suffix}"
                for p in tourney_players_list:
                    raw_upper = p['name'].upper()
                    p_key = NAME_LOOKUP.get(raw_upper, raw_upper)
                    if p_key in arg_names_set:
                        continue
                    if p.get('country', '') != 'ARG':
                        continue
                    p_type = p.get('type', '')
                    if p_type == 'MAIN':
                        suffix = ''
                    elif p_type == 'QUAL':
                        suffix = ' (Q)'
                    else:
                        suffix = f" (ALT {p.get('pos', '')})" if p.get('pos') else ' (ALT)'
                    if p_key not in unranked_schedule:
                        unranked_schedule[p_key] = {}
                    if week in unranked_schedule[p_key]:
                        if t_name not in unranked_schedule[p_key][week]:
                            unranked_schedule[p_key][week] += f"<br>{t_name}{suffix}"
                    else:
                        unranked_schedule[p_key][week] = f"{t_name}{suffix}"

    # Remove tournaments no longer in the next 4 weeks
    active_keys = set()
    for tourneys in tournament_groups.values():
        active_keys.update(tourneys.keys())
    entry_cache = {k: v for k, v in entry_cache.items() if k in active_keys}

    return schedule_map, tournament_store, entry_cache, unranked_schedule


def load_match_history():
    """Read all match CSV files and return raw + cleaned/normalized rows."""
    match_history_data = []
    matches_files = [
        os.path.join(DATA_DIR, 'itf_matches_arg.csv'),
        os.path.join(DATA_DIR, 'wta_matches_arg.csv'),
        os.path.join(DATA_DIR, 'gs_matches_arg.csv'),
        os.path.join(DATA_DIR, 'og_matches_arg.csv'),
        os.path.join(DATA_DIR, 'fed_bjkc_matches_arg.csv'),
    ]
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
        match_type_value = (m.get('matchType') or m.get('MatchType') or m.get('MATCH_TYPE') or '').strip()
        tournament_category_value = (m.get('tournamentCategory') or m.get('tournament_category') or m.get('TournamentCategory') or '').strip()
        tournament_name_value = (m.get('tournamentName') or m.get('tournament_name') or m.get('TournamentName') or '').strip()

        final_round = raw_round

        raw_surface = m.get('surface') or m.get('Surface') or ''
        in_or_outdoor = m.get('inOrOutdoor') or m.get('InOrOutdoor') or ''
        if raw_surface.startswith('I.'):
            formatted_surface = 'Ind. ' + raw_surface[2:].capitalize()
        elif in_or_outdoor == 'I':
            formatted_surface = 'Ind. ' + raw_surface
        else:
            formatted_surface = raw_surface

        cleaned_history.append({
            'DATE': fecha,
            'TOURNAMENT': fix_encoding(tournament_name_value),
            'CATEGORY': fix_encoding(tournament_category_value),
            'SURFACE': formatted_surface,
            'MATCH_TYPE': match_type_value,
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
            '_loserSeed': m.get('loserSeed') or m.get('loser_seed') or m.get('LoserSeed') or '',
            '_resultStatusDesc': m.get('resultStatusDesc') or m.get('result_status_desc') or m.get('ResultStatusDesc') or ''
        })

    def parse_match_date(item):
        d = item.get('DATE') or "1900-01-01"
        try:
            return pd.to_datetime(d, dayfirst=False)
        except:
            return pd.to_datetime("1900-01-01")

    cleaned_history.sort(key=parse_match_date, reverse=True)

    return match_history_data, cleaned_history


def build_calendar_snapshot(calendar_data):
    """Deduplicate calendar data into snapshot list and save JSON."""
    calendar_snapshot = []
    seen = set()
    for week in calendar_data:
        week_label = week.get("week_label", "")
        columns = week.get("columns", {})
        for column_name, continents in columns.items():
            for continent, tournaments in continents.items():
                for t in tournaments:
                    key = (
                        week_label, column_name, continent,
                        t.get("name", ""), t.get("level", ""), t.get("surface", ""),
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    calendar_snapshot.append({
                        "week_label": week_label,
                        "column": column_name,
                        "continent": continent,
                        "name": t.get("name", ""),
                        "level": t.get("level", ""),
                        "surface": t.get("surface", ""),
                    })
    save_json_file(CALENDAR_SNAPSHOT_FILE, calendar_snapshot)


def main():
    driver = create_driver()
    try:
        # 1. Build tournament groups (WTA + ITF)
        tournament_groups, monday_map = build_all_tournament_groups(driver)

        # 2. Fetch ARG player rankings
        players_data, arg_names_set, all_wta_players = fetch_arg_players()

        # 3. Process tournament entry lists
        entry_cache = load_cache(ENTRY_LISTS_CACHE_FILE)
        schedule_map, tournament_store, entry_cache, unranked_schedule = process_tournaments(
            driver, tournament_groups, monday_map, arg_names_set, entry_cache
        )
        save_cache(ENTRY_LISTS_CACHE_FILE, entry_cache)

        # Add unranked ARG players found in entry lists to players_data and schedule_map
        existing_player_keys = {p['Player'] for p in players_data}
        for name_upper, weeks in unranked_schedule.items():
            schedule_map[name_upper] = weeks
            if name_upper not in existing_player_keys:
                players_data.append({
                    'Player': name_upper,
                    'Key': name_upper,
                    'Rank': '-'
                })

        # 4. Load match history
        match_history_data, cleaned_history = load_match_history()

        # 5. Fetch full-year ITF calendar (needs Selenium)
        full_itf = get_full_itf_calendar(driver)
    finally:
        driver.quit()

    # 6. Build calendar
    full_wta = get_full_wta_calendar()
    calendar_data = build_calendar_data(full_wta + full_itf)
    build_calendar_snapshot(calendar_data)

    # 7. Generate HTML
    national_team_data = load_csv_rows(os.path.join(DATA_DIR, 'national_team_order.csv'), delimiter=';')
    captains_data = load_csv_rows(os.path.join(DATA_DIR, 'captains.csv'))

    generate_html(
        tournament_groups, tournament_store, players_data, schedule_map,
        cleaned_history, calendar_data, match_history_data, all_wta_players,
        national_team_data=national_team_data,
        captains_data=captains_data
    )


if __name__ == "__main__":
    main()
