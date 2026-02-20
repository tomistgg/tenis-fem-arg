import os
import json
import unicodedata

import csv

from config import (
    TOURNAMENT_NAME_OVERRIDES, CITY_CASE_FIXES,
    COUNTRY_TO_CONTINENT, COUNTRY_OVERRIDES
)


def format_player_name(text):
    if not text:
        return ""
    return text.title()


def fix_encoding(text):
    """Fix encoding issues and normalize special characters"""
    if not text:
        return ""
    try:
        if '\u00c3' in text or '\u00c3\u00a1' in text or '\u00c3\u00a9' in text or '\u00c3\u00ad' in text or '\u00c3\u00b3' in text or '\u00c3\u00ba' in text:
            text = text.encode('latin-1').decode('utf-8')
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass
    try:
        nfkd_form = unicodedata.normalize('NFKD', text)
        text_without_accents = "".join([c for c in nfkd_form if not unicodedata.combining(c)])
        return text_without_accents
    except:
        return text


def fix_encoding_keep_accents(text):
    """Fix encoding issues but preserve accents"""
    if not text:
        return ""
    try:
        if '\u00c3' in text or '\u00c3\u00a1' in text or '\u00c3\u00a9' in text or '\u00c3\u00ad' in text or '\u00c3\u00b3' in text or '\u00c3\u00ba' in text:
            text = text.encode('latin-1').decode('utf-8')
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass
    return text


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
    """Get rankings from cache or fetch if needed."""
    cache = load_cache(cache_file)
    if date_str in cache:
        return cache[date_str]
    new_data = fetch_func(date_str, nationality=nationality)
    if new_data:
        cache[date_str] = new_data
        save_cache(cache_file, cache)
        return new_data

    # Fallback: if current fetch fails/returns empty, use the latest cached snapshot.
    if cache:
        latest_key = sorted(cache.keys())[-1]
        return cache.get(latest_key, [])
    return []


def fix_display_name(name):
    """Apply tournament name overrides and city casing fixes."""
    base = name.split(" Qualifying")[0]
    is_qual = name.endswith(" Qualifying")
    if base in TOURNAMENT_NAME_OVERRIDES:
        name = TOURNAMENT_NAME_OVERRIDES[base] + (" Qualifying" if is_qual else "")
    for wrong, right in CITY_CASE_FIXES.items():
        name = name.replace(wrong, right)
    return name


def get_tournament_sort_order(level):
    level_order = {
        "GrandSlam": 0, "Grand Slam": 0, "grandSlam": 0,
        "WTA1000": 1, "WTA 1000": 1,
        "WTA500": 2, "WTA 500": 2,
        "WTA250": 3, "WTA 250": 3,
        "WTA125": 4, "WTA 125": 4,
        "W100": 5, "W75": 6, "W60": 7,
        "W50": 8, "W35": 9, "W25": 10, "W15": 11
    }
    return level_order.get(level, 99)


def get_continent(country_code):
    """Map country code to continent key."""
    return COUNTRY_TO_CONTINENT.get((country_code or "").upper(), "europe")


def get_calendar_column(level):
    """Map tournament level to one of the 3 calendar columns."""
    lv = level.lower().replace(" ", "")
    if lv in ("grandslam", "wta1000", "wta500", "wta250", "finals", "wtafinals"):
        return "wta_tour"
    elif lv in ("wta125",):
        return "wta_125"
    else:
        return "itf"


def get_surface_class(surface):
    """Map surface string to CSS class."""
    s = (surface or "").lower()
    if "clay" in s:
        return "cal-clay"
    elif "grass" in s:
        return "cal-grass"
    else:
        return "cal-hard"


def save_json_file(path, payload):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def override_country_for_player(player_name, country_code):
    key = (player_name or "").strip().upper()
    if key in COUNTRY_OVERRIDES:
        return COUNTRY_OVERRIDES[key]
    return country_code


def normalize_country_overrides(rows, name_key, country_key):
    for row in rows or []:
        row[country_key] = override_country_for_player(row.get(name_key, ""), row.get(country_key, ""))
    return rows


def load_csv_rows(file_path, delimiter=','):
    rows = []
    try:
        with open(file_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f, delimiter=delimiter)
            for row in reader:
                rows.append(row)
    except Exception as e:
        print(f"Error reading {file_path}: {e}")
    return rows
