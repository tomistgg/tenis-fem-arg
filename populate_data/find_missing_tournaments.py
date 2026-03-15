import csv
import json
import os
import re
import time
from typing import Dict, List, Set

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "..", "data")

MATCHES_CSV = os.path.join(DATA_DIR, "wta_matches_arg.csv")
PRE_2016_JSON = os.path.join(DATA_DIR, "wta_pre_2016.json")
OUTPUT_JSON = os.path.join(DATA_DIR, "wta_missing_tournaments.json")

PLAYER_ID_RE = re.compile(r"^800\d{6}$")


def load_existing_tournament_links(path: str) -> Set[str]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    links = set()
    for item in data:
        link = (item or {}).get("tournamentLink") or ""
        if link:
            links.add(link)
    return links


def extract_player_ids(matches_path: str) -> Set[str]:
    ids = set()
    with open(matches_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            winner_country = (row.get("winnerCountry") or "").strip().upper()
            loser_country = (row.get("loserCountry") or "").strip().upper()
            if winner_country == "ARG":
                pid = (row.get("winnerId") or "").strip()
                if PLAYER_ID_RE.match(pid):
                    ids.add(pid)
            if loser_country == "ARG":
                pid = (row.get("loserId") or "").strip()
                if PLAYER_ID_RE.match(pid):
                    ids.add(pid)
    return ids


def extract_tournaments(obj, out: Dict[str, Dict[str, str]]):
    if isinstance(obj, dict):
        # Primary structure (example.json): top-level "items" array of tournaments
        if "items" in obj and isinstance(obj["items"], list):
            for item in obj["items"]:
                if not isinstance(item, dict):
                    continue
                link = item.get("tournamentLink") or ""
                name = item.get("tournamentName") or ""
                if link and name:
                    out[link] = {
                        "tournamentName": name,
                        "tournamentLink": link,
                        "tourCode": item.get("tourCode") or "",
                        "dates": item.get("dates") or "",
                        "location": item.get("location") or "",
                        "surfaceDesc": item.get("surfaceDesc") or "",
                        "surfaceCode": item.get("surfaceCode") or "",
                    }
            return
        # Fallback: recursive search for tournament objects
        if "tournamentName" in obj and "tournamentLink" in obj:
            link = obj.get("tournamentLink") or ""
            name = obj.get("tournamentName") or ""
            if link and name:
                out[link] = {
                    "tournamentName": name,
                    "tournamentLink": link,
                    "tourCode": obj.get("tourCode") or "",
                    "dates": obj.get("dates") or "",
                    "location": obj.get("location") or "",
                    "surfaceDesc": obj.get("surfaceDesc") or "",
                    "surfaceCode": obj.get("surfaceCode") or "",
                }
        for v in obj.values():
            extract_tournaments(v, out)
    elif isinstance(obj, list):
        for v in obj:
            extract_tournaments(v, out)


def create_driver():
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("window-size=1920,1080")
    chrome_options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
    )
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=chrome_options)


def fetch_player_activity(session: requests.Session, driver, player_id: str, year: int, max_retries: int = 3) -> dict:
    url = (
        "https://www.itftennis.com/tennis/api/PlayerApi/GetPlayerActivity"
        f"?circuitCode=WT&matchTypeCode=S&playerId={player_id}"
        f"&skip=0&surfaceCode=&take=5000&tourCategoryCode=&year={year}"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
        "Referer": "https://www.itftennis.com/",
    }
    last_err = None
    for attempt in range(1, max_retries + 1):
        resp = session.get(url, timeout=20, headers=headers)
        if resp.status_code != 200:
            last_err = RuntimeError(f"HTTP {resp.status_code}")
            time.sleep(0.6 * attempt)
            continue
        if not resp.text or not resp.text.strip():
            last_err = ValueError("Empty response body")
            time.sleep(0.6 * attempt)
            continue
        try:
            return resp.json()
        except ValueError:
            # Sometimes returns HTML; fall back to Selenium on final attempt
            last_err = ValueError("Non-JSON response")
            time.sleep(0.8 * attempt)

    # Fallback to Selenium (same pattern as itf_load_new.py)
    if driver is not None:
        driver.get(url)
        time.sleep(1)
        raw = driver.find_element("tag name", "body").text.strip()
        if not raw:
            raise ValueError("Empty response body (selenium)")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            snippet = raw[:200].replace("\n", " ")
            # Incapsula/WAF block page: treat as empty response
            if "incapsula" in raw.lower() or "request unsuccessful" in raw.lower():
                return {"items": [], "totalItems": 0}
            raise ValueError(f"Non-JSON response (selenium): {snippet}")

    if last_err:
        raise last_err
    return {}


def main():
    existing_links = load_existing_tournament_links(PRE_2016_JSON)
    player_ids = sorted(extract_player_ids(MATCHES_CSV))

    missing: Dict[str, Dict[str, str]] = {}

    years = list(range(1975, 2016))

    driver = create_driver()
    try:
        with requests.Session() as session:
            for idx, pid in enumerate(player_ids, start=1):
                print(f"[{idx}/{len(player_ids)}] Fetching player {pid}...")
                for year in years:
                    print(year)
                    try:
                        data = fetch_player_activity(session, driver, pid, year)
                        found: Dict[str, Dict[str, str]] = {}
                        extract_tournaments(data, found)
                        for link, info in found.items():
                            if link not in existing_links:
                                missing[link] = info
                    except Exception as e:
                        print(f"  Error for player {pid} year {year}: {e}")
                    time.sleep(0.2)
    finally:
        driver.quit()

    tournament_list: List[Dict[str, str]] = sorted(
        missing.values(), key=lambda x: (x.get("tournamentName") or "", x.get("tournamentLink") or "")
    )

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(tournament_list, f, ensure_ascii=False, indent=2)

    print(f"Done. Missing tournaments: {len(tournament_list)}")
    print(f"Saved to {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
