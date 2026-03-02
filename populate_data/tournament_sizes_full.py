"""
Fetch tournament draw sizes for both WTA and ITF tournaments from the past 55 weeks.
Saves a single combined JSON file: data/tournament_draw_sizes.json
"""
import json
import time
import os
import requests
from datetime import datetime, timedelta
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "..", "data")
POINTS_DIST_PATH = os.path.join(DATA_DIR, "points_distribution.json")
OUTPUT_PATH = os.path.join(DATA_DIR, "tournament_draw_sizes.json")
ITF_CACHE_PATH = os.path.join(DATA_DIR, "itf_tournament_list_cache.json")

# ── WTA constants ──────────────────────────────────────────────────────────────

WTA_HEADERS = {
    "accept": "*/*",
    "accept-language": "es-ES,es;q=0.9,en;q=0.8",
    "account": "wta",
    "origin": "https://www.wtatennis.com",
    "referer": "https://www.wtatennis.com/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
}

WTA_CALENDAR_URL = "https://api.wtatennis.com/tennis/tournaments/"
WTA_MATCHES_URL = "https://api.wtatennis.com/tennis/tournaments/{tournament_id}/{year}/matches?states=L%2C+C"

# ── Shared helpers ─────────────────────────────────────────────────────────────

def get_monday(date_str):
    if not date_str:
        return None
    if "T" in date_str:
        date_str = date_str.split("T")[0]
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    monday = dt - timedelta(days=dt.weekday())
    return monday.strftime("%Y-%m-%d")


# ── WTA functions ──────────────────────────────────────────────────────────────

def wta_build_tournament_name(tournament):
    """Build tournament name matching the format used in wta_matches_arg.csv."""
    title = tournament.get("title", "")
    country = tournament.get("country", "")
    if country and title.endswith(f", {country}"):
        name = title[: -len(f", {country}")]
    else:
        name = title
    return name


def wta_fetch_tournaments(from_date, to_date):
    """Fetch all WTA tournaments in a date range (excludes ITF and Grand Slam)."""
    all_tournaments = []
    page = 0
    while True:
        params = {
            "page": page,
            "pageSize": 100,
            "excludeLevels": "ITF,Grand Slam",
            "from": from_date,
            "to": to_date,
        }
        try:
            r = requests.get(WTA_CALENDAR_URL, headers=WTA_HEADERS, params=params, timeout=10)
            r.raise_for_status()
            data = r.json()
            page_content = data.get("content", [])
            all_tournaments.extend(page_content)

            if len(page_content) < 100:
                break
            page += 1
        except Exception as e:
            print(f"Error fetching WTA page {page}: {e}")
            break
    return all_tournaments


def wta_count_qualifying_players(tournament_id, year):
    """Fetch matches for a WTA tournament and count unique qualifying players."""
    url = WTA_MATCHES_URL.format(tournament_id=tournament_id, year=year)
    try:
        r = requests.get(url, headers=WTA_HEADERS, timeout=10)
        r.raise_for_status()
        matches = r.json().get("matches", [])
    except Exception as e:
        print(f"  Error fetching matches for {tournament_id}/{year}: {e}")
        return 0

    q_matches = [
        m for m in matches
        if m.get("DrawLevelType") == "Q" and m.get("DrawMatchType") == "S"
    ]

    players = set()
    for m in q_matches:
        for key in ["PlayerIDA", "PlayerIDB"]:
            pid = m.get(key)
            if pid:
                players.add(pid)
    return len(players)


def wta_get_description(level, main_draw_size, qual_size):
    """Map WTA tournament level + draw sizes to the points_distribution description."""
    if level == "WTA 1000":
        # 96M draws may show as 94-96 due to withdrawals
        if main_draw_size > 64:
            return "WTA 1000 (96M, 48Q)"
        return "WTA 1000 (56M, 32Q)"

    if level == "WTA 500":
        if main_draw_size >= 48:
            return "WTA 500 (48M, 24Q)"
        if main_draw_size == 0:
            return None  # United Cup shows as WTA 500 with 0M
        return "WTA 500 (30/28M, 24/16Q)"

    if level == "WTA 250":
        return "WTA 250 (32M, 24/16Q)"

    if level == "WTA 125":
        if qual_size <= 8:
            return "WTA 125 (32M, 8Q)"
        return "WTA 125 (32M, 16Q)"

    return None


def fetch_wta_draw_sizes(from_date, to_date, desc_set):
    """Fetch all WTA tournament draw sizes."""
    print(f"\n{'='*60}")
    print(f"WTA TOURNAMENTS")
    print(f"{'='*60}")
    print(f"Fetching WTA tournaments from {from_date} to {to_date}...")
    tournaments = wta_fetch_tournaments(from_date, to_date)
    print(f"Found {len(tournaments)} WTA tournaments")

    today = datetime.now()
    results = []
    for t in tournaments:
        t_id = t.get("tournamentGroup", {}).get("id")
        level = t.get("level", "")
        start_date = t.get("startDate", "")
        year = int(start_date[:4]) if start_date else today.year
        main_draw_size = t.get("singlesDrawSize", 0)

        name = wta_build_tournament_name(t)
        date = get_monday(start_date)

        # For WTA 125, fetch matches to determine qualifying size
        qual_size = 0
        if level == "WTA 125" and t_id:
            qual_size = wta_count_qualifying_players(t_id, year)
            time.sleep(0.3)

        desc = wta_get_description(level, main_draw_size, qual_size)

        if desc and desc not in desc_set:
            print(f"  WARNING: Description '{desc}' not in points_distribution.json")
            desc = None

        results.append({
            "source": "WTA",
            "date": date,
            "tournamentName": name,
            "tournamentId": str(t_id) if t_id else "",
            "category": level,
            "mainDrawSize": main_draw_size,
            "qualifyingSize": qual_size,
            "description": desc,
        })

        q_info = f", {qual_size}Q" if qual_size else ""
        print(f"  {name}: {level}, {main_draw_size}M{q_info} -> {desc or 'NO MATCH'}")

    return results


# ── ITF functions ──────────────────────────────────────────────────────────────

def get_itf_level(name):
    if "W100" in name or "100k" in name: return "W100"
    if "W75" in name or "75k" in name: return "W75"
    if "W60" in name or "60k" in name: return "W60"
    if "W50" in name or "50k" in name: return "W50"
    if "W35" in name or "35k" in name: return "W35"
    if "W25" in name or "25k" in name: return "W25"
    return "W15"


def itf_fetch_drawsheet(t_id, classification, week_number=0):
    url = "https://www.itftennis.com/tennis/api/TournamentApi/GetDrawsheet"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
        "Referer": f"https://www.itftennis.com/en/tournament/draws-and-results/print/?tournamentId={t_id}&circuitCode=WT",
        "Origin": "https://www.itftennis.com",
        "Content-Type": "application/json"
    }
    payload = {
        "circuitCode": "WT",
        "eventClassificationCode": classification,
        "matchTypeCode": "S",
        "tourType": "WT",
        "tournamentId": str(t_id),
        "weekNumber": week_number
    }
    try:
        r = requests.post(url, json=payload, headers=headers)
        r.raise_for_status()
        return r.json()
    except:
        return None


def itf_count_draw_size(data):
    """Count draw size by counting unique player IDs in the drawsheet."""
    if not data or not isinstance(data, dict):
        return 0
    ko_groups = data.get("koGroups", [])
    if not ko_groups:
        return 0

    player_ids = set()
    for group in ko_groups:
        for rnd in group.get("rounds", []):
            for match in rnd.get("matches", []):
                for team in match.get("teams", []):
                    for player in team.get("players", []):
                        if not player:
                            continue
                        pid = player.get("playerId")
                        if pid:
                            player_ids.add(pid)
    return len(player_ids)


def itf_parse_descriptions(points_dist):
    """Parse ITF descriptions from points_distribution.json."""
    descs = []
    for entry in points_dist:
        d = entry.get("Description", "")
        if not any(d.startswith(cat) for cat in ["W15", "W25", "W35", "W50", "W60", "W75", "W100"]):
            continue
        if "(" not in d:
            continue
        inner = d.split("(")[1].rstrip(")")
        parts = inner.split(",")
        if len(parts) != 2:
            continue
        m_str = parts[0].strip().replace("M", "")
        q_str = parts[1].strip().replace("Q", "")
        descs.append({
            "description": d,
            "category": d.split(" ")[0],
            "main_size": int(m_str),
            "qual_sizes": [int(x) for x in q_str.split("/")]
        })
    return descs


def itf_round_to_draw_size(actual, valid_sizes):
    """Round an actual player count UP to the nearest valid draw size."""
    for size in sorted(valid_sizes):
        if actual <= size:
            return size
    return None


def itf_find_description(category, actual_main, actual_qual, descriptions):
    """Find the matching description by rounding actual sizes to nearest valid draw sizes."""
    cat_descs = [d for d in descriptions if d["category"] == category]
    if not cat_descs:
        return None

    valid_m_sizes = sorted(set(d["main_size"] for d in cat_descs))
    rounded_m = itf_round_to_draw_size(actual_main, valid_m_sizes)
    if rounded_m is None:
        return None

    m_descs = [d for d in cat_descs if d["main_size"] == rounded_m]
    if not m_descs:
        return None

    all_q_sizes = set()
    for d in m_descs:
        all_q_sizes.update(d["qual_sizes"])

    rounded_q = itf_round_to_draw_size(actual_qual, sorted(all_q_sizes))
    if rounded_q is None:
        return None

    for d in m_descs:
        if rounded_q in d["qual_sizes"]:
            return d["description"]
    return None


def itf_fetch_tournament_list():
    """Fetch ITF tournament list via Selenium, or load from cache."""
    if os.path.exists(ITF_CACHE_PATH):
        print("Loading ITF tournament list from cache...")
        with open(ITF_CACHE_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)

    today = datetime.now()
    date_from = (today - timedelta(weeks=55)).strftime("%Y-%m-%d")
    date_to = today.strftime("%Y-%m-%d")

    print(f"Date range: {date_from} to {date_to}")

    chrome_options = Options()
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("window-size=1920,1080")
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36")

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)

    try:
        print("Establishing session...")
        driver.get("https://www.itftennis.com/en/tournament-calendar/womens-world-tennis-tour-calendar/")
        time.sleep(5)

        # Fetch calendar
        print("Fetching ITF calendar...")
        all_items = []
        skip = 0
        take = 500
        while True:
            url = (
                f"https://www.itftennis.com/tennis/api/TournamentApi/GetCalendar?"
                f"circuitCode=WT&searchString=&skip={skip}&take={take}"
                f"&dateFrom={date_from}&dateTo={date_to}"
                f"&isOrderAscending=true&orderField=startDate"
            )
            driver.get(url)
            time.sleep(2)
            try:
                raw = driver.find_element("tag name", "body").text.strip()
                data = json.loads(raw)
                items = data.get('items', [])
                if not items:
                    break
                all_items.extend(items)
                total = data.get('totalItems', 0)
                print(f"  Fetched {len(all_items)}/{total}")
                if skip + take >= total:
                    break
                skip += take
            except Exception as e:
                print(f"  Error: {e}")
                break

        # Filter valid tournaments
        seen_keys = set()
        tournaments = []
        for item in all_items:
            status = (item.get('status') or '').lower()
            name = item.get('tournamentName', '')
            if 'cancel' in status or 'cancel' in name.lower():
                continue

            category = item.get("category", "")
            if category and category.strip().startswith("Tier"):
                continue

            link = item.get("tournamentLink", "")
            key = link.rstrip('/').split('/')[-1] if link else None
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)

            is_multiweek = category == "ITF Womens Multi-Week Circuit"

            tournaments.append({
                "startDate": item.get("startDate"),
                "tournamentName": name,
                "tournamentKey": key,
                "rawCategory": category,
                "isMultiweek": is_multiweek,
            })

        print(f"Valid ITF tournaments: {len(tournaments)}")

        # Fetch tournament IDs
        print("Fetching ITF tournament IDs...")
        for i, t in enumerate(tournaments):
            api_url = f"https://www.itftennis.com/tennis/api/TournamentApi/GetEventFilters?tournamentKey={t['tournamentKey']}"
            driver.get(api_url)
            time.sleep(1)
            try:
                raw = driver.find_element("tag name", "body").text.strip()
                data = json.loads(raw)
                t["tournamentId"] = data.get("tournamentId")
            except:
                t["tournamentId"] = None
            if (i + 1) % 50 == 0:
                print(f"  {i + 1}/{len(tournaments)}")

        # Save cache
        with open(ITF_CACHE_PATH, 'w', encoding='utf-8') as f:
            json.dump(tournaments, f, indent=2, ensure_ascii=False)
        print(f"Cached {len(tournaments)} ITF tournaments")

        return tournaments

    finally:
        driver.quit()


def fetch_itf_draw_sizes(points_dist):
    """Fetch all ITF tournament draw sizes."""
    print(f"\n{'='*60}")
    print(f"ITF TOURNAMENTS")
    print(f"{'='*60}")

    itf_descs = itf_parse_descriptions(points_dist)
    tournaments = itf_fetch_tournament_list()

    print("\nFetching ITF drawsheet sizes...")
    results = []

    for i, t in enumerate(tournaments):
        t_id = t.get("tournamentId")
        if not t_id:
            print(f"  Skipping {t['tournamentName']} (no ID)")
            continue

        name = t["tournamentName"]
        cat = get_itf_level(name)

        if t.get("isMultiweek"):
            week = 1
            while True:
                m_data = itf_fetch_drawsheet(t_id, "M", week_number=week)
                if not m_data or not m_data.get("koGroups"):
                    break

                main_size = itf_count_draw_size(m_data)
                time.sleep(0.2)

                q_data = itf_fetch_drawsheet(t_id, "Q", week_number=week)
                qual_size = itf_count_draw_size(q_data)
                time.sleep(0.2)

                base_date = t["startDate"]
                if base_date and "T" in base_date:
                    base_date = base_date.split("T")[0]
                if base_date:
                    dt = datetime.strptime(base_date, "%Y-%m-%d")
                    week_date = dt + timedelta(days=7 * (week - 1))
                    date = get_monday(week_date.strftime("%Y-%m-%d"))
                else:
                    date = None

                week_name = f"{name} (Week {week})"
                desc = itf_find_description(cat, main_size, qual_size, itf_descs)

                results.append({
                    "source": "ITF",
                    "date": date,
                    "tournamentName": week_name,
                    "tournamentKey": t["tournamentKey"],
                    "category": cat,
                    "mainDrawSize": main_size,
                    "qualifyingSize": qual_size,
                    "description": desc
                })

                print(f"  {week_name}: {main_size}M, {qual_size}Q -> {desc or 'NO MATCH'}")

                week += 1
                if week > 10:
                    break
        else:
            m_data = itf_fetch_drawsheet(t_id, "M")
            main_size = itf_count_draw_size(m_data)
            time.sleep(0.2)

            q_data = itf_fetch_drawsheet(t_id, "Q")
            qual_size = itf_count_draw_size(q_data)
            time.sleep(0.2)

            date = get_monday(t["startDate"])
            desc = itf_find_description(cat, main_size, qual_size, itf_descs)

            results.append({
                "source": "ITF",
                "date": date,
                "tournamentName": name,
                "tournamentKey": t["tournamentKey"],
                "category": cat,
                "mainDrawSize": main_size,
                "qualifyingSize": qual_size,
                "description": desc
            })

            print(f"  {name}: {main_size}M, {qual_size}Q -> {desc or 'NO MATCH'}")

        if (i + 1) % 50 == 0:
            print(f"Progress: {i + 1}/{len(tournaments)}")

    return results


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    with open(POINTS_DIST_PATH, 'r', encoding='utf-8') as f:
        points_dist = json.load(f)

    desc_set = {entry["Description"] for entry in points_dist}

    today = datetime.now()
    from_date = (today - timedelta(weeks=55)).strftime("%Y-%m-%d")
    to_date = today.strftime("%Y-%m-%d")

    # Fetch WTA
    wta_results = fetch_wta_draw_sizes(from_date, to_date, desc_set)

    # Fetch ITF
    itf_results = fetch_itf_draw_sizes(points_dist)

    # Combine and save
    all_results = wta_results + itf_results

    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*60}")
    print(f"SUMMARY")
    print(f"{'='*60}")
    print(f"WTA: {len(wta_results)} tournaments")
    print(f"ITF: {len(itf_results)} tournaments")
    print(f"Total: {len(all_results)} tournaments saved to {OUTPUT_PATH}")

    # Validation
    unmatched = [r for r in all_results if not r.get("description")]
    if unmatched:
        print(f"\nWARNING: {len(unmatched)} tournaments didn't match:")
        for r in unmatched:
            q_info = f", {r['qualifyingSize']}Q" if r.get('qualifyingSize') else ""
            print(f"  [{r.get('source','?')}] {r['tournamentName']}: {r['category']}, {r['mainDrawSize']}M{q_info}")
    else:
        print("\nAll tournaments matched a description!")


if __name__ == "__main__":
    main()
