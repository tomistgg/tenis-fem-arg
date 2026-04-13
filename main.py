import os
import json
import pandas as pd
import csv
import random
import time
from datetime import datetime, timedelta
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager

from config import ENTRY_LISTS_CACHE_FILE, NAME_LOOKUP, repair_name_text
from utils import (
    fix_encoding, fix_encoding_keep_accents,
    load_cache, save_cache, merge_entry_list,
    save_json_file,
    normalize_country_overrides, load_csv_rows
)
from calendar_builder import (
    get_monday_offset, generate_dynamic_monday_map,
    build_calendar_data
)
from wta import (
    build_tournament_groups, get_full_wta_calendar,
    get_wta_rankings_cached, scrape_tournament_players,
    get_draws_tournament_list, _load_wta_csv
)
from itf import (
    get_full_itf_calendar, get_itf_players,
    get_dynamic_itf_calendar, get_itf_rankings_cached,
    get_itf_level, parse_itf_entry_list,
    get_draws_itf_tournament_list
)
from html_generator import generate_html
from draws import fetch_tournament_draws, fetch_itf_tournament_draws
from tstrength import build_tstrength_data

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
TOURNAMENT_SNAPSHOT_FILE = os.path.join(DATA_DIR, "tournament_snapshot.json")
CALENDAR_SNAPSHOT_FILE = os.path.join(DATA_DIR, "calendar_snapshot.json")
PLAYER_ALIASES_WTA_ITF_FILE = os.path.join(DATA_DIR, "player_aliases_wta_itf.json")
DRAWS_STORE_CACHE_FILE = os.path.join(DATA_DIR, "draws_store_cache.json")
ENABLE_ITF_DRAWS_PREFETCH = False


def _canonical_draw_store_key(t_key):
    """Normalize draw cache keys so ITF keys are case-stable across runs."""
    key = str(t_key or "").strip()
    if not key:
        return ""
    if key.lower().startswith("w-itf-"):
        return key.lower()
    return key


def _merge_draw_store_entry(existing, incoming):
    """Merge two draw cache entries, preferring non-empty incoming metadata and newer draw payloads."""
    if not isinstance(existing, dict):
        existing = {}
    if not isinstance(incoming, dict):
        incoming = {}
    if not existing:
        return dict(incoming)
    if not incoming:
        return dict(existing)

    merged = dict(existing)
    for field in ("name", "level", "week", "startDate", "endDate"):
        value = incoming.get(field)
        if value is not None and str(value).strip() != "":
            merged[field] = value

    existing_draws = existing.get("draws") if isinstance(existing.get("draws"), dict) else {}
    incoming_draws = incoming.get("draws") if isinstance(incoming.get("draws"), dict) else {}
    if existing_draws or incoming_draws:
        merged_draws = {}
        merged_draws.update(existing_draws)
        merged_draws.update(incoming_draws)
        merged["draws"] = merged_draws

    return merged


def _normalize_draws_store_keys(draws_store):
    """Collapse case-only ITF key duplicates into a single canonical cache key."""
    if not isinstance(draws_store, dict):
        return {}
    normalized = {}
    for raw_key, tdata in draws_store.items():
        canonical_key = _canonical_draw_store_key(raw_key)
        if not canonical_key:
            continue
        normalized[canonical_key] = _merge_draw_store_entry(normalized.get(canonical_key), tdata)
    return normalized


def _normalize_name_for_lookup(name):
    """Normalize names for cross-source lookups (case/accents/whitespace)."""
    if not name:
        return ""
    return " ".join(fix_encoding(str(name)).strip().upper().split())


def _map_to_display_name_upper(name):
    """Map aliases to display_name (from `player_aliases_wta_itf.json`) when possible."""
    if not name:
        return ""
    raw_upper = " ".join(str(name).strip().upper().split())
    if not raw_upper:
        return ""
    # Try raw first, then encoding/accents-normalised key (common in older datasets).
    alt_upper = _normalize_name_for_lookup(raw_upper)
    return NAME_LOOKUP.get(raw_upper) or NAME_LOOKUP.get(alt_upper) or raw_upper


def _monday_from_date_str(date_str):
    if not date_str:
        return None
    base = str(date_str).strip()
    if len(base) >= 10:
        base = base[:10]
    try:
        d = datetime.strptime(base, "%Y-%m-%d")
    except Exception:
        return None
    monday = d - timedelta(days=d.weekday())
    return monday.strftime("%Y-%m-%d")


def enrich_history_with_wta_ranks(cleaned_history):
    """Add `_winnerRank` / `_loserRank` to cleaned history rows (empty if unknown)."""
    if not cleaned_history:
        return cleaned_history

    # Optional: map ITF-side names to WTA-side names (to resolve rankings even when
    # the match dataset uses ITF spelling while rankings CSV uses WTA spelling).
    aliases_lookup = {}
    itf_id_to_wta_id = {}
    if os.path.exists(PLAYER_ALIASES_WTA_ITF_FILE):
        try:
            with open(PLAYER_ALIASES_WTA_ITF_FILE, "r", encoding="utf-8-sig") as f:
                items = json.load(f)
            if not isinstance(items, list):
                items = []
        except Exception:
            items = []
        for it in items:
            if not isinstance(it, dict):
                continue
            itf_name = repair_name_text(it.get("itf_name")).strip()
            # New format: {display_name,wta_id,wta_name,itf_id,itf_name,bjkc_name}
            itf_id = (it.get("itf_id") or "").strip()
            wta_id = (it.get("wta_id") or "").strip()
            wta_name = repair_name_text(it.get("wta_name")).strip()
            display_name = repair_name_text(it.get("display_name")).strip()
            if itf_id and wta_id and itf_id not in itf_id_to_wta_id:
                itf_id_to_wta_id[itf_id] = wta_id
            cand_norms = []
            for n in [wta_name, display_name]:
                n = str(n or "").strip()
                if not n:
                    continue
                n_norm = _normalize_name_for_lookup(n)
                if n_norm and n_norm not in cand_norms:
                    cand_norms.append(n_norm)
                disp_norm = _normalize_name_for_lookup(_map_to_display_name_upper(n))
                if disp_norm and disp_norm not in cand_norms:
                    cand_norms.append(disp_norm)
            if not itf_name or not cand_norms:
                continue
            # Allow lookups by raw ITF name or by our display-mapped key.
            for k in {_normalize_name_for_lookup(itf_name), _normalize_name_for_lookup(_map_to_display_name_upper(itf_name))}:
                if not k:
                    continue
                if k not in aliases_lookup:
                    aliases_lookup[k] = []
                for cn in cand_norms:
                    if cn not in aliases_lookup[k]:
                        aliases_lookup[k].append(cn)

    csv_by_week = _load_wta_csv() or {}
    week_index_cache = {}

    def _is_itf_id(value):
        s = str(value or "").strip()
        if not s.isdigit():
            return False
        return len(s) >= 9 or s.startswith("800")

    def _index_variants(name):
        """Generate additional lookup keys for common WTA naming variants (e.g., married-name hyphens)."""
        if not name:
            return []
        base_upper = " ".join(str(name).strip().upper().split())
        if not base_upper:
            return []
        out = []
        for cand in [base_upper, base_upper.replace("-", " ")]:
            norm = _normalize_name_for_lookup(cand)
            if norm and norm not in out:
                out.append(norm)
        parts = base_upper.split()
        if len(parts) >= 2 and any("-" in p for p in parts[1:]):
            stripped = parts[:]
            for i in range(1, len(stripped)):
                if "-" in stripped[i]:
                    stripped[i] = stripped[i].split("-")[0]
            norm = _normalize_name_for_lookup(" ".join(stripped))
            if norm and norm not in out:
                out.append(norm)
        return out

    def week_index(week_date):
        if week_date in week_index_cache:
            return week_index_cache[week_date]
        idx_by_name = {}
        idx_by_id = {}
        for p in (csv_by_week.get(week_date) or []):
            r = p.get("Rank", "")
            if r is None or r == "":
                continue
            raw = p.get("OfficialPlayer") or p.get("Player", "")
            rank_str = str(r)
            pid = str(p.get("Id") or "").strip()
            if pid:
                idx_by_id[pid] = rank_str
            for key_name in [raw, _map_to_display_name_upper(raw)]:
                for k in _index_variants(key_name):
                    idx_by_name[k] = rank_str
        week_index_cache[week_date] = (idx_by_name, idx_by_id)
        return idx_by_name, idx_by_id

    def resolve_rank(name_raw, idx):
        """Resolve a ranking for a raw name using direct and alias-based lookups."""
        if not name_raw:
            return ""
        raw_norm = _normalize_name_for_lookup(name_raw)
        disp_norm = _normalize_name_for_lookup(_map_to_display_name_upper(name_raw))
        rank = idx.get(disp_norm) or idx.get(raw_norm) or ""
        if rank:
            return rank
        # Alias-based lookup: try ITF name -> WTA name candidates.
        for k in (disp_norm, raw_norm):
            if not k:
                continue
            for cand in (aliases_lookup.get(k) or []):
                rank = idx.get(cand) or ""
                if rank:
                    return rank
        return ""

    def resolve_rank_by_ids(name_raw, player_id_raw, idx_by_name, idx_by_id):
        """Resolve rank preferring WTA id lookups (direct or ITF-id->WTA-id via aliases JSON)."""
        pid = str(player_id_raw or "").strip()
        wta_id = ""
        if pid.isdigit():
            if _is_itf_id(pid):
                wta_id = itf_id_to_wta_id.get(pid, "")
            else:
                wta_id = pid
        if wta_id:
            rank = idx_by_id.get(wta_id) or ""
            if rank:
                return rank
        return resolve_rank(name_raw, idx_by_name)

    for row in cleaned_history:
        row["_winnerRank"] = ""
        row["_loserRank"] = ""
        week_date = _monday_from_date_str(row.get("DATE", ""))
        if not week_date or week_date not in csv_by_week:
            continue
        idx_by_name, idx_by_id = week_index(week_date)
        row["_winnerRank"] = resolve_rank_by_ids(
            row.get("_winnerName", ""), row.get("_winnerId", ""), idx_by_name, idx_by_id
        )
        row["_loserRank"] = resolve_rank_by_ids(
            row.get("_loserName", ""), row.get("_loserId", ""), idx_by_name, idx_by_id
        )

    return cleaned_history


def create_driver():
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("window-size=1920,1080")
    chrome_options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
    )
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
    try:
        driver.set_page_load_timeout(60)
        driver.set_script_timeout(30)
    except Exception:
        pass
    return driver


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
        t_name = item['tournamentName']
        if 'cancel' in t_name.lower():
            continue
        s_date = pd.to_datetime(item['startDate'])
        monday_date = (s_date - timedelta(days=s_date.weekday())).strftime('%Y-%m-%d')
        if monday_date in itf_monday_map:
            week_label = itf_monday_map[monday_date]
            tournament_groups[week_label][item['tournamentKey'].lower()] = {
                "name": t_name,
                "level": get_itf_level(t_name),
                "startDate": item['startDate'],
                "endDate": item.get('endDate', None)
            }

    tournament_snapshot = {}
    for week, tourneys in tournament_groups.items():
        for key, info in tourneys.items():
            if 'cancel' in info.get("name", "").lower():
                continue
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

    mondays = sorted(monday_map.keys())
    total_weeks = len(mondays) or 4

    for i, week_monday in enumerate(mondays, start=1):
        print(f"Processing Tournaments ({i}/{total_weeks})")
        week = monday_map.get(week_monday)
        if not week:
            continue
        tourneys = tournament_groups.get(week, {})

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
            if 'cancel' in t_name.lower():
                continue
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
    active_itf_keys = set()
    for tourneys in tournament_groups.values():
        for t_key in tourneys.keys():
            active_keys.add(t_key)
            if not str(t_key).startswith("http"):
                active_itf_keys.add(t_key)

    # Defensive fallback: when ITF calendar fetch fails for a run, keep previous ITF
    # entry lists instead of deleting them from cache.
    if not active_itf_keys:
        for cached_key in entry_cache.keys():
            if not str(cached_key).startswith("http"):
                active_keys.add(cached_key)

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
        os.path.join(DATA_DIR, 'bjkc_matches_arg.csv'),
        os.path.join(DATA_DIR, 'united_cup_matches_arg.csv'),
        os.path.join(DATA_DIR, 'manually_added_matches.csv'),
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

        tournament_id_value = (m.get('tournamentId') or m.get('tournament_id') or m.get('TournamentId') or '').strip()
        winner_id_value = (m.get('winnerId') or m.get('winner_id') or m.get('WinnerId') or '').strip()
        loser_id_value = (m.get('loserId') or m.get('loser_id') or m.get('LoserId') or '').strip()

        cleaned_history.append({
            'DATE': fecha,
            'TOURNAMENT': fix_encoding(tournament_name_value),
            'TOURNAMENT_ID': tournament_id_value,
            'CATEGORY': fix_encoding(tournament_category_value),
            'SURFACE': formatted_surface,
            'MATCH_TYPE': match_type_value,
            'DRAW': draw_type,
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
            '_winnerId': winner_id_value,
            '_loserId': loser_id_value,
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
    itf_draws_tournaments = {}
    prefetched_itf_draws = {}
    try:
        # 1. Fetch full-year ITF calendar first (populates cache for dynamic subset)
        full_itf = get_full_itf_calendar(driver)

        # 2. Build tournament groups (WTA + ITF) — uses cached ITF data
        tournament_groups, monday_map = build_all_tournament_groups(driver)

        # 2b. Fetch ITF draws tournament list and prefetch draw payloads before
        # heavier ITF traffic later in the run.
        print("Fetching ITF draws tournament list...")
        itf_draws_tournaments = get_draws_itf_tournament_list(driver)
        if ENABLE_ITF_DRAWS_PREFETCH:
            itf_prefetch_jobs = []
            for week, tourneys in (itf_draws_tournaments or {}).items():
                for t_key, t_info in (tourneys or {}).items():
                    tid = (t_info or {}).get("tournamentId")
                    if not tid:
                        continue
                    itf_prefetch_jobs.append((week, t_key, t_info))

            total_itf_prefetch = len(itf_prefetch_jobs) or 1
            for i, (week, t_key, t_info) in enumerate(itf_prefetch_jobs, start=1):
                print(f"Prefetching ITF Draws ({i}/{total_itf_prefetch})")
                tid = t_info.get("tournamentId")
                is_multiweek = t_info.get("is_multiweek", False)
                dvr = create_driver()
                try:
                    t_draws = fetch_itf_tournament_draws(
                        tid, is_multiweek=is_multiweek, driver=dvr
                    ) or {}
                finally:
                    try:
                        dvr.quit()
                    except Exception:
                        pass
                if t_draws:
                    prefetched_itf_draws[_canonical_draw_store_key(t_key)] = t_draws

        # 3. Fetch ARG player rankings
        players_data, arg_names_set, all_wta_players = fetch_arg_players()

        # 4. Process tournament entry lists
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

        # 5. Load match history
        match_history_data, cleaned_history = load_match_history()
        enrich_history_with_wta_ranks(cleaned_history)
        # Always rebuild history_data.json on each run
        try:
            history_data_path = os.path.join(DATA_DIR, "history_data.json")
            with open(history_data_path, "w", encoding="utf-8") as f:
                json.dump(cleaned_history or [], f, ensure_ascii=False, separators=(",", ":"))
        except Exception as e:
            print(f"Error writing history_data.json: {e}")

    finally:
        driver.quit()

    # 6. Fetch draws (WTA + ITF). Keep a persistent cache so draws don't "disappear"
    # when a fetch fails temporarily.
    draws_store = load_cache(DRAWS_STORE_CACHE_FILE) or {}
    if not isinstance(draws_store, dict):
        draws_store = {}
    draws_store = _normalize_draws_store_keys(draws_store)
    draws_tournaments = get_draws_tournament_list()
    current_year = str(datetime.now().year)
    wta_draw_jobs = []
    for week, tourneys in (draws_tournaments or {}).items():
        for t_key, t_info in (tourneys or {}).items():
            wta_draw_jobs.append((week, t_key, t_info))

    total_wta_draws = len(wta_draw_jobs) or 1
    for i, (week, t_key, t_info) in enumerate(wta_draw_jobs, start=1):
        print(f"Fetching WTA Draws ({i}/{total_wta_draws})")
        store_key = _canonical_draw_store_key(t_key)
        prev = draws_store.get(store_key) if isinstance(draws_store.get(store_key), dict) else {}
        prev_draws = (prev or {}).get("draws") or {}
        t_draws = fetch_tournament_draws(t_key, current_year) or {}
        merged_draws = {}
        if isinstance(prev_draws, dict):
            merged_draws.update(prev_draws)
        if isinstance(t_draws, dict):
            merged_draws.update(t_draws)
        if merged_draws:
            if not t_draws and prev_draws:
                print(f"  Using cached WTA draws for: {t_info.get('name','')}")
            draws_store[store_key] = {
                "name": t_info["name"],
                "level": t_info.get("level", ""),
                "week": week,
                "startDate": t_info.get("startDate"),
                "endDate": t_info.get("endDate"),
                "draws": merged_draws,
            }

    # 6b. Fetch ITF draws (prefer prefetched payloads captured earlier in the run)
    itf_draw_jobs = []
    for week, tourneys in (itf_draws_tournaments or {}).items():
        for t_key, t_info in (tourneys or {}).items():
            tid = (t_info or {}).get("tournamentId")
            if not tid:
                continue
            itf_draw_jobs.append((week, t_key, t_info))

    total_itf_draws = len(itf_draw_jobs) or 1
    itf_cooloff_applied = False
    itf_consecutive_empty = 0
    for i, (week, t_key, t_info) in enumerate(itf_draw_jobs, start=1):
        print(f"Fetching ITF Draws ({i}/{total_itf_draws})")
        # Keep request cadence gentle to avoid ITF anti-bot throttling.
        if i > 1:
            time.sleep(random.uniform(0.8, 1.8))
        tid = t_info.get("tournamentId")
        is_multiweek = t_info.get("is_multiweek", False)
        store_key = _canonical_draw_store_key(t_key)
        prev = draws_store.get(store_key) if isinstance(draws_store.get(store_key), dict) else {}
        prev_draws = (prev or {}).get("draws") or {}

        t_draws = (
            prefetched_itf_draws.get(store_key)
            if isinstance(prefetched_itf_draws.get(store_key), dict)
            else {}
        )
        if not t_draws:
            # Use a fresh browser session per tournament; ITF anti-bot filtering can
            # degrade a long-lived session and return blocked HTML for API endpoints.
            itf_draw_driver = create_driver()
            try:
                t_draws = fetch_itf_tournament_draws(
                    tid, is_multiweek=is_multiweek, driver=itf_draw_driver
                ) or {}
            finally:
                try:
                    itf_draw_driver.quit()
                except Exception:
                    pass
        if (
            not t_draws
            and not itf_cooloff_applied
            and i == 1
            and len(itf_draw_jobs) >= 6
        ):
            # ITF often enforces a short temporary block after the tournament-id burst.
            # Wait once, then retry the same event with a fresh session.
            print("  ITF cooldown triggered (65s) before retrying draw fetch...")
            time.sleep(65)
            itf_cooloff_applied = True
            retry_after_cooloff_driver = create_driver()
            try:
                t_draws = fetch_itf_tournament_draws(
                    tid, is_multiweek=is_multiweek, driver=retry_after_cooloff_driver
                ) or {}
            finally:
                try:
                    retry_after_cooloff_driver.quit()
                except Exception:
                    pass

        merged_draws = {}
        if isinstance(prev_draws, dict):
            merged_draws.update(prev_draws)
        if isinstance(t_draws, dict):
            merged_draws.update(t_draws)
        # Extra gap-fill pass: if one ITF draw type is missing, try a couple of
        # fresh sessions to recover the other type before falling back to cache.
        if set(merged_draws.keys()) != {"MDS", "QS"}:
            for _ in range(2):
                retry_driver = create_driver()
                try:
                    extra_draws = fetch_itf_tournament_draws(
                        tid, is_multiweek=is_multiweek, driver=retry_driver
                    ) or {}
                finally:
                    try:
                        retry_driver.quit()
                    except Exception:
                        pass
                if isinstance(extra_draws, dict):
                    merged_draws.update(extra_draws)
                if set(merged_draws.keys()) == {"MDS", "QS"}:
                    break
        if merged_draws:
            itf_consecutive_empty = 0
            if not t_draws and prev_draws:
                print(f"  Using cached ITF draws for: {t_info.get('name','')}")
            draws_store[store_key] = {
                "name": t_info["name"],
                "level": t_info.get("level", ""),
                "week": week,
                "startDate": t_info.get("startDate"),
                "endDate": t_info.get("endDate"),
                "draws": merged_draws,
            }
        else:
            itf_consecutive_empty += 1
            if itf_consecutive_empty >= 3 and i < total_itf_draws:
                # Back off periodically to recover from temporary ITF throttling.
                print("  ITF backoff triggered (35s) after consecutive empty draws...")
                time.sleep(35)
                itf_consecutive_empty = 0

    # Prune draws for tournaments that are definitely over (endDate < today).
    today = datetime.now().date()
    keys_to_delete = []
    for t_key, tdata in (draws_store or {}).items():
        if not isinstance(tdata, dict):
            continue
        end = (tdata.get("endDate") or "")[:10]
        if not end:
            continue
        try:
            end_date = datetime.strptime(end, "%Y-%m-%d").date()
        except Exception:
            continue
        if end_date < today:
            keys_to_delete.append(t_key)
    for t_key in keys_to_delete:
        draws_store.pop(t_key, None)

    # Persist draws cache so a successful draw doesn't disappear on a later failed run.
    save_json_file(DRAWS_STORE_CACHE_FILE, draws_store)

    # Save draws snapshot (tournament key -> list of draw types available)
    draws_snapshot = {}
    for t_key, tdata in draws_store.items():
        draws_snapshot[t_key] = {
            "name": tdata["name"],
            "types": list(tdata.get("draws", {}).keys()),
        }
    save_json_file(os.path.join(DATA_DIR, "draws_snapshot.json"), draws_snapshot)

    # 7. Build calendar — uses cached WTA data
    full_wta = get_full_wta_calendar()
    calendar_data = build_calendar_data(full_wta + full_itf)
    build_calendar_snapshot(calendar_data)

    # 7b. Build tournament strength data (cached)
    print("Processing WTA Tournament Strength")
    tstrength_data = build_tstrength_data()

    # 8. Generate HTML
    national_team_data = load_csv_rows(os.path.join(DATA_DIR, 'national_team_order.csv'), delimiter=';')
    captains_data = load_csv_rows(os.path.join(DATA_DIR, 'captains.csv'))

    generate_html(
        tournament_groups, tournament_store, players_data, schedule_map,
        cleaned_history, calendar_data, match_history_data, all_wta_players,
        national_team_data=national_team_data,
        captains_data=captains_data,
        draws_data=draws_store,
        tstrength_data=tstrength_data
    )


if __name__ == "__main__":
    main()
