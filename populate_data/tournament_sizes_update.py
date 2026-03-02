"""
Incremental update for tournament draw sizes.
- Fetches WTA and ITF tournaments from the current + next week
- Appends new entries to data/tournament_draw_sizes.json
- Removes entries older than 55 weeks
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

# ── Shared ─────────────────────────────────────────────────────────────────────

def get_monday(date_str):
    if not date_str:
        return None
    if "T" in date_str:
        date_str = date_str.split("T")[0]
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    monday = dt - timedelta(days=dt.weekday())
    return monday.strftime("%Y-%m-%d")


def load_existing():
    if os.path.exists(OUTPUT_PATH):
        with open(OUTPUT_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []


def save_results(data):
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ── WTA ────────────────────────────────────────────────────────────────────────

WTA_HEADERS = {
    "accept": "*/*",
    "accept-language": "es-ES,es;q=0.9,en;q=0.8",
    "account": "wta",
    "origin": "https://www.wtatennis.com",
    "referer": "https://www.wtatennis.com/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
}


def wta_fetch_tournaments(from_date, to_date):
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
            r = requests.get("https://api.wtatennis.com/tennis/tournaments/",
                             headers=WTA_HEADERS, params=params, timeout=10)
            r.raise_for_status()
            data = r.json()
            page_content = data.get("content", [])
            all_tournaments.extend(page_content)
            if len(page_content) < 100:
                break
            page += 1
        except Exception as e:
            print(f"  Error fetching WTA page {page}: {e}")
            break
    return all_tournaments


def wta_count_qualifying_players(tournament_id, year):
    url = f"https://api.wtatennis.com/tennis/tournaments/{tournament_id}/{year}/matches?states=L%2C+C"
    try:
        r = requests.get(url, headers=WTA_HEADERS, timeout=10)
        r.raise_for_status()
        matches = r.json().get("matches", [])
    except Exception as e:
        print(f"  Error fetching WTA matches for {tournament_id}/{year}: {e}")
        return 0

    players = set()
    for m in matches:
        if m.get("DrawLevelType") == "Q" and m.get("DrawMatchType") == "S":
            for key in ["PlayerIDA", "PlayerIDB"]:
                pid = m.get(key)
                if pid:
                    players.add(pid)
    return len(players)


def wta_get_description(level, main_draw_size, qual_size):
    if level == "WTA 1000":
        if main_draw_size > 64:
            return "WTA 1000 (96M, 48Q)"
        return "WTA 1000 (56M, 32Q)"
    if level == "WTA 500":
        if main_draw_size >= 48:
            return "WTA 500 (48M, 24Q)"
        if main_draw_size == 0:
            return None
        return "WTA 500 (30/28M, 24/16Q)"
    if level == "WTA 250":
        return "WTA 250 (32M, 24/16Q)"
    if level == "WTA 125":
        if qual_size <= 8:
            return "WTA 125 (32M, 8Q)"
        return "WTA 125 (32M, 16Q)"
    return None


def wta_build_tournament_name(tournament):
    title = tournament.get("title", "")
    country = tournament.get("country", "")
    if country and title.endswith(f", {country}"):
        return title[: -len(f", {country}")]
    return title


def fetch_wta_updates(from_date, to_date, desc_set):
    print("Fetching WTA tournaments...")
    tournaments = wta_fetch_tournaments(from_date, to_date)
    print(f"  Found {len(tournaments)} WTA tournaments in range")

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

        qual_size = 0
        if level == "WTA 125" and t_id:
            qual_size = wta_count_qualifying_players(t_id, year)
            time.sleep(0.3)

        desc = wta_get_description(level, main_draw_size, qual_size)
        if desc and desc not in desc_set:
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


# ── ITF ────────────────────────────────────────────────────────────────────────

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
    for size in sorted(valid_sizes):
        if actual <= size:
            return size
    return None


def itf_find_description(category, actual_main, actual_qual, descriptions):
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


def fetch_itf_updates(from_date, to_date, itf_descs):
    """Fetch ITF tournaments for the given date range using Selenium."""
    print("Fetching ITF tournaments...")

    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("window-size=1920,1080")
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36")

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)

    results = []

    try:
        driver.get("https://www.itftennis.com/en/tournament-calendar/womens-world-tennis-tour-calendar/")
        time.sleep(5)

        # Fetch calendar for the date range
        api_url = (
            f"https://www.itftennis.com/tennis/api/TournamentApi/GetCalendar?"
            f"circuitCode=WT&searchString=&skip=0&take=500"
            f"&dateFrom={from_date}&dateTo={to_date}"
            f"&isOrderAscending=true&orderField=startDate"
        )
        driver.get(api_url)
        time.sleep(2)

        raw = driver.find_element("tag name", "body").text.strip()
        data = json.loads(raw)
        items = data.get('items', [])
        print(f"  Found {len(items)} ITF tournaments in range")

        # Filter valid tournaments and get IDs
        seen_keys = set()
        tournaments = []
        for item in items:
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
                "isMultiweek": is_multiweek,
            })

        # Fetch tournament IDs
        for t in tournaments:
            api_url = f"https://www.itftennis.com/tennis/api/TournamentApi/GetEventFilters?tournamentKey={t['tournamentKey']}"
            driver.get(api_url)
            time.sleep(1)
            try:
                raw = driver.find_element("tag name", "body").text.strip()
                event_data = json.loads(raw)
                t["tournamentId"] = event_data.get("tournamentId")
            except:
                t["tournamentId"] = None

    except Exception as e:
        print(f"  Error fetching ITF calendar: {e}")
        return results
    finally:
        driver.quit()

    # Fetch drawsheets for each tournament
    for t in tournaments:
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
                    "description": desc,
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
                "description": desc,
            })
            print(f"  {name}: {main_size}M, {qual_size}Q -> {desc or 'NO MATCH'}")

    return results


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    with open(POINTS_DIST_PATH, 'r', encoding='utf-8') as f:
        points_dist = json.load(f)

    desc_set = {entry["Description"] for entry in points_dist}
    itf_descs = itf_parse_descriptions(points_dist)

    today = datetime.today().date()
    week_start = today - timedelta(days=today.weekday())  # Monday of current week
    next_monday = (week_start + timedelta(days=7)).strftime("%Y-%m-%d")
    cutoff = (today - timedelta(weeks=55)).strftime("%Y-%m-%d")

    # Load existing data
    existing = load_existing()
    print(f"Existing entries: {len(existing)}")

    # Prune old entries
    before_prune = len(existing)
    existing = [t for t in existing if (t.get("date") or "") >= cutoff]
    pruned = before_prune - len(existing)
    if pruned:
        print(f"Pruned {pruned} entries older than {cutoff}")
        save_results(existing)

    # Check if next week's tournaments are already present
    next_week_entries = [t for t in existing if t.get("date") == next_monday]
    if next_week_entries:
        print(f"Next week ({next_monday}) already has {len(next_week_entries)} entries, skipping fetch.")
        print(f"Total: {len(existing)} entries")
        return

    # Fetch range: prev week through next week
    from_date = (week_start - timedelta(days=7)).strftime("%Y-%m-%d")
    to_date = (week_start + timedelta(days=13)).strftime("%Y-%m-%d")
    print(f"Fetching tournaments from {from_date} to {to_date}")

    # Build dedup keys for existing entries
    existing_keys = set()
    for t in existing:
        if t.get("source") == "WTA":
            existing_keys.add(("WTA", t.get("tournamentId", ""), t.get("date", "")))
        else:
            existing_keys.add(("ITF", t.get("tournamentKey", ""), t.get("date", "")))

    # Fetch new WTA tournaments
    wta_new = fetch_wta_updates(from_date, to_date, desc_set)

    # Fetch new ITF tournaments
    itf_new = fetch_itf_updates(from_date, to_date, itf_descs)

    # Merge: add only entries not already present
    added = 0
    for t in wta_new + itf_new:
        if t.get("source") == "WTA":
            key = ("WTA", t.get("tournamentId", ""), t.get("date", ""))
        else:
            key = ("ITF", t.get("tournamentKey", ""), t.get("date", ""))

        if key not in existing_keys:
            existing.append(t)
            existing_keys.add(key)
            added += 1

    # Save
    save_results(existing)

    print(f"\nAdded {added} new entries")
    print(f"Total: {len(existing)} entries saved")


if __name__ == "__main__":
    main()
