import re
import time
import requests
import unicodedata
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

import csv as _csv
import os as _os

from config import (
    API_URL, HEADERS, NAME_LOOKUP,
    WTA_RANKINGS_CSV, WTA_RANKINGS_CSV_10_19,
    WTA_RANKINGS_CSV_00_09, WTA_RANKINGS_CSV_83_99
)
from utils import fix_display_name, format_player_name
from calendar_builder import get_next_monday, get_monday_from_date, format_week_label


_wta_tournaments_raw = None  # module-level cache for raw WTA tournament API data
_REQUESTS_SESSION = requests.Session()


class WtaApiRateLimited(RuntimeError):
    pass


def _fetch_wta_tournaments_raw():
    """Fetch all WTA tournaments from 1 week ago to end of year (single API call)."""
    global _wta_tournaments_raw
    if _wta_tournaments_raw is not None:
        return _wta_tournaments_raw

    today = datetime.now()
    next_monday = get_next_monday()
    from_date = (next_monday - timedelta(days=7)).strftime("%Y-%m-%d")
    to_date = f"{today.year}-12-31"

    url = "https://api.wtatennis.com/tennis/tournaments/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
        "referer": "https://www.wtatennis.com/",
        "account": "wta"
    }
    params = {
        "page": 0,
        "pageSize": 200,
        "excludeLevels": "ITF",
        "from": from_date,
        "to": to_date
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        data = response.json()
        _wta_tournaments_raw = data.get("content", [])
    except Exception as e:
        print(f"Error fetching WTA tournaments: {e}")
        _wta_tournaments_raw = []

    return _wta_tournaments_raw


def build_tournament_groups():
    next_monday = get_next_monday()
    four_weeks_later = next_monday + timedelta(weeks=4)

    raw_tournaments = _fetch_wta_tournaments_raw()

    tournament_groups = {}

    for tournament in raw_tournaments:
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
        end_date = tournament.get("endDate", None)

        monday = get_monday_from_date(start_date)

        if not (next_monday <= monday < four_weeks_later):
            continue

        week_label = format_week_label(monday)

        t_url = f"https://www.wtatennis.com/tournaments/{tournament_id}/{name}/{year}/player-list"
        if level.lower().replace(" ", "") == "grandslam":
            display_name = f"Grand Slam {city}{suffix}"
        else:
            display_name = f"{level} {city}{suffix}"
        display_name = fix_display_name(display_name)

        if week_label not in tournament_groups:
            tournament_groups[week_label] = {}

        tournament_groups[week_label][t_url] = {
            "name": display_name,
            "level": level,
            "startDate": start_date,
            "endDate": end_date
        }

    return tournament_groups


_WTA_TWO_WEEK_NAMES = [
    'Australian Open', 'Roland Garros', 'Wimbledon', 'US Open',
    'Indian Wells', 'Miami', 'Madrid', 'Rome', 'Internazionali'
]


def _is_two_week_wta(level, raw_name, city, display_name):
    if level.lower().replace(" ", "") == "grandslam":
        return True
    hay = " ".join([raw_name or "", city or "", display_name or ""]).lower()
    return any(n.lower() in hay for n in _WTA_TWO_WEEK_NAMES)


def get_draws_tournament_list():
    """Get WTA tournaments for the draws page.

    Show current + next week. Only include last week if the event is a 2-week tournament.
    """
    today = datetime.now()
    current_monday = today - timedelta(days=today.weekday())
    current_monday = current_monday.replace(hour=0, minute=0, second=0, microsecond=0)
    past_monday = current_monday - timedelta(weeks=1)
    two_weeks_later = current_monday + timedelta(weeks=2)

    raw_tournaments = _fetch_wta_tournaments_raw()
    result = {}

    for tournament in raw_tournaments:
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
        end_date = tournament.get("endDate", None)

        monday = get_monday_from_date(start_date)

        week_label = format_week_label(monday)
        t_url = f"https://www.wtatennis.com/tournaments/{tournament_id}/{name}/{year}/player-list"
        if level.lower().replace(" ", "") == "grandslam":
            display_name = f"Grand Slam {city}{suffix}"
        else:
            display_name = f"{level} {city}{suffix}"
        display_name = fix_display_name(display_name)
        is_two_week = _is_two_week_wta(level, raw_name, city, display_name)

        if monday < current_monday:
            if not (monday == past_monday and is_two_week):
                continue
        else:
            if not (monday < two_weeks_later):
                continue

        if week_label not in result:
            result[week_label] = {}

        result[week_label][t_url] = {
            "name": display_name,
            "level": level,
            "startDate": start_date,
            "endDate": end_date
        }

    return result


def get_full_wta_calendar():
    """Get all WTA tournaments from now until end of year for the calendar view."""
    today = datetime.now()
    today_str = today.strftime("%Y-%m-%d")

    raw_tournaments = _fetch_wta_tournaments_raw()

    tournaments = []
    for t in raw_tournaments:
        start_date = t["startDate"]
        if start_date < today_str:
            continue

        level = t["level"]
        city = t["city"].title()

        raw_name = t["tournamentGroup"]["name"]
        nfkd_form = unicodedata.normalize('NFKD', raw_name)
        clean_name = "".join([c for c in nfkd_form if not unicodedata.combining(c)])
        suffix = ""
        if "#" in clean_name:
            parts = clean_name.split("#")
            clean_name = parts[0].strip()
            suffix = " " + parts[1].strip()

        if level.lower().replace(" ", "") == "grandslam":
            display_name = f"Grand Slam {city}{suffix}"
        else:
            display_name = f"{level} {city}{suffix}"
        display_name = fix_display_name(display_name)
        surface = t.get("surface") or t.get("surfaceType") or t.get("surfaceCode") or ""
        country = t.get("countryCode") or t.get("country") or t.get("hostCountryCode") or ""
        tournaments.append({
            "name": display_name,
            "level": level,
            "surface": surface,
            "country": country,
            "startDate": start_date,
            "endDate": t.get("endDate", None)
        })

    return tournaments


def get_rankings(date_str, nationality=None):
    all_players, page = [], 0
    seen_keys = set()
    while True:
        params = {
            "page": page,
            # Larger pages reduce total request count (helps avoid CloudFront/WAF throttling).
            "pageSize": 2000,
            "type": "rankSingles",
            "sort": "asc",
            "metric": "SINGLES",
            "at": date_str
        }

        if nationality:
            params["nationality"] = nationality

        try:
            last_err = None
            data = None
            req_headers = dict(HEADERS or {})
            req_headers.setdefault("Accept", "application/json, text/plain, */*")
            req_headers.setdefault("Accept-Language", "en-US,en;q=0.9")
            req_headers.setdefault("Origin", "https://www.wtatennis.com")
            req_headers.setdefault("Referer", "https://www.wtatennis.com/")
            saw_rate_limit = False
            for attempt in range(8):
                try:
                    r = _REQUESTS_SESSION.get(API_URL, params=params, headers=req_headers, timeout=30)
                    # Retry on throttling / transient server errors.
                    if r.status_code in (429, 500, 502, 503, 504):
                        saw_rate_limit = saw_rate_limit or (r.status_code == 429)
                        time.sleep(min(120.0, 5.0 * (2 ** attempt)))
                        continue
                    ctype = (r.headers.get("content-type") or "").lower()
                    if "text/html" in ctype:
                        # CloudFront/WAF blocks often come back as HTML.
                        saw_rate_limit = True
                        time.sleep(min(120.0, 5.0 * (2 ** attempt)))
                        continue
                    r.raise_for_status()
                    data = r.json()
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    time.sleep(min(120.0, 5.0 * (2 ** attempt)))
            if last_err is not None and data is None:
                if saw_rate_limit and page == 0:
                    raise WtaApiRateLimited(f"WTA API rate limited for {date_str}")
                break
            items = data.get('content', []) if isinstance(data, dict) else data
            if not items: break
            # Defensive de-dup in case the API repeats pages (seen in the wild).
            new_items = []
            for it in items:
                player = it.get("player") or {}
                key = (
                    player.get("id")
                    or player.get("fullName")
                    or (it.get("ranking"), player.get("countryCode"), player.get("dateOfBirth"))
                )
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                new_items.append(it)
            if not new_items:
                break
            all_players.extend(new_items)
            page += 1
            time.sleep(0.25)
        except WtaApiRateLimited:
            raise
        except Exception:
            break

    ranking_results = []
    for p in all_players:
        if not p.get('player'): continue
        player_obj = p.get("player") or {}
        player_id = (
            player_obj.get("id")
            or player_obj.get("playerId")
            or p.get("playerId")
            or p.get("id")
        )
        official_name = (p.get('player', {}).get('fullName') or '').strip()
        official_upper = official_name.upper()
        display_name = NAME_LOOKUP.get(official_upper, official_upper)
        ranking_results.append({
            "Player": display_name,
            "OfficialPlayer": official_name,
            "Id": player_id,
            "Rank": p.get('ranking'),
            "Country": p.get('player', {}).get('countryCode', ''),
            "Key": display_name,
            "Points": p.get('points', 0),
            "DOB": p.get('player', {}).get('dateOfBirth', '')
        })
    return ranking_results


_wta_csv_cache = None  # module-level in-memory cache: date_str -> list of player dicts


def _load_wta_csv():
    global _wta_csv_cache
    if _wta_csv_cache is not None:
        return _wta_csv_cache
    _wta_csv_cache = {}
    # Load higher-priority decade files first so overlapping 2000 weeks prefer
    # the dedicated 00_09 CSV, while older-only weeks still come from 83_99.
    for csv_file in [WTA_RANKINGS_CSV, WTA_RANKINGS_CSV_10_19, WTA_RANKINGS_CSV_00_09, WTA_RANKINGS_CSV_83_99]:
        if not _os.path.exists(csv_file):
            continue
        existing_dates = set(_wta_csv_cache.keys())
        with open(csv_file, encoding="utf-8-sig") as f:
            for row in _csv.DictReader(f):
                d = row["week_date"]
                if d in existing_dates:
                    continue
                if d not in _wta_csv_cache:
                    _wta_csv_cache[d] = []
                pid = (row.get("id") or row.get("player_id") or row.get("playerId") or "").strip()
                official_name = row["player"]
                official_upper = official_name.upper()
                display_upper = NAME_LOOKUP.get(official_upper, official_upper)
                _wta_csv_cache[d].append({
                    "Player":  display_upper,
                    "OfficialPlayer": official_upper,
                    "Id": pid,
                    "Rank":    int(row["rank"]) if row.get("rank") else None,
                    "Country": row["country"],
                    "Key":     display_upper,
                    "Points":  int(row["points"]) if row.get("points") else 0,
                    "DOB":     row.get("dob", ""),
                })
    return _wta_csv_cache


def _save_wta_csv_date(date_str, players):
    """Append a new week's rankings to the current decade CSV file."""
    if not players:
        return
    with open(WTA_RANKINGS_CSV, "a", encoding="utf-8", newline="") as f:
        writer = _csv.writer(f)
        for p in players:
            name = (p.get("OfficialPlayer") or p.get("Player") or "").strip()
            writer.writerow([date_str, p.get("Id", ""), p.get("Rank", ""), p.get("Points", 0), name, p.get("Country", ""), p.get("DOB", "")])


def get_wta_rankings_cached(date_str, nationality=None):
    """Get WTA rankings from CSV, falling back to API if the date is missing."""
    csv_data = _load_wta_csv()

    if date_str in csv_data:
        players = csv_data[date_str]
        if nationality:
            return [p for p in players if p.get("Country") == nationality]
        return players

    # Date not in CSV — fetch from API, save to CSV, and keep in memory
    new_data = get_rankings(date_str, nationality=nationality)
    if new_data:
        csv_data[date_str] = new_data
        _save_wta_csv_date(date_str, new_data)
        return new_data

    # Fallback: use the latest available date in the CSV
    if csv_data:
        latest_key = sorted(csv_data.keys())[-1]
        players = csv_data.get(latest_key, [])
        if nationality:
            return [p for p in players if p.get("Country") == nationality]
        return players

    return []


def fetch_player_info(player_id):
    url = f"https://api.wtatennis.com/tennis/players/{player_id}/matches"
    params = {"page": 0, "pageSize": 1, "sort": "desc"}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
        "referer": "https://www.wtatennis.com/",
        "account": "wta"
    }
    try:
        r = requests.get(url, params=params, headers=headers, timeout=10)
        data = r.json()
        player = data.get("player", {})
        name = player.get("fullName")
        country = player.get("countryCode")
        if name:
            return {"name": name, "country": country}
    except:
        pass
    return None


def scrape_tournament_players(url, md_rankings, qual_rankings, cached_entries=None):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        soup = BeautifulSoup(r.text, 'html.parser')
    except Exception as e:
        print(f"Error scraping {url}: {e}")
        return [], {}

    # 1. Read player IDs/slugs from HTML
    main_entries, qual_entries = [], []
    main_seen, qual_seen = set(), set()
    current_state = "MAIN"

    for tag in soup.find_all(True):
        ui_tab = tag.get('data-ui-tab', '').lower()

        if "qualifying" in ui_tab:
            current_state = "QUAL"
        elif "doubles" in ui_tab:
            current_state = "IGNORE"
        if current_state == "IGNORE":
            continue

        href = tag.get('href', '')
        m = re.match(r'/players/(\d+)/([^/]+)', href)
        if m:
            pid, slug = m.group(1), m.group(2)
            if current_state == "MAIN" and pid not in main_seen:
                main_seen.add(pid)
                main_entries.append((pid, slug))
            elif current_state == "QUAL" and pid not in qual_seen:
                qual_seen.add(pid)
                qual_entries.append((pid, slug))

    # Build cache lookup from previous run
    cached_lookup = {}
    for entry in (cached_entries or []):
        cached_lookup[entry["name"].strip().upper()] = entry

    # Build ranked names set for quick lookup
    ranked_names = set()
    for rank_list in [md_rankings, qual_rankings]:
        for item in rank_list:
            if item.get("Player"):
                ranked_names.add(item["Player"].strip().upper())

    # 2-3. Resolve each player: cache first, then rankings, then API
    player_cache = {}
    seen_pids = set()
    for pid, slug in main_entries + qual_entries:
        if pid in seen_pids:
            continue
        seen_pids.add(pid)
        candidate = slug.replace("-", " ").upper()
        mapped = NAME_LOOKUP.get(candidate, candidate)

        if mapped in cached_lookup:
            player_cache[pid] = {"name": mapped, "country": cached_lookup[mapped].get("country")}
        elif candidate in cached_lookup:
            player_cache[pid] = {"name": candidate, "country": cached_lookup[candidate].get("country")}
        elif candidate in ranked_names:
            player_cache[pid] = {"name": candidate, "country": None}
        elif mapped in ranked_names:
            player_cache[pid] = {"name": mapped, "country": None}
        else:
            info = fetch_player_info(pid)
            if info:
                player_cache[pid] = info
            time.sleep(0.05)

    # 4. Fill the table
    main_draw_names = set()
    qualifying_names = set()

    def parse_rank_num(value):
        try:
            return int(str(value).strip())
        except Exception:
            return 9999

    def get_p_rank(name, rank_list):
        return next((item for item in rank_list if item["Player"] == name), {"Rank": 9999, "Country": "-"})

    md_list = []
    for pid, slug in main_entries:
        if pid not in player_cache:
            continue
        p_info = player_cache[pid]
        name_key = p_info["name"].strip().upper()
        matched_name = NAME_LOOKUP.get(name_key, name_key)
        main_draw_names.add(matched_name)
        rank_info = get_p_rank(matched_name, md_rankings)
        md_list.append({
            "name": format_player_name(matched_name),
            "country": rank_info["Country"] if rank_info["Country"] != "-" else (p_info.get("country") or "-"),
            "rank_num": rank_info["Rank"],
            "rank": f"{rank_info['Rank']}" if rank_info['Rank'] < 9999 else "-",
            "type": "MAIN"
        })

    # Some WTA pages temporarily expose only Qualifying. In that case, keep MAIN from cache.
    if not md_list and qual_entries and cached_entries:
        cached_main = [p for p in cached_entries if p.get("type") == "MAIN"]
        for p in cached_main:
            name_key = (p.get("name") or "").strip().upper()
            if not name_key:
                continue
            matched_name = NAME_LOOKUP.get(name_key, name_key)
            main_draw_names.add(matched_name)
            rank_num = p.get("rank_num")
            if not isinstance(rank_num, int):
                rank_num = parse_rank_num(p.get("rank"))
            rank_value = p.get("rank")
            rank_display = str(rank_value).strip() if rank_value not in (None, "") else (str(rank_num) if rank_num < 9999 else "-")
            md_list.append({
                "name": format_player_name(matched_name),
                "country": p.get("country") or "-",
                "rank_num": rank_num,
                "rank": rank_display if rank_num < 9999 else "-",
                "type": "MAIN"
            })

    md_list.sort(key=lambda x: (x["rank_num"], x["name"]))
    for idx, p in enumerate(md_list, 1):
        p["pos"] = str(idx)
        p["pos_num"] = idx

    qual_list = []
    for pid, slug in qual_entries:
        if pid not in player_cache:
            continue
        p_info = player_cache[pid]
        name_key = p_info["name"].strip().upper()
        matched_name = NAME_LOOKUP.get(name_key, name_key)
        qualifying_names.add(matched_name)
        rank_info = get_p_rank(matched_name, qual_rankings)
        qual_list.append({
            "name": format_player_name(matched_name),
            "country": rank_info["Country"] if rank_info["Country"] != "-" else (p_info.get("country") or "-"),
            "rank_num": rank_info["Rank"],
            "rank": f"{rank_info['Rank']}" if rank_info['Rank'] < 9999 else "-",
            "type": "QUAL"
        })
    qual_list.sort(key=lambda x: (x["rank_num"], x["name"]))
    for idx, p in enumerate(qual_list, 1):
        p["pos"] = str(idx)
        p["pos_num"] = idx

    final_tourney_list = md_list + qual_list

    suffix_map = {p: "" for p in main_draw_names}
    suffix_map.update({p: " (Q)" for p in qualifying_names})

    return final_tourney_list, suffix_map
