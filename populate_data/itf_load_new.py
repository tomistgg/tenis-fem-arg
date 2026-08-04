import json
import random
import sys
import time
import pandas as pd
import os
import shutil
import requests
import undetected_chromedriver as uc
from datetime import datetime, timedelta
from pathlib import Path

# Allow imports from the project root when invoked as `python populate_data/itf_load_new.py`.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from time_utils import madrid_today
from itf_drawsheet_cache import get_cached_drawsheet, save_drawsheet
from utils import save_json_file, expand_itf_calendar_cache, is_draw_completed
from canonical_data import source_match_key, sync_itf_players
from transactional_io import atomic_write_dataframe
from pipeline_errors import DataValidationError, PipelineError
from run_state import record_run_issue, report_run_issue
from http_client import get_with_retry

# `uc.Chrome.__del__` can raise WinError 6 on Windows after we already call
# `quit()` explicitly. We manage shutdown ourselves, so disable the destructor
# to keep cleanup quiet and deterministic.
uc.Chrome.__del__ = lambda self: None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
from runtime_paths import DATA_DIR as RUNTIME_DATA_DIR

DATA_DIR = str(RUNTIME_DATA_DIR)
TOURNAMENT_LINK_PREFIX = "/en/tournament/"
ITF_EVENT_FILTERS_CACHE_FILE = os.path.join(DATA_DIR, "itf_event_filters_cache.json")
ITF_CALENDAR_CACHE_FILE = os.path.join(DATA_DIR, "itf_calendar_cache.json")
ITF_BLOCKED_RESPONSES_FILE = os.path.join(DATA_DIR, "itf_blocked_responses.json")

ITF_BLOCKED_RESPONSES = []
_ITF_FETCH_BLOCKED = object()
ITF_PAGE_LOAD_TIMEOUT_SECONDS = 45
ITF_SCRIPT_TIMEOUT_SECONDS = 30
ITF_DRAWSHEET_REQUEST_INTERVAL_SECONDS = 13.0
ITF_DRAWSHEET_BLOCK_COOLDOWN_SECONDS = 30.0
_LAST_ITF_DRAWSHEET_REQUEST_AT = 0.0


def _quit_driver(driver, operation="quit browser"):
    if driver is None:
        return
    try:
        driver.quit()
    except Exception as exc:
        report_run_issue("itf-loader", operation, exc, severity="degraded")


def _is_cancelled_tournament(item):
    status = " ".join(
        str(item.get(field) or "")
        for field in (
            "status", "tournamentStatus", "statusDesc", "tournamentStatusDesc",
            "tourStatusCode", "tourStatusDesc",
        )
    ).strip().upper()
    if status == "CN" or "CANCEL" in status:
        return True
    text = " ".join(
        str(item.get(field) or "")
        for field in ("tournamentName", "name", "location", "tournamentLink")
    ).lower()
    return "cancel" in text


def _canonical_draw_store_key(t_key):
    key = str(t_key or "").strip()
    return key.lower() if key else ""

def _is_itf_block_page(text):
    raw = str(text or "")
    upper = raw.upper()
    return raw.startswith("<") and "NOINDEX" in upper and "NOFOLLOW" in upper


def _is_valid_itf_draw_payload(payload):
    return isinstance(payload, dict) and bool(payload.get("koGroups"))


def _wait_for_itf_drawsheet_request_slot():
    """Keep drawsheet calls below ITF/Imperva's observed five-per-minute limit."""
    global _LAST_ITF_DRAWSHEET_REQUEST_AT
    now = time.monotonic()
    wait_seconds = ITF_DRAWSHEET_REQUEST_INTERVAL_SECONDS - (
        now - _LAST_ITF_DRAWSHEET_REQUEST_AT
    )
    if wait_seconds > 0:
        time.sleep(wait_seconds)
    _LAST_ITF_DRAWSHEET_REQUEST_AT = time.monotonic()


def _drawsheet_has_arg_in_round1(data):
    """Return True when round 1 contains at least one ARG player.

    If round 1 has no ARG player, the rest of the draw cannot introduce one
    later. Qualifiers, wildcards, lucky losers, and byes all appear in the
    opening round if they exist at all.
    """
    if not _is_valid_itf_draw_payload(data):
        return False
    ko_groups = data.get("koGroups") or []
    if not ko_groups:
        return False
    rounds_data = ko_groups[0].get("rounds") or []
    if not rounds_data:
        return False

    for match in (rounds_data[0].get("matches") or []):
        for team in (match.get("teams") or []):
            for player in (team.get("players") or []):
                if isinstance(player, dict) and str(player.get("nationality") or "").upper() == "ARG":
                    return True
    return False


def _record_itf_block(tournament_id, code, week_number, tournament_name=""):
    item = {
        "endpoint": "drawsheet",
        "tournament_id": str(tournament_id or "").strip(),
        "tournament_name": str(tournament_name or "").strip(),
        "code": str(code or "").strip(),
        "week_number": "" if week_number in (None, "") else str(week_number),
    }
    for existing in ITF_BLOCKED_RESPONSES:
        if (
            existing.get("endpoint") == item["endpoint"]
            and existing.get("tournament_id") == item["tournament_id"]
            and existing.get("code") == item["code"]
            and existing.get("week_number") == item["week_number"]
        ):
            return
    ITF_BLOCKED_RESPONSES.append(item)


def _record_draw_outcome(
    tournament_id,
    classification,
    week_number,
    tournament_name,
    *,
    severity,
    reason,
):
    error = PipelineError(
        component="itf-loader",
        operation="fetch drawsheet",
        message=reason,
        context={
            "tournament_id": str(tournament_id),
            "tournament_name": str(tournament_name or ""),
            "classification": str(classification),
            "week_number": week_number,
        },
    )
    record_run_issue("itf-loader", error, severity=severity)

def get_week_start_end(today=None):
    if today is None:
        today = madrid_today()
    week_start = today - timedelta(days=today.weekday())  # Monday
    week_end = week_start + timedelta(days=6)              # Sunday
    return week_start, week_end


def _itf_draw_polling_open(start_date_str, today=None):
    """Allow draw polling for current-week events, and for next-week events only on Sat/Sun."""
    if today is None:
        today = madrid_today()
    elif isinstance(today, datetime):
        today = today.date()
    start_str = str(start_date_str or "")[:10]
    if not start_str:
        return True
    try:
        start_dt = datetime.strptime(start_str, "%Y-%m-%d").date()
    except ValueError:
        return True

    current_monday = today - timedelta(days=today.weekday())
    tournament_monday = start_dt - timedelta(days=start_dt.weekday())

    # Current-week and past-week events stay eligible. Next-week events only
    # become eligible on Saturday/Sunday of the week before they start.
    if tournament_monday > current_monday and today.weekday() < 5:
        return False
    return True


def _filter_tournaments_for_polling(tournaments_df, today=None):
    """Return only tournaments whose draws are expected to be polled today."""
    if tournaments_df is None or tournaments_df.empty:
        return tournaments_df, 0
    if today is None:
        today = madrid_today()
    polling_mask = tournaments_df["startDate"].map(
        lambda start_date: _itf_draw_polling_open(start_date, today=today)
    )
    return tournaments_df.loc[polling_mask].copy(), int((~polling_mask).sum())


def _warn_unresolved_tournament_ids(tournaments_df):
    """Log missing source IDs as expected skips, without affecting run status."""
    unresolved_ids = int((tournaments_df["tournamentId"] == "").sum())
    if unresolved_ids:
        print(
            f"  [i] Skipping {unresolved_ids} ITF tournament(s) with no source ID."
        )
    return unresolved_ids


def _get_chrome_major_version():
    import subprocess
    chrome_exe = _get_chrome_executable_path()
    if chrome_exe:
        try:
            out = subprocess.check_output(
                [chrome_exe, "--version"],
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).decode()
            version_str = out.strip().split()[-1]
            return int(version_str.split(".")[0])
        except Exception:
            try:
                import ctypes
                from ctypes import wintypes

                size = ctypes.windll.version.GetFileVersionInfoSizeW(chrome_exe, None)
                if size:
                    buf = ctypes.create_string_buffer(size)
                    if ctypes.windll.version.GetFileVersionInfoW(chrome_exe, 0, size, buf):
                        lp_buffer = ctypes.c_void_p()
                        length = wintypes.UINT()
                        if ctypes.windll.version.VerQueryValueW(
                            buf, "\\", ctypes.byref(lp_buffer), ctypes.byref(length)
                        ):
                            class VS_FIXEDFILEINFO(ctypes.Structure):
                                _fields_ = [
                                    ("dwSignature", wintypes.DWORD),
                                    ("dwStrucVersion", wintypes.DWORD),
                                    ("dwFileVersionMS", wintypes.DWORD),
                                    ("dwFileVersionLS", wintypes.DWORD),
                                    ("dwProductVersionMS", wintypes.DWORD),
                                    ("dwProductVersionLS", wintypes.DWORD),
                                    ("dwFileFlagsMask", wintypes.DWORD),
                                    ("dwFileFlags", wintypes.DWORD),
                                    ("dwFileOS", wintypes.DWORD),
                                    ("dwFileType", wintypes.DWORD),
                                    ("dwFileSubtype", wintypes.DWORD),
                                    ("dwFileDateMS", wintypes.DWORD),
                                    ("dwFileDateLS", wintypes.DWORD),
                                ]

                            info = VS_FIXEDFILEINFO.from_address(lp_buffer.value)
                            return int(info.dwFileVersionMS >> 16)
            except (OSError, ValueError, AttributeError):
                # Fall through to executable-based Chrome version detection.
                print("  [i] Native Chrome version probe failed; trying executable probes.")
    for cmd in (
        ["google-chrome", "--version"],
        ["google-chrome-stable", "--version"],
        ["chromium-browser", "--version"],
        ["chromium", "--version"],
    ):
        try:
            out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode()
            version_str = out.strip().split()[-1]
            return int(version_str.split(".")[0])
        except Exception:
            continue
    return None


def _get_chrome_executable_path():
    """Return the local Chrome executable path when it can be found."""
    candidates = []

    try:
        import winreg

        for root, subkey in (
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
        ):
            try:
                with winreg.OpenKey(root, subkey) as key:
                    value, _ = winreg.QueryValueEx(key, None)
                    if value:
                        candidates.append(value)
            except OSError:
                continue
    except (ImportError, OSError) as exc:
        print(f"  [!] Windows Chrome registry lookup unavailable: {exc}")

    for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(env_name) or ""
        if base:
            candidates.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))

    for exe_name in ("chrome", "chrome.exe", "google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
        found = shutil.which(exe_name)
        if found:
            candidates.append(found)

    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def create_driver():
    opts = uc.ChromeOptions()
    opts.page_load_strategy = "eager"
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("window-size=1920,1080")
    version_main = _get_chrome_major_version()
    chrome_exe = _get_chrome_executable_path()
    kwargs = {"options": opts, "headless": True}
    if version_main:
        kwargs["version_main"] = version_main
    if chrome_exe:
        kwargs["browser_executable_path"] = chrome_exe
    driver = uc.Chrome(**kwargs)
    driver.set_page_load_timeout(ITF_PAGE_LOAD_TIMEOUT_SECONDS)
    driver.set_script_timeout(ITF_SCRIPT_TIMEOUT_SECONDS)
    return driver


def get_itf_calendar_for_range(start_date, end_date, driver=None):
    api_url = (
        f"https://www.itftennis.com/tennis/api/TournamentApi/GetCalendar?"
        f"circuitCode=WT&searchString=&skip=0&take=1000&dateFrom={start_date}&dateTo={end_date}"
        f"&isOrderAscending=true&orderField=startDate"
    )
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.itftennis.com/en/tournament-calendar/womens-world-tennis-tour-calendar/",
    }

    all_tournaments = []
    seen_ids = set()

    try:
        resp = get_with_retry(
            api_url,
            component="itf-loader",
            attempts=3,
            headers=headers,
            timeout=(10, 20),
        )
        resp.raise_for_status()
        raw = resp.text.strip()
        # Incapsula returns an HTML block page instead of JSON when it blocks the
        # request. Detect this and treat it the same as a failed fetch.
        if not raw or raw.startswith("<"):
            raise ValueError("Blocked response (HTML instead of JSON)")
        range_data = resp.json()

        if isinstance(range_data, dict):
            range_data = range_data.get('items') or range_data.get('data') or []

        if not isinstance(range_data, list):
            raise ValueError("Unexpected response shape")

        for tournament in range_data:
            if isinstance(tournament, dict):
                if _is_cancelled_tournament(tournament):
                    continue
                t_id = tournament.get('tournamentKey')
                if t_id and t_id not in seen_ids:
                    all_tournaments.append(tournament)
                    seen_ids.add(t_id)

        all_tournaments.sort(key=lambda x: x.get('startDate', ''))
        return all_tournaments

    except PipelineError:
        return None
    except Exception as e:
        report_run_issue(
            "itf-loader", "fetch calendar", e, severity="partial",
            context={"start_date": start_date, "end_date": end_date},
        )
        return None

def create_tournament_df(tournament_list):
    if not tournament_list:
        return None

    rows = []
    for item in tournament_list:
        link = TOURNAMENT_LINK_PREFIX + item.get("tournamentLink", "")
        t_key = link.rstrip('/').split('/')[-1] if link else None

        rows.append({
            "startDate": item.get("startDate"),
            "endDate": item.get("endDate"),
            "tournamentName": item.get("tournamentName"),
            "hostNation": item.get("hostNation"),
            "category": item.get("category"),
            "surfaceDesc": item.get("surfaceDesc"),
            "indoorOrOutDoor": item.get("indoorOrOutDoor"),
            "tournamentKey": t_key
        })

    return pd.DataFrame(rows)

def fetch_itf_ids_to_json(keys_list, driver=None):
    if not keys_list:
        return "[]"

    id_cache = _load_cached_tournament_ids()
    results = []
    missing_keys = []

    for key in keys_list:
        cached = id_cache.get(key.lower())
        if cached:
            results.append({"tournamentKey": key, "tournamentId": cached})
        else:
            missing_keys.append(key)

    if missing_keys:
        print(f"  Fetching {len(missing_keys)} IDs not in cache (cached {len(results)}/{len(keys_list)}).")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.itftennis.com/en/tournament-calendar/womens-world-tennis-tour-calendar/",
        }
        newly_fetched = {}
        for idx, key in enumerate(missing_keys):
            if idx > 0:
                time.sleep(random.uniform(5.0, 10.0))
            url = f"https://www.itftennis.com/tennis/api/TournamentApi/GetEventFilters?tournamentKey={key}"
            try:
                response = get_with_retry(
                    url,
                    component="itf-loader",
                    attempts=3,
                    headers=headers,
                    timeout=(10, 20),
                )
                raw = response.text.strip()
                if response.status_code == 200 and raw and not raw.startswith("<"):
                    data = response.json()
                    if isinstance(data, dict) and "tournamentId" in data:
                        tid = data["tournamentId"]
                        results.append({"tournamentKey": key, "tournamentId": tid})
                        newly_fetched[key.lower()] = str(tid)
            except PipelineError:
                continue
            except Exception as e:
                report_run_issue(
                    "itf-loader", "fetch tournament ID", e, severity="partial",
                    context={"tournament_key": str(key)},
                )

        # Write newly fetched IDs back so subsequent scripts skip these calls.
        if newly_fetched:
            existing = {}
            if os.path.exists(ITF_EVENT_FILTERS_CACHE_FILE):
                with open(ITF_EVENT_FILTERS_CACHE_FILE, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            existing.update(newly_fetched)
            save_json_file(ITF_EVENT_FILTERS_CACHE_FILE, existing)
    else:
        print(f"  All {len(keys_list)} tournament IDs resolved from cache.")

    return json.dumps(results)

def merge_ids_with_pandas(calendar_df, json_ids_string):
    try:
        ids_list = json.loads(json_ids_string)
        ids_df = pd.DataFrame(ids_list)
        final_df = pd.merge(calendar_df, ids_df, on="tournamentKey", how="left")
        return final_df
    except Exception as e:
        raise DataValidationError(
            component="itf-loader",
            operation="merge tournament IDs",
            message="could not merge ITF calendar with tournament IDs",
            context={"cause": str(e)},
        ) from e


def fetch_api_data(tId, classification, week_number=0, driver=None, tournament_name=""):
    cached = get_cached_drawsheet(tId, classification, week_number)
    if cached is not None:
        return cached
    stale_cached = get_cached_drawsheet(tId, classification, week_number, allow_stale=True)

    url = "https://www.itftennis.com/tennis/api/TournamentApi/GetDrawsheet"
    params = {
        "eventClassificationCode": classification,
        "matchTypeCode": "S",
        "tourType": "N",
        "tournamentId": f"{tId}",
        "weekNumber": week_number
    }

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": f"https://www.itftennis.com/en/tournament/draws-and-results/print/?tournamentId={tId}&circuitCode=WT",
        "Origin": "https://www.itftennis.com",
        "Accept": "application/json, text/plain, */*"
    }

    # Keep this API call independent of the Selenium session. Live probing showed
    # that ITF's Imperva policy permits five canonical drawsheet GETs per minute;
    # the sixth request is served an HTML NOINDEX/NOFOLLOW block page. Visiting
    # the print page and retrying from that browser context also reproduces the
    # block, so use a paced canonical GET and one quiet-period retry instead.
    blocked_response = False
    request_error = None
    for attempt in range(2):
        try:
            _wait_for_itf_drawsheet_request_slot()
            response = requests.get(url, params=params, headers=headers, timeout=15)
            raw = response.text.strip()
            blocked_response = (
                response.status_code in {403, 429}
                or _is_itf_block_page(raw)
            )
            if blocked_response:
                if attempt == 0:
                    print(
                        f"  [i] ITF draw rate-limited for {tournament_name} "
                        f"(id={tId}, code={classification}); retrying after "
                        f"{ITF_DRAWSHEET_BLOCK_COOLDOWN_SECONDS:.0f}s"
                    )
                    time.sleep(ITF_DRAWSHEET_BLOCK_COOLDOWN_SECONDS)
                    continue
                break

            if response.status_code == 200 and raw and not raw.startswith("<"):
                data = response.json()
                if _is_valid_itf_draw_payload(data):
                    save_drawsheet(tId, classification, week_number, data)
                    return data
                if stale_cached is not None:
                    print(
                        f"  [i] Using stale cached ITF draw for {tournament_name} "
                        f"(id={tId}, code={classification}) after empty live payload"
                    )
                    _record_draw_outcome(
                        tId, classification, week_number, tournament_name,
                        severity="degraded", reason="using stale draw after empty live payload",
                    )
                    return stale_cached
                if isinstance(data, dict):
                    _record_draw_outcome(
                        tId, classification, week_number, tournament_name,
                        severity="degraded", reason="live draw payload was incomplete",
                    )
                    return _ITF_FETCH_BLOCKED
                return None
            break
        except (requests.RequestException, ValueError, TypeError) as exc:
            request_error = exc
            break

    if blocked_response:
        _record_itf_block(tId, classification, week_number, tournament_name)
        if stale_cached is not None:
            print(
                f"  [i] Using stale cached ITF draw for {tournament_name} "
                f"(id={tId}, code={classification}) after throttled retry was blocked"
            )
            _record_draw_outcome(
                tId, classification, week_number, tournament_name,
                severity="degraded", reason="using stale draw after HTTP block",
            )
            return stale_cached
        _record_draw_outcome(
            tId, classification, week_number, tournament_name,
            severity="degraded", reason="drawsheet blocked after throttled retry with no cached fallback",
        )
        return _ITF_FETCH_BLOCKED

    if request_error is not None:
        report_run_issue(
            "itf-loader", "fetch drawsheet via HTTP", request_error, severity="degraded",
            context={"tournament_id": str(tId), "classification": str(classification)},
        )

    if stale_cached is not None:
        print(
            f"  [i] Using stale cached ITF draw for {tournament_name} "
            f"(id={tId}, code={classification}) after live fetch failed"
        )
        _record_draw_outcome(
            tId, classification, week_number, tournament_name,
            severity="degraded", reason="using stale draw after live fetch failure",
        )
        return stale_cached

    _record_draw_outcome(
        tId, classification, week_number, tournament_name,
        severity="degraded", reason="drawsheet fetch failed with no cached fallback",
    )
    return None


def fetch_tournament_draw_data(tournament_id, tournament_name, codes, week_number=0, max_attempts=2, external_driver=None):
    """Fetch draw data for one tournament.

    ``external_driver`` indicates that the caller owns the shared browser. The
    drawsheet API deliberately does not borrow that browser's cookies because
    the canonical rate-limited GET is more reliable as an independent request.
    """
    tournament_id = int(tournament_id)

    # If every requested draw is already in the shared cache, skip the browser entirely.
    cached_results = {code: get_cached_drawsheet(tournament_id, code, week_number) for code in codes}
    if all(v is not None for v in cached_results.values()):
        return cached_results

    def _fetch_codes(session_driver, requested_codes):
        results = {}
        blocked_codes = []
        for code in requested_codes:
            payload = fetch_api_data(
                tournament_id,
                code,
                week_number=week_number,
                driver=session_driver,
                tournament_name=tournament_name,
            )
            if payload is _ITF_FETCH_BLOCKED:
                blocked_codes.append(code)
            elif payload:
                results[code] = payload
            time.sleep(random.uniform(2.0, 5.0) if session_driver is not None else random.uniform(5.0, 10.0))
        return results, blocked_codes

    if external_driver is not None:
        try:
            # The canonical API request is centrally paced. Do not navigate to
            # the print page here: that browser context reproduced the Imperva
            # block and made the immediate retry ineffective.
            results, blocked_codes = _fetch_codes(external_driver, codes)
            if blocked_codes:
                print(
                    f"  [!] ITF draw still blocked for {tournament_name} "
                    f"after throttled retry: {', '.join(blocked_codes)}"
                )
            return results
        except Exception as e:
            print(f"  [!] Draw fetch failed for {tournament_name} (shared session): {e}")
        return {}

    # Fallback for callers without a shared driver. The HTTP helper already
    # performs its own paced retry, so no browser session is needed here.
    attempt = 1
    best_results = {}
    while True:
        results = {}
        blocked_codes = []
        try:
            results, blocked_codes = _fetch_codes(None, codes)
            best_results.update(results)
            if any(best_results.values()) and not blocked_codes:
                return best_results
        except Exception as e:
            print(f"  [!] Draw fetch failed for {tournament_name} (attempt {attempt}): {e}")

        if blocked_codes and attempt == 1:
            cooldown = random.uniform(6.0, 10.0)
            print(
                f"  [!] Blocked draw response for {tournament_name}; "
                f"retrying with a fresh session after {cooldown:.1f}s"
            )
            time.sleep(cooldown)
            attempt += 1
            continue

        if blocked_codes:
            break

        if attempt < max_attempts:
            cooldown = random.uniform(10.0, 16.0)
            print(
                f"  [!] Empty/blocked draw response for {tournament_name}; "
                f"retrying with a fresh session after {cooldown:.1f}s"
            )
            time.sleep(cooldown)
            attempt += 1
            continue

        break

    return best_results


def parse_drawsheet(data, tourney_meta, draw_type, week_offset=0):
    if not data or not isinstance(data, dict): return []
    rows = []

    # Fast skip: if round 1 has no ARG player, this draw cannot produce an ARG
    # match later via Q, WC, LL, SE, or bye progression.
    if not _drawsheet_has_arg_in_round1(data):
        return rows
    
    t_id = tourney_meta.get('tournamentId')
    t_name = tourney_meta.get('tournamentName')
    t_cat = tourney_meta.get('category')
    t_surf = tourney_meta.get('surfaceDesc')
    t_indoor = tourney_meta.get('indoorOrOutDoor', '')
    t_io = 'I' if t_indoor == 'Indoor' else 'O'
    t_nation = tourney_meta.get('hostNation')
    # Requirement: load ITF matches using the ingestion date (today), not ITF event date.
    t_date = madrid_today().isoformat()

    ko_groups = data.get("koGroups", [])

    q_map = {}
    if draw_type == "Q":
        seen_q = {}
        for group in ko_groups:
            for rnd in group.get("rounds", []):
                rd = rnd.get("roundDesc")
                rn = rnd.get("roundNumber")
                if rd and rd not in seen_q:
                    seen_q[rd] = int(rn) if isinstance(rn, (int, float)) else 9999
        q_map = {rd: f"QR{i + 1}" for i, (rd, _) in enumerate(sorted(seen_q.items(), key=lambda x: x[1]))}

    def get_p(t):
        ps = t.get("players", [])
        if not ps or not isinstance(ps[0], dict): return "Unknown", "", ""
        p = ps[0]
        return p.get('playerId',''), f"{p.get('givenName','')} {p.get('familyName','')}".strip(), p.get('nationality','')

    for group in ko_groups:
        rounds = group.get("rounds", [])
        for rnd in rounds:
            r_ds = rnd.get("roundDesc")
            matches = rnd.get("matches", [])
            arg_survives_this_round = False
            for match in matches:
                try:
                    teams = match.get("teams", [])
                    match_has_arg = False
                    for team in teams:
                        for player in team.get("players", []):
                            if isinstance(player, dict) and str(player.get("nationality") or "").upper() == "ARG":
                                match_has_arg = True
                                break
                        if match_has_arg:
                            break
                    if match_has_arg and match.get("resultStatusCode") == "BYE":
                        arg_survives_this_round = True

                    if match.get("playStatusCode") != "PC" and match.get("resultStatusCode") not in ("WO", "BYE"):
                        continue

                    matchId = match.get("matchId")
                    if len(teams) < 2:
                        continue

                    is_winner_0 = str(teams[0].get("isWinner")).lower() == "true"

                    if is_winner_0:
                        winner, loser = teams[0], teams[1]
                    else:
                        winner, loser = teams[1], teams[0]

                    w_id, w_n, w_c = get_p(winner)
                    l_id, l_n, l_c = get_p(loser)
                    
                    w_en = winner.get('entryStatus') or ""
                    w_sd = int(winner['seeding']) if winner.get('seeding') else ""
                    l_en = loser.get('entryStatus') or ""
                    l_sd = int(loser['seeding']) if loser.get('seeding') else ""
                    
                    # Score Parsing
                    w_s, l_s = winner.get("scores", []), loser.get("scores", [])
                    parts = []
                    for i in range(max(len(w_s), len(l_s))):
                        ws = w_s[i] if i < len(w_s) else {}
                        ls = l_s[i] if i < len(l_s) else {}
                        if isinstance(ws, dict) and isinstance(ls, dict):
                            sc_w = ws.get("score")
                            sc_l = ls.get("score")
                            if sc_w is not None and sc_l is not None:
                                s = f"{sc_w}-{sc_l}"
                                tb = ws.get("losingScore") or ls.get("losingScore")
                                if tb: s += f"({tb})"
                                parts.append(s)
                                
                    res = " ".join(parts)
                    status_desc = match.get("resultStatusDesc", "Completed")
                    if status_desc:
                        if "Retired" in status_desc:
                            res += " ret."
                        elif "Defaulted" in status_desc or "Default" in status_desc:
                            res += " def."

                    if match.get("resultStatusCode") == "BYE":
                        res = "-"
                        status_desc = "Bye"
                        l_n = "Bye"
                        l_c = "-"
                    elif match.get("resultStatusCode") == "WO":
                        res = "W/O"
                        status_desc = "Walkover"
                        if not l_n:
                            l_id = "Unknown"
                            l_n = "Unknown"
                            l_c = "-"
                    elif not any(char.isdigit() for char in res):
                        res = "W/O"
                        status_desc = "Walkover"
                        if not l_n:
                            l_id = "Unknown"
                            l_n = "Unknown"
                            l_c = "-"

                    if w_c != "ARG" and l_c != "ARG":
                        continue

                    if w_c == "ARG":
                        arg_survives_this_round = True

                    rows.append({
                        "matchType": "ITF",
                        "matchId": matchId,
                        "date": t_date,
                        "tournamentId": t_id,
                        "tournamentName": t_name,
                        "tournamentCategory": t_cat,
                        "surface": t_surf,
                        "inOrOutdoor": t_io,
                        "tournamentCountry": t_nation,
                        "roundName": q_map.get(r_ds, r_ds) if draw_type == "Q" else r_ds,
                        "draw": draw_type,
                        "result": res,
                        "resultStatusDesc": status_desc,
                        "winnerId": w_id,
                        "winnerEntry": w_en,
                        "winnerSeed": w_sd,
                        "winnerName": w_n,
                        "winnerCountry": w_c,
                        "loserId": l_id,
                        "loserEntry": l_en,
                        "loserSeed": l_sd,
                        "loserName": l_n,
                        "loserCountry": l_c
                    })
                except Exception as e:
                    report_run_issue(
                        "itf-loader", "parse drawsheet match", e, severity="partial",
                        context={
                            "tournament_id": str(t_id),
                            "tournament_name": str(t_name or ""),
                            "draw": str(draw_type),
                            "match_id": str(match.get("matchId") or ""),
                        },
                    )
                    continue
            if not arg_survives_this_round:
                break
    return rows


def update_csv_smart(filename, new_data_df, reset_if_not_current_week=False, current_week_start=None):
    """
    Handles loading, checking dates, deduplicating, and saving.
    """
    file_path = os.path.join(DATA_DIR, filename)
    
    existing_df = pd.DataFrame()
    file_exists = os.path.exists(file_path)

    if file_exists:
        try:
            existing_df = pd.read_csv(file_path, dtype=str, keep_default_na=False)
        except Exception as e:
            raise DataValidationError(
                component="itf-loader",
                operation="read existing CSV",
                message=f"refusing to replace unreadable {filename}",
                context={"path": file_path, "cause": str(e)},
            ) from e

    # Logic 1: Weekly Reset Check
    if reset_if_not_current_week and file_exists and not existing_df.empty:
        if 'date' in existing_df.columns:
            try:
                sample_date_str = existing_df['date'].iloc[0]
                sample_date = datetime.strptime(str(sample_date_str), "%Y-%m-%d").date()
                file_week_start = sample_date - timedelta(days=sample_date.weekday())
                if file_week_start != current_week_start:
                    existing_df = pd.DataFrame()
            except (TypeError, ValueError, IndexError) as e:
                raise DataValidationError(
                    component="itf-loader",
                    operation="validate weekly CSV date",
                    message=f"refusing to reset {filename} after an invalid existing date",
                    context={"path": file_path, "cause": str(e)},
                ) from e
        else:
            print(f"[!] No date column found in {filename}. Resetting file.")
            existing_df = pd.DataFrame()

    # Logic 2: Deduplication (Add only what doesn't exist)
    if not existing_df.empty:
        existing_ids = set(
            existing_df.apply(lambda row: source_match_key(row.to_dict(), "itf"), axis=1)
        )
        new_data_df = new_data_df.copy()
        new_data_df['_canonical_key'] = new_data_df.apply(
            lambda row: source_match_key(row.to_dict(), "itf"), axis=1
        )
        unique_new_rows = new_data_df[
            ~new_data_df['_canonical_key'].isin(existing_ids)
        ].drop_duplicates(subset=['_canonical_key'], keep='last')
        unique_new_rows = unique_new_rows.drop(columns=['_canonical_key'])
        
        if unique_new_rows.empty:
            return

        final_df = pd.concat([existing_df, unique_new_rows], ignore_index=True)
    else:
        final_df = new_data_df.copy()
        final_df['_canonical_key'] = final_df.apply(
            lambda row: source_match_key(row.to_dict(), "itf"), axis=1
        )
        final_df = final_df.drop_duplicates(subset=['_canonical_key'], keep='last')
        final_df = final_df.drop(columns=['_canonical_key'])

    def _normalize_seed(value):
        if value is None or (hasattr(pd, "isna") and pd.isna(value)):
            return ""
        s = str(value).strip()
        if not s or s.lower() == "nan":
            return ""
        try:
            f = float(s)
            if f.is_integer():
                return str(int(f))
        except (TypeError, ValueError, OverflowError):
            # Non-numeric seeds are legitimate labels and are preserved below.
            return s
        return s

    for col in ("winnerSeed", "loserSeed"):
        if col in final_df.columns:
            final_df[col] = final_df[col].map(_normalize_seed)

    atomic_write_dataframe(final_df, file_path, index=False, encoding='utf-8-sig')

def _load_cached_tournament_ids():
    """Load ITF tournament IDs from the persistent event-filters cache.

    Returns a dict mapping tournamentKey (lowercase) -> tournamentId (str).
    Used as a fallback when the live Selenium-based ID fetch is blocked.
    """
    if not os.path.exists(ITF_EVENT_FILTERS_CACHE_FILE):
        return {}
    try:
        with open(ITF_EVENT_FILTERS_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise DataValidationError(
            component="itf-loader",
            operation="load tournament ID cache",
            message="cannot read existing ITF tournament ID cache",
            context={"path": ITF_EVENT_FILTERS_CACHE_FILE, "cause": str(exc)},
        ) from exc
    if not isinstance(data, dict):
        raise DataValidationError(
            component="itf-loader",
            operation="validate tournament ID cache",
            message="ITF tournament ID cache must contain a JSON object",
            context={"path": ITF_EVENT_FILTERS_CACHE_FILE},
        )
    return {k.lower(): str(v) for k, v in data.items() if v and str(v).strip().lower() not in ("none", "null", "")}


def _load_cached_calendar_tournaments(week_start, week_end):
    """Load ITF tournament entries from the persistent calendar cache that fall
    within [week_start, week_end].  Returns a list of raw tournament dicts in
    the same shape as the live GetCalendar API response so they can be fed
    directly into create_tournament_df().
    """
    if not os.path.exists(ITF_CALENDAR_CACHE_FILE):
        return []
    try:
        with open(ITF_CALENDAR_CACHE_FILE, "r", encoding="utf-8") as f:
            data = expand_itf_calendar_cache(json.load(f))
    except (OSError, json.JSONDecodeError) as exc:
        raise DataValidationError(
            component="itf-loader",
            operation="load calendar cache",
            message="cannot read existing ITF calendar cache",
            context={"path": ITF_CALENDAR_CACHE_FILE, "cause": str(exc)},
        ) from exc
    items = data.get("items", []) if isinstance(data, dict) else (data or [])

    results = []
    for item in items:
        if not isinstance(item, dict) or _is_cancelled_tournament(item):
            continue
        start = str(item.get("startDate") or "")[:10]
        if not start:
            continue
        try:
            start_dt = datetime.strptime(start, "%Y-%m-%d").date()
        except ValueError:
            continue
        if week_start <= start_dt <= week_end:
            results.append(item)
    return results


if __name__ == "__main__":
    from pipeline_transaction import run_current_script_transaction, transaction_is_active

    if not transaction_is_active():
        raise SystemExit(run_current_script_transaction(__file__))

    week_start, week_end = get_week_start_end()
    # Day-of-week tapered window: early-week keeps last week in scope for
    # late-arriving results, mid-week narrows to just this week, late-week
    # reaches forward to pick up the next week's entry lists & draws.
    weekday = madrid_today().weekday()  # 0=Mon, 6=Sun
    if weekday <= 1:       # Mon, Tue
        window_start = week_start - timedelta(days=7)
        window_end = week_end
        window_label = "last+this week"
    elif weekday <= 3:     # Wed, Thu
        window_start = week_start
        window_end = week_end
        window_label = "this week"
    else:                  # Fri, Sat, Sun
        window_start = week_start
        window_end = week_end + timedelta(days=7)
        window_label = "this+next week"

    # Single driver kept alive through the full run (calendar → IDs → drawsheets)
    driver = create_driver()
    try:
        # ITF tournaments in the window are scheduled well in advance, so the
        # year-wide calendar that main.py refreshed on the previous cron run is
        # authoritative here. Read it first and only fall back to a live
        # GetCalendar fetch when the persistent cache is missing (e.g. very
        # first run on a fresh checkout).
        print(f"  ITF calendar window ({window_label}): {window_start} -> {window_end}")
        cached_items = _load_cached_calendar_tournaments(window_start, window_end)
        if not cached_items:
            print("[!] No cached calendar available; falling back to live GetCalendar fetch.")
            live_items = get_itf_calendar_for_range(
                window_start.strftime("%Y-%m-%d"),
                window_end.strftime("%Y-%m-%d"),
                driver=driver
            )
            if live_items is None:
                raise PipelineError(
                    component="itf-loader",
                    operation="load calendar window",
                    message="ITF calendar was unavailable and no cached window exists",
                    context={"start": str(window_start), "end": str(window_end)},
                )
            cached_items = live_items

        seen_keys = set()
        raw_data = []
        for t in cached_items:
            key = t.get("tournamentKey")
            if key and key not in seen_keys:
                raw_data.append(t)
                seen_keys.add(key)
        if not raw_data:
            print("  No ITF tournaments are scheduled in this window.")
            raise SystemExit(0)
        print(f"  ITF calendar window: {len(raw_data)} tournaments.")

        tournaments_df = create_tournament_df(raw_data)

        if tournaments_df is None or tournaments_df.empty:
            print("  No eligible ITF tournaments remain after calendar normalization.")
            raise SystemExit(0)
        completed_mask = tournaments_df["tournamentKey"].map(
            lambda key: bool(key and is_draw_completed(_canonical_draw_store_key(key)))
        )
        if completed_mask.any():
            skipped_completed = int(completed_mask.sum())
            print(f"  Skipping {skipped_completed} completed tournament(s) already marked finished.")
            tournaments_df = tournaments_df.loc[~completed_mask].copy()
        if tournaments_df.empty:
            print("  All ITF tournaments in this window were already completed.")
            raise SystemExit(0)

        # On Friday, next-week tournaments are included in the calendar window
        # for visibility but their draws are intentionally not polled until the
        # weekend. Exclude them before ID resolution so an expected missing ID
        # cannot mark the transaction as partial.
        tournaments_df, skipped_future_week = _filter_tournaments_for_polling(
            tournaments_df,
            today=madrid_today(),
        )
        if skipped_future_week:
            print(f"  Skipping {skipped_future_week} next-week tournament(s) until the weekend before they start.")
        if tournaments_df.empty:
            print("  No ITF tournaments are eligible for draw polling today.")
            raise SystemExit(0)

        keys_list = tournaments_df["tournamentKey"].dropna().unique().tolist()

        # Warm up browser on ITF BEFORE any API calls so Incapsula session is valid
        print("  Warming up browser session...")
        try:
            driver.get("https://www.itftennis.com/en/tournament-calendar/womens-world-tennis-tour-calendar/")
            time.sleep(4)
            print("  Browser session ready.")
        except Exception as e:
            report_run_issue("itf-loader", "warm browser", e, severity="degraded")

        json_ids_string = fetch_itf_ids_to_json(keys_list, driver=driver)

        final_df = merge_ids_with_pandas(tournaments_df, json_ids_string)

        # If the live ID fetch returned no results (ITF blocked), fall back to the
        # persistent itf_event_filters_cache.json so we still fetch drawsheets for
        # the current set of known tournaments.
        cached_ids = _load_cached_tournament_ids()
        missing_id_mask = final_df["tournamentId"].isna()
        if missing_id_mask.any() and cached_ids:
            print(f"[!] {missing_id_mask.sum()} tournament(s) missing IDs from live fetch; filling from cache.")
            for idx in final_df.index[missing_id_mask]:
                key = str(final_df.at[idx, "tournamentKey"] or "").strip().lower()
                cached_id = cached_ids.get(key)
                if cached_id:
                    try:
                        final_df.at[idx, "tournamentId"] = float(cached_id)
                    except (ValueError, TypeError):
                        pass
        def _coerce_id(val):
            s = str(val).strip() if val is not None else ''
            if not s or s.lower() in ('none', 'nan', '0', 'null'):
                return ''
            try:
                return str(int(float(s)))
            except (ValueError, TypeError):
                return ''
        final_df['tournamentId'] = final_df['tournamentId'].apply(_coerce_id)
        _warn_unresolved_tournament_ids(final_df)


        all_matches = []
        active_count = 0
        consecutive_empty = 0
        _MAX_CONSECUTIVE_EMPTY = 2  # Recreate session after this many all-empty results

        def _week_key(t):
            raw = str(t.get("startDate") or "")[:10]
            try:
                d = datetime.strptime(raw, "%Y-%m-%d").date()
                return d - timedelta(days=d.weekday())
            except ValueError:
                return raw

        tournaments_list = final_df.to_dict('records')

        from itertools import groupby
        tournaments_list.sort(key=_week_key)
        week_groups = [list(g) for _, g in groupby(tournaments_list, key=_week_key)]
        for group_idx, week_group in enumerate(week_groups):
            if group_idx > 0:
                print(f"  Sleeping 35s before next week's tournaments...")
                time.sleep(35)
            random.shuffle(week_group)
            for tourney in week_group:
                tId = tourney.get("tournamentId")
                tName = tourney.get("tournamentName")
                tCategory = tourney.get("category", "")

                if tCategory and str(tCategory).strip().startswith("Tier"):
                    continue

                if not tId or pd.isna(tId) or str(tId) == "":
                    print(f"  Skipping {tName} — no tournament ID")
                    continue

                active_count += 1
                tourney_matches_before = len(all_matches)

                is_multiweek = tCategory == "ITF Womens Multi-Week Circuit"

                if is_multiweek:
                    week = 1
                    while True:
                        has_data_this_week = False

                        draw_payloads = fetch_tournament_draw_data(
                            tId,
                            tName,
                            ["Q", "M"],
                            week_number=week,
                            max_attempts=1,
                            external_driver=driver,
                        )

                        for code in ["Q", "M"]:
                            json_data = draw_payloads.get(code)

                            if json_data:
                                parsed = parse_drawsheet(json_data, tourney, code, week_offset=(week - 1))
                                if parsed:
                                    all_matches.extend(parsed)
                                    has_data_this_week = True
                            else:
                                print(f"  [!] No data returned for {tName} (id={tId}, code={code}, week={week})")

                        if not has_data_this_week:
                            break

                        week += 1
                        if week > 10:
                            break
                else:
                    draw_payloads = fetch_tournament_draw_data(
                        tId,
                        tName,
                        ["Q", "M"],
                        week_number=0,
                        max_attempts=1,
                        external_driver=driver,
                    )

                    for code in ["Q", "M"]:
                        json_data = draw_payloads.get(code)

                        if json_data:
                            parsed = parse_drawsheet(json_data, tourney, code, week_offset=0)
                            all_matches.extend(parsed)
                        else:
                            print(f"  [!] No data returned for {tName} (id={tId}, code={code})")

                added = len(all_matches) - tourney_matches_before
                print(f"  {tName} (id={tId}): {added} ARG matches found")

                got_any_draw = any(v for v in draw_payloads.values() if v)
                if got_any_draw:
                    consecutive_empty = 0
                else:
                    consecutive_empty += 1
                    if consecutive_empty >= _MAX_CONSECUTIVE_EMPTY:
                        print(f"  [!] {consecutive_empty} consecutive empty results — refreshing browser session.")
                        _quit_driver(driver, "recycle empty-results browser")
                        driver = create_driver()
                        try:
                            driver.get("https://www.itftennis.com/en/tournament-calendar/womens-world-tennis-tour-calendar/")
                            time.sleep(3)
                        except Exception as exc:
                            report_run_issue(
                                "itf-loader", "warm recycled browser", exc,
                                severity="partial",
                            )
                        consecutive_empty = 0

                time.sleep(random.uniform(5.0, 10.0))

        print(f"Tournaments processed: {active_count}, total ARG matches found: {len(all_matches)}")

    finally:
        _quit_driver(driver)

    save_json_file(ITF_BLOCKED_RESPONSES_FILE, ITF_BLOCKED_RESPONSES)

    if all_matches:
        added_players = sync_itf_players(
            Path(DATA_DIR) / "player_aliases_wta_itf.json", all_matches
        )
        if added_players:
            print(f"Added {added_players} new ITF identities to the canonical player table.")
        new_matches_df = pd.DataFrame(all_matches)
        update_csv_smart(
            "itf_matches_arg.csv",
            new_matches_df,
            reset_if_not_current_week=False
        )
        print(f"CSV update complete.")
    else:
        print("No new ARG matches found — CSV not updated.")
