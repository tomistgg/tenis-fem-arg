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
    md_list.sort(key=lambda x: (x["rank_num"], x["name"]))
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
    qual_list.sort(key=lambda x: (x["rank_num"], x["name"]))
    for idx, p in enumerate(qual_list, 1):
        p["pos"] = str(idx)

    final_tourney_list = md_list + qual_list

    suffix_map = {p: "" for p in main_draw_names}
    suffix_map.update({p: " (Q)" for p in qualifying_names})

    return final_tourney_list, suffix_map
