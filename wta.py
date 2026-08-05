import re
import json
import time
import requests
from datetime import timedelta
from bs4 import BeautifulSoup

import csv as _csv
import os as _os
from collections import OrderedDict
from collections.abc import MutableMapping
 
from config import (
    API_URL, HEADERS, NAME_LOOKUP, WTA_ID_TO_DISPLAY,
    resolve_player_display_name,
    WTA_RANKINGS_CSV, WTA_RANKINGS_CSV_10_19,
    WTA_RANKINGS_CSV_00_09, WTA_RANKINGS_CSV_83_99,
    DATA_DIR
)
from utils import (
    fix_display_name, fix_encoding, format_player_name, save_json_file,
    dumps_wta_full_calendar_cache, expand_wta_calendar_cache,
    make_data_status, set_cache_file_meta, utc_now_iso,
)
from calendar_builder import get_next_monday, get_monday_from_date, format_week_label
from time_utils import madrid_today
from transactional_io import atomic_write_csv
from run_state import report_run_issue
from pipeline_errors import PipelineError
from http_client import get_with_retry
from runtime_logging import get_logger


logger = get_logger("wta")
_wta_tournaments_raw = None  # module-level cache for raw WTA tournament API data
_WTA_FULL_CALENDAR_CACHE_FILE = _os.path.join(DATA_DIR, "wta_full_calendar_cache.json")
_WTA_FULL_CALENDAR_TTL = 3 * 60 * 60  # 3 hours
_REQUESTS_SESSION = requests.Session()

_WTA_MAX_ATTEMPTS = 8
_WTA_BACKOFF_BASE_SEC = 5.0
_WTA_BACKOFF_MAX_SEC = 120.0


def _normalize_country_code(code):
    """Normalize country codes used by the WTA feeds and cache.

    WTA occasionally emits ``GRC`` for Greece; the rest of the site uses the
    IOC code ``GRE``, so normalize here to keep flags, filters, and caches
    consistent.
    """
    value = str(code or "").strip().upper()
    if not value or value == "-":
        return ""
    if value == "GRC":
        return "GRE"
    return value


class WtaApiRateLimited(RuntimeError):
    pass


class WtaApiFetchError(RuntimeError):
    pass


class WtaApiPartialData(RuntimeError):
    pass


def _load_cached_wta_tournaments_raw():
    """Load the last saved WTA tournament snapshot from disk."""
    try:
        with open(_WTA_FULL_CALENDAR_CACHE_FILE, "r", encoding="utf-8") as f:
            cached = json.load(f)
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        report_run_issue(
            "wta", "load calendar cache", exc, severity="degraded",
            context={"path": _WTA_FULL_CALENDAR_CACHE_FILE},
        )
        return []
    cached = expand_wta_calendar_cache(cached)
    items = cached.get("items", cached) if isinstance(cached, dict) else cached
    return items if isinstance(items, list) else []


def _fetch_wta_tournaments_raw():
    """Fetch all WTA tournaments from 1 week ago to end of year (single API call)."""
    global _wta_tournaments_raw
    if _wta_tournaments_raw is not None:
        return _wta_tournaments_raw

    today = madrid_today()
    next_monday = get_next_monday()
    from_date = (next_monday - timedelta(days=7)).strftime("%Y-%m-%d")
    to_date = f"{today.year}-12-31"

    url = "https://api.wtatennis.com/tennis/tournaments/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
        "referer": "https://www.wtatennis.com/",
        "account": "wta"
    }
    params = {
        "page": 0,
        "pageSize": 200,
        "excludeLevels": "ITF",
        "from": from_date,
        "to": to_date
    }

    try:
        response = get_with_retry(
            url,
            component="wta",
            attempts=4,
            headers=headers,
            params=params,
            timeout=(10, 20),
            failure_status="degraded",
        )
        data = response.json()
        items = data.get("content", [])
        if not items:
            cached_items = _load_cached_wta_tournaments_raw()
            if cached_items:
                logger.info("Using cached WTA tournaments (live API returned no items)")
                _wta_tournaments_raw = cached_items
                return _wta_tournaments_raw
        _wta_tournaments_raw = items
        if items:
            save_json_file(_WTA_FULL_CALENDAR_CACHE_FILE, {
                "from": from_date,
                "to": to_date,
                "items": _wta_tournaments_raw,
            }, formatter=dumps_wta_full_calendar_cache)
            set_cache_file_meta(
                _WTA_FULL_CALENDAR_CACHE_FILE,
                fetchedAt=utc_now_iso(),
                **{"from": from_date, "to": to_date},
            )
    except Exception as e:
        logger.debug(f"Error fetching WTA tournaments: {e}")
        cached_items = _load_cached_wta_tournaments_raw()
        if cached_items:
            if not isinstance(e, PipelineError):
                report_run_issue(
                    "wta", "fetch calendar", e, severity="degraded",
                    context={"fallback": "cached calendar"},
                )
            logger.warning("Using cached WTA tournaments after live fetch failure")
            _wta_tournaments_raw = cached_items
        else:
            report_run_issue(
                "wta", "fetch calendar", e, severity="partial",
                context={"fallback": None},
            )
            _wta_tournaments_raw = []

    return _wta_tournaments_raw


def build_tournament_groups():
    today = madrid_today()
    next_monday = get_next_monday()
    current_monday = today - timedelta(days=today.weekday())
    five_weeks_later = next_monday + timedelta(weeks=5)
    is_mon_or_tue = today.weekday() in (0, 1)

    raw_tournaments = _fetch_wta_tournaments_raw()

    tournament_groups = {}

    for tournament in raw_tournaments:
        tournament_id = tournament["tournamentGroup"]["id"]
        raw_name = tournament["tournamentGroup"]["name"]

        clean_name = fix_encoding(raw_name)

        suffix = ""
        if "#" in clean_name:
            parts = clean_name.split("#")
            clean_name = parts[0].strip()
            suffix = " " + parts[1].strip()

        name = clean_name.lower().replace(" ", "-").replace("'", "-")
        if suffix:
            name += "-" + suffix.strip()

        year = tournament["year"]
        level = tournament["level"]
        city = tournament["city"].title()
        start_date = tournament["startDate"]
        end_date = tournament.get("endDate", None)
        country = _normalize_country_code(tournament.get("country") or tournament.get("countryCode") or "")

        monday = get_monday_from_date(start_date)

        is_current_week = (monday == current_monday)
        if is_current_week:
            # Schedule / Entry Lists keep the current week only on Monday.
            if not is_mon_or_tue:
                continue
        elif not (next_monday <= monday < five_weeks_later):
            continue

        week_label = format_week_label(monday)

        t_url = f"https://www.wtatennis.com/tournaments/{tournament_id}/{name}/{year}/player-list"
        if level.lower().replace(" ", "") == "grandslam":
            display_name = f"Grand Slam {city}{suffix}"
        else:
            display_name = f"{level} {city}{suffix}"
        display_name = fix_display_name(display_name)
        surface = tournament.get("surface") or tournament.get("surfaceType") or tournament.get("surfaceCode") or ""

        if week_label not in tournament_groups:
            tournament_groups[week_label] = {}

        tournament_groups[week_label][t_url] = {
            "name": display_name,
            "level": level,
            "surface": surface,
            "country": country,
            "startDate": start_date,
            "endDate": end_date
        }

    return tournament_groups


_WTA_TWO_WEEK_NAMES = [
    'Australian Open', 'Roland Garros', 'Wimbledon', 'US Open',
    'Indian Wells', 'Miami', 'Madrid', 'Rome', 'Internazionali'
]


def _is_two_week_wta(level, raw_name, city, display_name):
    if level.lower().replace(" ", "") == "grandslam":
        return True
    hay = " ".join([raw_name or "", city or "", display_name or ""]).lower()
    return any(n.lower() in hay for n in _WTA_TWO_WEEK_NAMES)


def get_draws_tournament_list():
    """Get WTA tournaments for the draws page.

    Show current + next week. Only include last week if the event is a 2-week tournament.
    """
    today = madrid_today()
    current_monday = today - timedelta(days=today.weekday())
    past_monday = current_monday - timedelta(weeks=1)
    two_weeks_later = current_monday + timedelta(weeks=2)

    raw_tournaments = _fetch_wta_tournaments_raw()
    result = {}

    for tournament in raw_tournaments:
        tournament_id = tournament["tournamentGroup"]["id"]
        raw_name = tournament["tournamentGroup"]["name"]

        clean_name = fix_encoding(raw_name)

        suffix = ""
        if "#" in clean_name:
            parts = clean_name.split("#")
            clean_name = parts[0].strip()
            suffix = " " + parts[1].strip()

        name = clean_name.lower().replace(" ", "-").replace("'", "-")
        if suffix:
            name += "-" + suffix.strip()

        year = tournament["year"]
        level = tournament["level"]
        city = tournament["city"].title()
        start_date = tournament["startDate"]
        end_date = tournament.get("endDate", None)

        monday = get_monday_from_date(start_date)

        week_label = format_week_label(monday)
        t_url = f"https://www.wtatennis.com/tournaments/{tournament_id}/{name}/{year}/player-list"
        if level.lower().replace(" ", "") == "grandslam":
            display_name = f"Grand Slam {city}{suffix}"
        else:
            display_name = f"{level} {city}{suffix}"
        display_name = fix_display_name(display_name)
        is_two_week = _is_two_week_wta(level, raw_name, city, display_name)

        if monday < current_monday:
            if not (monday == past_monday and is_two_week):
                continue
        else:
            if not (monday < two_weeks_later):
                continue

        if week_label not in result:
            result[week_label] = {}

        result[week_label][t_url] = {
            "name": display_name,
            "level": level,
            "startDate": start_date,
            "endDate": end_date
        }

    return result


def get_full_wta_calendar():
    """Get all WTA tournaments from now until end of year for the calendar view."""
    today = madrid_today()
    today_str = today.strftime("%Y-%m-%d")

    raw_tournaments = _fetch_wta_tournaments_raw()

    tournaments = []
    for t in raw_tournaments:
        tournament_id = t["tournamentGroup"]["id"]
        start_date = t["startDate"]
        if start_date < today_str:
            continue

        level = t["level"]
        city = t["city"].title()

        raw_name = t["tournamentGroup"]["name"]
        clean_name = fix_encoding(raw_name)
        suffix = ""
        if "#" in clean_name:
            parts = clean_name.split("#")
            clean_name = parts[0].strip()
            suffix = " " + parts[1].strip()

        if level.lower().replace(" ", "") == "grandslam":
            display_name = f"Grand Slam {city}{suffix}"
        else:
            display_name = f"{level} {city}{suffix}"
        display_name = fix_display_name(display_name)
        surface = t.get("surface") or t.get("surfaceType") or t.get("surfaceCode") or ""
        country = t.get("countryCode") or t.get("hostCountryCode") or ""
        if not country:
            raw_country = t.get("country") or ""
            if len(raw_country) == 3 and raw_country.isupper():
                country = raw_country
            else:
                # Extract 3-letter code from title e.g. "... - City, GBR"
                _m = re.search(r',\s*([A-Z]{3})\s*$', t.get("title", ""))
                country = _m.group(1) if _m else raw_country
        tournaments.append({
            "name": display_name,
            "level": level,
            "surface": surface,
            "country": country,
            "startDate": start_date,
            "endDate": t.get("endDate", None),
            "source": "WTA",
            "tournamentId": tournament_id,
            "calendarKey": f"wta:{tournament_id}",
        })

    return tournaments


def get_rankings(date_str, nationality=None):
    all_players, page = [], 0
    seen_keys = set()
    while True:
        params = {
            "page": page,
            # Larger pages reduce total request count (helps avoid CloudFront/WAF throttling).
            "pageSize": 2000,
            "type": "rankSingles",
            "sort": "asc",
            "metric": "SINGLES",
            "at": date_str
        }

        if nationality:
            params["nationality"] = nationality

        try:
            last_err = None
            data = None
            req_headers = dict(HEADERS or {})
            req_headers.setdefault("Accept", "application/json, text/plain, */*")
            req_headers.setdefault("Accept-Language", "en-US,en;q=0.9")
            req_headers.setdefault("Origin", "https://www.wtatennis.com")
            req_headers.setdefault("Referer", "https://www.wtatennis.com/")
            saw_rate_limit = False
            for attempt in range(_WTA_MAX_ATTEMPTS):
                try:
                    r = _REQUESTS_SESSION.get(API_URL, params=params, headers=req_headers, timeout=30)
                    # Retry on throttling / transient server errors.
                    if r.status_code in (429, 500, 502, 503, 504):
                        saw_rate_limit = saw_rate_limit or (r.status_code == 429)
                        time.sleep(min(_WTA_BACKOFF_MAX_SEC, _WTA_BACKOFF_BASE_SEC * (2 ** attempt)))
                        continue
                    ctype = (r.headers.get("content-type") or "").lower()
                    if "text/html" in ctype:
                        # CloudFront/WAF blocks often come back as HTML.
                        saw_rate_limit = True
                        time.sleep(min(_WTA_BACKOFF_MAX_SEC, _WTA_BACKOFF_BASE_SEC * (2 ** attempt)))
                        continue
                    r.raise_for_status()
                    data = r.json()
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    time.sleep(min(_WTA_BACKOFF_MAX_SEC, _WTA_BACKOFF_BASE_SEC * (2 ** attempt)))
            if last_err is not None and data is None:
                if saw_rate_limit:
                    raise WtaApiRateLimited(f"WTA API rate limited for {date_str} (page {page})")
                if page > 0:
                    raise WtaApiPartialData(
                        f"WTA rankings fetch interrupted for {date_str} at page {page} "
                        f"after {len(all_players)} players: {last_err}"
                    )
                raise WtaApiFetchError(f"WTA rankings fetch failed for {date_str}: {last_err}")
            if isinstance(data, dict):
                items = data.get('content', [])
            elif isinstance(data, list):
                items = data
            else:
                raise WtaApiFetchError(f"WTA rankings returned an unexpected payload for {date_str}")
            if items is None:
                raise WtaApiFetchError(f"WTA rankings payload was missing content for {date_str}")
            if not isinstance(items, list):
                raise WtaApiFetchError(f"WTA rankings content was not a list for {date_str}")
            if not items: break
            # Defensive de-dup in case the API repeats pages (seen in the wild).
            new_items = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                player = it.get("player") or {}
                key = (
                    player.get("id")
                    or player.get("fullName")
                    or (it.get("ranking"), player.get("countryCode"), player.get("dateOfBirth"))
                )
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                new_items.append(it)
            if not new_items:
                break
            all_players.extend(new_items)
            page += 1
            time.sleep(0.25)
        except (WtaApiRateLimited, WtaApiFetchError, WtaApiPartialData):
            raise
        except Exception as e:
            if page > 0:
                raise WtaApiPartialData(
                    f"WTA rankings fetch interrupted for {date_str} at page {page} "
                    f"after {len(all_players)} players: {e}"
                )
            raise WtaApiFetchError(f"WTA rankings fetch failed for {date_str}: {e}")

    ranking_results = []
    for p in all_players:
        if not p.get('player'): continue
        player_obj = p.get("player") or {}
        player_id = (
            player_obj.get("id")
            or player_obj.get("playerId")
            or p.get("playerId")
            or p.get("id")
        )
        official_name = (p.get('player', {}).get('fullName') or '').strip()
        official_upper = official_name.upper()
        display_name = WTA_ID_TO_DISPLAY.get(str(player_id or ""), NAME_LOOKUP.get(official_upper, official_upper))
        ranking_results.append({
            "Player": display_name,
            "OfficialPlayer": official_name,
            "Id": player_id,
            "Rank": p.get('ranking'),
            "Country": _normalize_country_code(p.get('player', {}).get('countryCode', '')),
            "Key": display_name,
            "Points": p.get('points', 0),
            "DOB": p.get('player', {}).get('dateOfBirth', '')
        })
    return ranking_results


class WtaRankingsCsvStore(MutableMapping):
    """Lazy date-to-rankings mapping backed by byte ranges in the source CSVs.

    The old cache retained more than two million row dictionaries. This index
    retains only one small entry per week and materializes at most a handful of
    requested weeks through the bounded LRU cache.
    """

    def __init__(self, csv_files):
        self._sources = []
        self._date_index = {}
        self._overrides = {}
        self._date_cache = OrderedDict()
        for csv_file in csv_files:
            path = _os.path.abspath(_os.fspath(csv_file))
            if _os.path.exists(path):
                self._index_source(path)

    def _index_source(self, path):
        source_index = len(self._sources)
        with open(path, "rb") as handle:
            header_line = handle.readline()
            if not header_line:
                raise ValueError(f"ranking CSV is empty: {path}")
            header = next(_csv.reader([header_line.decode("utf-8-sig")]))
            if "week_date" not in header:
                raise ValueError(f"ranking CSV has no week_date column: {path}")
            week_date_index = header.index("week_date")
            source = {"path": path, "header": header, "date_index": week_date_index}
            self._sources.append(source)

            current_date = None
            range_start = None
            range_end = None
            while True:
                row_start = handle.tell()
                raw_line = handle.readline()
                if not raw_line:
                    break
                row_end = handle.tell()
                if not raw_line.strip():
                    continue
                text = raw_line.decode("utf-8")
                values = next(_csv.reader([text]))
                if len(values) != len(header):
                    raise ValueError(
                        f"ranking CSV contains a multiline or malformed row at byte {row_start}: {path}"
                    )
                date_str = values[week_date_index].strip()
                if date_str != current_date:
                    if current_date is not None:
                        self._register_range(current_date, source_index, range_start, range_end)
                    current_date = date_str
                    range_start = row_start
                range_end = row_end

            if current_date is not None:
                self._register_range(current_date, source_index, range_start, range_end)

    def _register_range(self, date_str, source_index, start, end):
        existing = self._date_index.get(date_str)
        if existing is None:
            self._date_index[date_str] = {"source": source_index, "ranges": [(start, end)]}
        elif existing["source"] == source_index:
            existing["ranges"].append((start, end))
        # A higher-priority file already owns this date; match the previous
        # decade-loading behavior by ignoring the lower-priority duplicate.

    @staticmethod
    def _player_from_row(row):
        pid = (row.get("id") or row.get("player_id") or row.get("playerId") or "").strip()
        official_name = (row.get("player") or "").strip()
        official_upper = official_name.upper()
        display_upper = WTA_ID_TO_DISPLAY.get(pid, NAME_LOOKUP.get(official_upper, official_upper))
        return {
            "Player": display_upper,
            "OfficialPlayer": official_upper,
            "Id": pid,
            "Rank": int(row["rank"]) if row.get("rank") else None,
            "Country": _normalize_country_code(row.get("country", "")),
            "Key": display_upper,
            "Points": int(row["points"]) if row.get("points") else 0,
            "DOB": row.get("dob", ""),
        }

    def _read_date_uncached(self, date_str):
        entry = self._date_index[date_str]
        source = self._sources[entry["source"]]
        header = source["header"]
        players = []
        with open(source["path"], "rb") as handle:
            for start, end in entry["ranges"]:
                handle.seek(start)
                while handle.tell() < end:
                    raw_line = handle.readline()
                    if not raw_line:
                        break
                    if not raw_line.strip():
                        continue
                    values = next(_csv.reader([raw_line.decode("utf-8")]))
                    if len(values) != len(header):
                        raise ValueError(f"ranking CSV row width changed while reading {source['path']}")
                    players.append(self._player_from_row(dict(zip(header, values))))
        return players

    def _read_date(self, date_str):
        cached = self._date_cache.get(date_str)
        if cached is not None:
            self._date_cache.move_to_end(date_str)
            return cached
        players = self._read_date_uncached(date_str)
        self._date_cache[date_str] = players
        if len(self._date_cache) > 8:
            self._date_cache.popitem(last=False)
        return players

    def __getitem__(self, date_str):
        if date_str in self._overrides:
            return self._overrides[date_str]
        return self._read_date(date_str)

    def __setitem__(self, date_str, players):
        self._overrides[date_str] = players
        self._date_cache.clear()

    def __delitem__(self, date_str):
        removed = False
        if date_str in self._overrides:
            del self._overrides[date_str]
            removed = True
        if date_str in self._date_index:
            del self._date_index[date_str]
            removed = True
        self._date_cache.clear()
        if not removed:
            raise KeyError(date_str)

    def __iter__(self):
        return iter(dict.fromkeys((*self._date_index, *self._overrides)))

    def __len__(self):
        return len(set(self._date_index) | set(self._overrides))


_wta_csv_cache = None
_wta_csv_cache_paths = None


def _ranking_csv_paths(data_dir=None):
    if data_dir is None:
        return (
            WTA_RANKINGS_CSV,
            WTA_RANKINGS_CSV_10_19,
            WTA_RANKINGS_CSV_00_09,
            WTA_RANKINGS_CSV_83_99,
        )
    return tuple(
        _os.path.join(_os.fspath(data_dir), filename)
        for filename in (
            "wta_rankings_20_29.csv",
            "wta_rankings_10_19.csv",
            "wta_rankings_00_09.csv",
            "wta_rankings_83_99.csv",
        )
    )


def _load_wta_csv(data_dir=None):
    global _wta_csv_cache, _wta_csv_cache_paths
    paths = tuple(_os.path.abspath(path) for path in _ranking_csv_paths(data_dir))
    if _wta_csv_cache is None or _wta_csv_cache_paths != paths:
        _wta_csv_cache = WtaRankingsCsvStore(paths)
        _wta_csv_cache_paths = paths
    return _wta_csv_cache


def _save_wta_csv_date(date_str, players):
    """Merge a new ranking week and atomically replace the decade CSV."""
    if not players:
        return
    columns = ["week_date", "id", "rank", "points", "player", "country", "dob"]

    def merged_rows():
        if _os.path.exists(WTA_RANKINGS_CSV):
            with open(WTA_RANKINGS_CSV, encoding="utf-8-sig", newline="") as handle:
                reader = _csv.DictReader(handle)
                if reader.fieldnames is None:
                    raise ValueError(f"ranking CSV has no header: {WTA_RANKINGS_CSV}")
                yield from reader
        for player in players:
            yield {
                "week_date": date_str,
                "id": player.get("Id", ""),
                "rank": player.get("Rank", ""),
                "points": player.get("Points", 0),
                "player": (player.get("OfficialPlayer") or player.get("Player") or "").strip(),
                "country": player.get("Country", ""),
                "dob": player.get("DOB", ""),
            }

    atomic_write_csv(WTA_RANKINGS_CSV, columns, merged_rows(), encoding="utf-8")


def get_wta_rankings_cached(date_str, nationality=None, *, with_status=False):
    """Get WTA rankings from CSV/API, marking fallback data when requested."""
    def _finish(players, status):
        return (players, status) if with_status else players

    def _filter(players):
        if nationality:
            return [p for p in players if p.get("Country") == nationality]
        return players

    csv_data = _load_wta_csv()

    if date_str in csv_data:
        players = _filter(csv_data[date_str])
        return _finish(players, make_data_status(
            "WTA rankings",
            "fresh",
            requested=date_str,
            effective=date_str,
            row_count=len(players),
            reason="Exact cached rankings date available.",
        ))

    # Date not in CSV — fetch from API, save to CSV, and keep in memory
    new_data = []
    fetch_error = None
    try:
        new_data = get_rankings(date_str, nationality=nationality)
    except (WtaApiRateLimited, WtaApiFetchError, WtaApiPartialData) as e:
        fetch_error = e
        logger.warning(f"Warning: WTA rankings refresh failed for {date_str}: {e}")
    if new_data:
        csv_data[date_str] = new_data
        _save_wta_csv_date(date_str, new_data)
        players = _filter(new_data)
        return _finish(players, make_data_status(
            "WTA rankings",
            "fresh",
            requested=date_str,
            effective=date_str,
            fetched_at=utc_now_iso(),
            row_count=len(players),
            reason="Live rankings refreshed successfully.",
        ))

    # Fallback: use the latest available date in the CSV
    if csv_data:
        latest_key = sorted(csv_data.keys())[-1]
        players = _filter(csv_data.get(latest_key, []))
        reason = (
            "Live rankings refresh failed; showing latest cached rankings."
            if fetch_error else
            "No live rankings were returned for the requested date; showing latest cached rankings."
        )
        return _finish(players, make_data_status(
            "WTA rankings",
            "stale",
            requested=date_str,
            effective=latest_key,
            row_count=len(players),
            stale=True,
            reason=reason,
        ))

    reason = (
        "Live rankings refresh failed and no cached WTA rankings were available."
        if fetch_error else
        "No WTA rankings were available for the requested date."
    )
    return _finish([], make_data_status(
        "WTA rankings",
        "error",
        requested=date_str,
        row_count=0,
        stale=True,
        reason=reason,
    ))


def fetch_player_info(player_id):
    url = f"https://api.wtatennis.com/tennis/players/{player_id}/matches"
    params = {"page": 0, "pageSize": 1, "sort": "desc"}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
        "referer": "https://www.wtatennis.com/",
        "account": "wta"
    }
    try:
        r = get_with_retry(
            url,
            component="wta",
            attempts=3,
            params=params,
            headers=headers,
            timeout=(10, 20),
            failure_status="degraded",
        )
        data = r.json()
        player = data.get("player", {})
        name = player.get("fullName")
        country = _normalize_country_code(player.get("countryCode"))
        if name:
            return {"name": name, "country": country}
    except PipelineError:
        return None
    except Exception as exc:
        report_run_issue(
            "wta", "fetch player profile", exc, severity="degraded",
            context={"player_id": str(player_id)},
        )
    return None


def scrape_tournament_players(url, md_rankings, qual_rankings, cached_entries=None):
    try:
        r = get_with_retry(
            url,
            component="wta",
            attempts=3,
            headers=HEADERS,
            timeout=(10, 25),
            failure_status="degraded",
        )
        soup = BeautifulSoup(r.text, 'html.parser')
    except PipelineError as e:
        logger.error(f"Error scraping {url}: {e}")
        return [], {}
    except Exception as e:
        report_run_issue(
            "wta", "parse tournament player list", e, severity="degraded",
            context={"url": str(url)},
        )
        logger.debug(f"Error scraping {url}: {e}")
        return [], {}

    def _suffix_map_from_players(players):
        suffix_map = {}
        for p in players or []:
            name = str(p.get("name") or "").strip().upper()
            if not name:
                continue
            p_type = p.get("type", "")
            if p_type == "MAIN":
                suffix_map[name] = ""
            elif p_type == "QUAL":
                suffix_map[name] = " (Q)"
            else:
                pos = str(p.get("pos") or "").strip()
                suffix_map[name] = f" (ALT {pos})" if pos else " (ALT)"
        return suffix_map

    def _extract_player_list_state_from_jsonld(soup_obj):
        """Return a lightweight view of the player-list JSON-LD if present."""
        import json as _json

        state = {
            "has_player_list": False,
            "singles_count": None,
            "qualifying_count": None,
            "doubles_count": None,
        }

        for tag in soup_obj.find_all("script", attrs={"type": "application/ld+json"}):
            raw = (tag.string or tag.get_text() or "").strip()
            if not raw or "player-list" not in raw or "subEvent" not in raw:
                continue
            try:
                data = _json.loads(raw)
            except Exception:
                continue

            if not isinstance(data, dict):
                continue
            if str(data.get("@type", "")).lower() != "sportsevent":
                continue
            event_id = str(data.get("@id", "")).lower()
            if "player-list" not in event_id:
                continue

            sub_events = data.get("subEvent") or []
            if not isinstance(sub_events, list):
                continue
            state["has_player_list"] = True
            for sub_event in sub_events:
                if not isinstance(sub_event, dict):
                    continue
                name = str(sub_event.get("name") or "").strip().lower()
                performers = sub_event.get("performer") or []
                count = len(performers) if isinstance(performers, list) else 0
                if "singles" in name:
                    state["singles_count"] = count
                elif "qualifying" in name:
                    state["qualifying_count"] = count
                elif "doubles" in name:
                    state["doubles_count"] = count

        return state

    jsonld_state = _extract_player_list_state_from_jsonld(soup)
    if jsonld_state.get("has_player_list"):
        singles_count = jsonld_state.get("singles_count")
        qual_count = jsonld_state.get("qualifying_count")
        doubles_count = jsonld_state.get("doubles_count")
        # When the live page only exposes doubles or hides the singles/qualifying
        # performers entirely, keep the last saved singles/qualifying list instead
        # of overwriting the cache with an empty or doubles-only result.
        if (singles_count == 0 and (qual_count in (0, None)) and (doubles_count or 0) > 0):
            if cached_entries:
                logger.warning(f"Using cached WTA entry list for {url} (live page shows doubles only)")
                return list(cached_entries), _suffix_map_from_players(cached_entries)
            return [], {}

    # 1. Read player IDs/slugs from HTML
    main_entries, qual_entries = [], []
    main_seen, qual_seen = set(), set()
    current_state = "MAIN"

    for tag in soup.find_all(True):
        ui_tab = tag.get('data-ui-tab', '').lower()

        if "qualifying" in ui_tab:
            current_state = "QUAL"
        elif "doubles" in ui_tab:
            current_state = "IGNORE"
        if current_state == "IGNORE":
            continue

        href = tag.get('href', '')
        m = re.match(r'/players/(\d+)/([^/]+)', href)
        if m:
            pid, slug = m.group(1), m.group(2)
            if current_state == "MAIN" and pid not in main_seen:
                main_seen.add(pid)
                main_entries.append((pid, slug))
            elif current_state == "QUAL" and pid not in qual_seen:
                qual_seen.add(pid)
                qual_entries.append((pid, slug))

    # Build cache lookup from previous run
    cached_lookup = {}
    cached_lookup_by_id = {}
    for entry in (cached_entries or []):
        cached_lookup[entry["name"].strip().upper()] = entry
        cached_id = str(entry.get("player_id") or "").strip()
        if cached_id:
            cached_lookup_by_id[cached_id] = entry

    # Build ranked names set for quick lookup
    ranked_names = set()
    for rank_list in [md_rankings, qual_rankings]:
        for item in rank_list:
            if item.get("Player"):
                ranked_names.add(item["Player"].strip().upper())

    # 2-3. Resolve each player: cache first, then rankings, then API
    player_cache = {}
    seen_pids = set()
    for pid, slug in main_entries + qual_entries:
        if pid in seen_pids:
            continue
        seen_pids.add(pid)
        candidate = slug.replace("-", " ").upper()
        mapped = resolve_player_display_name("wta", player_id=pid, name=candidate).upper()

        if pid in cached_lookup_by_id:
            cached = cached_lookup_by_id[pid]
            player_cache[pid] = {"name": mapped, "country": cached.get("country")}
        elif mapped in cached_lookup:
            player_cache[pid] = {"name": mapped, "country": cached_lookup[mapped].get("country")}
        elif candidate in cached_lookup:
            player_cache[pid] = {"name": candidate, "country": cached_lookup[candidate].get("country")}
        elif candidate in ranked_names:
            player_cache[pid] = {"name": candidate, "country": None}
        elif mapped in ranked_names:
            player_cache[pid] = {"name": mapped, "country": None}
        else:
            info = fetch_player_info(pid)
            if info:
                player_cache[pid] = info
            time.sleep(0.05)

    # 4. Fill the table
    main_draw_names = set()
    qualifying_names = set()

    def parse_rank_num(value):
        try:
            return int(str(value).strip())
        except Exception:
            return 9999

    def get_p_rank(name, rank_list):
        return next((item for item in rank_list if item["Player"] == name), {"Rank": 9999, "Country": "-"})

    md_list = []
    for pid, slug in main_entries:
        if pid not in player_cache:
            continue
        p_info = player_cache[pid]
        name_key = p_info["name"].strip().upper()
        matched_name = resolve_player_display_name("wta", player_id=pid, name=name_key).upper()
        main_draw_names.add(matched_name)
        rank_info = get_p_rank(matched_name, md_rankings)
        md_list.append({
            "name": format_player_name(matched_name),
            "country": rank_info["Country"] if rank_info["Country"] != "-" else (_normalize_country_code(p_info.get("country")) or "-"),
            "rank_num": rank_info["Rank"],
            "rank": f"{rank_info['Rank']}" if rank_info['Rank'] < 9999 else "-",
            "type": "MAIN",
            "player_id": pid,
        })

    # Some WTA pages temporarily expose only Qualifying. In that case, keep MAIN from cache.
    if not md_list and qual_entries and cached_entries:
        cached_main = [p for p in cached_entries if p.get("type") == "MAIN"]
        for p in cached_main:
            name_key = (p.get("name") or "").strip().upper()
            if not name_key:
                continue
            cached_pid = str(p.get("player_id") or "").strip()
            matched_name = resolve_player_display_name(
                "wta", player_id=cached_pid, name=name_key
            ).upper()
            main_draw_names.add(matched_name)
            rank_num = p.get("rank_num")
            if not isinstance(rank_num, int):
                rank_num = parse_rank_num(p.get("rank"))
            rank_value = p.get("rank")
            rank_display = str(rank_value).strip() if rank_value not in (None, "") else (str(rank_num) if rank_num < 9999 else "-")
            md_list.append({
                "name": format_player_name(matched_name),
                "country": _normalize_country_code(p.get("country")) or "-",
                "rank_num": rank_num,
                "rank": rank_display if rank_num < 9999 else "-",
                "type": "MAIN",
                "player_id": cached_pid,
            })

    md_list.sort(key=lambda x: (x["rank_num"], x["name"]))
    for idx, p in enumerate(md_list, 1):
        p["pos"] = str(idx)
        p["pos_num"] = idx

    qual_list = []
    for pid, slug in qual_entries:
        if pid not in player_cache:
            continue
        p_info = player_cache[pid]
        name_key = p_info["name"].strip().upper()
        matched_name = resolve_player_display_name("wta", player_id=pid, name=name_key).upper()
        qualifying_names.add(matched_name)
        rank_info = get_p_rank(matched_name, qual_rankings)
        qual_list.append({
            "name": format_player_name(matched_name),
            "country": rank_info["Country"] if rank_info["Country"] != "-" else (_normalize_country_code(p_info.get("country")) or "-"),
            "rank_num": rank_info["Rank"],
            "rank": f"{rank_info['Rank']}" if rank_info['Rank'] < 9999 else "-",
            "type": "QUAL",
            "player_id": pid,
        })
    qual_list.sort(key=lambda x: (x["rank_num"], x["name"]))
    for idx, p in enumerate(qual_list, 1):
        p["pos"] = str(idx)
        p["pos_num"] = idx

    final_tourney_list = md_list + qual_list

    suffix_map = {p: "" for p in main_draw_names}
    suffix_map.update({p: " (Q)" for p in qualifying_names})

    return final_tourney_list, suffix_map
