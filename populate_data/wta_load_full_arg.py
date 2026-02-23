import csv
import os
import re
import time
import requests

MATCHES_URL = "https://api.wtatennis.com/tennis/tournaments/{tournament_id}/{year}/matches?states=L%2C+C"
CALENDAR_URL = "https://api.wtatennis.com/tennis/tournaments/?page={page}&pageSize=100&excludeLevels=ITF%2C+Grand%20Slam&from={year}-01-01&to={year}-12-31"

HEADERS = {
    "accept": "*/*",
    "accept-language": "es-ES,es;q=0.9,en;q=0.8",
    "account": "wta",
    "origin": "https://www.wtatennis.com",
    "referer": "https://www.wtatennis.com/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
}

START_YEAR = 2016
END_YEAR   = 2026
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(_BASE_DIR, "..", "data", "wta_matches_arg.csv")

CSV_COLUMNS = [
    "matchType", "matchId", "date", "tournamentId",
    "tournamentName", "tournamentCategory", "surface", "inOrOutdoor", "tournamentCountry",
    "roundName", "draw", "result", "resultStatusDesc",
    "winnerId", "winnerEntry", "winnerSeed", "winnerName", "winnerCountry",
    "loserId", "loserEntry", "loserSeed", "loserName", "loserCountry",
]

# Matches missing from the API that must be added manually.
# IDs follow the pattern MANUAL_001, MANUAL_002, …
MANUAL_MATCHES = [
    {
        "matchType": "WTA", "matchId": "MANUAL_001", "date": "2024-06-07",
        "tournamentId": "2077", "tournamentName": "Open delle Puglie - Bari, ITA",
        "tournamentCategory": "WTA 125", "surface": "Clay", "inOrOutdoor": "O",
        "tournamentCountry": "ITALY", "roundName": "Q", "draw": "M",
        "result": "6-2 6-3", "resultStatusDesc": "",
        "winnerId": "319112", "winnerEntry": "", "winnerSeed": "1",
        "winnerName": "Nadia Podoroska", "winnerCountry": "ARG",
        "loserId": "329308", "loserEntry": "", "loserSeed": "",
        "loserName": "Beatrice Ricci", "loserCountry": "ITA",
    },
]


MAIN_DRAW_ROUND_MAP = {
    "1": "1st Round", "2": "2nd Round", "3": "3rd Round",
    "4": "4th Round", "5": "5th Round",
    "Q": "Quarter-finals", "S": "Semi-finals", "F": "Final",
}


def _q_round_key(rnd):
    rnd = str(rnd)
    if rnd.isdigit():
        return int(rnd)
    m = re.match(r"^Q(\d+)$", rnd)
    if m:
        return int(m.group(1))
    m = re.match(r"^QR(\d+)$", rnd)
    if m:
        return int(m.group(1))
    text = {"1st Round": 1, "2nd Round": 2, "3rd Round": 3, "4th Round": 4}
    return text.get(rnd, 99)


def build_q_round_map(raw_matches):
    """Map qualifying RoundID values → QR1/QR2/.../QRF using ALL qualifying singles matches."""
    q_rounds = {
        str(m.get("RoundID", ""))
        for m in raw_matches
        if m.get("DrawLevelType") == "Q" and m.get("DrawMatchType") == "S" and m.get("RoundID", "")
    }
    if not q_rounds:
        return {}
    sorted_rounds = sorted(q_rounds, key=_q_round_key)
    result = {}
    for i, rnd in enumerate(sorted_rounds):
        result[rnd] = f"QR{i + 1}"
    return result


def fetch_tournaments_for_year(year):
    all_tournaments = []
    page = 0
    while True:
        url = CALENDAR_URL.format(year=year, page=page)
        response = requests.get(url, headers=HEADERS)
        response.raise_for_status()
        data = response.json()
        page_content = data.get("content", [])
        all_tournaments.extend(page_content)

        last = data.get("last", True)
        if last or not page_content:
            break
        page += 1

    print(f"  Total: {len(all_tournaments)} tournaments in {year}.")
    for t in all_tournaments:
        group_id = t.get("tournamentGroup", {}).get("id", "")
        live_id  = t.get("liveScoringId", "")
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


def parse_match(m, meta, q_map=None):
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
        "roundName":          _map_round(m.get("RoundID", ""), m.get("DrawLevelType", ""), q_map),
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


def _map_round(raw_round, draw_level, q_map):
    raw_round = str(raw_round)
    if draw_level == "Q":
        return q_map.get(raw_round, raw_round) if q_map else raw_round
    return MAIN_DRAW_ROUND_MAP.get(raw_round, raw_round)


def fetch_matches(tournament_id, year):
    url = MATCHES_URL.format(tournament_id=tournament_id, year=year)
    response = requests.get(url, headers=HEADERS)
    response.raise_for_status()
    return response.json().get("matches", [])


def deduplicate(rows):
    seen = set()
    unique = []
    for row in rows:
        key = (row["tournamentId"], row["date"][:4], row["matchId"])
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


def save_csv(rows, output_file):
    rows = deduplicate(rows)
    with open(output_file, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nSaved {len(rows)} total ARG matches to {output_file}")


if __name__ == "__main__":
    all_rows = []

    for year in range(START_YEAR, END_YEAR + 1):

        try:
            tournaments = fetch_tournaments_for_year(year)
        except Exception as e:
            print(f"  [!] Failed to fetch calendar for {year}: {e}")
            continue

        for t in tournaments:
            t_id = t.get("tournamentGroup", {}).get("id")
            t_name = t.get("title", "")
            if not t_id:
                continue

            try:
                raw_matches = fetch_matches(t_id, year)
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
                q_map = build_q_round_map(raw_matches)
                rows = [parse_match(m, meta, q_map) for m in arg_matches]
                all_rows.extend(rows)

            time.sleep(0.3)

    all_rows.extend(MANUAL_MATCHES)
    save_csv(all_rows, OUTPUT_FILE)
