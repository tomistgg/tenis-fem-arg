import csv
import json
import os
import re
import sys
import time
from datetime import timedelta
from pathlib import Path

# Allow imports from the project root when invoked as `python populate_data/wta_load_new.py`.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from canonical_data import source_match_key, sync_itf_players, sync_wta_match_players
from http_client import get_with_retry
from pipeline_errors import PipelineError
from run_state import report_run_issue
from runtime_logging import get_logger
from runtime_paths import DATA_DIR as RUNTIME_DATA_DIR
from time_utils import madrid_today, parse_utc_timestamp, utc_now, utc_timestamp
from transactional_io import atomic_write_csv
from utils import (
    dumps_wta_full_calendar_cache,
    expand_wta_calendar_cache,
    get_cache_timestamp,
    save_json_file,
    set_cache_file_meta,
)

logger = get_logger("wta-loader")

MATCHES_URL = "https://api.wtatennis.com/tennis/tournaments/{tournament_id}/{year}/matches?states=C"
CALENDAR_URL = "https://api.wtatennis.com/tennis/tournaments/?page={page}&pageSize=100&excludeLevels=ITF%2C+Grand%20Slam&from={from_date}&to={to_date}"

HEADERS = {
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

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = str(RUNTIME_DATA_DIR)
OUTPUT_FILE = os.path.join(DATA_DIR, "wta_matches_arg.csv")
WTA_CALENDAR_CACHE_FILE = os.path.join(DATA_DIR, "wta_calendar_cache.json")
_WTA_FULL_CALENDAR_CACHE_FILE = os.path.join(DATA_DIR, "wta_full_calendar_cache.json")
_WTA_FULL_CALENDAR_TTL = 3 * 60 * 60  # 3 hours


def _load_from_full_calendar_cache(from_date, to_date):
    """Read the year-wide cache written by main.py/wta.py, filtered to the needed window.

    Grand Slams are excluded here since wta_load_new.py handles only WTA-circuit matches.
    Returns a list of tournament dicts, or None if cache is missing/stale.
    """
    try:
        with open(_WTA_FULL_CALENDAR_CACHE_FILE, encoding="utf-8") as f:
            data = expand_wta_calendar_cache(json.load(f))
        fetched_at_str = get_cache_timestamp(_WTA_FULL_CALENDAR_CACHE_FILE, payload=data)
        if not fetched_at_str:
            return None
        fetched_at = parse_utc_timestamp(fetched_at_str)
        if (utc_now() - fetched_at).total_seconds() > _WTA_FULL_CALENDAR_TTL:
            return None
        if data.get("from", "") > from_date or data.get("to", "") < to_date:
            return None
        return [
            t
            for t in (data.get("items") or [])
            if from_date <= (t.get("startDate") or "")[:10] <= to_date and t.get("level") != "Grand Slam"
        ]
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        report_run_issue(
            "wta-loader",
            "load full calendar cache",
            exc,
            severity="degraded",
            context={"path": _WTA_FULL_CALENDAR_CACHE_FILE},
        )
        return None


def _load_from_window_calendar_cache(from_date, to_date):
    """Read the exact window cache written by this script, if it is still fresh."""
    try:
        with open(WTA_CALENDAR_CACHE_FILE, encoding="utf-8") as f:
            data = expand_wta_calendar_cache(json.load(f))
        fetched_at_str = get_cache_timestamp(WTA_CALENDAR_CACHE_FILE, payload=data)
        if not fetched_at_str:
            return None
        fetched_at = parse_utc_timestamp(fetched_at_str)
        if (utc_now() - fetched_at).total_seconds() > _WTA_FULL_CALENDAR_TTL:
            return None
        if data.get("from", "") > from_date or data.get("to", "") < to_date:
            return None
        return [
            t
            for t in (data.get("items") or [])
            if from_date <= (t.get("startDate") or "")[:10] <= to_date and t.get("level") != "Grand Slam"
        ]
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        report_run_issue(
            "wta-loader",
            "load window calendar cache",
            exc,
            severity="degraded",
            context={"path": WTA_CALENDAR_CACHE_FILE},
        )
        return None


CSV_COLUMNS = [
    "matchType",
    "matchId",
    "date",
    "tournamentId",
    "tournamentName",
    "tournamentCategory",
    "surface",
    "inOrOutdoor",
    "tournamentCountry",
    "roundName",
    "draw",
    "result",
    "resultStatusDesc",
    "winnerId",
    "winnerEntry",
    "winnerSeed",
    "winnerName",
    "winnerCountry",
    "loserId",
    "loserEntry",
    "loserSeed",
    "loserName",
    "loserCountry",
]


MAIN_DRAW_ROUND_MAP = {
    "1": "1st Round",
    "2": "2nd Round",
    "3": "3rd Round",
    "4": "4th Round",
    "5": "5th Round",
    "Q": "Quarter-finals",
    "S": "Semi-finals",
    "F": "Final",
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


def _map_round(raw_round, draw_level, q_map):
    raw_round = str(raw_round)
    if draw_level == "Q":
        return q_map.get(raw_round, raw_round) if q_map else raw_round
    return MAIN_DRAW_ROUND_MAP.get(raw_round, raw_round)


def get_week_boundaries(today=None):
    if today is None:
        today = madrid_today()
    week_start = today - timedelta(days=today.weekday())  # Monday
    prev_week_start = week_start - timedelta(days=7)
    next_week_end = week_start + timedelta(days=13)  # Sunday of next week
    return prev_week_start, next_week_end


def fetch_tournaments_for_range(from_date, to_date):
    all_tournaments = []
    page = 0
    while True:
        url = CALENDAR_URL.format(page=page, from_date=from_date, to_date=to_date)
        data = fetch_json(url)
        page_content = data.get("content", [])
        all_tournaments.extend(page_content)

        if data.get("last", True) or not page_content:
            break
        page += 1

    return all_tournaments


def build_meta(t):
    title = t.get("title", "")
    country = t.get("country", "")
    name = title[: -len(f", {country}")] if country and title.endswith(f", {country}") else title
    return {
        "tournamentName": name,
        "tournamentCategory": t.get("level", ""),
        "surface": t.get("surface", ""),
        "inOrOutdoor": t.get("inOutdoor", ""),
        "tournamentCountry": country,
    }


def format_score(score_string):
    if not score_string:
        return ""
    stripped = score_string.strip()
    if stripped == "W/O":
        return "W/O"
    normalized = score_string.replace(",", " ").replace("Ret'd", "ret.").replace("ret'd", "ret.")
    # Convert compact set notation like "60" to "6-0".
    return re.sub(r"(?<!\S)(\d{2})(?!\S)", lambda m: f"{m.group(1)[0]}-{m.group(1)[1]}", normalized)


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

    if winner in ("2", "4", "6"):
        w_id = m.get("PlayerIDA", "")
        w_entry = m.get("EntryTypeA", "").upper()
        w_seed = m.get("SeedA", "")
        w_name = f"{m.get('PlayerNameFirstA', '')} {m.get('PlayerNameLastA', '')}".strip()
        w_country = m.get("PlayerCountryA", "")
        l_id = m.get("PlayerIDB", "")
        l_entry = m.get("EntryTypeB", "").upper()
        l_seed = m.get("SeedB", "")
        l_name = f"{m.get('PlayerNameFirstB', '')} {m.get('PlayerNameLastB', '')}".strip()
        l_country = m.get("PlayerCountryB", "")
    else:
        w_id = m.get("PlayerIDB", "")
        w_entry = m.get("EntryTypeB", "").upper()
        w_seed = m.get("SeedB", "")
        w_name = f"{m.get('PlayerNameFirstB', '')} {m.get('PlayerNameLastB', '')}".strip()
        w_country = m.get("PlayerCountryB", "")
        l_id = m.get("PlayerIDA", "")
        l_entry = m.get("EntryTypeA", "").upper()
        l_seed = m.get("SeedA", "")
        l_name = f"{m.get('PlayerNameFirstA', '')} {m.get('PlayerNameLastA', '')}".strip()
        l_country = m.get("PlayerCountryA", "")

    timestamp = m.get("MatchTimeStamp", "")
    date = timestamp[:10] if timestamp else ""

    result = format_score(m.get("ScoreString", ""))
    status_desc = get_status_desc(result)

    if winner in ("6", "7"):
        result = "W/O"
        status_desc = "Walkover"

    return {
        "matchType": "WTA",
        "matchId": m.get("MatchID", ""),
        "date": date,
        "tournamentId": m.get("EventID", ""),
        "tournamentName": meta["tournamentName"],
        "tournamentCategory": meta["tournamentCategory"],
        "surface": meta["surface"],
        "inOrOutdoor": meta["inOrOutdoor"],
        "tournamentCountry": meta["tournamentCountry"],
        "roundName": _map_round(m.get("RoundID", ""), m.get("DrawLevelType", ""), q_map),
        "draw": m.get("DrawLevelType", ""),
        "result": result,
        "resultStatusDesc": status_desc,
        "winnerId": w_id,
        "winnerEntry": w_entry,
        "winnerSeed": w_seed,
        "winnerName": w_name,
        "winnerCountry": w_country,
        "loserId": l_id,
        "loserEntry": l_entry,
        "loserSeed": l_seed,
        "loserName": l_name,
        "loserCountry": l_country,
    }


def fetch_matches(tournament_id, year):
    url = MATCHES_URL.format(tournament_id=tournament_id, year=year)
    return fetch_json(url).get("matches", [])


def fetch_json(url):
    response = get_with_retry(url, component="wta-loader", headers=HEADERS)
    raw = response.content
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return json.loads(text)


def load_existing_match_ids(output_file):
    if not os.path.exists(output_file):
        return set()
    ids = set()
    with open(output_file, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ids.add(source_match_key(row, "wta"))
    return ids


def append_to_csv(new_rows, output_file):
    """Merge WTA rows by natural key and replace the whole CSV atomically."""
    rows = []
    if os.path.exists(output_file):
        with open(output_file, newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise ValueError(f"WTA match CSV has no header: {output_file}")
            rows.extend(reader)
    keyed_rows = {source_match_key(row, "wta"): row for row in rows}
    for row in new_rows:
        keyed_rows[source_match_key(row, "wta")] = row
    atomic_write_csv(output_file, CSV_COLUMNS, keyed_rows.values())


if __name__ == "__main__":
    from pipeline_transaction import run_current_script_transaction, transaction_is_active

    if not transaction_is_active():
        raise SystemExit(run_current_script_transaction(__file__))

    today = madrid_today()
    range_start, range_end = get_week_boundaries(today)
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)

    from_date_str = range_start.strftime("%Y-%m-%d")
    to_date_str = range_end.strftime("%Y-%m-%d")
    tournaments = _load_from_window_calendar_cache(from_date_str, to_date_str)
    if tournaments is None:
        tournaments = _load_from_full_calendar_cache(from_date_str, to_date_str)
    if tournaments is not None:
        logger.debug(f"  Using WTA calendar cache ({len(tournaments)} tournaments in window).")
    else:
        tournaments = fetch_tournaments_for_range(from_date_str, to_date_str)

    if not tournaments:
        raise PipelineError(
            component="wta-loader",
            operation="fetch tournament window",
            message="WTA tournament source returned no rows",
            context={"from": from_date_str, "to": to_date_str},
        )

    save_json_file(
        WTA_CALENDAR_CACHE_FILE,
        {
            "from": from_date_str,
            "to": to_date_str,
            "items": tournaments,
        },
        formatter=dumps_wta_full_calendar_cache,
    )
    set_cache_file_meta(
        WTA_CALENDAR_CACHE_FILE,
        fetchedAt=utc_timestamp(),
        **{"from": from_date_str, "to": to_date_str},
    )

    existing_ids = load_existing_match_ids(OUTPUT_FILE)
    new_rows = []

    for t in tournaments:
        t_id = t.get("tournamentGroup", {}).get("id")
        t_name = t.get("title", "")
        if not t_id:
            continue

        # Determine tournament year from startDate
        start_date = t.get("startDate", "")
        t_year = int(start_date[:4]) if start_date else today.year

        try:
            raw_matches = fetch_matches(t_id, t_year)
        except PipelineError as e:
            logger.warning(f"  [!] Failed to fetch matches for {t_name} ({t_id}): {e}")
            continue

        arg_matches = [
            m
            for m in raw_matches
            if m.get("MatchState") == "F"
            and m.get("DrawMatchType") == "S"
            and (m.get("PlayerCountryA") == "ARG" or m.get("PlayerCountryB") == "ARG")
        ]

        if arg_matches:
            meta = build_meta(t)
            q_map = build_q_round_map(raw_matches)
            for m in arg_matches:
                row = parse_match(m, meta, q_map)
                key = source_match_key(row, "wta")
                if key not in existing_ids:
                    new_rows.append(row)
                    existing_ids.add(key)

        time.sleep(0.3)

    if new_rows:
        player_table = Path(DATA_DIR) / "player_aliases_wta_itf.json"
        added_players = sync_itf_players(player_table, new_rows)
        added_players += sync_wta_match_players(player_table, new_rows)
        if added_players:
            logger.info(f"Added {added_players} new ITF identities to the canonical player table.")
        append_to_csv(new_rows, OUTPUT_FILE)
