import json
import time
import os
import requests
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "..", "data")
POINTS_DIST_PATH = os.path.join(DATA_DIR, "points_distribution.json")
OUTPUT_PATH = os.path.join(DATA_DIR, "wta_tournament_draw_sizes.json")

HEADERS = {
    "accept": "*/*",
    "accept-language": "es-ES,es;q=0.9,en;q=0.8",
    "account": "wta",
    "origin": "https://www.wtatennis.com",
    "referer": "https://www.wtatennis.com/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
}

CALENDAR_URL = "https://api.wtatennis.com/tennis/tournaments/"
MATCHES_URL = "https://api.wtatennis.com/tennis/tournaments/{tournament_id}/{year}/matches?states=L%2C+C"


def get_monday(date_str):
    if not date_str:
        return None
    if "T" in date_str:
        date_str = date_str.split("T")[0]
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    monday = dt - timedelta(days=dt.weekday())
    return monday.strftime("%Y-%m-%d")


def build_tournament_name(tournament):
    """Build tournament name matching the format used in wta_matches_arg.csv."""
    title = tournament.get("title", "")
    country = tournament.get("country", "")
    if country and title.endswith(f", {country}"):
        name = title[: -len(f", {country}")]
    else:
        name = title
    return name


def fetch_tournaments(from_date, to_date):
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
            r = requests.get(CALENDAR_URL, headers=HEADERS, params=params, timeout=10)
            r.raise_for_status()
            data = r.json()
            page_content = data.get("content", [])
            all_tournaments.extend(page_content)

            if len(page_content) < 100:
                break
            page += 1
        except Exception as e:
            print(f"Error fetching page {page}: {e}")
            break
    return all_tournaments


def count_qualifying_players(tournament_id, year):
    """Fetch matches for a tournament and count unique qualifying players."""
    url = MATCHES_URL.format(tournament_id=tournament_id, year=year)
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
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


def get_description(level, main_draw_size, qual_size):
    """Map tournament level + draw sizes to the points_distribution description."""
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


def main():
    with open(POINTS_DIST_PATH, 'r', encoding='utf-8') as f:
        points_dist = json.load(f)

    # Verify all descriptions exist in points_distribution
    desc_set = {entry["Description"] for entry in points_dist}

    today = datetime.now()
    from_date = (today - timedelta(weeks=55)).strftime("%Y-%m-%d")
    to_date = today.strftime("%Y-%m-%d")

    print(f"Fetching WTA tournaments from {from_date} to {to_date}...")
    tournaments = fetch_tournaments(from_date, to_date)
    print(f"Found {len(tournaments)} tournaments")

    results = []
    for i, t in enumerate(tournaments):
        t_id = t.get("tournamentGroup", {}).get("id")
        level = t.get("level", "")
        title = t.get("title", "")
        start_date = t.get("startDate", "")
        year = int(start_date[:4]) if start_date else today.year
        main_draw_size = t.get("singlesDrawSize", 0)

        name = build_tournament_name(t)
        date = get_monday(start_date)

        # For WTA 125, fetch matches to determine qualifying size
        qual_size = 0
        if level == "WTA 125" and t_id:
            qual_size = count_qualifying_players(t_id, year)
            time.sleep(0.3)

        desc = get_description(level, main_draw_size, qual_size)

        if desc and desc not in desc_set:
            print(f"  WARNING: Description '{desc}' not in points_distribution.json")
            desc = None

        results.append({
            "date": date,
            "tournamentName": name,
            "tournamentId": str(t_id) if t_id else "",
            "category": level,
            "mainDrawSize": main_draw_size,
            "qualifyingSize": qual_size,
            "description": desc,
        })

        status = "OK" if desc else "NO MATCH"
        q_info = f", {qual_size}Q" if qual_size else ""
        print(f"  {name}: {level}, {main_draw_size}M{q_info} -> {desc or status}")

    # Save results
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"\nSaved {len(results)} tournaments to {OUTPUT_PATH}")

    # Validation
    unmatched = [r for r in results if not r.get("description")]
    if unmatched:
        print(f"\nWARNING: {len(unmatched)} tournaments didn't match:")
        for r in unmatched:
            print(f"  {r['tournamentName']}: {r['category']}, {r['mainDrawSize']}M")
    else:
        print("\nAll tournaments matched a description!")


if __name__ == "__main__":
    main()
