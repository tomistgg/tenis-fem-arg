import time
import json
import random
import requests
from datetime import datetime, timedelta

from config import NAME_LOOKUP, ITF_CACHE_FILE
from utils import get_cached_rankings
from calendar_builder import get_next_monday


def get_itf_level(tournament_name):
    """Determine ITF tournament level from its name."""
    t = tournament_name
    if "W100" in t or "100k" in t: return "W100"
    if "W75" in t or "75k" in t: return "W75"
    if "W60" in t or "60k" in t: return "W60"
    if "W50" in t or "50k" in t: return "W50"
    if "W35" in t or "35k" in t: return "W35"
    if "W25" in t or "25k" in t: return "W25"
    return "W15"


def parse_itf_entry_list(itf_entries):
    """Parse raw ITF acceptance list classifications into a sorted player list."""
    players = []
    for classification in itf_entries:
        class_code = classification.get("entryClassificationCode", "")
        if class_code in ["MDA", "JR"]:
            section_type = "MAIN"
        elif class_code == "Q":
            section_type = "QUAL"
        elif class_code == "A":
            section_type = "ALT"
        else:
            continue

        for entry in classification.get("entries") or []:
            pos = entry.get("positionDisplay", "-")
            entry_players = entry.get("players") or []

            try:
                pos_digits = ''.join(filter(str.isdigit, str(pos)))
                pos_num = int(pos_digits) if pos_digits else 999
            except:
                pos_num = 999

            priority = str(entry.get("priority") or "").strip()

            if not entry_players:
                if entry.get("isAvailableSlot"):
                    display_name = "(Available Slot)"
                elif entry.get("isExemption"):
                    display_name = "(Special Exempt)"
                else:
                    continue
                players.append({
                    "pos": pos, "name": display_name, "country": "-",
                    "rank": "-", "priority": priority, "type": section_type, "pos_num": pos_num
                })
                continue

            p_node = entry_players[0]
            raw_f_name = f"{p_node.get('givenName', '')} {p_node.get('familyName', '')}".strip()

            wta = p_node.get("atpWtaRank", "")
            itf_rank = p_node.get("itfBTRank")
            wtn = p_node.get("worldRating", "")

            if class_code == "JR":
                erank_str = "JE"
            else:
                erank_str = "-"
                if wta and str(wta).strip() != "":
                    erank_str = f"{wta}"
                elif itf_rank is not None and str(itf_rank).strip() != "":
                    erank_str = f"ITF {itf_rank}"
                elif wtn and str(wtn).strip() != "":
                    erank_str = f"WTN {wtn}"

            players.append({
                "pos": pos, "name": raw_f_name, "country": p_node.get("nationalityCode", "-"),
                "rank": erank_str, "priority": priority, "type": section_type, "pos_num": pos_num
            })

    # Ensure Special Exempt positions don't conflict with JE (Junior Exempt) players
    real_main = [p for p in players if p["type"] == "MAIN" and p["name"] != "(Special Exempt)"]
    exempt = [p for p in players if p["type"] == "MAIN" and p["name"] == "(Special Exempt)"]
    if exempt and real_main:
        max_pos = max(p["pos_num"] for p in real_main)
        for i, p in enumerate(exempt):
            p["pos_num"] = max_pos + i + 1
            p["pos"] = str(max_pos + i + 1)

    players.sort(key=lambda x: (x["pos_num"], x["name"]))
    return players


def get_full_itf_calendar(driver):
    """Fetch all ITF tournaments for the full year via Selenium. Numbers duplicates across the whole year."""
    today = datetime.now()
    date_from = f"{today.year}-01-01"
    date_to = f"{today.year}-12-31"

    all_items = []
    skip = 0
    take = 500

    while True:
        url = f"https://www.itftennis.com/tennis/api/TournamentApi/GetCalendar?circuitCode=WT&dateFrom={date_from}&dateTo={date_to}&skip={skip}&take={take}"
        try:
            driver.get(url)
            time.sleep(random.uniform(3, 5))
            raw_content = driver.find_element("tag name", "body").text
            data = json.loads(raw_content)
            items = data.get('items', [])
            if not items:
                break
            all_items.extend(items)

            total = data.get('totalItems', 0)
            if skip + take >= total:
                break
            skip += take
        except Exception as e:
            print(f"Error fetching full ITF calendar (skip={skip}): {e}")
            break

    tournaments = []
    for item in all_items:
        status = (item.get('status') or item.get('tournamentStatus') or '').lower()
        if 'cancel' in status:
            continue
        t_name = item.get('tournamentName', '')
        if 'cancel' in t_name.lower():
            continue

        level = get_itf_level(t_name)

        surface = item.get('surfaceDesc') or item.get('surface') or ""
        country = item.get('hostNationCode') or item.get('hostNation') or item.get('countryCode') or ""
        tournaments.append({
            "name": t_name,
            "level": level,
            "surface": surface,
            "country": country,
            "startDate": item.get('startDate'),
            "endDate": item.get('endDate', None)
        })

    # Number duplicate names across the full year
    tournaments.sort(key=lambda x: x.get("startDate") or "")
    name_counts = {}
    for t in tournaments:
        name_counts[t["name"]] = name_counts.get(t["name"], 0) + 1
    name_seq = {}
    for t in tournaments:
        if name_counts[t["name"]] > 1:
            name_seq[t["name"]] = name_seq.get(t["name"], 0) + 1
            t["name"] = f'{t["name"]} {name_seq[t["name"]]}'

    # Only return future tournaments
    today_str = today.strftime("%Y-%m-%d")
    tournaments = [t for t in tournaments if (t.get("endDate") or t.get("startDate") or "") >= today_str]

    return tournaments


def get_itf_players(tournament_key, driver):
    url = f"https://www.itftennis.com/tennis/api/TournamentApi/GetAcceptanceList?tournamentKey={tournament_key}&circuitCode=WT"
    try:
        driver.get(url)
        time.sleep(random.uniform(4, 6))
        raw_content = driver.find_element("tag name", "body").text
        start = raw_content.find('[')
        end = raw_content.rfind(']') + 1
        if start == -1: return [], {}

        data = json.loads(raw_content[start:end])

        root_data = data[0].get("entryClassifications", []) if data else []

        name_map = {}
        for classification in root_data:
            desc = classification.get("entryClassification", "").upper()
            code = classification.get("entryClassificationCode", "")
            if "WITHDRAWAL" in desc: continue

            for entry in classification.get("entries") or []:
                pos = entry.get("positionDisplay", "")
                suffix = "" if code == "MDA" else (f" (ALT {pos})" if code == "ALT" or "ALTERNATE" in desc else " (Q)")
                players = entry.get("players") or []
                for p in players:
                    full_name = f"{p.get('givenName', '')} {p.get('familyName', '')}".strip().upper()
                    matched_name = NAME_LOOKUP.get(full_name, full_name)
                    name_map[matched_name] = suffix

        return root_data, name_map
    except Exception as e:
        print(f"Error en {tournament_key}: {e}")
        return [], {}


def get_dynamic_itf_calendar(driver, num_weeks=3):
    """Fetch ITF calendar dynamically for the next N weeks"""
    try:
        next_monday = get_next_monday()
        date_from = next_monday.strftime("%Y-%m-%d")
        date_to = (next_monday + timedelta(weeks=num_weeks)).strftime("%Y-%m-%d")

        url = f"https://www.itftennis.com/tennis/api/TournamentApi/GetCalendar?circuitCode=WT&dateFrom={date_from}&dateTo={date_to}&skip=0&take=500"
        driver.get(url)
        time.sleep(5)
        raw_content = driver.find_element("tag name", "body").text
        data = json.loads(raw_content)
        return data.get('items', [])
    except Exception as e:
        print(f"Error calendario: {e}")
        return []


def get_itf_rankings(nationality="ARG"):
    all_players = []
    skip = 0
    take = 50

    while True:
        url = "https://www.itftennis.com/tennis/api/PlayerRankApi/GetPlayerRankings"

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.itftennis.com/en/rankings/",
            "Sec-Ch-Ua": '"Not(A:Brand";v="8", "Chromium";v="144", "Google Chrome";v="144"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin"
        }

        params = {
            "circuitCode": "WT",
            "matchTypeCode": "S",
            "ageCategoryCode": "",
            "nationCode": nationality,
            "take": take,
            "skip": skip,
            "isOrderAscending": "true"
        }

        try:
            r = requests.get(url, headers=headers, params=params, timeout=10)
            data = r.json()
            items = data.get('items', []) if isinstance(data, dict) else []
            if not items: break
            all_players.extend(items)

            total_items = data.get("totalItems", 0)
            if skip + take >= total_items: break

            skip += take
            time.sleep(0.1)
        except:
            break

    ranking_results = []
    for p in all_players:
        if not p.get('playerId'): continue
        itf_name = f"{p.get('playerGivenName', '')} {p.get('playerFamilyName', '')}".strip().upper()
        display_name = NAME_LOOKUP.get(itf_name, itf_name)
        ranking_results.append({
            "Player": display_name,
            "Rank": f"ITF {p.get('rank')}",
            "Country": p.get('playerNationalityCode', ''),
            "Key": display_name
        })
    return ranking_results


def get_itf_rankings_cached(date_str, nationality="ARG"):
    """Get ITF rankings with caching"""
    return get_cached_rankings(
        date_str,
        ITF_CACHE_FILE,
        lambda d, **kw: get_itf_rankings(nationality=kw.get('nationality', 'ARG')),
        nationality=nationality
    )
