"""Compute tournament strength for WTA tournaments."""

import csv
import json
import math
import os
import time
from datetime import date, datetime, timedelta

from utils import (
    normalize_player_name,
    save_json_file,
    compress_tstrength_cache,
    expand_tstrength_cache,
)
from calendar_builder import get_previous_monday
from time_utils import madrid_today
from runtime_paths import DATA_DIR as RUNTIME_DATA_DIR
from pipeline_errors import DataValidationError
from pipeline_errors import PipelineError
from run_state import report_run_issue
from http_client import get_with_retry

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = str(RUNTIME_DATA_DIR)
RANKINGS_CSV = os.path.join(DATA_DIR, "wta_rankings_20_29.csv")
TSTRENGTH_CACHE = os.path.join(DATA_DIR, "tstrength_cache.json")

_WTA_API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "accept": "application/json",
    "referer": "https://www.wtatennis.com/",
    "account": "wta",
}

DEFAULT_RANK = 2000

_IGNORE_TOURNAMENT_NAMES = {
    "UNITED CUP",
}


def _is_ignored_tournament(name):
    if not name:
        return False
    norm = str(name).strip().upper()
    return norm in _IGNORE_TOURNAMENT_NAMES


# Map country codes to regions
_REGION_MAP = {
    # North America
    "USA": "North America", "CAN": "North America", "MEX": "North America",
    # Central America & Caribbean
    "CRC": "Central America", "PAN": "Central America", "DOM": "Caribbean",
    "PUR": "Caribbean", "JAM": "Caribbean", "CUB": "Caribbean",
    # South America
    "ARG": "South America", "BRA": "South America", "CHI": "South America",
    "COL": "South America", "PER": "South America", "ECU": "South America",
    "URU": "South America", "VEN": "South America", "PAR": "South America",
    "BOL": "South America",
    # Europe
    "GBR": "Europe", "FRA": "Europe", "GER": "Europe", "ESP": "Europe",
    "ITA": "Europe", "NED": "Europe", "BEL": "Europe", "SUI": "Europe",
    "AUT": "Europe", "CZE": "Europe", "POL": "Europe", "ROU": "Europe",
    "HUN": "Europe", "SVK": "Europe", "CRO": "Europe", "SRB": "Europe",
    "SLO": "Europe", "BUL": "Europe", "GRE": "Europe", "POR": "Europe",
    "SWE": "Europe", "NOR": "Europe", "DEN": "Europe", "FIN": "Europe",
    "IRL": "Europe", "RUS": "Europe", "UKR": "Europe", "BLR": "Europe",
    "LTU": "Europe", "LAT": "Europe", "EST": "Europe", "LUX": "Europe",
    "MON": "Europe", "MNE": "Europe", "BIH": "Europe", "MKD": "Europe",
    "ALB": "Europe", "GEO": "Europe", "ARM": "Europe", "CYP": "Europe",
    "MLT": "Europe", "ISR": "Europe", "TUR": "Europe",
    # Asia
    "CHN": "Asia", "JPN": "Asia", "KOR": "Asia", "TPE": "Asia",
    "HKG": "Asia", "THA": "Asia", "IND": "Asia", "KAZ": "Asia",
    "UZB": "Asia", "MAS": "Asia", "SGP": "Asia", "INA": "Asia",
    "PHI": "Asia", "VIE": "Asia", "MYA": "Asia",
    # Middle East
    "UAE": "Middle East", "QAT": "Middle East", "KSA": "Middle East",
    "BRN": "Middle East", "KUW": "Middle East", "OMA": "Middle East",
    # Oceania
    "AUS": "Oceania", "NZL": "Oceania",
    # Africa
    "RSA": "Africa", "MAR": "Africa", "TUN": "Africa", "EGY": "Africa",
    "NGR": "Africa", "KEN": "Africa",
}




def _resolve_ranking_week(start_date, draw, rankings_index, available_weeks):
    """Resolve the ranking week to use for a tournament draw.

    Qualifying often starts on Sunday, so using the tournament's Monday start date
    can point to a ranking week that isn't published yet. For Q draw we anchor on
    Sunday (start date - 1 day), then fall back to the latest available ranking
    week <= desired week.
    """
    try:
        dt = datetime.strptime((start_date or "")[:10], "%Y-%m-%d")
    except ValueError:
        return get_previous_monday(start_date)

    if draw == "Q":
        dt = dt - timedelta(days=1)

    desired_week = (dt - timedelta(days=dt.weekday())).strftime("%Y-%m-%d")
    if rankings_index.get(desired_week):
        return desired_week

    for week in reversed(available_weeks):
        if week <= desired_week and rankings_index.get(week):
            return week

    return available_weeks[-1] if available_weeks else desired_week


def _load_rankings_index():
    """Load all rankings into a dict: {week_date: {normalized_name: rank}}.

    Also builds partial-name entries (first name + first last name) as fallback
    for players with multiple last names (e.g. "Irene Burillo" for "Irene Burillo Escorihuela").
    """
    index = {}
    with open(RANKINGS_CSV, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            week = row["week_date"]
            if week not in index:
                index[week] = {}
            norm = normalize_player_name(row["player"])
            rank = int(row["rank"])
            index[week][norm] = rank
            # Add partial name (first + first-last) as fallback if 3+ words
            parts = norm.split()
            if len(parts) >= 3:
                partial = parts[0] + " " + parts[1]
                if partial not in index[week]:
                    index[week][partial] = rank
    return index


def _fetch_tournaments_range(year, from_date, to_date):
    """Fetch WTA tournaments (WTA 125+) within a date range from API."""
    url = "https://api.wtatennis.com/tennis/tournaments/"
    valid_levels = {"WTA 1000", "WTA 500", "WTA 250", "WTA 125"}
    result = []
    page = 0
    while True:
        params = {
            "page": page,
            "pageSize": 100,
            "excludeLevels": "ITF",
            "from": from_date,
            "to": to_date,
        }
        try:
            r = get_with_retry(
                url,
                component="tstrength",
                attempts=3,
                headers=_WTA_API_HEADERS,
                params=params,
                timeout=(10, 20),
                failure_status="degraded",
            )
            data = r.json()
            tournaments = data.get("content", [])
            if not tournaments:
                break
            for t in tournaments:
                level = t.get("level", "")
                if level not in valid_levels:
                    continue
                tid = t["tournamentGroup"]["id"]
                raw_name = t["tournamentGroup"]["name"]
                if _is_ignored_tournament(raw_name):
                    continue
                city = t.get("city", "")
                start_date = t.get("startDate", "")[:10]
                surface = t.get("surface") or t.get("surfaceType") or t.get("surfaceCode") or ""
                country = t.get("countryCode") or t.get("country") or t.get("hostCountryCode") or ""
                result.append({
                    "id": str(tid),
                    "name": raw_name,
                    "city": city,
                    "level": level,
                    "startDate": start_date,
                    "surface": surface,
                    "country": country,
                    "year": str(year),
                })
            page += 1
        except PipelineError as e:
            print(f"Error fetching tournaments ({from_date} to {to_date}, page {page}): {e}")
            return []
        except Exception as e:
            report_run_issue(
                "tstrength", "parse tournament page", e, severity="degraded",
                context={"from": from_date, "to": to_date, "page": page},
            )
            return []
    result.sort(key=lambda x: x["startDate"])
    return result


def _fetch_tournament_matches(tournament_id, year="2025"):
    """Fetch tournament matches from WTA API (includes main draw + qualifying)."""
    url = f"https://api.wtatennis.com/tennis/tournaments/{tournament_id}/{year}/matches"
    try:
        r = get_with_retry(
            url,
            component="tstrength",
            attempts=3,
            headers=_WTA_API_HEADERS,
            timeout=(10, 20),
            failure_status="degraded",
        )
        data = r.json()
        return data.get("matches", []) or []
    except PipelineError as e:
        print(f"  Error fetching matches for {tournament_id}: {e}")
        return None
    except Exception as e:
        report_run_issue(
            "tstrength", "parse tournament matches", e, severity="degraded",
            context={"tournament_id": str(tournament_id), "year": str(year)},
        )
        return None


def _extract_draw_players(matches, draw_level_type):
    """Extract singles players for a given draw level (e.g., 'M' for MD, 'Q' for qualy).

    Returns (players, participants_locked).
    participants_locked is True when every known participant in this draw has at least
    one played match recorded (i.e., they cannot be replaced anymore).
    """
    draw_matches = [m for m in matches if m.get("DrawLevelType") == draw_level_type and m.get("DrawMatchType") == "S"]
    all_players = set()
    played_players = set()
    for m in draw_matches:
        match_state = str(m.get("MatchState", "") or "").strip().upper()
        result_string = str(m.get("ResultString", "") or "").strip()
        is_played = bool(result_string) or match_state in {"F", "L"}
        for suffix in ("A", "B"):
            first = m.get(f"PlayerNameFirst{suffix}", "")
            last = m.get(f"PlayerNameLast{suffix}", "")
            if last:
                name = f"{first} {last}".strip()
                all_players.add(name)
                if is_played:
                    played_players.add(name)

    participants_locked = bool(all_players) and all_players.issubset(played_players)
    return sorted(all_players), participants_locked


def _harmonic_mean(values):
    """Compute harmonic mean of a list of positive numbers."""
    if not values or any(v <= 0 for v in values):
        return 0
    return len(values) / sum(1.0 / v for v in values)


def _geometric_mean(values):
    """Compute geometric mean of a list of positive numbers."""
    if not values or any(v <= 0 for v in values):
        return 0
    log_sum = sum(math.log(v) for v in values)
    return math.exp(log_sum / len(values))


def _needs_refresh(cached_entry):
    if not cached_entry:
        return True
    if cached_entry.get("playerCount", 0) <= 0:
        return True
    if cached_entry.get("gm", 0) <= 0 or cached_entry.get("hm", 0) <= 0:
        return True
    if cached_entry.get("participantsLocked") is False:
        return True
    rankings = cached_entry.get("rankings")
    if isinstance(rankings, list) and len(rankings) == 0:
        return True
    if isinstance(rankings, list) and rankings and all(r == DEFAULT_RANK for r in rankings):
        return True
    return False


def build_tstrength_data(from_year=None, full_backfill=False):
    """Build tournament strength data for WTA tournaments.

    Default mode (full_backfill=False):
      Returns cached entries plus any newly available draws from a rolling window.

    Full backfill mode (full_backfill=True):
      Scans tournaments from from_year (inclusive) through today and fills the cache.

    Note: If a tournament was previously cached with 0 players (e.g., API data
    temporarily unavailable), it will be retried when it appears in the recent
    window again.
    """

    # Load cache (keyed by "year_id")
    cache = {}
    if os.path.exists(TSTRENGTH_CACHE):
        try:
            with open(TSTRENGTH_CACHE, encoding="utf-8") as f:
                cached_list = expand_tstrength_cache(json.load(f))
            for entry in cached_list:
                if _is_ignored_tournament(entry.get("name", "")):
                    continue
                draw = (entry.get("draw") or entry.get("drawType") or "MD").strip().upper()
                if draw in {"M", "MAIN"}:
                    draw = "MD"
                if draw in {"QUALY", "QUAL", "Q"}:
                    draw = "Q"
                entry["draw"] = draw
                year = entry.get("year", "2025")
                cache_key = f"{year}_{entry['id']}_{draw}"
                cache[cache_key] = entry
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise DataValidationError(
                component="tstrength",
                operation="load cache",
                message=f"cannot read existing tournament-strength cache: {TSTRENGTH_CACHE}",
                context={"path": TSTRENGTH_CACHE, "cause": str(exc)},
            ) from exc

    today = madrid_today()
    today_str = today.strftime("%Y-%m-%d")
    current_year = str(today.year)

    tournaments_to_consider = []
    if full_backfill:
        start_year = int(from_year) if from_year is not None else int(current_year)
        end_year = int(current_year)
        print(f"Full backfill: scanning {start_year}..{end_year} (through {today_str})")
        for y in range(start_year, end_year + 1):
            ys = str(y)
            if ys == current_year:
                print(f"Fetching {ys} tournaments (YTD)...")
                tournaments_to_consider.extend(_fetch_tournaments_range(ys, f"{ys}-01-01", today_str))
            else:
                print(f"Fetching {ys} tournaments (full year)...")
                tournaments_to_consider.extend(
                    _fetch_tournaments_range(ys, f"{ys}-01-01", f"{ys}-12-31")
                )
        print(f"  Found {len(tournaments_to_consider)} tournaments in range")
    else:
        # Auto-backfill: if a run was missed for >3 weeks, widen the window so we still pick up
        # tournaments that finished while the script wasn't running.
        jan1 = date(today.year, 1, 1)
        last_dt = None
        for e in cache.values():
            if str(e.get("year", "")) != current_year:
                continue
            if e.get("playerCount", 0) <= 0:
                continue
            sd = (e.get("startDate") or "")[:10]
            if not sd:
                continue
            try:
                dt = datetime.strptime(sd, "%Y-%m-%d").date()
            except Exception:
                continue
            if last_dt is None or dt > last_dt:
                last_dt = dt

        if last_dt is None:
            from_date = jan1.strftime("%Y-%m-%d")
        else:
            from_dt = max(jan1, last_dt - timedelta(days=21))
            from_date = from_dt.strftime("%Y-%m-%d")

        print(f"Fetching recent WTA tournaments ({from_date} to {today_str})...")
        tournaments_to_consider = _fetch_tournaments_range(current_year, from_date, today_str)
        print(f"  Found {len(tournaments_to_consider)} recent tournaments")

    # Filter to only uncached tournaments, plus cached placeholders that need a retry (per draw)
    tournament_needs = {}
    for t in tournaments_to_consider:
        level = (t.get("level") or "").strip()
        allowed_draws = {"Q"}
        if level in {"WTA 125", "WTA 250", "WTA 500"}:
            allowed_draws.add("MD")
        needs = set()
        for draw in ("MD", "Q"):
            if draw not in allowed_draws:
                continue
            cache_key = f"{t['year']}_{t['id']}_{draw}"
            cached = cache.get(cache_key)
            if cache_key not in cache or _needs_refresh(cached) or ("participantsLocked" not in (cached or {})):
                needs.add(draw)
        if needs:
            tournament_needs[f"{t['year']}_{t['id']}"] = needs

    new_tournaments = [t for t in tournaments_to_consider if f"{t['year']}_{t['id']}" in tournament_needs]

    if not new_tournaments:
        print("  No new tournaments to process")
    else:
        print(f"  {len(new_tournaments)} new tournaments to process")

        # Load rankings only if we have new tournaments
        print("Loading rankings for T-Strength...")
        rankings_index = _load_rankings_index()
        available_weeks = sorted(rankings_index.keys())
        unranked_players = {}

        for t in new_tournaments:
            tid = t["id"]
            yr = t.get("year", current_year)
            needs = tournament_needs.get(f"{yr}_{tid}", set())

            print(f"  Fetching players for {t['name']} ({t['startDate']})...")
            matches = _fetch_tournament_matches(tid, yr)
            time.sleep(0.3)
            if matches is None:
                continue

            surface = t.get("surface", "")
            country = t.get("country", "")
            region = _REGION_MAP.get(country, country)

            for draw in ("MD", "Q"):
                if draw not in needs:
                    continue
                ranking_week = _resolve_ranking_week(t["startDate"], draw, rankings_index, available_weeks)
                week_rankings = rankings_index.get(ranking_week, {})
                draw_level = "M" if draw == "MD" else "Q"
                players, participants_locked = _extract_draw_players(matches, draw_level)
                cache_key = f"{yr}_{tid}_{draw}"
                if (not players) or (not participants_locked):
                    cache[cache_key] = {
                        "id": tid, "name": t["name"], "city": t["city"],
                        "level": t["level"], "startDate": t["startDate"],
                        "surface": surface,
                        "country": country,
                        "region": region,
                        "year": yr,
                        "draw": draw,
                        "participantsLocked": False,
                        "rankings": [], "hm": 0, "gm": 0, "playerCount": 0
                    }
                    continue

                player_ranks = []
                for p in players:
                    norm_p = normalize_player_name(p)
                    rank = week_rankings.get(norm_p)
                    if rank is None and len(norm_p.split()) >= 3:
                        partial = norm_p.split()[0] + " " + norm_p.split()[1]
                        rank = week_rankings.get(partial)
                    if rank is None:
                        rank = DEFAULT_RANK
                        unranked_players[p] = unranked_players.get(p, [])
                        unranked_players[p].append(f"{t['name']} ({draw})")
                    player_ranks.append(rank)

                player_ranks.sort()
                hm = round(_harmonic_mean(player_ranks), 1)
                gm = round(_geometric_mean(player_ranks), 1)

                entry = {
                    "id": tid,
                    "name": t["name"],
                    "city": t["city"],
                    "level": t["level"],
                    "startDate": t["startDate"],
                    "surface": surface,
                    "country": country,
                    "region": region,
                    "year": yr,
                    "draw": draw,
                    "participantsLocked": True,
                    "rankings": player_ranks,
                    "hm": hm,
                    "gm": gm,
                    "playerCount": len(player_ranks),
                }
                cache[cache_key] = entry

        still_empty = []
        for t in new_tournaments:
            for draw in sorted(tournament_needs.get(f"{t.get('year', current_year)}_{t['id']}", set())):
                cache_key = f"{t.get('year', current_year)}_{t['id']}_{draw}"
                if cache.get(cache_key, {}).get("playerCount", 0) <= 0:
                    still_empty.append(f"{t.get('name', t['id'])} ({t.get('startDate', '')}) [{draw}]")

        # Save updated cache
        filtered_cache_values = [e for e in cache.values() if not _is_ignored_tournament(e.get("name", ""))]
        save_json_file(TSTRENGTH_CACHE, compress_tstrength_cache(filtered_cache_values))

        if unranked_players:
            print(f"\n=== UNRANKED PLAYERS (defaulted to {DEFAULT_RANK}) ===")
            for player, tourneys in sorted(unranked_players.items()):
                print(f"  {player}: {', '.join(tourneys)}")
            print(f"Total: {len(unranked_players)} unranked players\n")

    # Return all cached entries with actual players
    results = [
        e for e in cache.values()
        if (not _is_ignored_tournament(e.get("name", "")))
        and e.get("playerCount", 0) > 0
        and (e.get("participantsLocked") is not False)
    ]
    results.sort(key=lambda x: x["startDate"])
    return results
