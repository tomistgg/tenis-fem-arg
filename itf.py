import json
import os
import random
import re
import time
from datetime import datetime, timedelta
from html import unescape
from time import monotonic
from typing import Any

import requests
from selenium.common.exceptions import InvalidSessionIdException, WebDriverException
from urllib3.exceptions import HTTPError as Urllib3HTTPError

from calendar_builder import get_next_monday
from config import (
    ITF_CACHE_FILE,
    ITF_CALENDAR_CACHE_FILE,
    NAME_LOOKUP,
    PLAYER_IDENTITY_INDEX,
    resolve_player_display_name,
)
from pipeline_errors import DataValidationError, SourceRequestError
from run_state import report_run_issue
from runtime_logging import get_logger
from time_utils import madrid_today, parse_utc_timestamp, utc_now
from utils import (
    compress_itf_rankings_cache,
    dumps_itf_calendar_cache,
    expand_itf_calendar_cache,
    expand_itf_rankings_cache,
    get_cache_timestamp,
    make_data_status,
    save_json_file,
    set_cache_file_meta,
    utc_now_iso,
)

logger = get_logger("itf")

ITF_BASE_URL = "https://www.itftennis.com"
ITF_CALENDAR_PAGE_URL = f"{ITF_BASE_URL}/en/tournament-calendar/womens-world-tennis-tour-calendar/"

# ITF rate limiting / anti-block pacing.
# Defaults are deliberately slow — Incapsula rate-limits aggressively on
# datacenter IPs (GHA runners). Tunable via env vars for local overrides.
_ITF_MIN_REQUEST_INTERVAL = float(os.getenv("ITF_API_MIN_INTERVAL_SEC", "10.0"))
_ITF_CALENDAR_CACHE_TTL = 3 * 60 * 60  # 3 hours
_ITF_REQUEST_JITTER_MAX = float(os.getenv("ITF_API_REQUEST_JITTER_SEC", "10.0"))
_ITF_BLOCK_BACKOFF_BASE = float(os.getenv("ITF_API_BLOCK_BACKOFF_BASE_SEC", "15.0"))
_ITF_BLOCK_BACKOFF_MAX = float(os.getenv("ITF_API_BLOCK_BACKOFF_MAX_SEC", "60.0"))
_itf_next_request_at = 0.0
_itf_block_streak = 0


class ItfApiFetchError(RuntimeError):
    pass


class ItfApiPartialData(RuntimeError):
    pass


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
    if "W100" in t or "100k" in t:
        return "W100"
    if "W75" in t or "75k" in t:
        return "W75"
    if "W60" in t or "60k" in t:
        return "W60"
    if "W50" in t or "50k" in t:
        return "W50"
    if "W35" in t or "35k" in t:
        return "W35"
    if "W25" in t or "25k" in t:
        return "W25"
    return "W15"


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


def _number_duplicate_itf_names(items, *, source_field, target_field, sort_key=None):
    """Assign stable numeric suffixes to duplicate ITF tournament names.

    The helper is shared by the full-calendar and draw-list flows so both use
    the same numbering rule.
    """
    if not isinstance(items, list):
        return items

    if sort_key is None:

        def sort_key(item):
            return item.get("startDate", "") if isinstance(item, dict) else ""

    name_counts: dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get(source_field) or "")
        name_counts[name] = name_counts.get(name, 0) + 1

    name_seq: dict[str, int] = {}
    for item in sorted(items, key=sort_key):
        if not isinstance(item, dict):
            continue
        name = str(item.get(source_field) or "")
        if name_counts.get(name, 0) > 1:
            name_seq[name] = name_seq.get(name, 0) + 1
            item[target_field] = f"{name} {name_seq[name]}"
        else:
            item[target_field] = name

    return items


def parse_itf_entry_list(itf_entries):
    """Parse raw ITF acceptance list classifications into a sorted player list."""
    players = []
    main_entry_codes = {"MDA", "DA", "CA", "JR", "JA", "JE", "SE", "WC"}
    direct_acceptance_codes = {"MDA", "DA"}
    for classification in itf_entries:
        class_code = classification.get("entryClassificationCode", "")
        # ITF may use CA/MDA for main acceptance and JR/JA/JE for junior entries.
        if class_code in main_entry_codes:
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
                pos_digits = "".join(filter(str.isdigit, str(pos)))
                pos_num = int(pos_digits) if pos_digits else 999
            except (TypeError, ValueError):
                pos_num = 999

            priority = str(entry.get("priority") or "").strip()

            if not entry_players:
                if entry.get("isAvailableSlot"):
                    display_name = "(Available Slot)"
                elif entry.get("isExemption"):
                    display_name = "(Special Exempt)"
                else:
                    continue
                players.append(
                    {
                        "pos": pos,
                        "name": display_name,
                        "country": "-",
                        "rank": "-",
                        "priority": priority,
                        "type": section_type,
                        "pos_num": pos_num,
                        "entry": "",
                    }
                )
                continue

            p_node = entry_players[0]
            raw_f_name = f"{p_node.get('givenName', '')} {p_node.get('familyName', '')}".strip()
            player_id = str(
                p_node.get("playerId") or p_node.get("playerID") or p_node.get("id") or p_node.get("player_id") or ""
            ).strip()
            display_name = resolve_player_display_name("itf", player_id=player_id, name=raw_f_name)

            wta = p_node.get("atpWtaRank", "")
            itf_rank = p_node.get("itfBTRank")
            wtn = p_node.get("worldRating", "")

            if section_type == "MAIN" and class_code not in direct_acceptance_codes:
                wta_rank = str(wta).strip() if wta is not None else ""
                erank_str = f"{class_code} ({wta_rank or '-'})"
            else:
                erank_str = "-"
                if wta and str(wta).strip() != "":
                    erank_str = f"{wta}"
                elif itf_rank is not None and str(itf_rank).strip() != "":
                    erank_str = f"ITF {itf_rank}"
                elif wtn and str(wtn).strip() != "":
                    erank_str = f"WTN {wtn}"

            country = str(p_node.get("nationalityCode") or "").strip().upper()
            if not country or country == "-":
                identity = PLAYER_IDENTITY_INDEX.resolve("itf", player_id=player_id, name=raw_f_name)
                if identity and identity.country:
                    country = identity.country

            players.append(
                {
                    "pos": pos,
                    "name": display_name,
                    "country": country or "-",
                    "rank": erank_str,
                    "priority": priority,
                    "type": section_type,
                    "pos_num": pos_num,
                    "entry": class_code if section_type == "MAIN" and class_code not in direct_acceptance_codes else "",
                    "player_id": player_id,
                }
            )

    # Special classifications may reuse the positions assigned to placeholders
    # by MDA. Keep every reserved slot and move placeholders after real players.
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


_itf_calendar_raw: list[dict[str, Any]] | None = None
_itf_session_warmed = False
_itf_browser_unavailable = False
_itf_event_filters_cache: dict[str, Any] | None = None
_ITF_EVENT_FILTERS_CACHE_FILE = os.path.join(os.path.dirname(ITF_CALENDAR_CACHE_FILE), "itf_event_filters_cache.json")


def _load_itf_calendar_disk_cache(target_year=None, max_age_seconds=None):
    if not os.path.exists(ITF_CALENDAR_CACHE_FILE):
        return []
    try:
        with open(ITF_CALENDAR_CACHE_FILE, encoding="utf-8") as f:
            payload = expand_itf_calendar_cache(json.load(f))
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
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

    if max_age_seconds is not None:
        fetched_at_str = get_cache_timestamp(ITF_CALENDAR_CACHE_FILE, payload=payload)
        if not fetched_at_str:
            return []
        try:
            fetched_at = parse_utc_timestamp(fetched_at_str)
            age = (utc_now() - fetched_at).total_seconds()
            if age > max_age_seconds:
                return []
        except (TypeError, ValueError):
            return []

    return items


def _save_itf_calendar_disk_cache(items, year):
    if not isinstance(items, list) or not items:
        return
    payload = {
        "year": int(year),
        "count": len(items),
        "items": items,
    }
    save_json_file(ITF_CALENDAR_CACHE_FILE, payload, formatter=dumps_itf_calendar_cache)
    set_cache_file_meta(
        ITF_CALENDAR_CACHE_FILE,
        year=int(year),
        count=len(items),
        fetchedAt=utc_now_iso(),
    )


def _load_itf_event_filters_cache():
    global _itf_event_filters_cache
    if isinstance(_itf_event_filters_cache, dict):
        return _itf_event_filters_cache
    if not os.path.exists(_ITF_EVENT_FILTERS_CACHE_FILE):
        _itf_event_filters_cache = {}
        return _itf_event_filters_cache
    try:
        with open(_ITF_EVENT_FILTERS_CACHE_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        _itf_event_filters_cache = raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        raise DataValidationError(
            component="itf",
            operation="load event-filter cache",
            message=f"cannot read existing ITF event-filter cache: {_ITF_EVENT_FILTERS_CACHE_FILE}",
            context={"path": _ITF_EVENT_FILTERS_CACHE_FILE, "cause": str(exc)},
        ) from exc
    return _itf_event_filters_cache


def _save_itf_event_filters_cache(cache_obj):
    save_json_file(_ITF_EVENT_FILTERS_CACHE_FILE, cache_obj or {})


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


def _is_invalid_browser_session(exc):
    return isinstance(exc, InvalidSessionIdException) or "invalid session id" in str(exc).lower()


def _itf_note_browser_unavailable(exc):
    """Disable Selenium retries after Chrome has invalidated the session."""
    global _itf_browser_unavailable
    if not _itf_browser_unavailable:
        message = str(exc).splitlines()[0] or type(exc).__name__
        logger.warning(f"Warning: ITF browser session unavailable; using cache/HTTP fallback: {message}")
    _itf_browser_unavailable = True


def _ensure_itf_session(driver, force_navigation=False):
    global _itf_session_warmed
    if driver is None or _itf_browser_unavailable:
        return
    if _itf_session_warmed and not force_navigation:
        return
    try:
        driver.get(ITF_CALENDAR_PAGE_URL)
        settle_min, settle_max = (2.5, 4.0) if force_navigation else (1.8, 3.0)
        time.sleep(random.uniform(settle_min, settle_max))
    except (WebDriverException, Urllib3HTTPError) as e:
        if _is_invalid_browser_session(e):
            _itf_note_browser_unavailable(e)
        else:
            logger.warning(f"Warning warming ITF session: {e}")
    _itf_session_warmed = True


def _fetch_itf_text(driver, url, timeout_ms=12000):
    if driver is None or _itf_browser_unavailable:
        return ""
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
    except (WebDriverException, Urllib3HTTPError) as exc:
        if _is_invalid_browser_session(exc):
            _itf_note_browser_unavailable(exc)
        return ""


def _fetch_itf_json(driver, url, timeout_ms=12000, retries=2):
    is_calendar_endpoint = "TournamentApi/GetCalendar" in str(url)
    _ensure_itf_session(driver, force_navigation=not _itf_session_warmed and is_calendar_endpoint)

    blocked_seen = False
    for attempt in range(retries):
        raw = _fetch_itf_text(driver, url, timeout_ms=timeout_ms)
        if raw and not _is_blocked_or_html_response(raw):
            try:
                parsed = json.loads(raw)
                _itf_note_successful_response()
                return parsed
            except json.JSONDecodeError:
                # Fall through to the bounded browser-navigation and HTTP fallbacks.
                raw = ""
        if raw and _is_blocked_or_html_response(raw):
            _itf_note_blocked_response()
            blocked_seen = True
        if attempt < retries - 1:
            _ensure_itf_session(driver, force_navigation=True)
            time.sleep(random.uniform(0.8, 1.4))

    nav_data = _fetch_itf_json_via_navigation(
        driver,
        url,
        settle_seconds=random.uniform(1.0, 1.8) if is_calendar_endpoint else random.uniform(0.8, 1.5),
    )
    if isinstance(nav_data, dict):
        return nav_data

    if blocked_seen:
        _ensure_itf_session(driver, force_navigation=True)
        nav_data = _fetch_itf_json_via_navigation(
            driver,
            url,
            settle_seconds=random.uniform(1.4, 2.2),
        )
        if isinstance(nav_data, dict):
            return nav_data

    browser_cookies = None
    try:
        if driver is not None and not _itf_browser_unavailable:
            browser_cookies = {
                item.get("name"): item.get("value")
                for item in driver.get_cookies()
                if item.get("name") and item.get("value") is not None
            }
    except (WebDriverException, Urllib3HTTPError):
        browser_cookies = None

    req_data = _fetch_itf_json_via_requests(
        url,
        timeout=max(8, int(timeout_ms / 1000)),
        retries=1,
        cookies=browser_cookies,
    )
    return req_data if isinstance(req_data, dict) else None


def _fetch_itf_json_via_navigation(driver, url, settle_seconds=1.0):
    """Fallback fetch path: navigate directly to JSON endpoint and parse body text."""
    if driver is None or _itf_browser_unavailable:
        return None
    try:
        _itf_wait_for_rate_limit()
        driver.get(url)
        time.sleep(max(0.0, float(settle_seconds)))
        raw = ""
        try:
            body = driver.find_element("tag name", "body")
            raw = (body.text or "").strip()
            if not raw:
                raw = (body.get_attribute("innerText") or "").strip()
        except WebDriverException:
            raw = ""

        if not raw:
            page_source = (driver.page_source or "").strip()
            if page_source and _is_blocked_or_html_response(page_source):
                _itf_note_blocked_response()
            return None

        if _is_blocked_or_html_response(raw):
            _itf_note_blocked_response()
            return None
        parsed = json.loads(raw)
        _itf_note_successful_response()
        return parsed if isinstance(parsed, dict) else None
    except (WebDriverException, Urllib3HTTPError, json.JSONDecodeError, AttributeError, TypeError, ValueError) as exc:
        if _is_invalid_browser_session(exc):
            _itf_note_browser_unavailable(exc)
        else:
            logger.debug(f"ITF navigation fallback failed for {url}: {exc}")
        return None


def _fetch_itf_json_via_requests(url, timeout=10, retries=2, cookies=None):
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
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            _itf_wait_for_rate_limit()
            resp = requests.get(url, headers=headers, timeout=timeout, cookies=cookies)
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
            except (requests.JSONDecodeError, json.JSONDecodeError, ValueError) as exc:
                last_error = exc
                try:
                    parsed = json.loads(raw)
                    _itf_note_successful_response()
                    return parsed
                except (json.JSONDecodeError, TypeError) as exc:
                    last_error = exc
        except (requests.RequestException, OSError) as exc:
            last_error = exc
        if attempt < retries - 1:
            time.sleep(random.uniform(0.6, 1.2))
    error = SourceRequestError(
        component="itf",
        operation="fetch JSON via requests",
        message=f"ITF request failed after {retries} attempts",
        context={"url": str(url), "attempts": retries, "cause": str(last_error or "empty or blocked response")},
        retryable=True,
    )
    report_run_issue("itf", "fetch JSON via requests", error, severity="partial")
    return None


def _lookup_acceptance_url_from_calendar(tournament_key):
    key_norm = (tournament_key or "").strip().lower()
    if not key_norm:
        return None

    for item in _itf_calendar_raw or []:
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
        r'<div class="acceptance-lists__details">\s*<h3>(.*?)</h3>.*?'
        r'<tbody class="acceptance-list__information">(.*?)</tbody>',
        page_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not section_matches:
        return []

    parsed = []
    for heading_html, tbody_html in section_matches:
        heading = _strip_html(heading_html).upper()
        # ITF sometimes renders the CA/current-acceptance bucket with an empty heading.
        if not heading:
            class_code = "CA"
        elif "MAIN DRAW" in heading:
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

            player_id_match = re.search(r"(?:playerId|player-id|players?/)(\d+)", row_html, flags=re.IGNORECASE)
            player_id = player_id_match.group(1) if player_id_match else ""
            player_node = {
                "givenName": display_name,
                "familyName": "",
                "nationalityCode": country,
                "atpWtaRank": wta_rank,
                "itfBTRank": itf_rank,
                "worldRating": wtn,
                "playerId": player_id,
            }
            entries.append(
                {
                    "positionDisplay": pos_text,
                    "priority": priority,
                    "players": [player_node],
                }
            )

        if entries:
            parsed.append(
                {
                    "entryClassification": heading,
                    "entryClassificationCode": class_code,
                    "entries": entries,
                }
            )

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
            suffix = (
                ""
                if code in ("MDA", "CA", "JR", "JA", "SE", "WC")
                else (f" (ALT {pos})" if code in ("ALT", "A") or "ALTERNATE" in desc else " (Q)")
            )
            players = entry.get("players") or []
            for p in players:
                full_name = f"{p.get('givenName', '')} {p.get('familyName', '')}".strip()
                player_id = str(
                    p.get("playerId") or p.get("playerID") or p.get("id") or p.get("player_id") or ""
                ).strip()
                matched_name = resolve_player_display_name("itf", player_id=player_id, name=full_name).upper()
                name_map[matched_name] = suffix
    return name_map


def _itf_calendar_item_identity(item):
    """Return a stable identity for one raw ITF calendar row."""
    if not isinstance(item, dict):
        return ""

    tournament_key = (item.get("tournamentKey") or "").strip().lower()
    if tournament_key:
        return f"key:{tournament_key}"

    tournament_link = (item.get("tournamentLink") or "").strip().lower().rstrip("/")
    if tournament_link:
        return f"link:{tournament_link}"

    start_date = str(item.get("startDate") or "")[:10]
    name = _collapse_ws(item.get("tournamentName") or item.get("name") or "").lower()
    country = _collapse_ws(
        item.get("hostNationCode") or item.get("countryCode") or item.get("hostNation") or ""
    ).lower()
    if start_date or name or country:
        return f"fallback:{start_date}|{name}|{country}"
    return ""


def _merge_itf_calendar_items(*collections):
    """Merge raw ITF calendar rows, letting later collections override earlier ones.

    Cancelled rows act like tombstones so they can remove stale active cache
    entries for the same tournament key.
    """
    merged: dict[str, dict[str, Any]] = {}
    cancelled_identities = set()
    for collection in collections:
        for item in collection or []:
            if not isinstance(item, dict):
                continue
            identity = _itf_calendar_item_identity(item)
            if not identity:
                continue
            if _is_cancelled_itf_calendar_item(item):
                cancelled_identities.add(identity)
                merged.pop(identity, None)
                continue
            if identity in cancelled_identities:
                continue
            merged[identity] = item
    return sorted(
        merged.values(),
        key=lambda item: (
            str(item.get("startDate") or ""),
            _collapse_ws(item.get("tournamentName") or item.get("name") or "").lower(),
        ),
    )


def _fetch_itf_calendar_range(driver, date_from, date_to, *, take=250, ascending=True, max_pages=None):
    """Fetch one ITF calendar range with pagination and browser-backed JSON fetches."""
    all_items = []
    expected_total = 0
    skip = 0
    pages_fetched = 0
    order_ascending = "true" if ascending else "false"

    while True:
        if max_pages is not None and pages_fetched >= max_pages:
            break

        url = (
            f"{ITF_BASE_URL}/tennis/api/TournamentApi/GetCalendar?"
            f"circuitCode=WT&searchString=&skip={skip}&take={take}"
            f"&dateFrom={date_from}&dateTo={date_to}"
            f"&isOrderAscending={order_ascending}&orderField=startDate"
        )
        try:
            data = _fetch_itf_json(driver, url, timeout_ms=12000, retries=3)
            if not isinstance(data, dict):
                break

            items = data.get("items", [])
            if not items:
                break

            all_items.extend(items)
            pages_fetched += 1

            total = data.get("totalItems", 0)
            if skip == 0 and total:
                expected_total = total

            batch_size = len(items)
            if total and (skip + batch_size >= total):
                break
            if batch_size <= 0:
                break

            skip += batch_size
            if not total and batch_size < take:
                break
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            report_run_issue(
                "itf",
                "parse calendar page",
                exc,
                severity="partial",
                context={"skip": skip, "take": take},
            )
            break

    fetch_complete = bool(all_items) and (not expected_total or len(all_items) >= expected_total)
    return all_items, expected_total, fetch_complete


def _fetch_itf_calendar_raw(driver):
    """Fetch all raw ITF calendar items for the full year (single Selenium call, cached)."""
    global _itf_calendar_raw
    if _itf_calendar_raw is not None:
        return _itf_calendar_raw

    today = madrid_today()
    current_year = today.year
    date_from = f"{current_year}-01-01"
    date_to = f"{current_year}-12-31"
    cached_items = _load_itf_calendar_disk_cache(target_year=current_year)

    # Use the disk cache when it was written within the last 3 hours — itf_load_new.py
    # runs before main.py and populates this cache, so the live paginated fetch is
    # almost always redundant on the same cron run.
    fresh_items = _load_itf_calendar_disk_cache(target_year=current_year, max_age_seconds=_ITF_CALENDAR_CACHE_TTL)
    if fresh_items:
        logger.debug(f"  Using fresh ITF calendar disk cache ({len(fresh_items)} items).")
        live_items, _, _ = _fetch_itf_calendar_range(
            driver,
            today.strftime("%Y-%m-%d"),
            date_to,
            take=250,
            ascending=True,
            max_pages=2,
        )
        if live_items:
            merged_items = _merge_itf_calendar_items(fresh_items, live_items)
            if merged_items:
                _itf_calendar_raw = merged_items
                _save_itf_calendar_disk_cache(merged_items, current_year)
                return _itf_calendar_raw
        _itf_calendar_raw = fresh_items
        return _itf_calendar_raw

    all_items, expected_total, fetch_complete = _fetch_itf_calendar_range(
        driver,
        date_from,
        date_to,
        take=250,
        ascending=True,
    )
    if fetch_complete:
        _itf_calendar_raw = all_items
        _save_itf_calendar_disk_cache(all_items, current_year)
        return _itf_calendar_raw

    # Partial or empty fetch — prefer disk cache to avoid overwriting complete data
    if all_items and not expected_total:
        # totalItems not reported by API; treat as complete
        _itf_calendar_raw = all_items
        _save_itf_calendar_disk_cache(all_items, current_year)
        return _itf_calendar_raw

    if all_items:
        logger.warning(
            f"Partial ITF calendar fetch ({len(all_items)}/{expected_total}) — using disk cache to avoid false-positive new-tournament alerts."  # noqa: E501
        )

    # When the year-wide paginated fetch is blocked or partial, supplement it
    # with a reverse-ordered future-only fetch. This makes late-season
    # additions more likely to land even when the full crawl cannot finish.
    future_from = today.strftime("%Y-%m-%d")
    future_items, future_total, future_complete = _fetch_itf_calendar_range(
        driver,
        future_from,
        date_to,
        take=250,
        ascending=False,
        max_pages=4,
    )
    if future_items and not future_complete and future_total:
        logger.warning(
            f"Partial ITF future supplement ({len(future_items)}/{future_total}) â€” merging with cache where possible."
        )

    merged_items = _merge_itf_calendar_items(cached_items, all_items, future_items)
    if merged_items:
        if cached_items:
            logger.warning(f"Using merged ITF calendar fallback ({len(merged_items)} items).")
        _itf_calendar_raw = merged_items
        _save_itf_calendar_disk_cache(merged_items, current_year)
        return _itf_calendar_raw

    _itf_calendar_raw = all_items
    return _itf_calendar_raw


def get_full_itf_calendar(driver):
    """Get all ITF tournaments for the full year. Numbers duplicates across the whole year."""
    today = madrid_today()

    all_items = _fetch_itf_calendar_raw(driver)

    tournaments = []
    for item in all_items:
        if _is_cancelled_itf_calendar_item(item):
            continue
        t_name = item.get("tournamentName", "")

        level = get_itf_level(t_name)

        surface = item.get("surfaceDesc") or item.get("surface") or ""
        country = item.get("hostNationCode") or item.get("hostNation") or item.get("countryCode") or ""
        tournament_key = (item.get("tournamentKey") or item.get("tournamentLink") or "").strip().lower()
        tournaments.append(
            {
                "name": t_name,
                "level": level,
                "surface": surface,
                "country": country,
                "startDate": item.get("startDate"),
                "endDate": item.get("endDate", None),
                "source": "ITF",
                "tournamentKey": tournament_key,
                "calendarKey": f"itf:{tournament_key}" if tournament_key else "",
            }
        )

    _number_duplicate_itf_names(tournaments, source_field="name", target_field="name")

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
        root_data: list[Any] = []
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
                except (WebDriverException, Urllib3HTTPError, AttributeError, TypeError, ValueError) as exc:
                    report_run_issue(
                        "itf",
                        "parse acceptance page fallback",
                        exc,
                        severity="partial",
                        context={"tournament_key": str(tournament_key)},
                    )
                    root_data = []

        name_map = _build_name_map(root_data)
        return root_data, name_map
    except (WebDriverException, Urllib3HTTPError, AttributeError, KeyError, TypeError, ValueError) as e:
        logger.error(f"Error en {tournament_key}: {e}")
        return [], {}


def get_dynamic_itf_calendar(driver, num_weeks=3):
    """Get ITF calendar for the next N weeks, filtered from the full-year cache.

    Also includes current-week tournaments when today is Monday.
    """
    today = madrid_today()
    next_monday = get_next_monday()
    date_from = next_monday.strftime("%Y-%m-%d")
    date_to = (next_monday + timedelta(weeks=num_weeks)).strftime("%Y-%m-%d")
    current_monday_str = (today - timedelta(days=today.weekday())).strftime("%Y-%m-%d")
    is_monday = today.weekday() == 0

    all_items = _fetch_itf_calendar_raw(driver)

    filtered = []
    for item in all_items:
        if _is_cancelled_itf_calendar_item(item):
            continue
        start = (item.get("startDate") or "")[:10]
        if not start:
            continue
        if date_from <= start < date_to:
            filtered.append(item)
            continue
        # Include current-week tournaments only on Monday.
        if current_monday_str <= start < date_from and is_monday:
            filtered.append(item)
    return filtered


def get_draws_itf_tournament_list(driver):
    """Get ITF tournaments for the draws page.

    Show current + next week. Only include last week if the event is multi-week.

    Returns dict: week_label -> {tournamentKey -> {name, level, tournamentId, ...}}
    Requires Selenium driver to fetch tournamentIds via GetEventFilters.
    """
    from calendar_builder import format_week_label

    today = madrid_today()
    current_monday = today - timedelta(days=today.weekday())
    past_monday = current_monday - timedelta(weeks=1)
    two_weeks_later = current_monday + timedelta(weeks=2)

    all_items = _fetch_itf_calendar_raw(driver)

    # Filter to relevant week range
    tournaments = []
    for item in all_items:
        if _is_cancelled_itf_calendar_item(item):
            continue
        start_str = (item.get("startDate") or "")[:10]
        if not start_str:
            continue
        try:
            start_date = datetime.strptime(start_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        monday = start_date - timedelta(days=start_date.weekday())
        is_multiweek = (item.get("category") or "") == "ITF Womens Multi-Week Circuit"
        # Weekday runs only need current-week ITF draw IDs. Next-week draw IDs
        # are deferred until Saturday/Sunday of the week before the event.
        if monday > current_monday and today.weekday() < 5:
            continue
        if monday < current_monday:
            if not (monday == past_monday and is_multiweek):
                continue
        else:
            if not (monday < two_weeks_later):
                continue
        item["_is_multiweek"] = is_multiweek
        tournaments.append(item)

    _number_duplicate_itf_names(
        tournaments,
        source_field="tournamentName",
        target_field="_display_name",
    )

    # Fetch tournamentIds
    event_filters_cache = _load_itf_event_filters_cache()
    cache_dirty = False
    for item in tournaments:
        key = item.get("tournamentKey") or ""
        if not key:
            link = item.get("tournamentLink", "")
            key = link.rstrip("/").split("/")[-1] if link else ""
        if not key:
            item["_tid"] = None
            continue
        key = key.lower()
        item["_key"] = key
        cached_tid = event_filters_cache.get(key)
        if isinstance(cached_tid, int) and cached_tid > 0:
            item["_tid"] = cached_tid
            continue
        api_url = f"{ITF_BASE_URL}/tennis/api/TournamentApi/GetEventFilters?tournamentKey={key}"
        try:
            data = _fetch_itf_json(driver, api_url, timeout_ms=9000, retries=2) or {}
            tid = data.get("tournamentId")
            if not (isinstance(tid, int) and tid > 0):
                # Fallback: some ITF sessions block browser fetch() but still return
                # JSON when navigating directly to the endpoint.
                nav_data = (
                    _fetch_itf_json_via_navigation(driver, api_url, settle_seconds=random.uniform(0.8, 1.5)) or {}
                )
                nav_tid = nav_data.get("tournamentId")
                if isinstance(nav_tid, int) and nav_tid > 0:
                    tid = nav_tid
            item["_tid"] = tid
            if isinstance(tid, int) and tid > 0:
                event_filters_cache[key] = tid
                cache_dirty = True
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            report_run_issue(
                "itf",
                "parse tournament event filters",
                exc,
                severity="partial",
                context={"tournament_key": str(key)},
            )
            item["_tid"] = None
        time.sleep(random.uniform(0.7, 1.3))

    if cache_dirty:
        _save_itf_event_filters_cache(event_filters_cache)

    # Build result grouped by week
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for item in tournaments:
        tid = item.get("_tid")
        key = item.get("_key", "")
        is_multiweek = bool(item.get("_is_multiweek"))
        start_str = (item.get("startDate") or "")[:10]
        start_date = datetime.strptime(start_str, "%Y-%m-%d").date()
        monday = start_date - timedelta(days=start_date.weekday())
        week_label = format_week_label(monday)
        level = get_itf_level(item.get("tournamentName", ""))
        if week_label not in result:
            result[week_label] = {}
        result[week_label][key] = {
            "name": item["_display_name"],
            "level": level,
            "tournamentId": tid,
            "startDate": item.get("startDate"),
            "endDate": item.get("endDate"),
            "is_multiweek": is_multiweek,
            "source": "ITF",
        }

    return result


def get_itf_rankings(nationality="ARG"):
    all_players = []
    skip = 0
    take = 50
    expected_total = None

    while True:
        url = "https://www.itftennis.com/tennis/api/PlayerRankApi/GetPlayerRankings"

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.itftennis.com/en/rankings/",
            "Sec-Ch-Ua": '"Not(A:Brand";v="8", "Chromium";v="144", "Google Chrome";v="144"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

        params = {
            "circuitCode": "WT",
            "matchTypeCode": "S",
            "ageCategoryCode": "",
            "nationCode": nationality,
            "take": take,
            "skip": skip,
            "isOrderAscending": "true",
        }

        try:
            r = requests.get(url, headers=headers, params=params, timeout=10)
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, dict):
                raise ItfApiFetchError("ITF rankings returned an unexpected payload")
            items = data.get("items", [])
            if items is None:
                raise ItfApiFetchError("ITF rankings payload was missing items")
            if not isinstance(items, list):
                raise ItfApiFetchError("ITF rankings items were not a list")
            if not items:
                break
            all_players.extend(items)

            try:
                total_items = int(data.get("totalItems", 0) or 0)
            except (TypeError, ValueError):
                total_items = 0
            if total_items:
                expected_total = total_items
            if skip + take >= total_items:
                break

            skip += take
            time.sleep(0.1)
        except (ItfApiFetchError, ItfApiPartialData):
            raise
        except Exception as e:
            if all_players:
                raise ItfApiPartialData(
                    f"ITF rankings fetch interrupted after {len(all_players)} players for {nationality}: {e}"
                ) from e
            raise ItfApiFetchError(f"ITF rankings fetch failed for {nationality}: {e}") from e

    if expected_total is not None and len(all_players) < expected_total:
        raise ItfApiPartialData(
            f"ITF rankings fetch ended early after {len(all_players)}/{expected_total} players for {nationality}"
        )

    ranking_results = []
    for p in all_players:
        if not p.get("playerId"):
            continue
        itf_name = f"{p.get('playerGivenName', '')} {p.get('playerFamilyName', '')}".strip().upper()
        display_name = NAME_LOOKUP.get(itf_name, itf_name)
        ranking_results.append(
            {
                "Player": display_name,
                "Rank": f"ITF {p.get('rank')}",
                "Country": p.get("playerNationalityCode", ""),
                "Key": display_name,
            }
        )
    return ranking_results


def _load_itf_rankings_cache(*, strict=False):
    if not os.path.exists(ITF_CACHE_FILE):
        return {}
    try:
        with open(ITF_CACHE_FILE, encoding="utf-8") as f:
            payload = expand_itf_rankings_cache(json.load(f))
        return payload if isinstance(payload, dict) else {}
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        if strict:
            raise
        logger.warning(f"Warning: ignoring unreadable ITF rankings cache {ITF_CACHE_FILE}: {e}")
        return {}


def _save_itf_rankings_cache(cache_obj):
    try:
        save_json_file(ITF_CACHE_FILE, compress_itf_rankings_cache(cache_obj or {}))
    except Exception as e:
        logger.warning(f"Warning: could not save ITF rankings cache {ITF_CACHE_FILE}: {e}")


def get_itf_rankings_cached(date_str, nationality="ARG", *, with_status=False):
    """Get ITF rankings with caching and optional freshness metadata."""

    def _finish(players, status):
        return (players, status) if with_status else players

    cache = _load_itf_rankings_cache()
    if date_str in cache:
        players = cache[date_str]
        return _finish(
            players,
            make_data_status(
                "ITF rankings",
                "fresh",
                requested=date_str,
                effective=date_str,
                row_count=len(players),
                reason="Exact cached rankings date available.",
            ),
        )

    new_data = []
    fetch_error = None
    try:
        new_data = get_itf_rankings(nationality=nationality)
    except (ItfApiFetchError, ItfApiPartialData) as e:
        fetch_error = e
        logger.warning(f"Warning: ITF rankings refresh failed for {date_str}: {e}")
    if new_data:
        cache[date_str] = new_data
        _save_itf_rankings_cache(cache)
        return _finish(
            new_data,
            make_data_status(
                "ITF rankings",
                "fresh",
                requested=date_str,
                effective=date_str,
                fetched_at=utc_now_iso(),
                row_count=len(new_data),
                reason="Live rankings refreshed successfully.",
            ),
        )

    if cache:
        latest_key = sorted(cache.keys())[-1]
        players = cache.get(latest_key, [])
        reason = (
            "Live rankings refresh failed; showing latest cached rankings."
            if fetch_error
            else "No live rankings were returned for the requested date; showing latest cached rankings."
        )
        return _finish(
            players,
            make_data_status(
                "ITF rankings",
                "stale",
                requested=date_str,
                effective=latest_key,
                row_count=len(players),
                stale=True,
                reason=reason,
            ),
        )

    reason = (
        "Live rankings refresh failed and no cached ITF rankings were available."
        if fetch_error
        else "No ITF rankings were available for the requested date."
    )
    return _finish(
        [],
        make_data_status(
            "ITF rankings",
            "error",
            requested=date_str,
            row_count=0,
            stale=True,
            reason=reason,
        ),
    )
