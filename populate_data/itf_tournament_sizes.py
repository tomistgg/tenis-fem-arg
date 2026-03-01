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
OUTPUT_PATH = os.path.join(DATA_DIR, "itf_tournament_draw_sizes.json")
CACHE_PATH = os.path.join(DATA_DIR, "itf_tournament_list_cache.json")


def get_itf_level(name):
    if "W100" in name or "100k" in name: return "W100"
    if "W75" in name or "75k" in name: return "W75"
    if "W60" in name or "60k" in name: return "W60"
    if "W50" in name or "50k" in name: return "W50"
    if "W35" in name or "35k" in name: return "W35"
    if "W25" in name or "25k" in name: return "W25"
    return "W15"


def get_monday(date_str):
    if not date_str:
        return None
    if "T" in date_str:
        date_str = date_str.split("T")[0]
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    monday = dt - timedelta(days=dt.weekday())
    return monday.strftime("%Y-%m-%d")


def fetch_drawsheet(t_id, classification, week_number=0):
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


def count_draw_size(data):
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


def parse_itf_descriptions(points_dist):
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


def round_to_draw_size(actual, valid_sizes):
    """Round an actual player count UP to the nearest valid draw size."""
    for size in sorted(valid_sizes):
        if actual <= size:
            return size
    return None


def find_description(category, actual_main, actual_qual, descriptions):
    """Find the matching description by rounding actual sizes to nearest valid draw sizes."""
    # Get all descriptions for this category
    cat_descs = [d for d in descriptions if d["category"] == category]
    if not cat_descs:
        return None

    # Get all valid main draw sizes for this category
    valid_m_sizes = sorted(set(d["main_size"] for d in cat_descs))

    # Round main draw to nearest valid size
    rounded_m = round_to_draw_size(actual_main, valid_m_sizes)
    if rounded_m is None:
        return None

    # Get descriptions matching this main draw size
    m_descs = [d for d in cat_descs if d["main_size"] == rounded_m]
    if not m_descs:
        return None

    # For qualifying, get all valid Q sizes across matching descriptions
    all_q_sizes = set()
    for d in m_descs:
        all_q_sizes.update(d["qual_sizes"])

    # Round qualifying to nearest valid size
    rounded_q = round_to_draw_size(actual_qual, sorted(all_q_sizes))
    if rounded_q is None:
        return None

    # Find the exact description that contains this Q size
    for d in m_descs:
        if rounded_q in d["qual_sizes"]:
            return d["description"]
    return None


def fetch_tournament_list():
    """Fetch tournament list via Selenium, or load from cache."""
    if os.path.exists(CACHE_PATH):
        print("Loading tournament list from cache...")
        with open(CACHE_PATH, 'r', encoding='utf-8') as f:
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
        print("Fetching calendar...")
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

        print(f"Valid tournaments: {len(tournaments)}")

        # Fetch tournament IDs
        print("Fetching tournament IDs...")
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
        with open(CACHE_PATH, 'w', encoding='utf-8') as f:
            json.dump(tournaments, f, indent=2, ensure_ascii=False)
        print(f"Cached {len(tournaments)} tournaments")

        return tournaments

    finally:
        driver.quit()


def main():
    with open(POINTS_DIST_PATH, 'r', encoding='utf-8') as f:
        points_dist = json.load(f)

    itf_descs = parse_itf_descriptions(points_dist)
    tournaments = fetch_tournament_list()

    # Fetch drawsheets and determine sizes
    print("\nFetching drawsheet sizes...")
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
                m_data = fetch_drawsheet(t_id, "M", week_number=week)
                if not m_data or not m_data.get("koGroups"):
                    break

                main_size = count_draw_size(m_data)
                time.sleep(0.2)

                q_data = fetch_drawsheet(t_id, "Q", week_number=week)
                qual_size = count_draw_size(q_data)
                time.sleep(0.2)

                # Calculate date for this week
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
                desc = find_description(cat, main_size, qual_size, itf_descs)

                results.append({
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
            m_data = fetch_drawsheet(t_id, "M")
            main_size = count_draw_size(m_data)
            time.sleep(0.2)

            q_data = fetch_drawsheet(t_id, "Q")
            qual_size = count_draw_size(q_data)
            time.sleep(0.2)

            date = get_monday(t["startDate"])
            desc = find_description(cat, main_size, qual_size, itf_descs)

            results.append({
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

    # Save results
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"\nSaved {len(results)} tournament entries to {OUTPUT_PATH}")

    # Validation
    unmatched = [r for r in results if not r.get("description")]
    if unmatched:
        print(f"\nWARNING: {len(unmatched)} tournaments didn't match any description:")
        sizes_seen = {}
        for r in unmatched:
            key = f"{r['category']} {r['mainDrawSize']}M {r['qualifyingSize']}Q"
            if key not in sizes_seen:
                sizes_seen[key] = []
            sizes_seen[key].append(r['tournamentName'])
        for key, names in sorted(sizes_seen.items()):
            print(f"  {key}: {len(names)} tournaments")
            for n in names[:3]:
                print(f"    - {n}")
            if len(names) > 3:
                print(f"    ... and {len(names) - 3} more")
    else:
        print("\nAll tournaments matched a description!")


if __name__ == "__main__":
    main()
