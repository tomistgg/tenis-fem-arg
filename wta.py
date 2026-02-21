import re
import time
import requests
import unicodedata
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

from config import API_URL, HEADERS, NAME_LOOKUP, WTA_CACHE_FILE
from utils import fix_display_name, format_player_name, get_cached_rankings
from calendar_builder import get_next_monday, get_monday_from_date, format_week_label


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


def get_full_wta_calendar():
    """Fetch all WTA tournaments from now until end of year for the calendar view."""
    today = datetime.now()
    from_date = today.strftime("%Y-%m-%d")
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
    except Exception as e:
        print(f"Error fetching full WTA calendar: {e}")
        return []

    tournaments = []
    for t in data.get("content", []):
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
            "startDate": t["startDate"],
            "endDate": t.get("endDate", None)
        })

    return tournaments


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
            "Key": display_name,
            "Points": p.get('points', 0),
            "Played": p.get('tournamentsPlayed', 0),
            "DOB": p.get('player', {}).get('dateOfBirth', '')
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

    final_tourney_list = md_list + qual_list

    suffix_map = {p: "" for p in main_draw_names}
    suffix_map.update({p: " (Q)" for p in qualifying_names})

    return final_tourney_list, suffix_map
