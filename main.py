import os
import json
import io
import pandas as pd
import csv
import random
import time
from datetime import datetime, timedelta, timezone
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager

from config import ENTRY_LISTS_CACHE_FILE, ITF_ACCEPTANCE_STATE_FILE, NAME_LOOKUP, repair_name_text
from utils import (
    fix_encoding, fix_encoding_keep_accents,
    load_cache, save_cache, merge_entry_list,
    save_json_file,
    normalize_country_overrides, load_csv_rows
)
from calendar_builder import (
    get_monday_offset, generate_dynamic_monday_map,
    build_calendar_data, format_week_label, get_previous_monday
)
from wta import (
    build_tournament_groups, get_full_wta_calendar,
    get_wta_rankings_cached, scrape_tournament_players,
    get_draws_tournament_list, _load_wta_csv
)
from itf import (
    get_full_itf_calendar, get_itf_players,
    get_dynamic_itf_calendar, get_itf_rankings_cached,
    get_itf_level, parse_itf_entry_list,
    get_draws_itf_tournament_list, _load_itf_event_filters_cache
)
from html_generator import generate_html
from draws import fetch_tournament_draws, fetch_itf_tournament_draws, _draw_is_complete
from tstrength import build_tstrength_data
from populate_data.imagekit_gallery_sync import sync_gallery_manifest

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
TOURNAMENT_SNAPSHOT_FILE = os.path.join(DATA_DIR, "tournament_snapshot.json")
CALENDAR_SNAPSHOT_FILE = os.path.join(DATA_DIR, "calendar_snapshot.json")
PLAYER_ALIASES_WTA_ITF_FILE = os.path.join(DATA_DIR, "player_aliases_wta_itf.json")
DRAWS_STORE_CACHE_FILE = os.path.join(DATA_DIR, "draws_store_cache.json")
DRAW_FETCH_ERRORS_FILE = os.path.join(DATA_DIR, "draw_fetch_errors.json")
ENABLE_ITF_DRAWS_PREFETCH = False

# ITF draw-fetch pacing — anti-bot recovery strategy.
# Sleep between requests, cooldown after the first burst (Incapsula often blocks
# right after the tournament-id resolution burst), and longer backoff if we keep
# getting empty draws back (likely a session-level block — driver is recreated).
ITF_INTER_DRAW_SLEEP_RANGE = (15.0, 30.0)
ITF_FIRST_BURST_COOLDOWN_SEC = 65
ITF_FIRST_BURST_MIN_JOBS = 6
ITF_CONSECUTIVE_EMPTY_BACKOFF_SEC = 35
ITF_CONSECUTIVE_EMPTY_THRESHOLD = 3


GS_PDF_URLS_FILE = os.path.join(DATA_DIR, "gs_pdf_urls.json")


def _parse_gs_entry_list_pdf(pdf_bytes, alt_limit=10):
    """Parse a Grand Slam entry list PDF into (main_players, alt_players) lists.

    The PDF uses a two-column layout for the main draw (page 1) and single-column
    for the alternates section. Each player entry is parsed from per-column word
    tokens using positional rules (not regex on combined text).

    Strikethrough detection uses thin filled rectangles (height<2, width<200) with
    both y and x overlap checks to correctly distinguish left/right column entries.

    Returns (main_players, alt_players[:alt_limit]).
    """
    import pdfplumber
    import re

    _STATUS_FLAGS = frozenset(["F", "A", "S", "None", "WC", "SE", "LL"])

    def _parse_tokens(tokens):
        """Parse word tokens for one player entry.
        Returns (pos_num, name, country, rank_num) or None."""
        if len(tokens) < 4:
            return None
        if not tokens[0].isdigit():
            return None
        pos_num = int(tokens[0])
        if tokens[1] not in _STATUS_FLAGS:
            return None
        end = len(tokens) - 1
        if tokens[end] != "1":  # pref is always 1
            return None
        end -= 1
        if end >= 0 and tokens[end] == "SR":
            end -= 1
        if end < 2 or not tokens[end].isdigit():
            return None
        rank_num = int(tokens[end])
        end -= 1
        country = ""
        if end >= 2 and re.match(r"^[A-Z]{2,3}$", tokens[end]):
            country = tokens[end]
            end -= 1
        name_parts = tokens[2:end + 1]
        if not name_parts:
            return None
        name = " ".join(name_parts)
        return pos_num, name, country, rank_num

    def _is_struck(col_words, struck_rects):
        if not struck_rects or not col_words:
            return False
        y_mid = float(col_words[0]["top"]) + float(col_words[0].get("height", 10)) / 2
        col_x0 = min(float(w["x0"]) for w in col_words)
        col_x1 = max(float(w.get("x1", w["x0"])) for w in col_words)
        return any(
            r["top"] <= y_mid <= r["bottom"]
            and r["x0"] < col_x1
            and r["x0"] + r.get("width", 0) > col_x0
            for r in struck_rects
        )

    main_players = []
    alt_players = []
    moved_in_players = []
    section = None  # "MAIN", "MOVED_IN", "ALTERNATES"

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            mid_x = page.width / 2
            struck_rects = [
                r for r in (page.rects or [])
                if r.get("height", 99) < 2 and 8 < r.get("width", 0) < 200
            ]

            words = page.extract_words(keep_blank_chars=False, x_tolerance=3, y_tolerance=3)
            lines = {}
            for w in words:
                y = round(float(w["top"]), 1)
                lines.setdefault(y, []).append(w)

            for y_top, word_list in sorted(lines.items()):
                word_list.sort(key=lambda w: float(w["x0"]))
                line_text = " ".join(w["text"] for w in word_list).strip()
                if not line_text:
                    continue
                upper = line_text.upper()

                # Section header detection
                if "MAIN DRAW ALTERNATES" in upper or "QUALIFYING ALTERNATES" in upper:
                    section = "ALTERNATES"
                    continue
                if upper.strip() in ("MAIN DRAW", "QUALIFYING DRAW"):
                    section = "MAIN"
                    continue
                if "MOVED IN" in upper:
                    section = "MOVED_IN"
                    continue
                if "PLAYER NAME" in upper or "NAT RANK" in upper or upper.startswith("POS PLAYER"):
                    continue

                if section is None:
                    continue

                # Split into left and right columns by page midpoint
                left_words = [w for w in word_list if float(w["x0"]) < mid_x]
                right_words = [w for w in word_list if float(w["x0"]) >= mid_x]

                for col_words in (left_words, right_words):
                    if not col_words:
                        continue
                    entry = _parse_tokens([w["text"] for w in col_words])
                    if not entry:
                        continue
                    pos_num, name, country, rank_num = entry

                    if _is_struck(col_words, struck_rects):
                        continue

                    player = {
                        "name": name.title(),
                        "country": country,
                        "rank_num": rank_num,
                        "rank": str(rank_num),
                        "pos": str(pos_num),
                        "pos_num": pos_num,
                    }
                    if section == "MAIN":
                        player["type"] = "MAIN"
                        main_players.append(player)
                    elif section == "MOVED_IN":
                        player["type"] = "MAIN"
                        moved_in_players.append(player)
                    elif section == "ALTERNATES":
                        player["type"] = "ALT"
                        alt_players.append(player)

    all_main = (
        sorted(main_players, key=lambda p: p["pos_num"])
        + sorted(moved_in_players, key=lambda p: p["pos_num"])
    )
    for i, p in enumerate(all_main, 1):
        p["pos"] = str(i)
        p["pos_num"] = i

    alt_players.sort(key=lambda p: p["pos_num"])
    alt_capped = alt_players[:alt_limit]
    for i, p in enumerate(alt_capped, 1):
        p["pos"] = str(i)
        p["pos_num"] = i

    return all_main, alt_capped


_wta_country_lookup_cache = None


def _build_wta_country_lookup():
    """Build name→country from WTA ranking CSVs (most recent entry per player)."""
    global _wta_country_lookup_cache
    if _wta_country_lookup_cache is not None:
        return _wta_country_lookup_cache
    from config import WTA_RANKINGS_CSV, WTA_RANKINGS_CSV_10_19, _lookup_keys
    player_latest = {}  # upper_name → (week_date, country)
    for csv_path in [WTA_RANKINGS_CSV, WTA_RANKINGS_CSV_10_19]:
        if not os.path.exists(csv_path):
            continue
        try:
            with open(csv_path, newline="", encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    name = (row.get("player") or "").strip()
                    country = (row.get("country") or "").strip()
                    week = (row.get("week_date") or "").strip()
                    if not name or not country:
                        continue
                    key = name.upper()
                    existing = player_latest.get(key)
                    if not existing or week > existing[0]:
                        player_latest[key] = (week, country)
        except Exception:
            pass
    lookup = {}
    for player_key, (_, country) in player_latest.items():
        for k in _lookup_keys(player_key):
            lookup[k] = country
    _wta_country_lookup_cache = lookup
    return lookup


def _fill_missing_countries(players, entry_cache=None):
    """For any player with no country, look them up in WTA Rankings."""
    country_lookup = _build_wta_country_lookup()
    from config import _lookup_keys

    filled = 0
    for player in players:
        if not (player.get("country") or "").strip():
            name = player.get("name", "")
            for key in _lookup_keys(name):
                country = country_lookup.get(key)
                if country:
                    player["country"] = country
                    filled += 1
                    break
    if filled:
        print(f"[PDF] Filled {filled} missing country codes from WTA Rankings")


def _apply_pdf_schedule_entries(tournament_store, tournament_groups, arg_names_set, schedule_map, unranked_schedule, players_data):
    """Add MAIN/QUAL draw players from PDF-sourced entry lists to schedule_map for ARG players.

    Uses NAME_LOOKUP from config for alias resolution (e.g. 'Jazmin Ortenzi' -> 'Jazmín Ortenzi').
    Qualifying players appear under the '#qual' key which is already in tournament_groups with
    the correct week (May 18) and display name ('Roland Garros (Q)').
    """
    from config import NAME_LOOKUP, _lookup_keys

    url_to_week = {}
    for week_label, tourneys in tournament_groups.items():
        for t_url, t_info in tourneys.items():
            url_to_week[t_url] = (week_label, t_info["name"])

    existing_player_keys = {p['Player'] for p in players_data}
    added = 0
    for cache_key, (week_label, t_name) in url_to_week.items():
        players = tournament_store.get(cache_key)
        if not players:
            continue
        # Only process PDF-sourced entries (main or #qual)
        is_pdf_key = cache_key in _get_pdf_cache_keys()
        if not is_pdf_key:
            continue
        for player in players:
            p_type = player.get('type', 'MAIN')
            if p_type not in ('MAIN', 'QUAL'):
                continue
            raw_name = player.get('name', '')
            p_upper = raw_name.upper()
            # Resolve through alias lookup
            for key in _lookup_keys(raw_name):
                canonical = NAME_LOOKUP.get(key)
                if canonical:
                    p_upper = canonical
                    break
            p_country = player.get('country', '')
            for target_map, condition in [
                (schedule_map, p_upper in arg_names_set),
                (unranked_schedule, p_country == 'ARG' and p_upper not in arg_names_set),
            ]:
                if not condition:
                    continue
                weeks = target_map.setdefault(p_upper, {})
                if week_label not in weeks:
                    weeks[week_label] = t_name
                else:
                    weeks[week_label] = t_name + "<br>" + weeks[week_label]
                if target_map is unranked_schedule and p_upper not in existing_player_keys:
                    players_data.append({'Player': p_upper, 'Key': p_upper, 'Rank': '-'})
                    existing_player_keys.add(p_upper)
                added += 1
    if added:
        print(f"[PDF] Added {added} schedule entries from PDF entry lists")


def _get_pdf_cache_keys():
    """Return the set of tournament_store keys that are PDF-sourced (main + #qual)."""
    try:
        with open(GS_PDF_URLS_FILE, "r", encoding="utf-8") as f:
            pdf_urls = json.load(f)
    except Exception:
        return set()
    keys = set()
    for cache_key, url_config in pdf_urls.items():
        keys.add(cache_key)
        if isinstance(url_config, dict) and "qual" in url_config:
            keys.add(cache_key + "#qual")
    return keys


def _refresh_entry_lists_from_pdfs(entry_cache, tournament_store, tournament_groups=None, monday_map=None):
    """Fetch PDFs listed in gs_pdf_urls.json and override entry lists in-place.

    Qualifying draws (qual key with start_date/display_name) are stored under
    cache_key + '#qual' and injected into tournament_groups for the correct week.
    """
    import requests
    from wta import get_monday_from_date, format_week_label

    try:
        with open(GS_PDF_URLS_FILE, "r", encoding="utf-8") as f:
            pdf_urls = json.load(f)
    except FileNotFoundError:
        return
    except Exception as e:
        print(f"[PDF] Failed to load {GS_PDF_URLS_FILE}: {e}")
        return

    for cache_key, url_config in pdf_urls.items():
        if isinstance(url_config, str):
            url_config = {"main": url_config}

        main_players = []
        for draw_type, draw_config in url_config.items():
            if isinstance(draw_config, str):
                pdf_url = draw_config
                qual_meta = None
            else:
                pdf_url = (draw_config or {}).get("url", "")
                qual_meta = draw_config if draw_type == "qual" else None
            if not pdf_url:
                continue

            print(f"[PDF] Fetching {draw_type} entry list PDF for {cache_key}")
            try:
                resp = requests.get(pdf_url, timeout=30)
                resp.raise_for_status()
                pdf_bytes = resp.content
            except Exception as e:
                print(f"[PDF] Download failed for {pdf_url}: {e}")
                continue

            try:
                draw_main, draw_alt = _parse_gs_entry_list_pdf(
                    pdf_bytes, alt_limit=20 if draw_type == "qual" else 10
                )
            except Exception as e:
                print(f"[PDF] Parse failed for {pdf_url}: {e}")
                continue

            if not draw_main:
                print(f"[PDF] No players parsed from {pdf_url}, skipping")
                continue

            print(f"[PDF] Parsed {len(draw_main)} {draw_type.upper()} + {len(draw_alt)} ALT from {pdf_url}")

            if draw_type == "qual":
                for p in draw_main:
                    p["type"] = "QUAL"
                qual_players = draw_main + draw_alt
                qual_key = cache_key + "#qual"
                _fill_missing_countries(qual_players, entry_cache)
                entry_cache[qual_key] = qual_players
                tournament_store[qual_key] = qual_players
                # Inject into tournament_groups for the qualifying week
                if tournament_groups is not None and monday_map is not None and qual_meta:
                    start_date = qual_meta.get("start_date", "")
                    display_name = qual_meta.get("display_name", "Grand Slam (Q)")
                    if start_date:
                        try:
                            qual_monday = get_monday_from_date(start_date)
                            week_label = format_week_label(qual_monday)
                            if week_label in monday_map.values():
                                tournament_groups.setdefault(week_label, {})[qual_key] = {
                                    "name": display_name,
                                    "level": "Grand Slam",
                                    "startDate": start_date,
                                }
                        except Exception:
                            pass
            else:
                main_players.extend(draw_main + draw_alt)

        if main_players:
            _fill_missing_countries(main_players, entry_cache)
            entry_cache[cache_key] = main_players
            tournament_store[cache_key] = main_players


def _canonical_draw_store_key(t_key):
    """Normalize draw cache keys so ITF keys are case-stable across runs."""
    key = str(t_key or "").strip()
    if not key:
        return ""
    if key.lower().startswith("w-itf-"):
        return key.lower()
    return key


def _merge_draw_store_entry(existing, incoming):
    """Merge two draw cache entries, preferring non-empty incoming metadata and newer draw payloads."""
    if not isinstance(existing, dict):
        existing = {}
    if not isinstance(incoming, dict):
        incoming = {}
    if not existing:
        return dict(incoming)
    if not incoming:
        return dict(existing)

    merged = dict(existing)
    for field in ("name", "level", "week", "startDate", "endDate"):
        value = incoming.get(field)
        if value is not None and str(value).strip() != "":
            merged[field] = value

    existing_draws = existing.get("draws") if isinstance(existing.get("draws"), dict) else {}
    incoming_draws = incoming.get("draws") if isinstance(incoming.get("draws"), dict) else {}
    if existing_draws or incoming_draws:
        merged_draws = {}
        merged_draws.update(existing_draws)
        for dtype_code, new_draw in incoming_draws.items():
            old_draw = merged_draws.get(dtype_code)
            if (isinstance(old_draw, dict) and old_draw.get("players")
                    and isinstance(new_draw, dict) and not new_draw.get("players")):
                continue
            merged_draws[dtype_code] = new_draw
        merged["draws"] = merged_draws

    return merged


def _normalize_draws_store_keys(draws_store):
    """Collapse case-only ITF key duplicates into a single canonical cache key."""
    if not isinstance(draws_store, dict):
        return {}
    normalized = {}
    for raw_key, tdata in draws_store.items():
        canonical_key = _canonical_draw_store_key(raw_key)
        if not canonical_key:
            continue
        normalized[canonical_key] = _merge_draw_store_entry(normalized.get(canonical_key), tdata)
    return normalized


def _normalize_name_for_lookup(name):
    """Normalize names for cross-source lookups (case/accents/whitespace)."""
    if not name:
        return ""
    return " ".join(fix_encoding(str(name)).strip().upper().split())


def _map_to_display_name_upper(name):
    """Map aliases to display_name (from `player_aliases_wta_itf.json`) when possible."""
    if not name:
        return ""
    raw_upper = " ".join(str(name).strip().upper().split())
    if not raw_upper:
        return ""
    # Try raw first, then encoding/accents-normalised key (common in older datasets).
    alt_upper = _normalize_name_for_lookup(raw_upper)
    return NAME_LOOKUP.get(raw_upper) or NAME_LOOKUP.get(alt_upper) or raw_upper




def enrich_history_with_wta_ranks(cleaned_history):
    """Add `_winnerRank` / `_loserRank` to cleaned history rows (empty if unknown)."""
    if not cleaned_history:
        return cleaned_history

    # Optional: map ITF-side names to WTA-side names (to resolve rankings even when
    # the match dataset uses ITF spelling while rankings CSV uses WTA spelling).
    aliases_lookup = {}
    itf_id_to_wta_id = {}
    if os.path.exists(PLAYER_ALIASES_WTA_ITF_FILE):
        try:
            with open(PLAYER_ALIASES_WTA_ITF_FILE, "r", encoding="utf-8-sig") as f:
                items = json.load(f)
            if not isinstance(items, list):
                items = []
        except Exception:
            items = []
        for it in items:
            if not isinstance(it, dict):
                continue
            itf_name = repair_name_text(it.get("itf_name")).strip()
            # New format: {display_name,wta_id,wta_name,itf_id,itf_name,bjkc_name}
            itf_id = (it.get("itf_id") or "").strip()
            wta_id = (it.get("wta_id") or "").strip()
            wta_name = repair_name_text(it.get("wta_name")).strip()
            display_name = repair_name_text(it.get("display_name")).strip()
            if itf_id and wta_id and itf_id not in itf_id_to_wta_id:
                itf_id_to_wta_id[itf_id] = wta_id
            cand_norms = []
            for n in [wta_name, display_name]:
                n = str(n or "").strip()
                if not n:
                    continue
                n_norm = _normalize_name_for_lookup(n)
                if n_norm and n_norm not in cand_norms:
                    cand_norms.append(n_norm)
                disp_norm = _normalize_name_for_lookup(_map_to_display_name_upper(n))
                if disp_norm and disp_norm not in cand_norms:
                    cand_norms.append(disp_norm)
            if not itf_name or not cand_norms:
                continue
            # Allow lookups by raw ITF name or by our display-mapped key.
            for k in {_normalize_name_for_lookup(itf_name), _normalize_name_for_lookup(_map_to_display_name_upper(itf_name))}:
                if not k:
                    continue
                if k not in aliases_lookup:
                    aliases_lookup[k] = []
                for cn in cand_norms:
                    if cn not in aliases_lookup[k]:
                        aliases_lookup[k].append(cn)

    csv_by_week = _load_wta_csv() or {}
    week_index_cache = {}

    def _is_itf_id(value):
        s = str(value or "").strip()
        if not s.isdigit():
            return False
        return len(s) >= 9 or s.startswith("800")

    def _index_variants(name):
        """Generate additional lookup keys for common WTA naming variants (e.g., married-name hyphens)."""
        if not name:
            return []
        base_upper = " ".join(str(name).strip().upper().split())
        if not base_upper:
            return []
        out = []
        for cand in [base_upper, base_upper.replace("-", " ")]:
            norm = _normalize_name_for_lookup(cand)
            if norm and norm not in out:
                out.append(norm)
        parts = base_upper.split()
        if len(parts) >= 2 and any("-" in p for p in parts[1:]):
            stripped = parts[:]
            for i in range(1, len(stripped)):
                if "-" in stripped[i]:
                    stripped[i] = stripped[i].split("-")[0]
            norm = _normalize_name_for_lookup(" ".join(stripped))
            if norm and norm not in out:
                out.append(norm)
        return out

    def week_index(week_date):
        if week_date in week_index_cache:
            return week_index_cache[week_date]
        idx_by_name = {}
        idx_by_id = {}
        for p in (csv_by_week.get(week_date) or []):
            r = p.get("Rank", "")
            if r is None or r == "":
                continue
            raw = p.get("OfficialPlayer") or p.get("Player", "")
            rank_str = str(r)
            pid = str(p.get("Id") or "").strip()
            if pid:
                idx_by_id[pid] = rank_str
            for key_name in [raw, _map_to_display_name_upper(raw)]:
                for k in _index_variants(key_name):
                    idx_by_name[k] = rank_str
        week_index_cache[week_date] = (idx_by_name, idx_by_id)
        return idx_by_name, idx_by_id

    def resolve_rank(name_raw, idx):
        """Resolve a ranking for a raw name using direct and alias-based lookups."""
        if not name_raw:
            return ""
        raw_norm = _normalize_name_for_lookup(name_raw)
        disp_norm = _normalize_name_for_lookup(_map_to_display_name_upper(name_raw))
        rank = idx.get(disp_norm) or idx.get(raw_norm) or ""
        if rank:
            return rank
        # Alias-based lookup: try ITF name -> WTA name candidates.
        for k in (disp_norm, raw_norm):
            if not k:
                continue
            for cand in (aliases_lookup.get(k) or []):
                rank = idx.get(cand) or ""
                if rank:
                    return rank
        return ""

    def resolve_rank_by_ids(name_raw, player_id_raw, idx_by_name, idx_by_id):
        """Resolve rank preferring WTA id lookups (direct or ITF-id->WTA-id via aliases JSON)."""
        pid = str(player_id_raw or "").strip()
        wta_id = ""
        if pid.isdigit():
            if _is_itf_id(pid):
                wta_id = itf_id_to_wta_id.get(pid, "")
            else:
                wta_id = pid
        if wta_id:
            rank = idx_by_id.get(wta_id) or ""
            if rank:
                return rank
        return resolve_rank(name_raw, idx_by_name)

    for row in cleaned_history:
        row["_winnerRank"] = ""
        row["_loserRank"] = ""
        week_date = get_previous_monday(row.get("DATE", ""))
        if not week_date or week_date not in csv_by_week:
            continue
        idx_by_name, idx_by_id = week_index(week_date)
        row["_winnerRank"] = resolve_rank_by_ids(
            row.get("_winnerName", ""), row.get("_winnerId", ""), idx_by_name, idx_by_id
        )
        row["_loserRank"] = resolve_rank_by_ids(
            row.get("_loserName", ""), row.get("_loserId", ""), idx_by_name, idx_by_id
        )

    return cleaned_history


def create_driver():
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
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
    try:
        driver.set_page_load_timeout(60)
        driver.set_script_timeout(30)
    except Exception:
        pass
    return driver


def build_all_tournament_groups(driver):
    """Merge WTA tournament groups with ITF calendar and save snapshot."""
    tournament_groups = build_tournament_groups()
    monday_map = generate_dynamic_monday_map(num_weeks=4)
    itf_monday_map = generate_dynamic_monday_map(num_weeks=3)

    # Add current week's Monday only on Monday/Tuesday when current-week
    # tournaments are intentionally included, dropping the last future week to
    # keep total at 4.
    today = datetime.now()
    current_monday = today - timedelta(days=today.weekday())
    current_monday_str = current_monday.strftime("%Y-%m-%d")
    current_monday_label = format_week_label(current_monday)
    if today.weekday() == 0 and current_monday_label in tournament_groups and tournament_groups[current_monday_label]:
        m_keys = list(monday_map.keys())
        monday_map = {k: monday_map[k] for k in m_keys[:-1]}
        monday_map = {current_monday_str: current_monday_label, **monday_map}
        itf_keys = list(itf_monday_map.keys())
        itf_monday_map = {k: itf_monday_map[k] for k in itf_keys[:-1]}
        itf_monday_map = {current_monday_str: current_monday_label, **itf_monday_map}

    itf_items = get_dynamic_itf_calendar(driver, num_weeks=3)

    for label in monday_map.values():
        if label not in tournament_groups:
            tournament_groups[label] = {}

    for item in itf_items:
        t_name = item['tournamentName']
        if 'cancel' in t_name.lower():
            continue
        s_date = pd.to_datetime(item['startDate'])
        monday_date = (s_date - timedelta(days=s_date.weekday())).strftime('%Y-%m-%d')
        if monday_date in itf_monday_map:
            week_label = itf_monday_map[monday_date]
            tournament_groups[week_label][item['tournamentKey'].lower()] = {
                "name": t_name,
                "level": get_itf_level(t_name),
                "startDate": item['startDate'],
                "endDate": item.get('endDate', None)
            }

    tournament_snapshot = {}
    for week, tourneys in tournament_groups.items():
        for key, info in tourneys.items():
            if 'cancel' in info.get("name", "").lower():
                continue
            tournament_snapshot[key] = {
                "name": info.get("name", key),
                "level": info.get("level", ""),
                "startDate": info.get("startDate"),
                "endDate": info.get("endDate"),
                "week": week,
            }
    save_json_file(TOURNAMENT_SNAPSHOT_FILE, tournament_snapshot)

    return tournament_groups, monday_map


def fetch_arg_players():
    """Fetch WTA+ITF rankings and return deduplicated ARG player list."""
    today = datetime.now()
    ranking_monday = (today - timedelta(days=today.weekday())).strftime("%Y-%m-%d")

    all_wta_players = get_wta_rankings_cached(ranking_monday, nationality=None)
    normalize_country_overrides(all_wta_players, "Player", "Country")

    wta_players_arg = [p for p in all_wta_players if p['Country'] == 'ARG']
    itf_players_arg = get_itf_rankings_cached(ranking_monday, nationality="ARG")

    wta_names_arg = {p['Player'] for p in wta_players_arg}
    itf_only_arg = [p for p in itf_players_arg if p['Player'] not in wta_names_arg]

    players_data = wta_players_arg + itf_only_arg
    arg_names_set = {p['Player'] for p in players_data}

    return players_data, arg_names_set, all_wta_players


def _itf_acceptance_list_available(start_date_str, today):
    """Return True if the ITF acceptance list should be available yet.

    Entry lists for a given week are published on the Friday that is 3 weeks
    before the tournament's start Monday (Mon - 17 days = Friday 3 weeks prior).
    """
    if not start_date_str:
        return True
    try:
        start_dt = datetime.strptime(start_date_str[:10], "%Y-%m-%d")
    except ValueError:
        return True
    week_monday = start_dt - timedelta(days=start_dt.weekday())
    threshold_friday = week_monday - timedelta(days=17)
    return today.date() >= threshold_friday.date()


def _acceptance_fingerprint(players):
    """Stable fingerprint of a player list used to detect acceptance-list changes."""
    if not players:
        return ""
    key_fields = sorted(
        (p.get("name", ""), p.get("type", ""), p.get("pos_num", 9999))
        for p in players
    )
    return json.dumps(key_fields, ensure_ascii=False, separators=(",", ":"))


def _load_acceptance_state():
    """Load per-tournament acceptance-check state (last_changed_date) from disk."""
    if not os.path.exists(ITF_ACCEPTANCE_STATE_FILE):
        return {}
    try:
        with open(ITF_ACCEPTANCE_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_acceptance_state(state):
    """Persist per-tournament acceptance-check state to disk."""
    try:
        with open(ITF_ACCEPTANCE_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Warning: could not save ITF acceptance state: {e}")


def process_tournaments(driver, tournament_groups, monday_map, arg_names_set, entry_cache):
    """Process WTA & ITF tournaments: scrape entry lists, build schedule map."""
    schedule_map = {}
    tournament_store = {}
    ranking_cache = {}
    unranked_schedule = {}
    itf_schedule_pending = {}
    unranked_itf_pending = {}

    _now = datetime.now()
    today_str = _now.strftime("%Y-%m-%d")
    # After noon UTC, give up re-fetching acceptance lists we already tried today
    # without detecting a change. Pre-noon keeps retrying to catch morning updates.
    _utc_now = datetime.now(timezone.utc)
    _past_noon_utc = _utc_now.hour >= 12
    # On Sat/Sun/Mon allow a second check after 6 pm Spain time (CEST=UTC+2 Apr-Oct, CET=UTC+1 otherwise)
    _spain_offset = 2 if 3 < _utc_now.month < 11 else 1
    _past_6pm_spain = (_utc_now.hour + _spain_offset) % 24 >= 18
    _is_double_check_day = _now.weekday() in (5, 6, 0)  # Sat, Sun, Mon
    current_monday_str = (_now - timedelta(days=_now.weekday())).strftime("%Y-%m-%d")
    next_monday_str = (_now + timedelta(days=7 - _now.weekday())).strftime("%Y-%m-%d")
    acceptance_state = _load_acceptance_state()
    acceptance_state_dirty = False

    def _priority_num(value):
        text = str(value or "").strip()
        if text.isdigit():
            return int(text)
        return 9999

    def _entry_type_rank(entry_type):
        et = str(entry_type or "").upper()
        if et == "MAIN":
            return 0
        if et == "QUAL":
            return 1
        if et == "ALT":
            return 2
        return 3

    def _safe_pos_num(value):
        try:
            return int(value)
        except Exception:
            return 9999

    def _suffix_from_itf_player(player_row):
        p_type = (player_row or {}).get('type', '')
        if p_type == 'MAIN':
            return ''
        if p_type == 'QUAL':
            return ' (Q)'
        pos = (player_row or {}).get('pos', '')
        return f" (ALT {pos})" if pos else ' (ALT)'

    def _queue_itf_entry(container, player_key, week_label, tournament_key, tournament_name, suffix, priority, entry_type, pos_num):
        if not player_key or not week_label:
            return
        by_week = container.setdefault(player_key, {})
        items = by_week.setdefault(week_label, [])
        if any(item.get("tournament_key") == tournament_key for item in items):
            return
        items.append({
            "tournament_key": tournament_key,
            "name": tournament_name,
            "suffix": suffix or "",
            "priority": priority,
            "entry_type": entry_type,
            "pos_num": _safe_pos_num(pos_num),
        })

    def _itf_item_sort_key(item):
        return (
            _priority_num(item.get("priority")),
            _entry_type_rank(item.get("entry_type")),
            _safe_pos_num(item.get("pos_num")),
            str(item.get("name") or "").lower(),
        )

    def _flush_itf_pending(target_map, pending_map):
        for p_key, weeks_map in pending_map.items():
            if p_key not in target_map:
                target_map[p_key] = {}
            for week_label, items in weeks_map.items():
                if not items:
                    continue
                sorted_items = sorted(items, key=_itf_item_sort_key)
                formatted = "<br>".join(f"{it['name']}{it['suffix']}" for it in sorted_items)
                if week_label in target_map[p_key] and target_map[p_key][week_label]:
                    target_map[p_key][week_label] += f"<br>{formatted}"
                else:
                    target_map[p_key][week_label] = formatted

    mondays = sorted(monday_map.keys())
    total_weeks = len(mondays) or 4

    for i, week_monday in enumerate(mondays, start=1):
        print(f"Processing Tournaments ({i}/{total_weeks})")
        week = monday_map.get(week_monday)
        if not week:
            continue
        tourneys = tournament_groups.get(week, {})
        is_current_week = week_monday < next_monday_str

        md_date = get_monday_offset(week_monday, 4)
        q_date = get_monday_offset(week_monday, 3)

        today_date = datetime.now()
        md_datetime = datetime.strptime(md_date, "%Y-%m-%d")
        q_datetime = datetime.strptime(q_date, "%Y-%m-%d")

        if md_datetime > today_date:
            md_date = (today_date - timedelta(days=today_date.weekday())).strftime("%Y-%m-%d")
        if q_datetime > today_date:
            q_date = (today_date - timedelta(days=today_date.weekday())).strftime("%Y-%m-%d")

        if md_date not in ranking_cache:
            ranking_cache[md_date] = get_wta_rankings_cached(md_date, nationality=None)
        if q_date not in ranking_cache:
            ranking_cache[q_date] = get_wta_rankings_cached(q_date, nationality=None)
        normalize_country_overrides(ranking_cache[md_date], "Player", "Country")
        normalize_country_overrides(ranking_cache[q_date], "Player", "Country")

        # WTA tournaments
        for key, t_info in tourneys.items():
            t_name = t_info["name"]
            if key.startswith("http"):
                t_list, status_dict = scrape_tournament_players(key, ranking_cache[md_date], ranking_cache[q_date], entry_cache.get(key, []))
                t_list = merge_entry_list(entry_cache.get(key, []), t_list)
                normalize_country_overrides(t_list, "name", "country")
                entry_cache[key] = t_list
                tournament_store[key] = t_list

                # Compute seeds for WTA tournaments based on level and draw size.
                # Grand Slams: 32 seeds for main draw and qualifying independently.
                # Other WTA: seed count by main draw player count.
                _level = t_info.get("level", "")
                _is_gs = "grand slam" in _level.lower()
                _main_players = [_p for _p in t_list if _p.get("type") == "MAIN"]
                _qual_players  = [_p for _p in t_list if _p.get("type") == "QUAL"]
                _main_count = len(_main_players)
                if _is_gs:
                    _num_seeds = 32
                elif _main_count > 70:
                    _num_seeds = 32
                elif _main_count >= 40:
                    _num_seeds = 16
                elif _main_count >= 18:
                    _num_seeds = 8
                else:
                    _num_seeds = 4
                _seed_date = (datetime.strptime(week_monday, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
                if _seed_date > current_monday_str:
                    _seed_date = current_monday_str
                if _seed_date not in ranking_cache:
                    ranking_cache[_seed_date] = get_wta_rankings_cached(_seed_date, nationality=None)
                _name_to_rank = {}
                for _sp in ranking_cache.get(_seed_date, []):
                    _sname = _sp.get("Player", "").strip().upper()
                    _srank = _sp.get("Rank")
                    if _sname and _srank is not None:
                        _name_to_rank[_sname] = int(_srank)
                def _build_seed_map(player_list, n):
                    candidates = []
                    for _p in player_list:
                        _pname_up = NAME_LOOKUP.get(_p["name"].upper(), _p["name"].upper())
                        _r = _name_to_rank.get(_pname_up)
                        if _r is not None:
                            candidates.append((_r, _p["name"]))
                    candidates.sort()
                    return {name: i + 1 for i, (_, name) in enumerate(candidates[:n])}
                _main_seed_map = _build_seed_map(_main_players, _num_seeds)
                for _p in t_list:
                    if _p.get("type") == "MAIN":
                        _sv = _main_seed_map.get(_p["name"])
                        _p["seed"] = _sv if _sv is not None else ""
                if _is_gs and _qual_players:
                    _qual_seed_map = _build_seed_map(_qual_players, 32)
                    for _p in t_list:
                        if _p.get("type") == "QUAL":
                            _sv = _qual_seed_map.get(_p["name"])
                            _p["seed"] = _sv if _sv is not None else ""

                for p_name, suffix in status_dict.items():
                    p_key = p_name.upper()
                    if p_key not in arg_names_set:
                        continue
                    if p_key not in schedule_map:
                        schedule_map[p_key] = {}
                    if week in schedule_map[p_key]:
                        schedule_map[p_key][week] += f'<div style="margin-top: 3px;">{t_name}{suffix}</div>'
                    else:
                        schedule_map[p_key][week] = f"{t_name}{suffix}"
                for p in t_list:
                    p_upper = p['name'].upper()
                    if p_upper in arg_names_set:
                        continue
                    if p.get('country', '') != 'ARG':
                        continue
                    suffix = '' if p.get('type') == 'MAIN' else ' (Q)'
                    if p_upper not in unranked_schedule:
                        unranked_schedule[p_upper] = {}
                    if week in unranked_schedule[p_upper]:
                        if t_name not in unranked_schedule[p_upper][week]:
                            unranked_schedule[p_upper][week] += f'<div style="margin-top: 3px;">{t_name}{suffix}</div>'
                    else:
                        unranked_schedule[p_upper][week] = f"{t_name}{suffix}"

        # ITF tournaments
        for key, t_info in tourneys.items():
            t_name = t_info["name"]
            if 'cancel' in t_name.lower():
                continue
            if not key.startswith("http"):
                cached_players = entry_cache.get(key, [])
                if not isinstance(cached_players, list):
                    cached_players = []

                state_entry = acceptance_state.get(key, {}) or {}
                already_updated_today = state_entry.get("last_changed_date") == today_str
                fetched_today_no_change = (
                    state_entry.get("last_fetched_date") == today_str
                    and not already_updated_today
                )
                evening_already_fetched = (
                    state_entry.get("last_fetched_evening_date") == today_str
                )
                start_date_str = t_info.get("startDate", "")
                list_available = _itf_acceptance_list_available(start_date_str, _now)

                if is_current_week:
                    # Tournament week already started — use cache, don't hit API
                    tourney_players_list = list(cached_players)
                    itf_name_map = {}
                elif already_updated_today:
                    print(f"  ITF acceptance list already updated today, skipping fetch: {t_name}")
                    tourney_players_list = list(cached_players)
                    itf_name_map = {}
                elif fetched_today_no_change and _past_noon_utc and (
                    not _is_double_check_day or not _past_6pm_spain or evening_already_fetched
                ):
                    if _is_double_check_day and not _past_6pm_spain:
                        print(f"  ITF acceptance list unchanged this morning, will re-check after 6 pm Spain: {t_name}")
                    else:
                        print(f"  ITF acceptance list unchanged through noon UTC, skipping: {t_name}")
                    tourney_players_list = list(cached_players)
                    itf_name_map = {}
                elif not list_available:
                    # Entry list not published yet (before Friday 3 weeks prior)
                    tourney_players_list = list(cached_players)
                    itf_name_map = {}
                else:
                    itf_entries, itf_name_map = get_itf_players(key, driver)
                    fresh_players = parse_itf_entry_list(itf_entries)
                    # Record the fetch attempt (success or failure) so we can
                    # stop retrying after noon UTC when nothing's changed.
                    state_entry = acceptance_state.setdefault(key, {})
                    if state_entry.get("last_fetched_date") != today_str:
                        state_entry["last_fetched_date"] = today_str
                        acceptance_state_dirty = True
                    if _is_double_check_day and _past_6pm_spain and state_entry.get("last_fetched_evening_date") != today_str:
                        state_entry["last_fetched_evening_date"] = today_str
                        acceptance_state_dirty = True
                    if fresh_players:
                        cached_fp = _acceptance_fingerprint(cached_players)
                        fresh_fp = _acceptance_fingerprint(fresh_players)
                        if cached_fp != fresh_fp:
                            print(f"  ITF acceptance list updated for: {t_name}")
                            state_entry["last_changed_date"] = today_str
                            acceptance_state_dirty = True
                        else:
                            print(f"  No changes in ITF acceptance list yet for: {t_name}")
                    else:
                        print(f"  Using cached ITF acceptance list (fetch failed): {t_name}")
                    tourney_players_list = merge_entry_list(cached_players, fresh_players)

                normalize_country_overrides(tourney_players_list, "name", "country")
                entry_cache[key] = tourney_players_list
                tournament_store[key] = tourney_players_list

                # Compute seeds for ITF main draws using WTA ranking one week before.
                # ≤24 MAIN entries → 32-draw (8 seeds); >24 → 64-draw (16 seeds).
                _main_count = sum(1 for _p in tourney_players_list if _p.get("type") == "MAIN")
                _num_seeds = 8 if _main_count <= 24 else 16
                _seed_date = (datetime.strptime(week_monday, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
                if _seed_date > current_monday_str:
                    _seed_date = current_monday_str
                if _seed_date not in ranking_cache:
                    ranking_cache[_seed_date] = get_wta_rankings_cached(_seed_date, nationality=None)
                _name_to_rank = {}
                for _sp in ranking_cache.get(_seed_date, []):
                    _sname = _sp.get("Player", "").strip().upper()
                    _srank = _sp.get("Rank")
                    if _sname and _srank is not None:
                        _name_to_rank[_sname] = int(_srank)
                _main_candidates = []
                for _p in tourney_players_list:
                    if _p.get("type") != "MAIN":
                        continue
                    _pname_up = NAME_LOOKUP.get(_p["name"].upper(), _p["name"].upper())
                    _wta_rank = _name_to_rank.get(_pname_up)
                    if _wta_rank is not None:
                        _main_candidates.append((_wta_rank, _p["name"]))
                _main_candidates.sort()
                _seed_map = {name: i + 1 for i, (_, name) in enumerate(_main_candidates[:_num_seeds])}
                for _p in tourney_players_list:
                    if _p.get("type") == "MAIN":
                        _sv = _seed_map.get(_p["name"])
                        _p["seed"] = _sv if _sv is not None else ""

                itf_player_meta = {}
                for p in tourney_players_list:
                    raw_upper = p.get('name', '').upper()
                    p_key = NAME_LOOKUP.get(raw_upper, raw_upper)
                    candidate = {
                        'priority': str(p.get('priority', '')).strip(),
                        'entry_type': p.get('type', ''),
                        'pos_num': p.get('pos_num', 9999),
                    }
                    prev = itf_player_meta.get(p_key)
                    if not prev:
                        itf_player_meta[p_key] = candidate
                        continue
                    prev_key = (
                        _entry_type_rank(prev.get('entry_type')),
                        _priority_num(prev.get('priority')),
                        _safe_pos_num(prev.get('pos_num')),
                    )
                    cand_key = (
                        _entry_type_rank(candidate.get('entry_type')),
                        _priority_num(candidate.get('priority')),
                        _safe_pos_num(candidate.get('pos_num')),
                    )
                    if cand_key < prev_key:
                        itf_player_meta[p_key] = candidate

                for p_name, suffix in itf_name_map.items():
                    if p_name not in arg_names_set:
                        continue
                    suffix_text = suffix.get('suffix', '') if isinstance(suffix, dict) else str(suffix or '')
                    p_meta = itf_player_meta.get(p_name, {})
                    _queue_itf_entry(
                        itf_schedule_pending,
                        p_name,
                        week,
                        key,
                        t_name,
                        suffix_text,
                        p_meta.get('priority', ''),
                        p_meta.get('entry_type', ''),
                        p_meta.get('pos_num', 9999),
                    )
                if not itf_name_map:
                    for p in tourney_players_list:
                        raw_upper = p.get('name', '').upper()
                        p_key = NAME_LOOKUP.get(raw_upper, raw_upper)
                        if p_key not in arg_names_set:
                            continue
                        p_meta = itf_player_meta.get(p_key, {})
                        _queue_itf_entry(
                            itf_schedule_pending,
                            p_key,
                            week,
                            key,
                            t_name,
                            _suffix_from_itf_player(p),
                            p_meta.get('priority', ''),
                            p_meta.get('entry_type', ''),
                            p_meta.get('pos_num', 9999),
                        )
                for p in tourney_players_list:
                    raw_upper = p['name'].upper()
                    p_key = NAME_LOOKUP.get(raw_upper, raw_upper)
                    if p_key in arg_names_set:
                        continue
                    if p.get('country', '') != 'ARG':
                        continue
                    p_type = p.get('type', '')
                    suffix = _suffix_from_itf_player(p)
                    _queue_itf_entry(
                        unranked_itf_pending,
                        p_key,
                        week,
                        key,
                        t_name,
                        suffix,
                        str(p.get('priority', '')).strip(),
                        p_type,
                        p.get('pos_num', 9999),
                    )

    if acceptance_state_dirty:
        _save_acceptance_state(acceptance_state)

    _flush_itf_pending(schedule_map, itf_schedule_pending)
    _flush_itf_pending(unranked_schedule, unranked_itf_pending)

    # Remove tournaments no longer in the next 4 weeks
    active_keys = set()
    active_itf_keys = set()
    for tourneys in tournament_groups.values():
        for t_key in tourneys.keys():
            active_keys.add(t_key)
            if not str(t_key).startswith("http"):
                active_itf_keys.add(t_key)

    # Defensive fallback: when ITF calendar fetch fails for a run, keep previous ITF
    # entry lists instead of deleting them from cache.
    if not active_itf_keys:
        for cached_key in entry_cache.keys():
            if not str(cached_key).startswith("http"):
                active_keys.add(cached_key)

    entry_cache = {k: v for k, v in entry_cache.items() if k in active_keys}

    return schedule_map, tournament_store, entry_cache, unranked_schedule


def load_match_history():
    """Read all match CSV files and return raw + cleaned/normalized rows."""
    match_history_data = []
    matches_files = [
        os.path.join(DATA_DIR, 'itf_matches_arg.csv'),
        os.path.join(DATA_DIR, 'wta_matches_arg.csv'),
        os.path.join(DATA_DIR, 'gs_matches_arg.csv'),
        os.path.join(DATA_DIR, 'og_matches_arg.csv'),
        os.path.join(DATA_DIR, 'bjkc_matches_arg.csv'),
        os.path.join(DATA_DIR, 'united_cup_matches_arg.csv'),
        os.path.join(DATA_DIR, 'manually_added_matches.csv'),
    ]
    for file_path in matches_files:
        try:
            with open(file_path, 'r', encoding='utf-8-sig') as file_obj:
                reader = csv.DictReader(file_obj, delimiter=',')
                for row in reader:
                    match_history_data.append(row)
        except Exception as e:
            print(f"Error reading matches data from {file_path}: {e}")

    cleaned_history = []
    for m in match_history_data:
        fecha = (m.get('date') or m.get('Date') or m.get('matchDate') or
                m.get('match_date') or m.get('FECHA') or '')

        winner_entry = m.get('winnerEntry') or m.get('winner_entry') or m.get('WinnerEntry') or ''
        loser_entry = m.get('loserEntry') or m.get('loser_entry') or m.get('LoserEntry') or ''
        winner_entry = winner_entry.strip().upper()
        loser_entry = loser_entry.strip().upper()
        winner_entry = 'LL' if winner_entry == 'L' else ('' if winner_entry == 'DA' else winner_entry)
        loser_entry = 'LL' if loser_entry == 'L' else ('' if loser_entry == 'DA' else loser_entry)

        raw_round = m.get('roundName') or m.get('round_name') or m.get('RoundName') or ''
        draw_type = m.get('draw') or m.get('Draw') or m.get('DRAW') or ''
        match_type_value = (m.get('matchType') or m.get('MatchType') or m.get('MATCH_TYPE') or '').strip()
        tournament_category_value = (m.get('tournamentCategory') or m.get('tournament_category') or m.get('TournamentCategory') or '').strip()
        tournament_name_value = (m.get('tournamentName') or m.get('tournament_name') or m.get('TournamentName') or '').strip()

        final_round = raw_round

        raw_surface = m.get('surface') or m.get('Surface') or ''
        in_or_outdoor = m.get('inOrOutdoor') or m.get('InOrOutdoor') or ''
        if raw_surface.startswith('I.'):
            formatted_surface = 'Ind. ' + raw_surface[2:].capitalize()
        elif in_or_outdoor == 'I':
            formatted_surface = 'Ind. ' + raw_surface
        else:
            formatted_surface = raw_surface

        tournament_id_value = (m.get('tournamentId') or m.get('tournament_id') or m.get('TournamentId') or '').strip()
        winner_id_value = (m.get('winnerId') or m.get('winner_id') or m.get('WinnerId') or '').strip()
        loser_id_value = (m.get('loserId') or m.get('loser_id') or m.get('LoserId') or '').strip()

        cleaned_history.append({
            'DATE': fecha,
            'TOURNAMENT': fix_encoding(tournament_name_value),
            'TOURNAMENT_ID': tournament_id_value,
            'CATEGORY': fix_encoding(tournament_category_value),
            'SURFACE': formatted_surface,
            'MATCH_TYPE': match_type_value,
            'DRAW': draw_type,
            'ROUND': final_round,
            'PLAYER': '',
            'ENTRY': '',
            'SEED': '',
            'RESULT': '',
            'SCORE': m.get('result') or m.get('Result') or '',
            'RIVAL_ENTRY': '',
            'RIVAL_SEED': '',
            'RIVAL': '',
            'RIVAL_COUNTRY': '',
            '_winnerId': winner_id_value,
            '_loserId': loser_id_value,
            '_winnerName': fix_encoding_keep_accents(m.get('winnerName') or m.get('winner_name') or m.get('WinnerName') or ''),
            '_loserName': fix_encoding_keep_accents(m.get('loserName') or m.get('loser_name') or m.get('LoserName') or ''),
            '_winnerCountry': m.get('winnerCountry') or m.get('winner_country') or m.get('WinnerCountry') or '',
            '_loserCountry': m.get('loserCountry') or m.get('loser_country') or m.get('LoserCountry') or '',
            '_winnerEntry': winner_entry,
            '_loserEntry': loser_entry,
            '_winnerSeed': m.get('winnerSeed') or m.get('winner_seed') or m.get('WinnerSeed') or '',
            '_loserSeed': m.get('loserSeed') or m.get('loser_seed') or m.get('LoserSeed') or '',
            '_resultStatusDesc': m.get('resultStatusDesc') or m.get('result_status_desc') or m.get('ResultStatusDesc') or ''
        })

    def parse_match_date(item):
        d = item.get('DATE') or "1900-01-01"
        try:
            return pd.to_datetime(d, dayfirst=False)
        except (ValueError, TypeError):
            return pd.to_datetime("1900-01-01")

    cleaned_history.sort(key=parse_match_date, reverse=True)

    return match_history_data, cleaned_history


def build_calendar_snapshot(calendar_data):
    """Deduplicate calendar data into snapshot list and save JSON."""
    calendar_snapshot = []
    seen = set()
    for week in calendar_data:
        week_label = week.get("week_label", "")
        columns = week.get("columns", {})
        for column_name, continents in columns.items():
            for continent, tournaments in continents.items():
                for t in tournaments:
                    key = (
                        week_label, column_name, continent,
                        t.get("name", ""), t.get("level", ""), t.get("surface", ""),
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    calendar_snapshot.append({
                        "week_label": week_label,
                        "column": column_name,
                        "continent": continent,
                        "name": t.get("name", ""),
                        "level": t.get("level", ""),
                        "surface": t.get("surface", ""),
                    })
    save_json_file(CALENDAR_SNAPSHOT_FILE, calendar_snapshot)


def main():
    gallery_path = os.path.join(DATA_DIR, "gallery.json")
    try:
        sync_result = sync_gallery_manifest(gallery_path)
        if sync_result.get("status") == "updated":
            print(
                f"Gallery sync: updated {sync_result.get('count', 0)} photos "
                f"from ImageKit root {sync_result.get('root', '/')}"
            )
        else:
            print(f"Gallery sync: skipped ({sync_result.get('reason', 'unknown')})")
    except Exception as e:
        print(f"Gallery sync: failed ({e})")

    driver = create_driver()
    itf_draws_tournaments = {}
    prefetched_itf_draws = {}
    try:
        # 1. Fetch full-year ITF calendar first (populates cache for dynamic subset)
        full_itf = get_full_itf_calendar(driver)

        # 2. Build tournament groups (WTA + ITF) — uses cached ITF data
        tournament_groups, monday_map = build_all_tournament_groups(driver)

        # 2b. Fetch ITF draws tournament list and prefetch draw payloads before
        # heavier ITF traffic later in the run.
        print("Fetching ITF draws tournament list...")
        itf_draws_tournaments = get_draws_itf_tournament_list(driver)
        if ENABLE_ITF_DRAWS_PREFETCH:
            itf_prefetch_jobs = []
            for week, tourneys in (itf_draws_tournaments or {}).items():
                for t_key, t_info in (tourneys or {}).items():
                    tid = (t_info or {}).get("tournamentId")
                    if not tid:
                        continue
                    itf_prefetch_jobs.append((week, t_key, t_info))

            total_itf_prefetch = len(itf_prefetch_jobs) or 1
            for i, (week, t_key, t_info) in enumerate(itf_prefetch_jobs, start=1):
                print(f"Prefetching ITF Draws ({i}/{total_itf_prefetch})")
                tid = t_info.get("tournamentId")
                is_multiweek = t_info.get("is_multiweek", False)
                dvr = create_driver()
                try:
                    t_draws = fetch_itf_tournament_draws(
                        tid, is_multiweek=is_multiweek, driver=dvr
                    ) or {}
                finally:
                    try:
                        dvr.quit()
                    except Exception:
                        pass
                if t_draws:
                    prefetched_itf_draws[_canonical_draw_store_key(t_key)] = t_draws

        # 3. Fetch ARG player rankings
        players_data, arg_names_set, all_wta_players = fetch_arg_players()

        # 4. Process tournament entry lists
        entry_cache = load_cache(ENTRY_LISTS_CACHE_FILE)
        schedule_map, tournament_store, entry_cache, unranked_schedule = process_tournaments(
            driver, tournament_groups, monday_map, arg_names_set, entry_cache
        )
        save_cache(ENTRY_LISTS_CACHE_FILE, entry_cache)

        # 4b. Override entry lists from authoritative PDFs (Grand Slams, etc.)
        _refresh_entry_lists_from_pdfs(entry_cache, tournament_store, tournament_groups, monday_map)
        save_cache(ENTRY_LISTS_CACHE_FILE, entry_cache)

        # 4b-seed. Compute seeds 1-32 for GS PDF entry lists (main and qualifying separately).
        _gs_now = datetime.now()
        _gs_seed_date = (_gs_now - timedelta(days=_gs_now.weekday())).strftime("%Y-%m-%d")
        _gs_rankings = get_wta_rankings_cached(_gs_seed_date, nationality=None)
        _gs_name_to_rank = {}
        for _sp in _gs_rankings:
            _sname = (_sp.get("Player") or "").strip().upper()
            _srank = _sp.get("Rank")
            if _sname and _srank is not None:
                _gs_name_to_rank[_sname] = int(_srank)
        for _gs_key in _get_pdf_cache_keys():
            _gs_players = tournament_store.get(_gs_key)
            if not _gs_players:
                continue
            _ptype = "QUAL" if _gs_key.endswith("#qual") else "MAIN"
            _candidates = []
            for _p in _gs_players:
                if _p.get("type") != _ptype:
                    continue
                _pname_up = NAME_LOOKUP.get(_p["name"].upper(), _p["name"].upper())
                _r = _gs_name_to_rank.get(_pname_up)
                if _r is not None:
                    _candidates.append((_r, _p["name"]))
            _candidates.sort()
            _gs_seed_map = {name: i + 1 for i, (_, name) in enumerate(_candidates[:32])}
            for _p in _gs_players:
                if _p.get("type") == _ptype:
                    _sv = _gs_seed_map.get(_p["name"])
                    _p["seed"] = _sv if _sv is not None else ""

        # 4c. Apply PDF entry lists to schedule_map for ARG players
        _apply_pdf_schedule_entries(
            tournament_store, tournament_groups, arg_names_set,
            schedule_map, unranked_schedule, players_data
        )

        # Add unranked ARG players found in entry lists to players_data and schedule_map
        existing_player_keys = {p['Player'] for p in players_data}
        for name_upper, weeks in unranked_schedule.items():
            schedule_map[name_upper] = weeks
            if name_upper not in existing_player_keys:
                players_data.append({
                    'Player': name_upper,
                    'Key': name_upper,
                    'Rank': '-'
                })

        # 5. Load match history
        match_history_data, cleaned_history = load_match_history()
        enrich_history_with_wta_ranks(cleaned_history)
        # Always rebuild history_data.json on each run
        try:
            history_data_path = os.path.join(DATA_DIR, "history_data.json")
            with open(history_data_path, "w", encoding="utf-8") as f:
                json.dump(cleaned_history or [], f, ensure_ascii=False, separators=(",", ":"))
        except Exception as e:
            print(f"Error writing history_data.json: {e}")

    except Exception:
        try:
            driver.quit()
        except Exception:
            pass
        raise

    # 6. Fetch draws (WTA + ITF). Keep a persistent cache so draws don't "disappear"
    # when a fetch fails temporarily.
    draws_store = load_cache(DRAWS_STORE_CACHE_FILE) or {}
    if not isinstance(draws_store, dict):
        draws_store = {}
    draws_store = _normalize_draws_store_keys(draws_store)
    draws_tournaments = get_draws_tournament_list()
    current_year = str(datetime.now().year)
    active_draw_keys = set()
    wta_draw_jobs = []
    for week, tourneys in (draws_tournaments or {}).items():
        for t_key, t_info in (tourneys or {}).items():
            active_draw_keys.add(_canonical_draw_store_key(t_key))
            wta_draw_jobs.append((week, t_key, t_info))

    total_wta_draws = len(wta_draw_jobs) or 1
    print(f"Fetching WTA Draws (0/{total_wta_draws}) — parallel")

    def _fetch_wta_draw_job(job):
        week, t_key, t_info = job
        return week, t_key, t_info, fetch_tournament_draws(t_key, current_year) or {}

    from concurrent.futures import ThreadPoolExecutor, as_completed
    wta_draw_results = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_fetch_wta_draw_job, job): job for job in wta_draw_jobs}
        done = 0
        for fut in as_completed(futures):
            done += 1
            try:
                week, t_key, t_info, t_draws = fut.result()
            except Exception as e:
                week, t_key, t_info = futures[fut]
                t_draws = {}
                print(f"  [!] WTA draw fetch failed for {t_info.get('name','')}: {e}")
            wta_draw_results[_canonical_draw_store_key(t_key)] = (week, t_key, t_info, t_draws)
            print(f"  WTA draw fetched ({done}/{total_wta_draws}): {t_info.get('name','')}")

    for store_key, (week, t_key, t_info, t_draws) in wta_draw_results.items():
        prev = draws_store.get(store_key) if isinstance(draws_store.get(store_key), dict) else {}
        prev_draws = (prev or {}).get("draws") or {}
        merged_draws = {}
        if isinstance(prev_draws, dict):
            merged_draws.update(prev_draws)
        if isinstance(t_draws, dict):
            for dtype_code, new_draw in t_draws.items():
                old_draw = merged_draws.get(dtype_code)
                # Don't overwrite a non-empty cached draw with an empty new fetch
                if (isinstance(old_draw, dict) and old_draw.get("players")
                        and isinstance(new_draw, dict) and not new_draw.get("players")):
                    print(f"  Keeping cached {dtype_code} for {t_info.get('name','')} (new fetch returned empty)")
                    continue
                merged_draws[dtype_code] = new_draw
        if merged_draws:
            fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t_draws else (prev or {}).get("fetchedAt")
            if not t_draws and prev_draws:
                print(f"  Using cached WTA draws for: {t_info.get('name','')}")
            draws_store[store_key] = {
                "name": t_info["name"],
                "level": t_info.get("level", ""),
                "week": week,
                "startDate": t_info.get("startDate"),
                "endDate": t_info.get("endDate"),
                "fetchedAt": fetched_at,
                "draws": merged_draws,
            }

    # 6b. Fetch ITF draws (prefer prefetched payloads captured earlier in the run)
    # process_tournaments (step 4) has already fetched and cached tournament IDs
    # via get_itf_players → itf_event_filters_cache.json. Use that cache to fill
    # any IDs that were missing when get_draws_itf_tournament_list ran earlier.
    _event_filters_cache = _load_itf_event_filters_cache()
    itf_draw_jobs = []
    for week, tourneys in (itf_draws_tournaments or {}).items():
        for t_key, t_info in (tourneys or {}).items():
            store_key = _canonical_draw_store_key(t_key)
            active_draw_keys.add(store_key)
            tid = (t_info or {}).get("tournamentId")
            if not tid and isinstance(t_key, str) and t_key.lower().startswith("w-itf-"):
                cached_tid = _event_filters_cache.get(t_key.lower())
                if isinstance(cached_tid, int) and cached_tid > 0:
                    tid = cached_tid
                    t_info["tournamentId"] = cached_tid
            if not tid:
                existing = draws_store.get(store_key) if isinstance(draws_store.get(store_key), dict) else {}
                existing_draws = existing.get("draws") if isinstance(existing.get("draws"), dict) else {}
                if existing_draws:
                    print(f"  Keeping cached ITF draws for: {t_info.get('name', '')} (missing tournamentId)")
                    draws_store[store_key] = _merge_draw_store_entry(existing, {
                        "name": t_info.get("name", ""),
                        "level": t_info.get("level", ""),
                        "week": week,
                        "startDate": t_info.get("startDate"),
                        "endDate": t_info.get("endDate"),
                        "draws": existing_draws,
                    })
                continue
            itf_draw_jobs.append((week, t_key, t_info))

    total_itf_draws = len(itf_draw_jobs) or 1
    itf_cooloff_applied = False
    itf_consecutive_empty = 0
    draw_fetch_errors = []
    for i, (week, t_key, t_info) in enumerate(itf_draw_jobs, start=1):
        print(f"Fetching ITF Draws ({i}/{total_itf_draws})")
        # Keep request cadence gentle to avoid ITF anti-bot throttling.
        # draws._fetch_itf_drawsheet bypasses itf.py's rate limiter, so pace here.
        if i > 1:
            time.sleep(random.uniform(*ITF_INTER_DRAW_SLEEP_RANGE))
        tid = t_info.get("tournamentId")
        is_multiweek = t_info.get("is_multiweek", False)
        store_key = _canonical_draw_store_key(t_key)
        prev = draws_store.get(store_key) if isinstance(draws_store.get(store_key), dict) else {}
        prev_draws = (prev or {}).get("draws") or {}

        # Skip fetching entirely if all cached draws are already complete.
        qs_complete = _draw_is_complete(prev_draws.get("QS"), is_qualifying=True)
        mds_complete = _draw_is_complete(prev_draws.get("MDS"))
        if mds_complete and (qs_complete or "QS" not in prev_draws):
            print(f"  Draws complete, using cache: {t_info.get('name','')}")
            draws_store[store_key] = _merge_draw_store_entry(prev, {
                "name": t_info["name"],
                "level": t_info.get("level", ""),
                "week": week,
                "startDate": t_info.get("startDate"),
                "endDate": t_info.get("endDate"),
                "draws": prev_draws,
            })
            continue

        t_draws = (
            prefetched_itf_draws.get(store_key)
            if isinstance(prefetched_itf_draws.get(store_key), dict)
            else {}
        )
        if not t_draws:
            t_draws = fetch_itf_tournament_draws(
                tid, is_multiweek=is_multiweek, driver=driver,
                cached_draws=prev_draws
            ) or {}
        if (
            not t_draws
            and not itf_cooloff_applied
            and i == 1
            and len(itf_draw_jobs) >= ITF_FIRST_BURST_MIN_JOBS
        ):
            # ITF often enforces a short temporary block after the tournament-id burst.
            # Wait once, then retry the same event with a fresh session.
            print(f"  ITF cooldown triggered ({ITF_FIRST_BURST_COOLDOWN_SEC}s) before retrying draw fetch...")
            time.sleep(ITF_FIRST_BURST_COOLDOWN_SEC)
            itf_cooloff_applied = True
            t_draws = fetch_itf_tournament_draws(
                tid, is_multiweek=is_multiweek, driver=driver,
                cached_draws=prev_draws
            ) or {}

        merged_draws = {}
        if isinstance(prev_draws, dict):
            merged_draws.update(prev_draws)
        if isinstance(t_draws, dict):
            merged_draws.update(t_draws)
        # Extra gap-fill pass: if one ITF draw type is missing, try a couple of
        # fresh sessions to recover the other type before falling back to cache.
        if set(merged_draws.keys()) != {"MDS", "QS"}:
            for _ in range(2):
                extra_draws = fetch_itf_tournament_draws(
                    tid, is_multiweek=is_multiweek, driver=driver,
                    cached_draws=merged_draws
                ) or {}
                if isinstance(extra_draws, dict):
                    merged_draws.update(extra_draws)
                if set(merged_draws.keys()) == {"MDS", "QS"}:
                    break
        if merged_draws:
            itf_consecutive_empty = 0
            fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t_draws else (prev or {}).get("fetchedAt")
            if not t_draws and prev_draws:
                print(f"  Using cached ITF draws for: {t_info.get('name','')}")
            draws_store[store_key] = {
                "name": t_info["name"],
                "level": t_info.get("level", ""),
                "week": week,
                "startDate": t_info.get("startDate"),
                "endDate": t_info.get("endDate"),
                "fetchedAt": fetched_at,
                "draws": merged_draws,
            }
        else:
            itf_consecutive_empty += 1
            draw_fetch_errors.append({
                "key": t_key,
                "name": t_info.get("name", t_key),
                "startDate": (t_info.get("startDate") or "")[:10],
            })
            if itf_consecutive_empty >= ITF_CONSECUTIVE_EMPTY_THRESHOLD and i < total_itf_draws:
                # Back off periodically to recover from temporary ITF throttling.
                print(f"  ITF backoff triggered ({ITF_CONSECUTIVE_EMPTY_BACKOFF_SEC}s) after consecutive empty draws — refreshing session.")
                time.sleep(ITF_CONSECUTIVE_EMPTY_BACKOFF_SEC)
                try:
                    driver.quit()
                except Exception:
                    pass
                driver = create_driver()
                itf_consecutive_empty = 0

    # Write draw fetch errors for this run (always overwrite so stale errors are cleared).
    save_json_file(DRAW_FETCH_ERRORS_FILE, draw_fetch_errors)

    # Prune draws for tournaments that are definitely over (endDate < today).
    today = datetime.now().date()
    keys_to_delete = []
    for t_key, tdata in (draws_store or {}).items():
        if not isinstance(tdata, dict):
            continue
        if _canonical_draw_store_key(t_key) in active_draw_keys:
            continue
        end = (tdata.get("endDate") or "")[:10]
        if not end:
            continue
        try:
            end_date = datetime.strptime(end, "%Y-%m-%d").date()
        except Exception:
            continue
        if end_date < today:
            keys_to_delete.append(t_key)
    for t_key in keys_to_delete:
        draws_store.pop(t_key, None)

    # Persist draws cache so a successful draw doesn't disappear on a later failed run.
    save_json_file(DRAWS_STORE_CACHE_FILE, draws_store)

    # Save draws snapshot (tournament key -> list of draw types available)
    draws_snapshot = {}
    for t_key, tdata in draws_store.items():
        draws_snapshot[t_key] = {
            "name": tdata["name"],
            "types": list(tdata.get("draws", {}).keys()),
        }
    save_json_file(os.path.join(DATA_DIR, "draws_snapshot.json"), draws_snapshot)

    try:
        driver.quit()
    except Exception:
        pass

    # 7. Build calendar — uses cached WTA data
    full_wta = get_full_wta_calendar()
    _manual_entries_file = os.path.join(DATA_DIR, "manual_calendar_entries.json")
    _manual_entries = json.load(open(_manual_entries_file, encoding="utf-8")) if os.path.exists(_manual_entries_file) else []
    calendar_data = build_calendar_data(full_wta + full_itf + _manual_entries)
    build_calendar_snapshot(calendar_data)

    # 7b. Build tournament strength data (cached)
    print("Processing WTA Tournament Strength")
    tstrength_data = build_tstrength_data()

    # 8. Generate HTML
    national_team_data = load_csv_rows(os.path.join(DATA_DIR, 'national_team_order.csv'), delimiter=';')
    captains_data = load_csv_rows(os.path.join(DATA_DIR, 'captains.csv'))

    generate_html(
        tournament_groups, tournament_store, players_data, schedule_map,
        cleaned_history, calendar_data, match_history_data, all_wta_players,
        national_team_data=national_team_data,
        captains_data=captains_data,
        draws_data=draws_store,
        tstrength_data=tstrength_data,
        monday_map=monday_map
    )


if __name__ == "__main__":
    main()
