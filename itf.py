import time
import json
import random
import re
import os
import requests
from html import unescape
from datetime import datetime, timedelta
from time import monotonic

from config import NAME_LOOKUP, ITF_CACHE_FILE, ITF_CALENDAR_CACHE_FILE
from utils import get_cached_rankings
from calendar_builder import get_next_monday

ITF_BASE_URL = "https://www.itftennis.com"
ITF_CALENDAR_PAGE_URL = f"{ITF_BASE_URL}/en/tournament-calendar/womens-world-tennis-tour-calendar/"

# ITF rate limiting / anti-block pacing.
_ITF_MIN_REQUEST_INTERVAL = float(os.getenv("ITF_API_MIN_INTERVAL_SEC", "0.9"))
_ITF_REQUEST_JITTER_MAX = float(os.getenv("ITF_API_REQUEST_JITTER_SEC", "0.35"))
_ITF_BLOCK_BACKOFF_BASE = float(os.getenv("ITF_API_BLOCK_BACKOFF_BASE_SEC", "3.0"))
_ITF_BLOCK_BACKOFF_MAX = float(os.getenv("ITF_API_BLOCK_BACKOFF_MAX_SEC", "15.0"))
_itf_next_request_at = 0.0
_itf_block_streak = 0


def _itf_wait_for_rate_limit():
    global _itf_next_request_at
    now = monotonic()
    if _itf_next_request_at > now:
        time.sleep(_itf_next_request_at - now)

    jitter = random.uniform(0.0, max(0.0, _ITF_REQUEST_JITTER_MAX))
    _itf_next_request_at = monotonic() + max(0.0, _ITF_MIN_REQUEST_INTERVAL + jitter)


def _itf_note_blocked_response():
    global _itf_block_streak, _itf_next_request_at
    _itf_block_streak += 1
    backoff = min(_ITF_BLOCK_BACKOFF_MAX, _ITF_BLOCK_BACKOFF_BASE * (2 ** (_itf_block_streak - 1)))
    _itf_next_request_at = max(_itf_next_request_at, monotonic() + backoff)


def _itf_note_successful_response():
    global _itf_block_streak
    _itf_block_streak = 0


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
                "rank": erank_str, "priority": priority, "type": section_type, "pos_num": pos_num,
                "entry": "JR" if class_code == "JR" else ""
            })

    # Keep MAIN placeholders at the end of occupied MAIN positions so JR/MDA merges don't duplicate slots.
    placeholder_names = {"(Available Slot)", "(Special Exempt)"}
    real_main = [p for p in players if p["type"] == "MAIN" and p["name"] not in placeholder_names]
    main_placeholders = [p for p in players if p["type"] == "MAIN" and p["name"] in placeholder_names]
    if real_main and main_placeholders:
        next_pos = max(p["pos_num"] for p in real_main) + 1
        for p in main_placeholders:
            p["pos_num"] = next_pos
            p["pos"] = str(next_pos)
            next_pos += 1

    players.sort(key=lambda x: (x["pos_num"], x["name"]))
    return players


_itf_calendar_raw = None  # module-level cache for raw ITF calendar items
_itf_session_warmed = False
_itf_event_filters_cache = None
_ITF_EVENT_FILTERS_CACHE_FILE = os.path.join(
    os.path.dirname(ITF_CALENDAR_CACHE_FILE), "itf_event_filters_cache.json"
)


def _load_itf_calendar_disk_cache(target_year=None):
    if not os.path.exists(ITF_CALENDAR_CACHE_FILE):
        return []
    try:
        with open(ITF_CALENDAR_CACHE_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return []

    # Backward compatibility: plain list payload.
    if isinstance(payload, list):
        return payload

    if not isinstance(payload, dict):
        return []

    items = payload.get("items")
    if not isinstance(items, list):
        return []

    cache_year = payload.get("year")
    if target_year and cache_year and str(cache_year) != str(target_year):
        return []

    return items


def _save_itf_calendar_disk_cache(items, year):
    if not isinstance(items, list) or not items:
        return
    payload = {
        "year": int(year),
        "fetchedAt": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(items),
        "items": items,
    }
    try:
        with open(ITF_CALENDAR_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def _load_itf_event_filters_cache():
    global _itf_event_filters_cache
    if isinstance(_itf_event_filters_cache, dict):
        return _itf_event_filters_cache
    if not os.path.exists(_ITF_EVENT_FILTERS_CACHE_FILE):
        _itf_event_filters_cache = {}
        return _itf_event_filters_cache
    try:
        with open(_ITF_EVENT_FILTERS_CACHE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        _itf_event_filters_cache = raw if isinstance(raw, dict) else {}
    except Exception:
        _itf_event_filters_cache = {}
    return _itf_event_filters_cache


def _save_itf_event_filters_cache(cache_obj):
    try:
        with open(_ITF_EVENT_FILTERS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache_obj or {}, f, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        pass


def _collapse_ws(text):
    return " ".join(str(text or "").split())


def _strip_html(html_snippet):
    # Keep plain text extraction lightweight and dependency-free.
    plain = re.sub(r"<[^>]+>", " ", html_snippet or "")
    return _collapse_ws(unescape(plain))


def _extract_first_number(text, allow_decimal=False):
    if not text:
        return ""
    pattern = r"\d+(?:\.\d+)?" if allow_decimal else r"\d+"
    m = re.search(pattern, str(text))
    return m.group(0) if m else ""


def _is_blocked_or_html_response(raw_text):
    raw = (raw_text or "").strip()
    if not raw:
        return True
    low = raw.lower()
    if "incapsula" in low or "request unsuccessful" in low:
        return True
    return low.startswith("<html") or low.startswith("<!doctype html")


def _ensure_itf_session(driver, force_navigation=False):
    global _itf_session_warmed
    if _itf_session_warmed and not force_navigation:
        return
    if not force_navigation:
        # Start lightweight: use browser fetch first and only navigate if retries fail.
        _itf_session_warmed = True
        return
    try:
        driver.get(ITF_CALENDAR_PAGE_URL)
        time.sleep(random.uniform(2.5, 4))
    except Exception as e:
        print(f"Warning warming ITF session: {e}")
    _itf_session_warmed = True


def _fetch_itf_text(driver, url, timeout_ms=12000):
    _itf_wait_for_rate_limit()
    script = """
const url = arguments[0];
const timeoutMs = arguments[1];
const done = arguments[arguments.length - 1];

let sent = false;
const finish = (payload) => {
  if (sent) return;
  sent = true;
  done(payload);
};

const controller = new AbortController();
const timer = setTimeout(() => {
  controller.abort();
  finish({ ok: false, error: "timeout" });
}, timeoutMs);

fetch(url, { credentials: "include", signal: controller.signal, cache: "no-store" })
  .then((resp) => resp.text().then((text) => finish({ ok: true, status: resp.status, text })))
  .catch((err) => finish({ ok: false, error: String(err) }))
  .finally(() => clearTimeout(timer));
"""
    try:
        result = driver.execute_async_script(script, url, int(timeout_ms))
        if isinstance(result, dict) and result.get("ok"):
            return result.get("text", "")
        return ""
    except Exception:
        return ""


def _fetch_itf_json(driver, url, timeout_ms=12000, retries=2):
    _ensure_itf_session(driver)
    for attempt in range(retries):
        raw = _fetch_itf_text(driver, url, timeout_ms=timeout_ms)
        if raw and not _is_blocked_or_html_response(raw):
            try:
                parsed = json.loads(raw)
                _itf_note_successful_response()
                return parsed
            except Exception:
                pass
        if raw and _is_blocked_or_html_response(raw):
            _itf_note_blocked_response()
        if attempt < retries - 1:
            if attempt == 0:
                # Escalate to a full page load only after the first failed fetch.
                _ensure_itf_session(driver, force_navigation=True)
            else:
                time.sleep(random.uniform(1, 2))
    return None


def _fetch_itf_json_via_navigation(driver, url, settle_seconds=1.0):
    """Fallback fetch path: navigate directly to JSON endpoint and parse body text."""
    if driver is None:
        return None
    try:
        _itf_wait_for_rate_limit()
        driver.get(url)
        time.sleep(max(0.0, float(settle_seconds)))
        body = driver.find_element("tag name", "body")
        raw = (body.text or "").strip()
        if not raw or _is_blocked_or_html_response(raw):
            if raw:
                _itf_note_blocked_response()
            return None
        parsed = json.loads(raw)
        _itf_note_successful_response()
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _fetch_itf_json_via_requests(url, timeout=10, retries=2):
    """Fallback fetch path: direct HTTP request outside browser session."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": ITF_CALENDAR_PAGE_URL,
    }
    for attempt in range(retries):
        try:
            _itf_wait_for_rate_limit()
            resp = requests.get(url, headers=headers, timeout=timeout)
            raw = (resp.text or "").strip()
            if not raw or _is_blocked_or_html_response(raw):
                if raw:
                    _itf_note_blocked_response()
                if attempt < retries - 1:
                    time.sleep(random.uniform(0.6, 1.2))
                continue
            try:
                parsed = resp.json()
                _itf_note_successful_response()
                return parsed
            except Exception:
                try:
                    parsed = json.loads(raw)
                    _itf_note_successful_response()
                    return parsed
                except Exception:
                    pass
        except Exception:
            pass
        if attempt < retries - 1:
            time.sleep(random.uniform(0.6, 1.2))
    return None


def _lookup_acceptance_url_from_calendar(tournament_key):
    key_norm = (tournament_key or "").strip().lower()
    if not key_norm:
        return None

    for item in (_itf_calendar_raw or []):
        item_key = (item.get("tournamentKey") or "").strip().lower()
        item_link = (item.get("tournamentLink") or "").strip()
        link_key = item_link.rstrip("/").split("/")[-1].lower() if item_link else ""
        if key_norm not in {item_key, link_key}:
            continue
        base = item_link if item_link.startswith("http") else f"{ITF_BASE_URL}{item_link}"
        return base.rstrip("/") + "/acceptance-list"
    return None


def _parse_acceptance_html_sections(page_html):
    if not page_html:
        return []

    section_matches = re.findall(
        r'<div class="acceptance-lists__details">\s*<h3>(.*?)</h3>.*?<tbody class="acceptance-list__information">(.*?)</tbody>',
        page_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not section_matches:
        return []

    parsed = []
    for heading_html, tbody_html in section_matches:
        heading = _strip_html(heading_html).upper()
        if "MAIN DRAW" in heading:
            class_code = "MDA"
        elif "QUALIFYING" in heading:
            class_code = "Q"
        elif "ALTERNATE" in heading:
            class_code = "A"
        else:
            # Skip withdrawals and unknown sections.
            continue

        entries = []
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", tbody_html, flags=re.IGNORECASE | re.DOTALL)
        for row_html in rows:
            cols = re.findall(r"<td[^>]*>(.*?)</td>", row_html, flags=re.IGNORECASE | re.DOTALL)
            if len(cols) < 2:
                continue

            pos_text = _strip_html(cols[0]) or "-"

            player_text = _strip_html(cols[1])
            if not player_text:
                continue
            country = "-"
            display_name = player_text
            country_match = re.match(r"^([A-Z]{3})\s+(.+)$", player_text)
            if country_match:
                country = country_match.group(1).strip()
                display_name = country_match.group(2).strip()

            wta_rank = _extract_first_number(_strip_html(cols[2]) if len(cols) > 2 else "")
            itf_rank = _extract_first_number(_strip_html(cols[3]) if len(cols) > 3 else "")
            wtn = _extract_first_number(_strip_html(cols[4]) if len(cols) > 4 else "", allow_decimal=True)
            priority = _strip_html(cols[6]) if len(cols) > 6 else ""

            player_node = {
                "givenName": display_name,
                "familyName": "",
                "nationalityCode": country,
                "atpWtaRank": wta_rank,
                "itfBTRank": itf_rank,
                "worldRating": wtn,
            }
            entries.append({
                "positionDisplay": pos_text,
                "priority": priority,
                "players": [player_node],
            })

        if entries:
            parsed.append({
                "entryClassification": heading,
                "entryClassificationCode": class_code,
                "entries": entries,
            })

    return parsed


def _build_name_map(entry_classifications):
    name_map = {}
    for classification in entry_classifications or []:
        desc = classification.get("entryClassification", "").upper()
        code = classification.get("entryClassificationCode", "")
        if "WITHDRAWAL" in desc:
            continue

        for entry in classification.get("entries") or []:
            pos = entry.get("positionDisplay", "")
            suffix = "" if code in ("MDA", "JR", "SE", "WC") else (f" (ALT {pos})" if code in ("ALT", "A") or "ALTERNATE" in desc else " (Q)")
            players = entry.get("players") or []
            for p in players:
                full_name = f"{p.get('givenName', '')} {p.get('familyName', '')}".strip().upper()
                matched_name = NAME_LOOKUP.get(full_name, full_name)
                name_map[matched_name] = suffix
    return name_map


def _fetch_itf_calendar_raw(driver):
    """Fetch all raw ITF calendar items for the full year (single Selenium call, cached)."""
    global _itf_calendar_raw
    if _itf_calendar_raw is not None:
        return _itf_calendar_raw

    today = datetime.now()
    current_year = today.year
    date_from = f"{current_year}-01-01"
    date_to = f"{current_year}-12-31"

    all_items = []
    skip = 0
    take = 250

    while True:
        url = (
            f"{ITF_BASE_URL}/tennis/api/TournamentApi/GetCalendar?"
            f"circuitCode=WT&searchString=&skip={skip}&take={take}"
            f"&dateFrom={date_from}&dateTo={date_to}"
            f"&isOrderAscending=true&orderField=startDate"
        )
        try:
            data = _fetch_itf_json(driver, url, timeout_ms=12000, retries=3)
            if not isinstance(data, dict):
                if skip == 0:
                    print("Error fetching full ITF calendar (skip=0): empty or non-JSON response")
                break
            items = data.get('items', [])
            if not items:
                break
            all_items.extend(items)

            total = data.get('totalItems', 0)
            batch_size = len(items)
            if total and (skip + batch_size >= total):
                break
            if batch_size <= 0:
                break
            skip += batch_size
        except Exception as e:
            print(f"Error fetching full ITF calendar (skip={skip}): {e}")
            break

    if all_items:
        _itf_calendar_raw = all_items
        _save_itf_calendar_disk_cache(all_items, current_year)
        return _itf_calendar_raw

    cached_items = _load_itf_calendar_disk_cache(target_year=current_year)
    if cached_items:
        print(f"Using cached ITF calendar fallback ({len(cached_items)} items).")
        _itf_calendar_raw = cached_items
        return _itf_calendar_raw

    _itf_calendar_raw = all_items
    return _itf_calendar_raw


def get_full_itf_calendar(driver):
    """Get all ITF tournaments for the full year. Numbers duplicates across the whole year."""
    today = datetime.now()

    all_items = _fetch_itf_calendar_raw(driver)

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
    key = (tournament_key or "").strip()
    key_lower = key.lower()
    url = f"{ITF_BASE_URL}/tennis/api/TournamentApi/GetAcceptanceList?tournamentKey={key_lower}&circuitCode=WT"
    try:
        data = _fetch_itf_json(driver, url, timeout_ms=10000, retries=2)
        root_data = []
        if isinstance(data, list) and data:
            root_data = data[0].get("entryClassifications", []) if isinstance(data[0], dict) else []
        elif isinstance(data, dict):
            root_data = data.get("entryClassifications", [])

        # Fallback 1: direct requests path when browser/session is blocked.
        if not root_data:
            req_data = _fetch_itf_json_via_requests(url, timeout=10, retries=2)
            if isinstance(req_data, list) and req_data:
                root_data = req_data[0].get("entryClassifications", []) if isinstance(req_data[0], dict) else []
            elif isinstance(req_data, dict):
                root_data = req_data.get("entryClassifications", [])

        # Fallback: parse rendered acceptance page HTML when API is empty/unavailable.
        if not root_data:
            acceptance_url = _lookup_acceptance_url_from_calendar(key_lower)
            if acceptance_url:
                try:
                    driver.get(acceptance_url)
                    time.sleep(random.uniform(3, 5))
                    root_data = _parse_acceptance_html_sections(driver.page_source)
                except Exception:
                    root_data = []

        name_map = _build_name_map(root_data)
        return root_data, name_map
    except Exception as e:
        print(f"Error en {tournament_key}: {e}")
        return [], {}


def get_dynamic_itf_calendar(driver, num_weeks=3):
    """Get ITF calendar for the next N weeks, filtered from the full-year cache."""
    next_monday = get_next_monday()
    date_from = next_monday.strftime("%Y-%m-%d")
    date_to = (next_monday + timedelta(weeks=num_weeks)).strftime("%Y-%m-%d")

    all_items = _fetch_itf_calendar_raw(driver)

    filtered = []
    for item in all_items:
        start = (item.get('startDate') or '')[:10]
        if start and date_from <= start < date_to:
            filtered.append(item)
    return filtered


def get_draws_itf_tournament_list(driver):
    """Get ITF tournaments for the draws page.

    Show current + next week. Only include last week if the event is multi-week.

    Returns dict: week_label -> {tournamentKey -> {name, level, tournamentId, ...}}
    Requires Selenium driver to fetch tournamentIds via GetEventFilters.
    """
    from calendar_builder import format_week_label

    today = datetime.now()
    current_monday = today - timedelta(days=today.weekday())
    current_monday = current_monday.replace(hour=0, minute=0, second=0, microsecond=0)
    past_monday = current_monday - timedelta(weeks=1)
    two_weeks_later = current_monday + timedelta(weeks=2)

    all_items = _fetch_itf_calendar_raw(driver)

    # Filter to relevant week range
    tournaments = []
    name_counts = {}
    for item in all_items:
        status = (item.get('status') or item.get('tournamentStatus') or '').lower()
        if 'cancel' in status:
            continue
        t_name = item.get('tournamentName', '')
        if 'cancel' in t_name.lower():
            continue
        start_str = (item.get('startDate') or '')[:10]
        if not start_str:
            continue
        try:
            start_date = datetime.strptime(start_str, "%Y-%m-%d")
        except ValueError:
            continue
        monday = start_date - timedelta(days=start_date.weekday())
        is_multiweek = (item.get('category') or '') == "ITF Womens Multi-Week Circuit"
        if monday < current_monday:
            if not (monday == past_monday and is_multiweek):
                continue
        else:
            if not (monday < two_weeks_later):
                continue
        item['_is_multiweek'] = is_multiweek
        tournaments.append(item)
        name_counts[t_name] = name_counts.get(t_name, 0) + 1

    # Number duplicate names
    name_seq = {}
    for item in sorted(tournaments, key=lambda x: x.get('startDate', '')):
        t_name = item.get('tournamentName', '')
        if name_counts[t_name] > 1:
            name_seq[t_name] = name_seq.get(t_name, 0) + 1
            item['_display_name'] = f"{t_name} {name_seq[t_name]}"
        else:
            item['_display_name'] = t_name

    # Fetch tournamentIds
    event_filters_cache = _load_itf_event_filters_cache()
    cache_dirty = False
    for item in tournaments:
        key = item.get('tournamentKey') or ''
        if not key:
            link = item.get('tournamentLink', '')
            key = link.rstrip('/').split('/')[-1] if link else ''
        if not key:
            item['_tid'] = None
            continue
        key = key.lower()
        item['_key'] = key
        cached_tid = event_filters_cache.get(key)
        if isinstance(cached_tid, int) and cached_tid > 0:
            item['_tid'] = cached_tid
            continue
        api_url = f"{ITF_BASE_URL}/tennis/api/TournamentApi/GetEventFilters?tournamentKey={key}"
        try:
            data = _fetch_itf_json(driver, api_url, timeout_ms=9000, retries=2) or {}
            tid = data.get("tournamentId")
            if not (isinstance(tid, int) and tid > 0):
                # Fallback: some ITF sessions block browser fetch() but still return
                # JSON when navigating directly to the endpoint.
                nav_data = _fetch_itf_json_via_navigation(driver, api_url, settle_seconds=random.uniform(0.8, 1.5)) or {}
                nav_tid = nav_data.get("tournamentId")
                if isinstance(nav_tid, int) and nav_tid > 0:
                    tid = nav_tid
            item['_tid'] = tid
            if isinstance(tid, int) and tid > 0:
                event_filters_cache[key] = tid
                cache_dirty = True
        except Exception:
            item['_tid'] = None
        time.sleep(random.uniform(0.7, 1.3))

    if cache_dirty:
        _save_itf_event_filters_cache(event_filters_cache)

    # Build result grouped by week
    result = {}
    for item in tournaments:
        tid = item.get('_tid')
        key = item.get('_key', '')
        is_multiweek = bool(item.get('_is_multiweek'))
        start_str = (item.get('startDate') or '')[:10]
        start_date = datetime.strptime(start_str, "%Y-%m-%d")
        monday = start_date - timedelta(days=start_date.weekday())
        week_label = format_week_label(monday)
        level = get_itf_level(item.get('tournamentName', ''))
        if week_label not in result:
            result[week_label] = {}
        result[week_label][key] = {
            "name": item['_display_name'],
            "level": level,
            "tournamentId": tid,
            "startDate": item.get('startDate'),
            "endDate": item.get('endDate'),
            "is_multiweek": is_multiweek,
        }

    return result


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
