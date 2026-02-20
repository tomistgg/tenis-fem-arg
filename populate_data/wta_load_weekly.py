import csv
import os
import time
import requests
from datetime import datetime, timedelta

MATCHES_URL = "https://api.wtatennis.com/tennis/tournaments/{tournament_id}/{year}/matches?states=L%2C+C"
CALENDAR_URL = "https://api.wtatennis.com/tennis/tournaments/?page={page}&pageSize=100&excludeLevels=ITF%2C+Grand%20Slam&from={from_date}&to={to_date}"

HEADERS = {
    "accept": "*/*",
    "accept-language": "es-ES,es;q=0.9,en;q=0.8",
    "account": "wta",
    "origin": "https://www.wtatennis.com",
    "referer": "https://www.wtatennis.com/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
}

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(_BASE_DIR, "..", "data", "wta_matches_arg.csv")

CSV_COLUMNS = [
    "matchType", "matchId", "date", "tournamentId",
    "tournamentName", "tournamentCategory", "surface", "inOrOutdoor", "tournamentCountry",
    "roundName", "draw", "result", "resultStatusDesc",
    "winnerId", "winnerEntry", "winnerSeed", "winnerName", "winnerCountry",
    "loserId", "loserEntry", "loserSeed", "loserName", "loserCountry",
]


def get_week_boundaries(today=None):
    if today is None:
        today = datetime.today().date()
    week_start = today - timedelta(days=today.weekday())  # Monday
    prev_week_start = week_start - timedelta(days=7)
    next_week_end = week_start + timedelta(days=13)  # Sunday of next week
    return prev_week_start, next_week_end


def fetch_tournaments_for_range(from_date, to_date):
    all_tournaments = []
    page = 0
    while True:
        url = CALENDAR_URL.format(page=page, from_date=from_date, to_date=to_date)
        response = requests.get(url, headers=HEADERS)
        response.raise_for_status()
        data = response.json()
        page_content = data.get("content", [])
        all_tournaments.extend(page_content)

        if data.get("last", True) or not page_content:
            break
        page += 1

    print(f"  Found {len(all_tournaments)} tournaments from {from_date} to {to_date}.")
    return all_tournaments


def build_meta(t):
    title = t.get("title", "")
    country = t.get("country", "")
    if country and title.endswith(f", {country}"):
        name = title[: -len(f", {country}")]
    else:
        name = title
    return {
        "tournamentName":     name,
        "tournamentCategory": t.get("level", ""),
        "surface":            t.get("surface", ""),
        "inOrOutdoor":        t.get("inOutdoor", ""),
        "tournamentCountry":  country,
    }


def format_score(score_string):
    if not score_string:
        return ""
    stripped = score_string.strip()
    if stripped == "W/O":
        return "W/O"
    return score_string.replace(",", " ").replace("Ret'd", "ret.").replace("ret'd", "ret.")


def get_status_desc(result):
    if result == "W/O":
        return "Walkover"
    if result.endswith("ret."):
        return "Retired"
    if result.endswith("def."):
        return "Default"
    return ""


def parse_match(m, meta):
    winner = str(m.get("Winner", ""))

    if winner in ("2", "4"):
        w_id      = m.get("PlayerIDA", "")
        w_entry   = m.get("EntryTypeA", "").upper()
        w_seed    = m.get("SeedA", "")
        w_name    = f"{m.get('PlayerNameFirstA', '')} {m.get('PlayerNameLastA', '')}".strip()
        w_country = m.get("PlayerCountryA", "")
        l_id      = m.get("PlayerIDB", "")
        l_entry   = m.get("EntryTypeB", "").upper()
        l_seed    = m.get("SeedB", "")
        l_name    = f"{m.get('PlayerNameFirstB', '')} {m.get('PlayerNameLastB', '')}".strip()
        l_country = m.get("PlayerCountryB", "")
    else:
        w_id      = m.get("PlayerIDB", "")
        w_entry   = m.get("EntryTypeB", "").upper()
        w_seed    = m.get("SeedB", "")
        w_name    = f"{m.get('PlayerNameFirstB', '')} {m.get('PlayerNameLastB', '')}".strip()
        w_country = m.get("PlayerCountryB", "")
        l_id      = m.get("PlayerIDA", "")
        l_entry   = m.get("EntryTypeA", "").upper()
        l_seed    = m.get("SeedA", "")
        l_name    = f"{m.get('PlayerNameFirstA', '')} {m.get('PlayerNameLastA', '')}".strip()
        l_country = m.get("PlayerCountryA", "")

    timestamp = m.get("MatchTimeStamp", "")
    date = timestamp[:10] if timestamp else ""

    result = format_score(m.get("ScoreString", ""))
    status_desc = get_status_desc(result)

    return {
        "matchType":          "WTA",
        "matchId":            m.get("MatchID", ""),
        "date":               date,
        "tournamentId":       m.get("EventID", ""),
        "tournamentName":     meta["tournamentName"],
        "tournamentCategory": meta["tournamentCategory"],
        "surface":            meta["surface"],
        "inOrOutdoor":        meta["inOrOutdoor"],
        "tournamentCountry":  meta["tournamentCountry"],
        "roundName":          m.get("RoundID", ""),
        "draw":               m.get("DrawLevelType", ""),
        "result":             result,
        "resultStatusDesc":   status_desc,
        "winnerId":           w_id,
        "winnerEntry":        w_entry,
        "winnerSeed":         w_seed,
        "winnerName":         w_name,
        "winnerCountry":      w_country,
        "loserId":            l_id,
        "loserEntry":         l_entry,
        "loserSeed":          l_seed,
        "loserName":          l_name,
        "loserCountry":       l_country,
    }


def fetch_matches(tournament_id, year):
    url = MATCHES_URL.format(tournament_id=tournament_id, year=year)
    response = requests.get(url, headers=HEADERS)
    response.raise_for_status()
    return response.json().get("matches", [])


def load_existing_match_ids(output_file):
    if not os.path.exists(output_file):
        return set()
    ids = set()
    with open(output_file, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            t_id = row.get("tournamentId", "")
            date = row.get("date", "")
            m_id = row.get("matchId", "")
            ids.add((t_id, date[:4], m_id))
    return ids


def append_to_csv(new_rows, output_file):
    file_exists = os.path.exists(output_file) and os.path.getsize(output_file) > 0
    with open(output_file, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if not file_exists:
            writer.writeheader()
        writer.writerows(new_rows)


if __name__ == "__main__":
    today = datetime.today().date()
    range_start, range_end = get_week_boundaries(today)
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)

    print(f"\n{'='*60}")
    print(f"WTA Weekly Load")
    print(f"Today:       {today}")
    print(f"Prev week:   {range_start} to {week_start - timedelta(days=1)}")
    print(f"This week:   {week_start} to {week_end}")
    print(f"Next week:   {week_end + timedelta(days=1)} to {range_end}")
    print(f"{'='*60}\n")

    # Determine years covered by the range
    years = set()
    d = range_start
    while d <= range_end:
        years.add(d.year)
        d += timedelta(days=1)

    print("Step 1: Fetching calendar...")
    tournaments = fetch_tournaments_for_range(
        range_start.strftime("%Y-%m-%d"),
        range_end.strftime("%Y-%m-%d"),
    )

    if not tournaments:
        print("No tournaments found in this date range.")
        raise SystemExit(0)

    print(f"\nStep 2: Loading existing matches from CSV...")
    existing_ids = load_existing_match_ids(OUTPUT_FILE)
    print(f"  {len(existing_ids)} existing match IDs loaded.")

    print(f"\nStep 3: Fetching match details for {len(tournaments)} tournaments...")
    new_rows = []

    for t in tournaments:
        t_id = t.get("tournamentGroup", {}).get("id")
        t_name = t.get("title", "")
        if not t_id:
            continue

        # Determine tournament year from startDate
        start_date = t.get("startDate", "")
        if start_date:
            t_year = int(start_date[:4])
        else:
            t_year = today.year

        try:
            raw_matches = fetch_matches(t_id, t_year)
        except Exception as e:
            print(f"  [!] Failed to fetch matches for {t_name} ({t_id}): {e}")
            time.sleep(1)
            continue

        arg_matches = [
            m for m in raw_matches
            if m.get("DrawMatchType") == "S"
            and (m.get("PlayerCountryA") == "ARG" or m.get("PlayerCountryB") == "ARG")
        ]

        if arg_matches:
            meta = build_meta(t)
            for m in arg_matches:
                row = parse_match(m, meta)
                key = (row["tournamentId"], row["date"][:4], row["matchId"])
                if key not in existing_ids:
                    new_rows.append(row)
                    existing_ids.add(key)

        time.sleep(0.3)

    print(f"\nStep 4: Saving results...")
    if new_rows:
        append_to_csv(new_rows, OUTPUT_FILE)
        print(f"  [+] Added {len(new_rows)} new matches to {OUTPUT_FILE}")
    else:
        print(f"  [i] No new matches to add.")

    print("\nDone.")
