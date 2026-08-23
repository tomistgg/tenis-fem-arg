"""
Incremental update for tournament draw sizes.
- Scans a weekday-specific draw window: Monday includes last and current week,
  Tuesday-Saturday current week only, and Sunday includes current and next week
- Reuses every valid draw size already saved instead of polling it again
- Adds newly published sizes and resolves earlier zero-size placeholders
- Removes entries older than 55 weeks
"""

import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(BASE_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from http_client import get_with_retry
from itf_drawsheet_cache import get_cached_drawsheet, save_drawsheet
from runtime_logging import get_logger
from runtime_paths import DATA_DIR as RUNTIME_DATA_DIR
from time_utils import madrid_today

DATA_DIR = str(RUNTIME_DATA_DIR)
from utils import (
    compress_tournament_draw_sizes,
    expand_itf_calendar_cache,
    expand_points_distribution,
    expand_tournament_draw_sizes,
    save_json_array_one_line_per_item,
)
from wta_calendar_cache import get_shared_wta_calendar

logger = get_logger("draw-sizes")
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


def get_draw_lookup_range(today=None):
    """Return the draw lookup window for the current Madrid weekday.

    Monday keeps the previous week in scope for late results. Tuesday through
    Saturday only need the current week. Sunday also looks ahead one week so
    newly published draws are available before Monday's run.
    """
    today = today or madrid_today()
    week_start = today - timedelta(days=today.weekday())
    if today.weekday() == 0:  # Monday: last + current week
        start = week_start - timedelta(days=7)
        end = week_start + timedelta(days=6)
    elif today.weekday() == 6:  # Sunday: current + next week
        start = week_start
        end = week_start + timedelta(days=13)
    else:  # Tuesday-Saturday: current week
        start = week_start
        end = week_start + timedelta(days=6)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def load_existing():
    if os.path.exists(OUTPUT_PATH):
        with open(OUTPUT_PATH, encoding="utf-8") as f:
            return expand_tournament_draw_sizes(json.load(f))
    return []


def save_results(data):
    save_json_array_one_line_per_item(
        OUTPUT_PATH,
        compress_tournament_draw_sizes(data),
    )


def _normalized_tournament_name(value):
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_name = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", ascii_name.casefold()).strip()


def _has_valid_draw_size(entry):
    """Return whether an entry contains a published main-draw size."""
    try:
        return int(entry.get("mainDrawSize") or 0) > 0
    except (AttributeError, TypeError, ValueError):
        return False


def _tournament_aliases(entry):
    """Return durable identities for old and new compact draw-size rows."""
    source = str(entry.get("source") or "").upper()
    date = str(entry.get("date") or "")
    aliases = set()

    if source == "WTA":
        tournament_id = str(entry.get("tournamentId") or "").strip()
        if tournament_id:
            aliases.add(("WTA_ID", tournament_id, date))
    elif source == "ITF":
        # Older compact rows did not retain tournamentKey. New rows also store
        # it in the shared tournamentId slot, while the name alias keeps those
        # legacy rows cacheable without a data migration.
        tournament_key = str(entry.get("tournamentKey") or entry.get("tournamentId") or "").strip().casefold()
        if tournament_key:
            aliases.add(("ITF_KEY", tournament_key, date))

    name = _normalized_tournament_name(entry.get("tournamentName"))
    if name and (source != "WTA" or not aliases):
        aliases.add((f"{source}_NAME", name, date))
    return aliases


def _valid_draw_size_aliases(entries):
    aliases = set()
    for entry in entries:
        if _has_valid_draw_size(entry):
            aliases.update(_tournament_aliases(entry))
    return aliases


def _draw_size_is_saved(entry, saved_aliases):
    return bool(saved_aliases and _tournament_aliases(entry) & saved_aliases)


def _merge_draw_size_updates(existing, updates):
    """Append missing sizes and replace matching unresolved legacy rows."""
    added = 0
    updated = 0
    for candidate in updates:
        if not _has_valid_draw_size(candidate):
            continue

        aliases = _tournament_aliases(candidate)
        matching_indexes = [
            index
            for index, entry in enumerate(existing)
            if aliases & _tournament_aliases(entry)
        ]
        if any(_has_valid_draw_size(existing[index]) for index in matching_indexes):
            continue
        if matching_indexes:
            existing[matching_indexes[0]] = candidate
            updated += 1
        else:
            existing.append(candidate)
            added += 1
    return added, updated


# ── WTA ────────────────────────────────────────────────────────────────────────

WTA_HEADERS = {
    "accept": "*/*",
    "accept-language": "es-ES,es;q=0.9,en;q=0.8",
    "account": "wta",
    "origin": "https://www.wtatennis.com",
    "referer": "https://www.wtatennis.com/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
    ),
}


def wta_count_qualifying_players(tournament_id, year):
    url = f"https://api.wtatennis.com/tennis/tournaments/{tournament_id}/{year}/matches?states=C"
    try:
        r = get_with_retry(
            url,
            component="draw-sizes-wta-matches",
            headers=WTA_HEADERS,
        )
        matches = r.json().get("matches", [])
    except Exception as e:
        logger.warning(f"  Error fetching WTA matches for {tournament_id}/{year}: {e}")
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


def _load_wta_calendar_cache(from_date, to_date):
    """Return this consumer's window from the one shared WTA calendar."""
    return get_shared_wta_calendar(
        from_date,
        to_date,
        exclude_levels={"Grand Slam"},
        component="draw-sizes-wta-calendar",
    )


def fetch_wta_updates(from_date, to_date, desc_set, saved_size_aliases=None):
    logger.info("Fetching WTA tournaments...")
    saved_size_aliases = saved_size_aliases or set()
    tournaments = _load_wta_calendar_cache(from_date, to_date)
    logger.debug(f"  Using shared WTA calendar ({len(tournaments)} tournaments).")
    logger.info(f"  Found {len(tournaments)} WTA tournaments in range")

    today = madrid_today()
    results = []
    for t in tournaments:
        t_id = t.get("tournamentGroup", {}).get("id")
        level = t.get("level", "")
        start_date = t.get("startDate", "")
        year = int(start_date[:4]) if start_date else today.year
        main_draw_size = t.get("singlesDrawSize", 0)

        name = wta_build_tournament_name(t)
        date = get_monday(start_date)

        identity = {
            "source": "WTA",
            "date": date,
            "tournamentName": name,
            "tournamentId": str(t_id) if t_id else "",
        }
        if _draw_size_is_saved(identity, saved_size_aliases):
            logger.debug(f"  {name}: using permanently saved draw size")
            continue

        qual_size = 0
        if level == "WTA 125" and t_id:
            qual_size = wta_count_qualifying_players(t_id, year)
            time.sleep(0.3)

        desc = wta_get_description(level, main_draw_size, qual_size)
        if desc and desc not in desc_set:
            desc = None

        results.append(
            {
                "source": "WTA",
                "date": date,
                "tournamentName": name,
                "tournamentId": str(t_id) if t_id else "",
                "category": level,
                "mainDrawSize": main_draw_size,
                "qualifyingSize": qual_size,
                "description": desc,
            }
        )

        q_info = f", {qual_size}Q" if qual_size else ""
        logger.debug(f"  {name}: {level}, {main_draw_size}M{q_info} -> {desc or 'NO MATCH'}")

    return results


# ── ITF ────────────────────────────────────────────────────────────────────────


def get_itf_level(name):
    if "W100" in name or "100k" in name:
        return "W100"
    if "W75" in name or "75k" in name:
        return "W75"
    if "W60" in name or "60k" in name:
        return "W60"
    if "W50" in name or "50k" in name:
        return "W50"
    if "W35" in name or "35k" in name:
        return "W35"
    if "W25" in name or "25k" in name:
        return "W25"
    return "W15"


def itf_fetch_drawsheet(t_id, classification, week_number=0):
    t_id = _normalize_itf_tournament_id(t_id)
    if t_id is None:
        logger.debug("  Skipping ITF drawsheet fetch with no valid tournament ID")
        return None

    cached = get_cached_drawsheet(t_id, classification, week_number)
    if cached is not None:
        return cached
    stale_cached = get_cached_drawsheet(
        t_id,
        classification,
        week_number,
        allow_stale=True,
    )

    url = "https://www.itftennis.com/tennis/api/TournamentApi/GetDrawsheet"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
        ),
        "Referer": f"https://www.itftennis.com/en/tournament/draws-and-results/print/?tournamentId={t_id}&circuitCode=WT",
        "Origin": "https://www.itftennis.com",
        "Accept": "application/json, text/plain, */*",
    }
    params = {
        "eventClassificationCode": classification,
        "matchTypeCode": "S",
        "tourType": "N",
        "tournamentId": str(t_id),
        "weekNumber": week_number,
    }
    try:
        r = get_with_retry(
            url,
            component="draw-sizes-itf",
            failure_status="degraded",
            params=params,
            headers=headers,
        )
        data = r.json()
        if isinstance(data, dict):
            save_drawsheet(t_id, classification, week_number, data)
        return data
    except (RuntimeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.debug(f"  ITF drawsheet fetch failed for {t_id}: {exc}")
        return stale_cached


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
        descs.append(
            {
                "description": d,
                "category": d.split(" ")[0],
                "main_size": int(m_str),
                "qual_sizes": [int(x) for x in q_str.split("/")],
            }
        )
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


ITF_CALENDAR_CACHE_FILE = os.path.join(DATA_DIR, "itf_calendar_cache.json")
ITF_EVENT_FILTERS_CACHE_FILE = os.path.join(DATA_DIR, "itf_event_filters_cache.json")


def _is_cancelled_itf_calendar_item(item):
    status = (
        " ".join(
            str(item.get(field) or "")
            for field in (
                "status",
                "tournamentStatus",
                "statusDesc",
                "tournamentStatusDesc",
                "tourStatusCode",
                "tourStatusDesc",
            )
        )
        .strip()
        .upper()
    )
    if status == "CN" or "CANCEL" in status:
        return True
    text = " ".join(
        str(item.get(field) or "") for field in ("tournamentName", "name", "location", "tournamentLink")
    ).lower()
    return "cancel" in text


def _load_itf_calendar_cache(from_date, to_date):
    """Return filtered items from the persistent year-wide calendar cache."""
    try:
        with open(ITF_CALENDAR_CACHE_FILE, encoding="utf-8") as f:
            data = expand_itf_calendar_cache(json.load(f))
        items = data.get("items", []) if isinstance(data, dict) else (data or [])
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    lo = datetime.strptime(from_date, "%Y-%m-%d").date()
    hi = datetime.strptime(to_date, "%Y-%m-%d").date()
    results = []
    for item in items:
        s = str(item.get("startDate") or "")[:10]
        try:
            sd = datetime.strptime(s, "%Y-%m-%d").date()
        except ValueError:
            continue
        if lo <= sd <= hi:
            results.append(item)
    return results if results else None


def _load_itf_id_cache():
    """Return dict of tournamentKey (lower) -> tournamentId from persistent cache."""
    try:
        with open(ITF_EVENT_FILTERS_CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        normalized = {}
        for key, value in data.items():
            tournament_id = _normalize_itf_tournament_id(value)
            if tournament_id is not None:
                normalized[str(key).lower()] = tournament_id
        return normalized
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
        return {}


def _normalize_itf_tournament_id(value):
    """Return a positive numeric ITF ID, rejecting legacy ``"None"`` values."""
    try:
        tournament_id = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return str(tournament_id) if tournament_id > 0 else None


def _make_itf_driver():
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("window-size=1920,1080")
    chrome_options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
    )
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=chrome_options)


def _fill_ids_via_selenium(tournaments):
    """Fill missing tournamentId fields in-place using a short-lived Selenium session."""
    driver = _make_itf_driver()
    try:
        driver.get("https://www.itftennis.com/en/tournament-calendar/womens-world-tennis-tour-calendar/")
        time.sleep(5)
        for t in tournaments:
            url = (
                f"https://www.itftennis.com/tennis/api/TournamentApi/GetEventFilters?tournamentKey={t['tournamentKey']}"
            )
            driver.get(url)
            time.sleep(1)
            try:
                raw = driver.find_element("tag name", "body").text.strip()
                t["tournamentId"] = _normalize_itf_tournament_id(
                    json.loads(raw).get("tournamentId")
                )
            except (WebDriverException, json.JSONDecodeError, AttributeError, KeyError, TypeError, ValueError):
                t["tournamentId"] = None
    except (WebDriverException, json.JSONDecodeError, AttributeError, KeyError, TypeError, ValueError) as e:
        logger.warning(f"  [!] Selenium ID fetch error: {e}")
    finally:
        driver.quit()


def _fetch_itf_via_selenium(from_date, to_date):
    """Full Selenium fallback: GetCalendar + GetEventFilters. Returns tournament list or None."""
    driver = _make_itf_driver()
    try:
        driver.get("https://www.itftennis.com/en/tournament-calendar/womens-world-tennis-tour-calendar/")
        time.sleep(5)
        api_url = (
            f"https://www.itftennis.com/tennis/api/TournamentApi/GetCalendar?"
            f"circuitCode=WT&searchString=&skip=0&take=500"
            f"&dateFrom={from_date}&dateTo={to_date}"
            f"&isOrderAscending=true&orderField=startDate"
        )
        driver.get(api_url)
        time.sleep(2)
        raw = driver.find_element("tag name", "body").text.strip()
        items = json.loads(raw).get("items", [])
        logger.info(f"  Found {len(items)} ITF tournaments in range")

        seen_keys = set()
        tournaments = []
        for item in items:
            name = item.get("tournamentName", "")
            if _is_cancelled_itf_calendar_item(item):
                continue
            category = item.get("category", "")
            if category and category.strip().startswith("Tier"):
                continue
            link = item.get("tournamentLink", "")
            key = link.rstrip("/").split("/")[-1] if link else None
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            tournaments.append(
                {
                    "startDate": item.get("startDate"),
                    "tournamentName": name,
                    "tournamentKey": key,
                    "isMultiweek": category == "ITF Womens Multi-Week Circuit",
                    "tournamentId": None,
                }
            )

        for t in tournaments:
            url = (
                f"https://www.itftennis.com/tennis/api/TournamentApi/GetEventFilters?tournamentKey={t['tournamentKey']}"
            )
            driver.get(url)
            time.sleep(1)
            try:
                raw = driver.find_element("tag name", "body").text.strip()
                t["tournamentId"] = _normalize_itf_tournament_id(
                    json.loads(raw).get("tournamentId")
                )
            except (WebDriverException, json.JSONDecodeError, AttributeError, KeyError, TypeError, ValueError):
                t["tournamentId"] = None

        return tournaments
    except (WebDriverException, json.JSONDecodeError, AttributeError, KeyError, TypeError, ValueError) as e:
        logger.warning(f"  Error in Selenium ITF fetch: {e}")
        return None
    finally:
        driver.quit()


def fetch_itf_updates(from_date, to_date, itf_descs, saved_size_aliases=None):
    """Fetch ITF tournaments for the given date range, preferring persistent caches."""
    logger.info("Fetching ITF tournaments...")
    saved_size_aliases = saved_size_aliases or set()

    # Try calendar cache first — year-wide cache written by main.py is authoritative.
    cached_items = _load_itf_calendar_cache(from_date, to_date)
    id_cache = _load_itf_id_cache()

    results = []

    if cached_items is not None:
        logger.debug(f"  Using calendar cache ({len(cached_items)} items in range).")
        seen_keys = set()
        tournaments = []
        for item in cached_items:
            name = item.get("tournamentName", "")
            if _is_cancelled_itf_calendar_item(item):
                continue
            category = item.get("category", "")
            if category and category.strip().startswith("Tier"):
                continue
            link = item.get("tournamentLink", "")
            key = link.rstrip("/").split("/")[-1] if link else None
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            is_multiweek = category == "ITF Womens Multi-Week Circuit"
            t_id = id_cache.get(key.lower())
            tournaments.append(
                {
                    "startDate": item.get("startDate"),
                    "tournamentName": name,
                    "tournamentKey": key,
                    "isMultiweek": is_multiweek,
                    "tournamentId": t_id,
                }
            )
    else:
        logger.warning("  No calendar cache found; falling back to live Selenium fetch.")
        tournaments = _fetch_itf_via_selenium(from_date, to_date)
        if tournaments is None:
            return results

    pending_tournaments = []
    saved_count = 0
    for tournament in tournaments:
        if tournament.get("isMultiweek"):
            pending_tournaments.append(tournament)
            continue
        identity = {
            "source": "ITF",
            "date": get_monday(tournament.get("startDate")),
            "tournamentName": tournament.get("tournamentName"),
            "tournamentId": tournament.get("tournamentKey"),
            "tournamentKey": tournament.get("tournamentKey"),
        }
        if _draw_size_is_saved(identity, saved_size_aliases):
            saved_count += 1
            continue
        pending_tournaments.append(tournament)
    tournaments = pending_tournaments
    if saved_count:
        logger.info(f"  Reusing {saved_count} permanently saved ITF draw size(s).")

    # The cache path may still need IDs for newly discovered tournaments. The
    # full Selenium fallback has already attempted those lookups itself.
    if cached_items is not None:
        missing_ids = [t for t in tournaments if not t["tournamentId"]]
        if missing_ids:
            logger.warning(f"  {len(missing_ids)} tournament(s) missing IDs from cache; fetching via Selenium.")
            _fill_ids_via_selenium(missing_ids)

    # Fetch drawsheets for each tournament
    for t in tournaments:
        t_id = t.get("tournamentId")
        if not t_id:
            logger.debug(f"  Skipping {t['tournamentName']} (no ID)")
            continue

        name = t["tournamentName"]
        cat = get_itf_level(name)

        if t.get("isMultiweek"):
            week = 1
            while True:
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
                identity = {
                    "source": "ITF",
                    "date": date,
                    "tournamentName": week_name,
                    "tournamentId": t["tournamentKey"],
                    "tournamentKey": t["tournamentKey"],
                }
                if _draw_size_is_saved(identity, saved_size_aliases):
                    logger.debug(f"  {week_name}: using permanently saved draw size")
                    week += 1
                    if week > 10:
                        break
                    continue

                m_data = itf_fetch_drawsheet(t_id, "M", week_number=week)
                if not m_data or not m_data.get("koGroups"):
                    break

                main_size = itf_count_draw_size(m_data)
                time.sleep(0.2)
                q_data = itf_fetch_drawsheet(t_id, "Q", week_number=week)
                qual_size = itf_count_draw_size(q_data)
                time.sleep(0.2)

                desc = itf_find_description(cat, main_size, qual_size, itf_descs)

                results.append(
                    {
                        "source": "ITF",
                        "date": date,
                        "tournamentName": week_name,
                        "tournamentId": t["tournamentKey"],
                        "tournamentKey": t["tournamentKey"],
                        "category": cat,
                        "mainDrawSize": main_size,
                        "qualifyingSize": qual_size,
                        "description": desc,
                    }
                )
                logger.debug(f"  {week_name}: {main_size}M, {qual_size}Q -> {desc or 'NO MATCH'}")

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

            results.append(
                {
                    "source": "ITF",
                    "date": date,
                    "tournamentName": name,
                    "tournamentId": t["tournamentKey"],
                    "tournamentKey": t["tournamentKey"],
                    "category": cat,
                    "mainDrawSize": main_size,
                    "qualifyingSize": qual_size,
                    "description": desc,
                }
            )
            logger.debug(f"  {name}: {main_size}M, {qual_size}Q -> {desc or 'NO MATCH'}")

    return results


# ── Main ───────────────────────────────────────────────────────────────────────


def main():
    with open(POINTS_DIST_PATH, encoding="utf-8") as f:
        points_dist = expand_points_distribution(json.load(f))

    desc_set = {entry["Description"] for entry in points_dist if isinstance(entry, dict)}
    itf_descs = itf_parse_descriptions(points_dist)

    today = madrid_today()
    cutoff = (today - timedelta(weeks=55)).strftime("%Y-%m-%d")

    # Load existing data
    existing = load_existing()
    logger.debug(f"Existing entries: {len(existing)}")

    # Prune old entries
    before_prune = len(existing)
    existing = [t for t in existing if (t.get("date") or "") >= cutoff]
    pruned = before_prune - len(existing)
    if pruned:
        logger.info(f"Pruned {pruned} entries older than {cutoff}")
        save_results(existing)

    from_date, to_date = get_draw_lookup_range(today)
    logger.info(f"Fetching tournaments from {from_date} to {to_date}")

    # A published draw size is immutable. Keep its aliases independently from
    # the short-lived response caches so later runs only poll missing events.
    saved_size_aliases = _valid_draw_size_aliases(existing)

    # Fetch new WTA tournaments
    wta_new = fetch_wta_updates(from_date, to_date, desc_set, saved_size_aliases)

    # Fetch new ITF tournaments
    itf_new = fetch_itf_updates(from_date, to_date, itf_descs, saved_size_aliases)

    # Merge only valid sizes. A newly published size replaces an unresolved
    # zero-size row instead of creating another entry for the same tournament.
    added, updated = _merge_draw_size_updates(existing, wta_new + itf_new)

    # Save
    save_results(existing)

    logger.info(f"Added {added} new entries")
    if updated:
        logger.info(f"Resolved {updated} previously missing draw size(s)")
    logger.info(f"Total: {len(existing)} entries saved")


if __name__ == "__main__":
    from pipeline_transaction import run_current_script_transaction, transaction_is_active

    if transaction_is_active():
        main()
    else:
        raise SystemExit(run_current_script_transaction(__file__))
