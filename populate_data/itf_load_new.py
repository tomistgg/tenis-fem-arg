import json
import random
import time
import pandas as pd
import os
import requests
import undetected_chromedriver as uc
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "..", "data")
TOURNAMENT_LINK_PREFIX = "/en/tournament/"
ITF_EVENT_FILTERS_CACHE_FILE = os.path.join(DATA_DIR, "itf_event_filters_cache.json")
ITF_CALENDAR_CACHE_FILE = os.path.join(DATA_DIR, "itf_calendar_cache.json")

def get_week_start_end(today=None):
    if today is None:
        today = datetime.today().date()
    week_start = today - timedelta(days=today.weekday())  # Monday
    week_end = week_start + timedelta(days=6)              # Sunday
    return week_start, week_end

def create_driver():
    opts = uc.ChromeOptions()
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("window-size=1920,1080")
    return uc.Chrome(options=opts, headless=True)


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
        resp = requests.get(api_url, headers=headers, timeout=15)
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
                t_id = tournament.get('tournamentKey')
                if t_id and t_id not in seen_ids:
                    all_tournaments.append(tournament)
                    seen_ids.add(t_id)

        all_tournaments.sort(key=lambda x: x.get('startDate', ''))
        return all_tournaments

    except Exception as e:
        print(f"[!] Calendar fetch error: {e}")

def create_tournament_df(tournament_list):
    if not tournament_list:
        return None

    rows = []
    for item in tournament_list:
        link = TOURNAMENT_LINK_PREFIX + item.get("tournamentLink", "")
        t_key = link.rstrip('/').split('/')[-1] if link else None

        rows.append({
            "startDate": item.get("startDate"),
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

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.itftennis.com/en/tournament-calendar/womens-world-tennis-tour-calendar/",
    }

    results = []
    failed_keys = []

    for key in keys_list:
        url = f"https://www.itftennis.com/tennis/api/TournamentApi/GetEventFilters?tournamentKey={key}"
        fetched = False
        try:
            response = requests.get(url, headers=headers, timeout=15)
            raw = response.text.strip()
            if response.status_code == 200 and raw and not raw.startswith("<"):
                data = response.json()
                if isinstance(data, dict) and "tournamentId" in data:
                    results.append({"tournamentKey": key, "tournamentId": data["tournamentId"]})
                    fetched = True
        except Exception as e:
            print(f"  [!] requests failed for {key}: {e}")

        if not fetched:
            failed_keys.append(key)

    fetched_count = len(results)
    total = len(keys_list)
    if fetched_count < total:
        print(f"  [!] {total - fetched_count} tournament(s) missing IDs from live fetch; filling from cache.")
    else:
        print(f"  Fetched IDs for all {total} tournaments.")

    return json.dumps(results)

def merge_ids_with_pandas(calendar_df, json_ids_string):
    try:
        ids_list = json.loads(json_ids_string)
        ids_df = pd.DataFrame(ids_list)
        final_df = pd.merge(calendar_df, ids_df, on="tournamentKey", how="left")
        return final_df
    except Exception as e:
        print(f"[!] Error merging DataFrames: {e}")
        return calendar_df


def fetch_api_data(tId, classification, week_number=0, driver=None):
    url = "https://www.itftennis.com/tennis/api/TournamentApi/GetDrawsheet"
    payload = {
        "circuitCode": "WT",
        "eventClassificationCode": classification,
        "matchTypeCode": "S",
        "tourType": "WT",
        "tournamentId": f"{tId}",
        "weekNumber": week_number
    }

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": f"https://www.itftennis.com/en/tournament/draws-and-results/print/?tournamentId={tId}&circuitCode=WT",
        "Origin": "https://www.itftennis.com",
        "Content-Type": "application/json"
    }

    # Primary: requests with browser cookies (Incapsula validates session cookies)
    browser_cookies = {}
    if driver is not None:
        try:
            for cookie in driver.get_cookies():
                browser_cookies[cookie['name']] = cookie['value']
        except Exception:
            pass

    try:
        response = requests.post(url, json=payload, headers=headers,
                                 cookies=browser_cookies if browser_cookies else None)
        raw = response.text.strip()
        if response.status_code == 200 and raw and not raw.startswith("<"):
            return response.json()
    except Exception:
        pass

    # Fallback: fetch directly from browser context (same TLS fingerprint + cookies)
    if driver is not None:
        script = """
var url = arguments[0], payload = arguments[1], done = arguments[arguments.length - 1];
fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    credentials: 'include',
    body: JSON.stringify(payload)
})
.then(function(r) {
    var status = r.status;
    return r.text().then(function(text) {
        if (text && (text.charAt(0) === '{' || text.charAt(0) === '[')) {
            try { done({ok: true, data: JSON.parse(text), status: status}); }
            catch(e) { done({ok: false, error: 'parse:' + e.message, status: status}); }
        } else {
            done({ok: false, error: 'html', status: status, preview: text.substring(0, 80)});
        }
    });
})
.catch(function(e) { done({ok: false, error: String(e)}); });
"""
        try:
            result = driver.execute_async_script(script, url, payload)
            if isinstance(result, dict) and result.get("ok"):
                data = result.get("data")
                if isinstance(data, dict):
                    return data
            else:
                print(f"    [debug] browser fetch: {result}")
        except Exception as e:
            print(f"    [debug] browser exception: {e}")

    return None


def fetch_tournament_draw_data(tournament_id, tournament_name, codes, week_number=0, max_attempts=2):
    """Fetch draw data for one tournament using a fresh browser session per attempt.

    This helps avoid ITF/Incapsula session degradation across a long run.
    """
    tournament_id = int(tournament_id)

    for attempt in range(1, max_attempts + 1):
        driver = None
        results = {}
        try:
            driver = create_driver()
            draw_page_url = (
                "https://www.itftennis.com/en/tournament/draws-and-results/print/"
                f"?tournamentId={tournament_id}&circuitCode=WT"
            )
            driver.get(draw_page_url)
            time.sleep(random.uniform(3.5, 5.0))

            for code in codes:
                results[code] = fetch_api_data(
                    tournament_id,
                    code,
                    week_number=week_number,
                    driver=driver,
                )
                time.sleep(random.uniform(0.7, 1.3))

            if any(results.values()):
                return results
        except Exception as e:
            print(f"  [!] Draw fetch session failed for {tournament_name} (attempt {attempt}): {e}")
        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass

        if attempt < max_attempts:
            cooldown = random.uniform(10.0, 16.0)
            print(
                f"  [!] Empty/blocked draw response for {tournament_name}; "
                f"retrying with a fresh session after {cooldown:.1f}s"
            )
            time.sleep(cooldown)

    return {}

def parse_drawsheet(data, tourney_meta, draw_type, week_offset=0):
    if not data or not isinstance(data, dict): return []
    rows = []
    
    t_id = tourney_meta.get('tournamentId')
    t_name = tourney_meta.get('tournamentName')
    t_cat = tourney_meta.get('category')
    t_surf = tourney_meta.get('surfaceDesc')
    t_indoor = tourney_meta.get('indoorOrOutDoor', '')
    t_io = 'I' if t_indoor == 'Indoor' else 'O'
    t_nation = tourney_meta.get('hostNation')
    # Requirement: load ITF matches using the ingestion date (today), not ITF event date.
    t_date = datetime.today().strftime('%Y-%m-%d')

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
            for match in matches:
                try:
                    if match.get("playStatusCode") != "PC" and match.get("resultStatusCode") not in ("WO", "BYE"): continue

                    matchId = match.get("matchId")
                    teams = match.get("teams", [])
                    if len(teams) < 2: continue

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
                    elif not any(char.isdigit() for char in res):
                        res = ""
                        status_desc = "Walkover"

                    if w_c != "ARG" and l_c != "ARG":
                        continue

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
                    continue
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
            existing_df = pd.read_csv(file_path)
        except Exception as e:
            print(f"[!] Could not read existing {filename}: {e}. Starting fresh.")
            file_exists = False

    # Logic 1: Weekly Reset Check
    if reset_if_not_current_week and file_exists and not existing_df.empty:
        if 'date' in existing_df.columns:
            try:
                sample_date_str = existing_df['date'].iloc[0]
                sample_date = datetime.strptime(str(sample_date_str), "%Y-%m-%d").date()
                file_week_start = sample_date - timedelta(days=sample_date.weekday())
                if file_week_start != current_week_start:
                    existing_df = pd.DataFrame()
            except Exception as e:
                print(f"[!] Date check failed ({e}). Resetting file to be safe.")
                existing_df = pd.DataFrame()
        else:
            print(f"[!] No date column found in {filename}. Resetting file.")
            existing_df = pd.DataFrame()

    # Logic 2: Deduplication (Add only what doesn't exist)
    if not existing_df.empty:
        # We use matchId as the unique hash
        existing_ids = set(existing_df['matchId'].astype(str))
        
        # Filter new_data_df to only keep rows where matchId is NOT in existing_ids
        # We ensure matchId is string for comparison
        new_data_df['matchId'] = new_data_df['matchId'].astype(str)
        
        # Determine which rows are new
        is_new = ~new_data_df['matchId'].isin(existing_ids)
        unique_new_rows = new_data_df[is_new]
        
        if unique_new_rows.empty:
            return

        final_df = pd.concat([existing_df, unique_new_rows], ignore_index=True)
    else:
        final_df = new_data_df

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
        except Exception:
            pass
        return s

    for col in ("winnerSeed", "loserSeed"):
        if col in final_df.columns:
            final_df[col] = final_df[col].map(_normalize_seed)

    final_df.to_csv(file_path, index=False, encoding='utf-8-sig')

def _load_cached_tournament_ids():
    """Load ITF tournament IDs from the persistent event-filters cache.

    Returns a dict mapping tournamentKey (lowercase) -> tournamentId (str).
    Used as a fallback when the live Selenium-based ID fetch is blocked.
    """
    try:
        with open(ITF_EVENT_FILTERS_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return {k.lower(): str(v) for k, v in data.items() if v}
    except Exception:
        return {}


def _load_cached_calendar_tournaments(week_start, week_end):
    """Load ITF tournament entries from the persistent calendar cache that fall
    within [week_start, week_end].  Returns a list of raw tournament dicts in
    the same shape as the live GetCalendar API response so they can be fed
    directly into create_tournament_df().
    """
    try:
        with open(ITF_CALENDAR_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("items", []) if isinstance(data, dict) else (data or [])
    except Exception:
        return []

    results = []
    for item in items:
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
    week_start, week_end = get_week_start_end()
    last_week_start = week_start - timedelta(days=7)
    next_week_start = week_start + timedelta(days=7)
    next_week_end = next_week_start + timedelta(days=6)

    # Single driver kept alive through the full run (calendar → IDs → drawsheets)
    driver = create_driver()
    try:
        raw_all = get_itf_calendar_for_range(
            last_week_start.strftime("%Y-%m-%d"),
            next_week_end.strftime("%Y-%m-%d"),
            driver=driver
        )

        # Deduplicate by tournamentKey
        seen_keys = set()
        raw_data = []
        for t in (raw_all or []):
            key = t.get("tournamentKey")
            if key and key not in seen_keys:
                raw_data.append(t)
                seen_keys.add(key)

        if not raw_data:
            # Live calendar fetch blocked — fall back to the persisted cache so
            # we can still pick up match results for tournaments already known.
            print("[!] Live calendar fetch returned no results; using cached calendar as fallback.")
            raw_data = _load_cached_calendar_tournaments(last_week_start, next_week_end)
            if not raw_data:
                raise SystemExit(0)

        tournaments_df = create_tournament_df(raw_data)

        if tournaments_df is None or tournaments_df.empty:
            raise SystemExit(0)
        keys_list = tournaments_df["tournamentKey"].dropna().unique().tolist()

        # Warm up browser on ITF BEFORE any API calls so Incapsula session is valid
        print("  Warming up browser session...")
        try:
            driver.get("https://www.itftennis.com/en/tournament-calendar/womens-world-tennis-tour-calendar/")
            time.sleep(4)
            print("  Browser session ready.")
        except Exception as e:
            print(f"  [!] Browser warm-up failed: {e}")

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
        final_df['tournamentId'] = final_df['tournamentId'].fillna(0).astype(int).astype(str).replace('0', '')


        all_matches = []
        tournaments_list = final_df.to_dict('records')
        active_count = 0

        try:
            driver.quit()
        except Exception:
            pass
        driver = None

        for tourney in tournaments_list:
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
                        max_attempts=2,
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
                    max_attempts=2,
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
            time.sleep(random.uniform(1.5, 3.0))

        print(f"Tournaments processed: {active_count}, total ARG matches found: {len(all_matches)}")

    finally:
        try:
            driver.quit()
        except Exception:
            pass

    if all_matches:
        new_matches_df = pd.DataFrame(all_matches)
        update_csv_smart(
            "itf_matches_arg.csv",
            new_matches_df,
            reset_if_not_current_week=False
        )
        print(f"CSV update complete.")
    else:
        print("No new ARG matches found — CSV not updated.")
