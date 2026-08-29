import os
import sys

from python_bootstrap import ensure_project_environment

ensure_project_environment(os.path.dirname(os.path.abspath(__file__)))
if "--check-environment" in sys.argv:
    print(f"WTARG environment ready: {sys.executable} (Python {sys.version.split()[0]})")
    raise SystemExit(0)

import argparse
import copy
import csv
import html
import io
import json
import random
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta

import pandas as pd
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

from pipeline_errors import DataValidationError
from ranking_publication import effective_wta_ranking_date
from run_state import report_run_issue
from runtime_logging import configure_logging, get_logger
from runtime_paths import DATA_DIR as RUNTIME_DATA_DIR
from time_utils import MADRID, madrid_now, madrid_today, new_york_now, utc_now, utc_timestamp

try:
    import undetected_chromedriver as uc

    uc.Chrome.__del__ = lambda self: None
except Exception:
    uc = None

import contextlib

from calendar_builder import (
    build_calendar_data,
    format_week_label,
    generate_dynamic_monday_map,
    get_calendar_tournament_key,
    get_monday_offset,
    get_previous_monday,
)
from config import (
    ENTRY_LISTS_CACHE_FILE,
    ITF_ACCEPTANCE_STATE_FILE,
    NAME_LOOKUP,
    repair_name_text,
    resolve_player_display_name,
    resolve_player_presentation_name,
)
from draws import _draw_is_complete, fetch_itf_tournament_draws, fetch_tournament_draws, wta_draw_polling_open
from itf import (
    _load_itf_event_filters_cache,
    get_draws_itf_tournament_list,
    get_dynamic_itf_calendar,
    get_full_itf_calendar,
    get_itf_level,
    get_itf_players,
    get_itf_rankings_cached,
    parse_itf_entry_list,
)
from itf_drawsheet_cache import (
    tournament_draw_codes_with_definitive_no_nationality,
    tournament_ids_with_published_main_draw,
    tournament_ids_with_published_qualifying_draw,
)
from lazy_browser import LazyBrowserSession
from tournament_snapshot import (
    TournamentSnapshotRecord,
    dumps_tournament_snapshot,
    normalize_tournament_snapshot_key,
    normalize_tournament_snapshot_record,
)
from tstrength import build_tstrength_data
from utils import (
    compact_tournament_name,
    compress_calendar_snapshot,
    compress_draws_snapshot,
    compress_tournament_snapshot,
    dumps_calendar_snapshot,
    dumps_draws_store_cache,
    dumps_entry_lists_cache,
    expand_draws_store_cache,
    expand_entry_lists_cache,
    fix_encoding,
    fix_encoding_keep_accents,
    format_player_name,
    get_cache_timestamp,
    is_draw_completed,
    load_cache,
    mark_draw_completed,
    merge_entry_list,
    normalize_country_overrides,
    save_cache,
    save_json_file,
    set_cache_entry_meta,
    utc_now_iso,
)
from wta import (
    _load_wta_csv,
    build_tournament_groups,
    get_draws_tournament_list,
    get_full_wta_calendar,
    get_wta_rankings_cached,
    scrape_tournament_players,
)

logger = get_logger("main")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = str(RUNTIME_DATA_DIR)
TOURNAMENT_SNAPSHOT_FILE = os.path.join(DATA_DIR, "tournament_snapshot.json")
CALENDAR_SNAPSHOT_FILE = os.path.join(DATA_DIR, "calendar_snapshot.json")
DRAWS_STORE_CACHE_FILE = os.path.join(DATA_DIR, "draws_store_cache.json")
DRAW_FETCH_ERRORS_FILE = os.path.join(DATA_DIR, "draw_fetch_errors.json")
ENABLE_ITF_DRAWS_PREFETCH = False
HOURLY_PREFLIGHT_SCRIPTS = (
    ("weekly ranking", os.path.join(BASE_DIR, "populate_data", "load_weekly_ranking.py")),
    ("ITF loader", os.path.join(BASE_DIR, "populate_data", "itf_load_new.py")),
    ("WTA loader", os.path.join(BASE_DIR, "populate_data", "wta_load_new.py")),
    ("draw sizes updater", os.path.join(BASE_DIR, "populate_data", "tournament_sizes_update.py")),
)


def _subprocess_env():
    env = os.environ.copy()
    # Keep child scripts from crashing on Windows when they print Unicode status text.
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    return env


def _find_local_chromedriver():
    """Return a cached chromedriver.exe path if one exists locally."""
    base_dir = os.path.join(os.path.expanduser("~"), ".wdm", "drivers", "chromedriver", "win64")
    if not os.path.isdir(base_dir):
        return None

    def _version_key(name):
        parts = re.findall(r"\d+", name or "")
        return tuple(int(p) for p in parts[:4]) if parts else ()

    candidates = []
    for version_name in os.listdir(base_dir):
        version_dir = os.path.join(base_dir, version_name)
        if not os.path.isdir(version_dir):
            continue
        exe = os.path.join(version_dir, "chromedriver-win32", "chromedriver.exe")
        if os.path.exists(exe):
            candidates.append((version_name, exe))

    if not candidates:
        return None

    candidates.sort(key=lambda item: _version_key(item[0]), reverse=True)
    return candidates[0][1]


def _get_chrome_executable_path():
    candidates = []
    for exe in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(exe)
        if found:
            candidates.append(found)

    if os.name == "nt":
        for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env_name)
            if base:
                candidates.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))

    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def _get_chrome_major_version():
    chrome_path = _get_chrome_executable_path()
    if chrome_path and os.name == "nt":
        quoted_path = chrome_path.replace("'", "''")
        try:
            output = subprocess.check_output(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f"(Get-Item -LiteralPath '{quoted_path}').VersionInfo.ProductVersion",
                ],
                stderr=subprocess.STDOUT,
                text=True,
                timeout=5,
            )
            match = re.search(r"(\d+)\.", output or "")
            if match:
                return int(match.group(1))
        except (OSError, subprocess.SubprocessError, ValueError):
            # Continue through the executable-based version probes below.
            output = ""

    commands = []
    if chrome_path:
        commands.append([chrome_path, "--version"])
    for exe in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(exe)
        if found:
            commands.append([found, "--version"])

    for cmd in commands:
        try:
            output = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError, UnicodeError):
            continue
        match = re.search(r"(\d+)\.", output or "")
        if match:
            return int(match.group(1))
    return None


def _run_hourly_preflight():
    """Run the same data-refresh scripts the hourly workflow used to call separately."""
    timeout_seconds = int(os.getenv("WTARG_PREFLIGHT_TIMEOUT_SECONDS", "1500"))
    for label, script_path in HOURLY_PREFLIGHT_SCRIPTS:
        logger.info(f"Running {label} preflight...")
        subprocess.run(
            [sys.executable, script_path],
            check=True,
            env=_subprocess_env(),
            timeout=timeout_seconds,
        )


# ITF draw-fetch pacing — anti-bot recovery strategy.
# Sleep between requests, cooldown after the first burst (Incapsula often blocks
# right after the tournament-id resolution burst), and longer backoff if we keep
# getting empty draws back (likely a session-level block — driver is recreated).
ITF_INTER_DRAW_SLEEP_RANGE = (15.0, 30.0)
ITF_FIRST_BURST_COOLDOWN_SEC = 65
ITF_FIRST_BURST_MIN_JOBS = 6
ITF_CONSECUTIVE_EMPTY_BACKOFF_SEC = 35
ITF_CONSECUTIVE_EMPTY_THRESHOLD = 3
ITF_CONSECUTIVE_BLOCKED_BACKOFF_SEC = 35
ITF_CONSECUTIVE_BLOCKED_THRESHOLD = 3


ITF_BLOCKED_RESPONSES_FILE = os.path.join(DATA_DIR, "itf_blocked_responses.json")
GS_PDF_URLS_FILE = os.path.join(DATA_DIR, "gs_pdf_urls.json")
EXCLUDED_ENTRY_LIST_TOURNAMENT_IDS = {"903"}  # Roland Garros
EXCLUDED_DRAWS_TOURNAMENT_IDS = {"903", "904"}  # Roland Garros, Wimbledon
# Entry lists maintained manually by the project owner. These tournaments stay
# visible, but their cached player lists must not be replaced or merged with
# live WTA data during a refresh.
MANUAL_ENTRY_LIST_TOURNAMENT_IDS = {"1166"}  # Philadelphia 125


def _is_excluded_entry_list_tournament(t_key, t_info=None):
    """Return True when a tournament should be hidden from Schedule/Entry Lists."""
    key_text = str(t_key or "").lower()
    name_text = str((t_info or {}).get("name") or "").lower()
    if any(f"/tournaments/{tid}/" in key_text for tid in EXCLUDED_ENTRY_LIST_TOURNAMENT_IDS):
        return True
    return "roland garros" in name_text


def _is_manual_entry_list_tournament(t_key):
    """Return True when an entry list is frozen and maintained in the cache."""
    key_text = str(t_key or "").lower()
    return any(f"/tournaments/{tid}/" in key_text for tid in MANUAL_ENTRY_LIST_TOURNAMENT_IDS)


def _is_excluded_draw_tournament(t_key, t_info=None):
    """Return True when a tournament should be hidden from Draws."""
    key_text = str(t_key or "").lower()
    name_text = str((t_info or {}).get("name") or "").lower()
    if any(f"/tournaments/{tid}/" in key_text for tid in EXCLUDED_DRAWS_TOURNAMENT_IDS):
        return True
    return bool("roland garros" in name_text or "wimbledon" in name_text)


def _parse_gs_entry_list_pdf(pdf_bytes, alt_limit=10):
    """Parse a Grand Slam entry list PDF into (main_players, alt_players) lists.

    The PDF uses a two-column layout for the main draw (page 1) and single-column
    for the alternates section. Each player entry is parsed from per-column word
    tokens using positional rules (not regex on combined text).

    Strikethrough detection uses thin rectangles or lines (height<2, width<200)
    with both y and x overlap checks to correctly distinguish entries.

    Returns (main_players, alt_players[:alt_limit]).
    """
    import re

    import pdfplumber

    _STATUS_FLAGS = frozenset(["F", "A", "S", "None", "WC", "SE", "LL"])
    _ENTRY_FLAGS = frozenset(["WC", "SE", "LL", "A"])
    _PDF_COUNTRY_NORMALIZE = {
        "FR": "FRA",
        "ES": "ESP",
        "GB": "GBR",
        "UK": "GBR",
        "US": "USA",
        "ANDORRA": "AND",
        "ARGENTINA": "ARG",
        "ARMENIA": "ARM",
        "AUSTRALIA": "AUS",
        "AUSTRIA": "AUT",
        "BELGIUM": "BEL",
        "BRAZIL": "BRA",
        "BULGARIA": "BUL",
        "CANADA": "CAN",
        "CHINA": "CHN",
        "CHINESE TAIPEI": "TPE",
        "COLOMBIA": "COL",
        "CROATIA": "CRO",
        "CZECH REPUBLIC": "CZE",
        "DENMARK": "DEN",
        "ECUADOR": "ECU",
        "EGYPT": "EGY",
        "FRANCE": "FRA",
        "GEORGIA": "GEO",
        "GERMANY": "GER",
        "GREAT BRITAIN": "GBR",
        "GREECE": "GRE",
        "HUNGARY": "HUN",
        "INDIA": "IND",
        "INDONESIA": "INA",
        "ITALY": "ITA",
        "JAPAN": "JPN",
        "KAZAKHSTAN": "KAZ",
        "LATVIA": "LAT",
        "MEXICO": "MEX",
        "NETHERLANDS": "NED",
        "NEW ZEALAND": "NZL",
        "NORTH MACEDONIA": "MKD",
        "PHILIPPINES": "PHI",
        "POLAND": "POL",
        "PORTUGAL": "POR",
        "REPUBLIC OF KOREA": "KOR",
        "ROMANIA": "ROU",
        "SERBIA": "SRB",
        "SLOVAKIA": "SVK",
        "SLOVENIA": "SLO",
        "SPAIN": "ESP",
        "SWEDEN": "SWE",
        "SWITZERLAND": "SUI",
        "THAILAND": "THA",
        "TURKEY": "TUR",
        "UKRAINE": "UKR",
        "UNITED STATES OF AMERICA": "USA",
        "UZBEKISTAN": "UZB",
    }

    def _normalize_pdf_country(code):
        c = (code or "").strip().upper()
        if not c or c == "---":
            return ""
        if c == "GRC":
            return "GRE"
        return _PDF_COUNTRY_NORMALIZE.get(c, c)

    def _format_player_name(raw_name):
        """Normalize player name to display format (GivenName Surname)."""
        raw = str(raw_name or "").strip()
        if "," in raw:
            surname, given = raw.split(",", 1)
            raw = f"{given.strip()} {surname.strip()}"
        parts = [p for p in raw.split() if p]
        if not parts:
            return ""
        # FFT compact PDFs often render names as UPPERCASE surname + mixed-case given.
        # Reorder when the first mixed/lowercase token appears after index 0.
        first_mixed_idx = None
        for idx, part in enumerate(parts):
            if any(ch.islower() for ch in part):
                first_mixed_idx = idx
                break
        if first_mixed_idx is not None and first_mixed_idx > 0:
            parts = parts[first_mixed_idx:] + parts[:first_mixed_idx]
        return " ".join(parts).title()

    def _split_name_tail(name_tokens):
        """Strip trailing country and entry markers from Wimbledon name tokens."""
        tokens = list(name_tokens or [])
        country = ""
        entry = ""
        while tokens:
            tok = tokens[-1]
            up = str(tok).upper()
            if not entry and up in _ENTRY_FLAGS:
                entry = up
                tokens.pop()
                continue
            m_country = re.match(r"^\(([A-Z]{2,3})\)$", tok)
            if m_country and not country:
                country = _normalize_pdf_country(m_country.group(1))
                tokens.pop()
                continue
            m_country = re.match(r"^\(([A-Z]{1,3})$", tok)
            if m_country and not country:
                country = _normalize_pdf_country(m_country.group(1))
                tokens.pop()
                continue
            break
        return tokens, country, entry

    def _clean_wimbledon_name(raw_name, country=""):
        """Remove stray inline country markers left behind by PDF text extraction."""
        text = " ".join(str(raw_name or "").split()).strip()
        if not text:
            return "", country
        if not country:
            m_country = re.search(r"\(([A-Za-z]{2,3})\)?", text)
            if m_country:
                country = _normalize_pdf_country(m_country.group(1))
        text = re.sub(r"\s*\(([A-Za-z]{1,3})\)?\s*", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text, country

    def _parse_rank_table_entry_tokens(seg_tokens):
        """Parse one Wimbledon entry from tokenized text."""
        if not seg_tokens or not seg_tokens[0].isdigit():
            return None

        pos_num = int(seg_tokens[0])
        tokens = seg_tokens[1:]
        if not tokens:
            return None

        # Rank is the last numeric token; optional SR immediately before it.
        rank_idx = None
        for i in range(len(tokens) - 1, -1, -1):
            if re.match(r"^\d{1,4}$", tokens[i]):
                rank_idx = i
                break
        if rank_idx is None:
            return None
        rank_num = int(tokens[rank_idx])
        has_sr = rank_idx > 0 and tokens[rank_idx - 1].upper() == "SR"
        name_tokens = tokens[: rank_idx - 1] if has_sr else tokens[:rank_idx]
        name_tokens, country, entry = _split_name_tail(name_tokens)
        raw_name = " ".join(name_tokens).strip()
        raw_name, country = _clean_wimbledon_name(raw_name, country)
        if not raw_name:
            return None

        rank_text = f"{rank_num} SR" if has_sr else str(rank_num)
        return pos_num, raw_name, country, entry, rank_num, rank_text

    def _parse_rank_table_line_entries(line_text):
        """Parse Wimbledon rank-table line text into one or more entries."""
        text = " ".join(str(line_text or "").strip().split())
        if not text:
            return []
        if not re.match(r"^\d+\s", text):
            return []

        # Primary parser: consume repeated "{pos} {name} [(CC)] [entry] [SR] {rank}" chunks.
        # The lookahead ensures we stop before the next entry start or end-of-line.
        entry_pat = re.compile(
            r"""
            (?P<pos>\d{1,3})\s+
            (?P<name>.+?)
            (?:\s+\((?P<country>[A-Z]{2,3})\))?
            (?:\s+(?P<entry>WC|SE|LL|A))?
            \s+(?:(?P<sr>SR)\s+)?
            (?P<rank>\d{1,4})
            (?=\s+\d{1,3}\s+[A-Z]|$)
            """,
            re.VERBOSE,
        )
        parsed_entries = []
        for m in entry_pat.finditer(text):
            try:
                pos_num = int(m.group("pos"))
                raw_name = (m.group("name") or "").strip()
                country = _normalize_pdf_country(m.group("country") or "")
                entry = (m.group("entry") or "").upper().strip()
                rank_num = int(m.group("rank"))
            except (AttributeError, TypeError, ValueError):
                continue
            if not raw_name:
                continue
            raw_name, country = _clean_wimbledon_name(raw_name, country)
            rank_text = f"{rank_num} SR" if (m.group("sr") or "").upper() == "SR" else str(rank_num)
            parsed_entries.append((pos_num, raw_name, country, entry, rank_num, rank_text))
        if parsed_entries:
            return parsed_entries

        # Fallback parser: token slicing for odd line-breaks/truncated country markers.
        tokens = text.split()
        starts = [0]
        for i in range(1, len(tokens)):
            if not tokens[i].isdigit():
                continue
            # A new entry start is usually followed by an uppercase surname token.
            if i + 1 < len(tokens) and re.match(r"^[A-Z][A-Z'`.-]*$", tokens[i + 1]):
                starts.append(i)
        starts = sorted(set(starts))
        for idx, start in enumerate(starts):
            end = starts[idx + 1] if idx + 1 < len(starts) else len(tokens)
            entry = _parse_rank_table_entry_tokens(tokens[start:end])
            if entry:
                parsed_entries.append(entry)
        return parsed_entries

    def _parse_rank_table_layout(pdf_obj, alt_limit):
        """Parse Wimbledon-style pages using layout-preserving text extraction."""
        parsed_main = []
        parsed_alt = []

        for page in pdf_obj.pages:
            page_text = page.extract_text(layout=True) or page.extract_text() or ""
            page_upper = page_text.upper()
            page_type = "ALT" if "ALTERNATES" in page_upper else "MAIN"
            for line in page_text.splitlines():
                entries = _parse_rank_table_line_entries(line)
                for pos_num, raw_name, country, entry, rank_num, rank_text in entries:
                    player = {
                        "name": _format_player_name(raw_name),
                        "country": country,
                        "rank_num": rank_num,
                        "rank": rank_text,
                        "pos": str(pos_num),
                        "pos_num": pos_num,
                        "type": page_type,
                    }
                    if entry:
                        player["entry"] = entry
                    if page_type == "ALT":
                        parsed_alt.append(player)
                    else:
                        parsed_main.append(player)

        if not parsed_main:
            return [], []

        parsed_main.sort(key=lambda p: p["pos_num"])
        parsed_alt.sort(key=lambda p: p["pos_num"])
        alt_capped = parsed_alt[:alt_limit]
        for i, p in enumerate(alt_capped, 1):
            p["pos"] = str(i)
            p["pos_num"] = i
        return parsed_main, alt_capped

    def _parse_tokens(tokens):
        """Parse word tokens for one player entry.
        Returns (pos_num, name, country, entry, rank_num) or None."""
        if len(tokens) < 4:
            return None
        if not tokens[0].isdigit():
            return None
        pos_num = int(tokens[0])
        if tokens[1] not in _STATUS_FLAGS:
            return None
        entry_flag = tokens[1].upper() if tokens[1].upper() in _ENTRY_FLAGS else ""
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
            country = _normalize_pdf_country(tokens[end])
            end -= 1
        name_parts = tokens[2 : end + 1]
        if not name_parts:
            return None
        name, country = _clean_wimbledon_name(" ".join(name_parts), country)
        return pos_num, name, country, entry_flag, rank_num

    def _parse_compact_entry_list_line(line_text):
        """Parse 1-2 compact entries from a single line.

        Newer GS PDFs sometimes render one line as:
        '1 SURNAME Name (USA) 12 65 OTHER Player (FRA) 90'
        """
        tokens = (line_text or "").strip().split()
        if not tokens or not tokens[0].isdigit():
            return []

        entries = []
        i = 0
        while i < len(tokens):
            if not tokens[i].isdigit():
                break
            pos_num = int(tokens[i])
            i += 1

            name_parts = []
            country = ""
            entry = ""
            rank_token_from_country = ""
            saw_country_marker = False

            while i < len(tokens):
                tok = tokens[i]
                if not entry and tok.upper() in _ENTRY_FLAGS:
                    entry = tok.upper()
                    i += 1
                    continue
                if re.match(r"^\([A-Z-]{3}\)$", tok):
                    country = _normalize_pdf_country(tok[1:-1])
                    saw_country_marker = True
                    i += 1
                    continue
                # Some FFT rows are malformed like "(FR135" (missing ')' and merged with rank).
                mc = re.match(r"^\(([A-Z-]{2,3})(\d+\*?)$", tok)
                if mc:
                    country = _normalize_pdf_country(mc.group(1))
                    rank_token_from_country = mc.group(2)
                    saw_country_marker = True
                    i += 1
                    break
                if re.match(r"^\d+\*?$", tok):
                    break
                name_parts.append(tok)
                i += 1

            if i < len(tokens) and re.match(r"^\d+\*?$", tokens[i]):
                rank_num = int(re.sub(r"\D", "", tokens[i]))
                i += 1
            elif rank_token_from_country:
                rank_num = int(re.sub(r"\D", "", rank_token_from_country))
            else:
                break

            # Ignore non-player lines that accidentally look numeric (e.g. "24 mai - 07 juin").
            # Some valid rows have no country marker, so allow them with extra validation.
            if name_parts:
                if not saw_country_marker:
                    lowered_parts = [p.lower() for p in name_parts]
                    month_tokens = {
                        "janvier",
                        "fevrier",
                        "février",
                        "mars",
                        "avril",
                        "mai",
                        "juin",
                        "juillet",
                        "aout",
                        "août",
                        "septembre",
                        "octobre",
                        "novembre",
                        "decembre",
                        "décembre",
                    }
                    if "-" in name_parts:
                        continue
                    if any(p in month_tokens for p in lowered_parts):
                        continue
                name, country = _clean_wimbledon_name(" ".join(name_parts), country)
                entries.append((pos_num, name, country, entry, rank_num))

        return entries

    def _parse_compact_layout(pdf_obj):
        """Fallback parser for single-line compact PDFs without section headers."""
        compact_main = []
        with_country_missing = 0
        for page in pdf_obj.pages:
            words = page.extract_words(keep_blank_chars=False, x_tolerance=3, y_tolerance=3)
            lines = {}
            for w in words:
                y = round(float(w["top"]), 1)
                lines.setdefault(y, []).append(w)

            for _, word_list in sorted(lines.items()):
                word_list.sort(key=lambda w: float(w["x0"]))
                line_text = " ".join(w["text"] for w in word_list).strip()
                if not line_text or not re.match(r"^\d+\s", line_text):
                    continue
                for pos_num, name, country, entry, rank_num in _parse_compact_entry_list_line(line_text):
                    if not country:
                        with_country_missing += 1
                    compact_main.append(
                        {
                            "name": _format_player_name(name),
                            "country": country,
                            "entry": entry,
                            "rank_num": rank_num,
                            "rank": str(rank_num),
                            "pos": str(pos_num),
                            "pos_num": pos_num,
                            "type": "MAIN",
                        }
                    )

        compact_main.sort(key=lambda p: p["pos_num"])
        for i, p in enumerate(compact_main, 1):
            p["pos"] = str(i)
            p["pos_num"] = i
        return compact_main

    def _parse_us_open_layout(pdf_obj, alt_limit):
        """Parse the USTA Rank/S/Name/Country layout used by US Open PDFs."""
        parsed_main = []
        parsed_alt = []
        main_withdrawals = 0
        section = "MAIN"

        for page in pdf_obj.pages:
            page_text = page.extract_text() or ""
            if "ALTERNATES TO MAIN DRAW" in page_text.upper():
                section = "ALT"

            struck_segments = [
                segment
                for segment in [*(page.rects or []), *(page.lines or [])]
                if segment.get("height", 99) < 2 and 8 < segment.get("width", 0) < 200
            ]

            words = page.extract_words(keep_blank_chars=False, x_tolerance=2, y_tolerance=2)
            lines = {}
            for word in words:
                y = round(float(word["top"]), 1)
                lines.setdefault(y, []).append(word)

            for _, line_words in sorted(lines.items()):
                line_words.sort(key=lambda word: float(word["x0"]))
                rank_words = [
                    word for word in line_words if float(word["x0"]) < 100 and re.fullmatch(r"\d{1,4}", word["text"])
                ]
                if len(rank_words) != 1:
                    continue

                name_words = [word["text"] for word in line_words if 140 <= float(word["x0"]) < 310]
                if not name_words:
                    continue

                rank_num = int(rank_words[0]["text"])
                has_special_rank = any(
                    word["text"].upper() == "S" and 100 <= float(word["x0"]) < 140 for word in line_words
                )
                raw_name = re.sub(r"\s*\*\s*$", "", " ".join(name_words)).strip()
                country_text = " ".join(word["text"] for word in line_words if float(word["x0"]) >= 310).strip()
                country = _PDF_COUNTRY_NORMALIZE.get(country_text.upper(), "")
                if not raw_name:
                    continue

                if _is_struck(line_words, struck_segments):
                    if section == "MAIN":
                        main_withdrawals += 1
                    continue

                target = parsed_alt if section == "ALT" else parsed_main
                pos_num = len(target) + 1
                target.append(
                    {
                        "name": _format_player_name(raw_name),
                        "country": country,
                        "rank_num": rank_num,
                        "rank": f"SR {rank_num}" if has_special_rank else str(rank_num),
                        "pos": str(pos_num),
                        "pos_num": pos_num,
                        "type": section,
                    }
                )

        # USTA leaves withdrawn direct acceptances struck through in place and
        # moves the first eligible alternates into the main draw. Keep the main
        # draw at its original size and remove promoted players from alternates.
        promoted = parsed_alt[:main_withdrawals]
        remaining_alt = parsed_alt[main_withdrawals:]
        for player in promoted:
            player["type"] = "MAIN"
            player["pos_num"] = len(parsed_main) + 1
            player["pos"] = str(player["pos_num"])
            parsed_main.append(player)
        for pos_num, player in enumerate(remaining_alt[:alt_limit], 1):
            player["pos_num"] = pos_num
            player["pos"] = str(pos_num)

        return parsed_main, remaining_alt[:alt_limit]

    def _is_struck(col_words, struck_rects):
        if not struck_rects or not col_words:
            return False
        y_mid = float(col_words[0]["top"]) + float(col_words[0].get("height", 10)) / 2
        col_x0 = min(float(w["x0"]) for w in col_words)
        col_x1 = max(float(w.get("x1", w["x0"])) for w in col_words)
        return any(
            r["top"] <= y_mid <= r["bottom"] and r["x0"] < col_x1 and r["x0"] + r.get("width", 0) > col_x0
            for r in struck_rects
        )

    main_players = []
    alt_players = []
    moved_in_players = []
    section = None  # "MAIN", "MOVED_IN", "ALTERNATES"

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        document_text = "\n".join((page.extract_text() or "") for page in pdf.pages[:2]).upper()
        if "US OPEN" in document_text and "ENTRY LIST" in document_text:
            return _parse_us_open_layout(pdf, alt_limit)

        for page in pdf.pages:
            mid_x = page.width / 2
            struck_rects = [r for r in (page.rects or []) if r.get("height", 99) < 2 and 8 < r.get("width", 0) < 200]

            words = page.extract_words(keep_blank_chars=False, x_tolerance=3, y_tolerance=3)
            lines = {}
            for w in words:
                y = round(float(w["top"]), 1)
                lines.setdefault(y, []).append(w)

            for _y_top, word_list in sorted(lines.items()):
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
                    pos_num, name, country, entry_flag, rank_num = entry

                    if _is_struck(col_words, struck_rects):
                        continue

                    player = {
                        "name": _format_player_name(name),
                        "country": country,
                        "entry": entry_flag,
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

    all_main = sorted(main_players, key=lambda p: p["pos_num"]) + sorted(moved_in_players, key=lambda p: p["pos_num"])
    for i, p in enumerate(all_main, 1):
        p["pos"] = str(i)
        p["pos_num"] = i

    alt_players.sort(key=lambda p: p["pos_num"])
    alt_capped = alt_players[:alt_limit]
    for i, p in enumerate(alt_capped, 1):
        p["pos"] = str(i)
        p["pos_num"] = i

    if all_main:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            rank_table_main, rank_table_alt = _parse_rank_table_layout(pdf, alt_limit)
        if len(rank_table_main) > len(all_main) or (rank_table_alt and not alt_capped):
            return rank_table_main, rank_table_alt
        return all_main, alt_capped

    # Fallback for newer compact-list PDFs that have no MAIN/ALTERNATES headers
    # and no status/pref columns.
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        rank_table_main, rank_table_alt = _parse_rank_table_layout(pdf, alt_limit)
        if rank_table_main:
            return rank_table_main, rank_table_alt
        compact_main = _parse_compact_layout(pdf)
    return compact_main, []


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
        except (OSError, csv.Error, UnicodeError) as exc:
            raise DataValidationError(
                component="main",
                operation="load ranking country lookup",
                message=f"cannot read rankings CSV: {csv_path}",
                context={"path": str(csv_path), "cause": str(exc)},
            ) from exc
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
        logger.info(f"[PDF] Filled {filled} missing country codes from WTA Rankings")


def _canonicalize_player_names(players, source="", names_only=False):
    """Map player rows to canonical names, preferring their source ID.

    Entry Lists use ``names_only`` to retain canonical ID matching while
    presenting the explicitly configured public name without disambiguators.
    """
    changed = 0
    for player in players or []:
        if not isinstance(player, dict):
            continue
        raw_name = str(player.get("name") or "").strip()
        if not raw_name:
            continue
        player_id = str(
            player.get("player_id") or player.get("itf_id") or player.get("wta_id") or player.get("playerId") or ""
        ).strip()
        resolver = resolve_player_presentation_name if names_only else resolve_player_display_name
        mapped_name = resolver(source, player_id=player_id, name=raw_name)
        if mapped_name != raw_name:
            player["name"] = mapped_name
            changed += 1
    if changed:
        logger.info(f"[Alias] Canonicalized {changed} player names")
    return players


def _normalize_schedule_text(text):
    """Normalize a schedule label/cell for robust duplicate detection."""
    raw = str(text or "")
    raw = re.sub(r"(?i)<br\\s*/?>", "\n", raw)
    raw = raw.replace("</div>", "\n")
    raw = re.sub(r"<[^>]+>", "", raw)
    raw = html.unescape(raw)
    return re.sub(r"\s+", " ", raw).strip().lower()


def _schedule_cell_contains_label(cell_html, label):
    """Return True when a rendered schedule cell already contains a label."""
    normalized_label = _normalize_schedule_text(label)
    if not normalized_label:
        return False
    raw = str(cell_html or "")
    raw = re.sub(r"(?i)<br\\s*/?>", "\n", raw)
    raw = raw.replace("</div>", "\n")
    raw = re.sub(r"<[^>]+>", "", raw)
    raw = html.unescape(raw)
    return any(_normalize_schedule_text(line) == normalized_label for line in raw.splitlines())


def _append_schedule_label(target_map, player_key, week_label, label, style="append_div"):
    """Append one schedule label with style, skipping duplicates per player/week."""
    if not player_key or not week_label or not label:
        return False
    weeks = target_map.setdefault(player_key, {})
    existing = weeks.get(week_label, "")
    if _schedule_cell_contains_label(existing, label):
        return False
    if not existing:
        weeks[week_label] = label
    elif style == "prepend_br":
        weeks[week_label] = f"{label}<br>{existing}"
    elif style == "append_br":
        weeks[week_label] = f"{existing}<br>{label}"
    else:
        weeks[week_label] = f'{existing}<div style="margin-top: 3px;">{label}</div>'
    return True


def _schedule_tournament_name(cache_key, tournament_name):
    """Return the Schedule label without a redundant qualifying descriptor."""
    name = str(tournament_name or "").strip()
    if str(cache_key or "").endswith("#qual"):
        name = re.sub(r"\s+Qualifying\s*$", "", name, flags=re.IGNORECASE)
    return compact_tournament_name(name)


def _get_pdf_cache_keys():
    """Return the set of tournament_store keys that are PDF-sourced (main + #qual)."""
    try:
        with open(GS_PDF_URLS_FILE, encoding="utf-8") as f:
            pdf_urls = json.load(f)
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return set()
    keys = set()
    for cache_key, url_config in pdf_urls.items():
        if _is_excluded_entry_list_tournament(cache_key, {}):
            continue
        keys.add(cache_key)
        if isinstance(url_config, dict) and "qual" in url_config:
            keys.add(cache_key + "#qual")
    return keys


def _apply_manual_entry_list_withdrawals(main_players, alt_players, withdrawals, main_type="MAIN"):
    """Remove configured withdrawals, promote alternates, and renumber both lists."""
    withdrawal_names = {str(name or "").strip().casefold() for name in (withdrawals or []) if str(name or "").strip()}
    remaining_main = [
        player for player in main_players if str(player.get("name") or "").strip().casefold() not in withdrawal_names
    ]
    removed_count = len(main_players) - len(remaining_main)
    promoted = alt_players[:removed_count]
    remaining_alt = alt_players[removed_count:]
    updated_main = remaining_main + promoted

    for pos_num, player in enumerate(updated_main, 1):
        player["type"] = main_type
        player["pos"] = str(pos_num)
        player["pos_num"] = pos_num
    for pos_num, player in enumerate(remaining_alt, 1):
        player["type"] = "ALT"
        player["pos"] = str(pos_num)
        player["pos_num"] = pos_num

    return updated_main, remaining_alt


def _apply_manual_entry_list_additions(main_players, alt_players, additions, main_type="MAIN"):
    """Append configured players by ranking, remove them from alternates, and renumber."""
    configured = []
    for config_index, addition in enumerate(additions or []):
        if not isinstance(addition, dict):
            continue
        name = str(addition.get("name") or "").strip()
        if not name:
            continue
        rank_match = re.search(r"\d+", str(addition.get("rank") or ""))
        rank = int(rank_match.group()) if rank_match else float("inf")
        configured.append((rank, config_index, name.casefold(), addition))

    if not configured:
        return main_players, alt_players

    configured.sort(key=lambda item: (item[0], item[1]))
    main_by_name = {str(player.get("name") or "").strip().casefold(): player for player in main_players}
    configured_names = {name_key for _, _, name_key, _ in configured}
    # Configured additions belong together at the end of the list. Removing
    # an already-promoted player here also makes the ordering deterministic.
    main_players = [
        player for player in main_players if str(player.get("name") or "").strip().casefold() not in configured_names
    ]
    remaining_alt = [
        player for player in alt_players if str(player.get("name") or "").strip().casefold() not in configured_names
    ]

    for _, _, name_key, addition in configured:
        player = main_by_name.get(name_key)
        if player is None:
            player = {"name": str(addition["name"]).strip()}
            main_by_name[name_key] = player
        for field in ("name", "country", "rank", "priority", "entry", "player_id"):
            if field in addition:
                player[field] = addition[field]
        player.setdefault("country", "")
        player.setdefault("rank", "")
        player.setdefault("priority", "")
        player.setdefault("entry", "")
        main_players.append(player)

    for pos_num, player in enumerate(main_players, 1):
        player["type"] = main_type
        player["pos"] = str(pos_num)
        player["pos_num"] = pos_num
    for pos_num, player in enumerate(remaining_alt, 1):
        player["type"] = "ALT"
        player["pos"] = str(pos_num)
        player["pos_num"] = pos_num

    return main_players, remaining_alt


def _apply_cached_manual_entry_list_overrides(entry_cache, pdf_urls):
    """Apply configured withdrawals/additions without refreshing external sources."""
    for cache_key, url_config in (pdf_urls or {}).items():
        if not isinstance(url_config, dict):
            continue
        for draw_type, draw_meta in url_config.items():
            if not isinstance(draw_meta, dict):
                continue
            if not draw_meta.get("withdrawals") and not draw_meta.get("additions"):
                continue

            target_key = cache_key + "#qual" if draw_type == "qual" else cache_key
            cached_players = copy.deepcopy(entry_cache.get(target_key) or [])
            if not cached_players:
                continue

            main_type = "QUAL" if draw_type == "qual" else "MAIN"
            main_players = [player for player in cached_players if player.get("type") == main_type]
            alt_players = [player for player in cached_players if player.get("type") == "ALT"]
            main_players, alt_players = _apply_manual_entry_list_withdrawals(
                main_players,
                alt_players,
                draw_meta.get("withdrawals"),
                main_type=main_type,
            )
            main_players, alt_players = _apply_manual_entry_list_additions(
                main_players,
                alt_players,
                draw_meta.get("additions"),
                main_type=main_type,
            )

            seed_candidates = []
            for player in main_players:
                seed_rank = player.get("seed_rank")
                if isinstance(seed_rank, int):
                    seed_candidates.append((seed_rank, str(player.get("name") or "")))
            seed_candidates.sort()
            seed_map = {name: seed for seed, (_, name) in enumerate(seed_candidates[:32], 1)}
            for player in main_players:
                player["seed"] = seed_map.get(str(player.get("name") or ""), "")

            entry_cache[target_key] = main_players + alt_players

    return entry_cache


def _refresh_entry_lists_from_pdfs(
    entry_cache,
    tournament_store,
    tournament_groups=None,
    monday_map=None,
    original_entry_cache=None,
):
    """Fetch configured Grand Slam PDFs and override active entry lists in-place."""
    import requests

    from wta import format_week_label, get_monday_from_date

    try:
        with open(GS_PDF_URLS_FILE, encoding="utf-8") as f:
            pdf_urls = json.load(f)
    except FileNotFoundError:
        return
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as e:
        logger.warning(f"[PDF] Failed to load {GS_PDF_URLS_FILE}: {e}")
        return

    pdf_session = requests.Session()
    pdf_session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/144 Safari/537.36",
            "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
        }
    )

    def _inject_group(cache_key, draw_type, draw_meta):
        if tournament_groups is None or monday_map is None or not isinstance(draw_meta, dict):
            return False
        start_date = str(draw_meta.get("start_date") or "")[:10]
        if not start_date:
            return False
        try:
            monday = get_monday_from_date(start_date)
        except (TypeError, ValueError):
            return False

        week_label = format_week_label(monday)
        target_key = cache_key if draw_type == "main" else cache_key + "#qual"
        if draw_type == "main":
            # A configured main-draw date is authoritative. It may intentionally
            # add the next visible Schedule week before the WTA feed does.
            monday_map.setdefault(monday.strftime("%Y-%m-%d"), week_label)
            for other_week, week_tourneys in tournament_groups.items():
                if other_week != week_label:
                    week_tourneys.pop(target_key, None)
        elif week_label not in monday_map.values():
            return False

        existing_info = {}
        for week_tourneys in tournament_groups.values():
            candidate = week_tourneys.get(cache_key)
            if isinstance(candidate, dict):
                existing_info = candidate
                break
        default_name = existing_info.get("name", "Grand Slam")
        if draw_type == "qual":
            default_name = f"{default_name.replace('Grand Slam ', '').strip()} Qualifying"
        tournament_groups.setdefault(week_label, {})[target_key] = {
            "name": draw_meta.get("display_name", default_name),
            "level": draw_meta.get("level", existing_info.get("level", "Grand Slam")),
            "surface": draw_meta.get("surface", existing_info.get("surface", "")),
            "country": draw_meta.get("country", existing_info.get("country", "")),
            "startDate": start_date,
            "endDate": draw_meta.get("end_date", existing_info.get("endDate")),
        }
        return True

    def _is_active(target_key):
        if tournament_groups is None:
            return True
        return any(target_key in week_tourneys for week_tourneys in tournament_groups.values())

    for cache_key, url_config in pdf_urls.items():
        if _is_excluded_entry_list_tournament(cache_key, {}):
            continue
        if isinstance(url_config, str):
            url_config = {"main": url_config}

        def _restore_cached_list(target_key):
            if original_entry_cache is None or target_key not in original_entry_cache:
                return False
            restored = copy.deepcopy(original_entry_cache[target_key])
            entry_cache[target_key] = restored
            tournament_store[target_key] = copy.deepcopy(restored)
            return True

        main_players = []
        for draw_type, draw_config in url_config.items():
            if isinstance(draw_config, str):
                pdf_url = draw_config
                draw_meta = {}
            else:
                pdf_url = (draw_config or {}).get("url", "")
                draw_meta = draw_config or {}

            target_key = cache_key if draw_type != "qual" else cache_key + "#qual"
            _inject_group(cache_key, draw_type, draw_meta)
            if not pdf_url:
                continue
            if not _is_active(target_key):
                continue

            logger.debug(f"[PDF] Fetching {draw_type} entry list PDF for {cache_key}")
            try:
                response = pdf_session.get(pdf_url, timeout=30)
                response.raise_for_status()
                pdf_bytes = response.content
                if not bytes(pdf_bytes).startswith(b"%PDF-"):
                    raise ValueError(f"Unexpected non-PDF response for {pdf_url}")
            except Exception as e:
                _restore_cached_list(target_key)
                logger.warning(f"[PDF] Download failed for {pdf_url}: {e}")
                continue

            try:
                alt_limit = 20 if draw_type == "qual" else 10
                if draw_meta.get("alt_limit") is not None:
                    with contextlib.suppress(TypeError, ValueError):
                        alt_limit = int(draw_meta["alt_limit"])
                draw_main, draw_alt = _parse_gs_entry_list_pdf(pdf_bytes, alt_limit=alt_limit)
            except Exception as e:
                _restore_cached_list(target_key)
                logger.warning(f"[PDF] Parse failed for {pdf_url}: {e}")
                continue

            if not draw_main:
                _restore_cached_list(target_key)
                logger.warning(f"[PDF] No players parsed from {pdf_url}, skipping")
                continue

            logger.debug(f"[PDF] Parsed {len(draw_main)} {draw_type.upper()} + {len(draw_alt)} ALT from {pdf_url}")

            if draw_type == "qual":
                _canonicalize_player_names(draw_main, source="wta")
                _canonicalize_player_names(draw_alt, source="wta")
                draw_main, draw_alt = _apply_manual_entry_list_withdrawals(
                    draw_main,
                    draw_alt,
                    draw_meta.get("withdrawals"),
                    main_type="QUAL",
                )
                draw_main, draw_alt = _apply_manual_entry_list_additions(
                    draw_main,
                    draw_alt,
                    draw_meta.get("additions"),
                    main_type="QUAL",
                )
                qual_players = draw_main + draw_alt
                qual_key = cache_key + "#qual"
                _fill_missing_countries(qual_players, entry_cache)
                entry_cache[qual_key] = qual_players
                tournament_store[qual_key] = qual_players
            else:
                _canonicalize_player_names(draw_main, source="wta")
                _canonicalize_player_names(draw_alt, source="wta")
                draw_main, draw_alt = _apply_manual_entry_list_withdrawals(
                    draw_main,
                    draw_alt,
                    draw_meta.get("withdrawals"),
                )
                draw_main, draw_alt = _apply_manual_entry_list_additions(
                    draw_main,
                    draw_alt,
                    draw_meta.get("additions"),
                )
                _canonicalize_player_names(draw_main, source="wta")
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


def _itf_entry_has_arg(players):
    """Return True if an ITF acceptance list contains an ARG player."""
    for p in players or []:
        if str(p.get("country") or "").upper() == "ARG" and p.get("type") in {"MAIN", "QUAL", "ALT"}:
            return True
    return False


def _entry_list_proves_no_arg(players):
    """Return True only for a real, non-empty Entry List with no ARG players."""
    return bool(players) and not _itf_entry_has_arg(players)


def _published_draw_has_content(draw_data):
    """Return True once a parsed draw contains published players."""
    if not isinstance(draw_data, dict):
        return False
    return bool(draw_data.get("players"))


def _keys_with_published_itf_draw(
    itf_draws_tournaments,
    cached_draws_store,
    prefetched_itf_draws,
    *,
    draw_code,
    draw_type,
):
    """Map a published ITF draw from raw and parsed caches to tournament keys."""
    keys_by_id = {}
    for tourneys in (itf_draws_tournaments or {}).values():
        for tournament_key, tournament_info in (tourneys or {}).items():
            tournament_id = str((tournament_info or {}).get("tournamentId") or "").strip()
            if not tournament_id:
                continue
            keys_by_id.setdefault(tournament_id, set()).add(_canonical_draw_store_key(tournament_key))

    published_id_loader = (
        tournament_ids_with_published_main_draw
        if draw_code == "M"
        else tournament_ids_with_published_qualifying_draw
    )
    published_ids = published_id_loader(keys_by_id)
    published_keys = {
        tournament_key for tournament_id in published_ids for tournament_key in keys_by_id.get(tournament_id, ())
    }

    for source in (cached_draws_store, prefetched_itf_draws):
        for tournament_key, entry in (source or {}).items():
            draws = entry.get("draws") if isinstance(entry, dict) else None
            if not isinstance(draws, dict):
                draws = entry if isinstance(entry, dict) else {}
            if _published_draw_has_content(draws.get(draw_type)):
                published_keys.add(_canonical_draw_store_key(tournament_key))
    return published_keys


def _itf_keys_with_published_main_draw(
    itf_draws_tournaments,
    cached_draws_store,
    prefetched_itf_draws,
):
    """Return entry-list keys whose main draw has been published."""
    return _keys_with_published_itf_draw(
        itf_draws_tournaments,
        cached_draws_store,
        prefetched_itf_draws,
        draw_code="M",
        draw_type="MDS",
    )


def _itf_keys_with_published_qualifying_draw(
    itf_draws_tournaments,
    cached_draws_store,
    prefetched_itf_draws,
):
    """Return entry-list keys whose qualifying draw has been published."""
    return _keys_with_published_itf_draw(
        itf_draws_tournaments,
        cached_draws_store,
        prefetched_itf_draws,
        draw_code="Q",
        draw_type="QS",
    )


def _itf_empty_draw_counts_toward_backoff(acceptance_players, cached_draw_entry):
    """Return True when an empty ITF draw is suspicious enough to count."""
    if _itf_entry_has_arg(acceptance_players):
        return True
    return _itf_cached_draw_arg_visibility(cached_draw_entry).get("has_arg_any", False)


def _itf_requested_draw_types(prev_draws):
    """Return incomplete draw types, for both WTA and ITF polling."""
    requested = []
    if not _draw_is_complete((prev_draws or {}).get("MDS")):
        requested.append("MDS")
    if not _draw_is_complete((prev_draws or {}).get("QS"), is_qualifying=True):
        requested.append("QS")
    return requested


def _itf_draw_types_label(draw_types):
    """Return a human-readable label for a draw-type list."""
    types = [t for t in (draw_types or []) if t in {"MDS", "QS"}]
    if types == ["MDS"]:
        return "main"
    if types == ["QS"]:
        return "qualifying"
    if set(types) == {"MDS", "QS"}:
        return "main and qualifying"
    if types:
        return " / ".join(types)
    return "main and qualifying"


def _itf_empty_draw_status(draw_types):
    """Return a compact status string for an empty draw fetch."""
    types = [t for t in (draw_types or []) if t in {"MDS", "QS"}]
    short = []
    if "MDS" in types:
        short.append("MD")
    if "QS" in types:
        short.append("QS")
    if short == ["MD"]:
        return "MD is empty"
    if short == ["QS"]:
        return "QS is empty"
    if set(short) == {"MD", "QS"}:
        return "MD and QS are empty"
    if short:
        return f"{' / '.join(short)} is empty"
    return "MD and QS are empty"


def _itf_cached_draw_arg_visibility(draw_entry):
    """Return ARG visibility flags for a cached ITF draw entry."""
    draws = (draw_entry or {}).get("draws")
    if not isinstance(draws, dict):
        draws = {}
    has_arg_main = False
    has_arg_qual = False
    for dtype, draw_data in draws.items():
        if not isinstance(draw_data, dict):
            continue
        players = draw_data.get("players") or []
        has_arg = any(isinstance(p, dict) and str(p.get("country") or "").upper() == "ARG" for p in players)
        if dtype == "MDS" and has_arg:
            has_arg_main = True
        elif dtype == "QS" and has_arg:
            has_arg_qual = True
    return {
        "has_arg_main": has_arg_main,
        "has_arg_qual": has_arg_qual,
        "has_arg_any": has_arg_main or has_arg_qual,
    }


def _itf_draw_skip_reason(
    store_key,
    t_info,
    acceptance_players,
    cached_draw_entry,
    today,
    definitive_no_arg_draw=False,
):
    """Return the reason an ITF draw should be skipped, or None if it should be fetched."""
    start_date = str((t_info or {}).get("startDate") or "")[:10]
    end_date = str((t_info or {}).get("endDate") or "")[:10]
    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d") if start_date else None
    except (TypeError, ValueError):
        start_dt = None
    try:
        end_dt = datetime.strptime(end_date, "%Y-%m-%d") if end_date else None
    except (TypeError, ValueError):
        end_dt = None

    prev_draws = (cached_draw_entry or {}).get("draws") or {}
    mds_complete = _draw_is_complete(prev_draws.get("MDS"))
    qs_complete = _draw_is_complete(prev_draws.get("QS"), is_qualifying=True)
    cached_visibility = _itf_cached_draw_arg_visibility(cached_draw_entry)

    # A completion marker is trustworthy only after the completed main draw has
    # reached draws_store_cache.json. Older loaders could mark the tournament as
    # complete immediately after updating the raw drawsheet, leaving Draws stale.
    if is_draw_completed(store_key) and mds_complete:
        return "tournament already completed"

    # A real Entry List with no ARG players makes the tournament irrelevant to
    # the Draws page. Do not poll either draw merely to track publication.
    if _entry_list_proves_no_arg(acceptance_players):
        return "published acceptance list contains no ARG players"

    # For next-week ITF events, only start draw polling on the Saturday/Sunday
    # immediately before the event, or once the tournament week itself begins.
    if start_dt:
        days_until_start = (start_dt.date() - today.date()).days
        if days_until_start > 2:
            return "next-week gating: wait until the weekend before the event"

    if cached_visibility["has_arg_any"]:
        if mds_complete and (qs_complete or "QS" not in prev_draws):
            return "cached ARG draws already complete"
        return None

    if definitive_no_arg_draw:
        return "published qualifying and main draws contain no ARG players"

    if _itf_entry_has_arg(acceptance_players):
        return None

    if end_dt and today.date() > end_dt.date():
        return "event already ended"

    if mds_complete and (qs_complete or "QS" not in prev_draws):
        return "cached draws already complete"

    return None


def _filter_itf_draws_for_website(draws_store):
    """Keep only ITF draws that actually contain ARG players, with allowed sides."""
    filtered = {}
    for store_key, entry in (draws_store or {}).items():
        if not isinstance(entry, dict):
            continue
        if not str(store_key).lower().startswith("w-itf-"):
            filtered[store_key] = entry
            continue

        vis = entry.get("arg_visibility")
        if not isinstance(vis, dict):
            vis = _itf_cached_draw_arg_visibility(entry)
        if not vis.get("has_arg_any"):
            continue

        draws = entry.get("draws")
        if not isinstance(draws, dict):
            continue

        visible_draws = {}
        if vis.get("has_arg_main"):
            for dtype in ("MDS", "QS"):
                if dtype in draws:
                    visible_draws[dtype] = draws[dtype]
        elif vis.get("has_arg_qual") and "QS" in draws:
            visible_draws["QS"] = draws["QS"]

        if not visible_draws:
            continue

        visible_entry = dict(entry)
        visible_entry["draws"] = visible_draws
        visible_entry["arg_visibility"] = vis
        filtered[store_key] = visible_entry
    return filtered


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
            if (
                isinstance(old_draw, dict)
                and old_draw.get("players")
                and isinstance(new_draw, dict)
                and not new_draw.get("players")
            ):
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


def enrich_history_with_wta_ranks(cleaned_history, data_dir=None):
    """Add `_winnerRank` / `_loserRank` to cleaned history rows (empty if unknown)."""
    if not cleaned_history:
        return cleaned_history

    source_data_dir = os.fspath(data_dir or DATA_DIR)
    aliases_file = os.path.join(source_data_dir, "player_aliases_wta_itf.json")

    # Optional: map ITF-side names to WTA-side names (to resolve rankings even when
    # the match dataset uses ITF spelling while rankings CSV uses WTA spelling).
    aliases_lookup = {}
    itf_id_to_wta_id = {}
    if os.path.exists(aliases_file):
        try:
            with open(aliases_file, encoding="utf-8-sig") as f:
                items = json.load(f)
            if not isinstance(items, list):
                items = []
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
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
            for k in {
                _normalize_name_for_lookup(itf_name),
                _normalize_name_for_lookup(_map_to_display_name_upper(itf_name)),
            }:
                if not k:
                    continue
                if k not in aliases_lookup:
                    aliases_lookup[k] = []
                for cn in cand_norms:
                    if cn not in aliases_lookup[k]:
                        aliases_lookup[k].append(cn)

    csv_by_week = _load_wta_csv(source_data_dir) or {}

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
        idx_by_name = {}
        idx_by_id = {}
        for p in csv_by_week.get(week_date) or []:
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
            for cand in aliases_lookup.get(k) or []:
                rank = idx.get(cand) or ""
                if rank:
                    return rank
        return ""

    def resolve_rank_by_ids(name_raw, player_id_raw, idx_by_name, idx_by_id):
        """Resolve rank preferring WTA id lookups (direct or ITF-id->WTA-id via aliases JSON)."""
        pid = str(player_id_raw or "").strip()
        wta_id = ""
        if pid.isdigit():
            wta_id = itf_id_to_wta_id.get(pid, "") if _is_itf_id(pid) else pid
        if wta_id:
            rank = idx_by_id.get(wta_id) or ""
            if rank:
                return rank
        return resolve_rank(name_raw, idx_by_name)

    history_rows_by_week = {}
    for row in cleaned_history:
        row["_winnerRank"] = ""
        row["_loserRank"] = ""
        week_date = get_previous_monday(row.get("DATE", ""))
        if not week_date or week_date not in csv_by_week:
            continue
        history_rows_by_week.setdefault(week_date, []).append(row)

    # Process each week as one batch so its name/id indexes are released before
    # the next week instead of retaining an index for the entire archive.
    for week_date, week_rows in history_rows_by_week.items():
        idx_by_name, idx_by_id = week_index(week_date)
        for row in week_rows:
            row["_winnerRank"] = resolve_rank_by_ids(
                row.get("_winnerName", ""), row.get("_winnerId", ""), idx_by_name, idx_by_id
            )
            row["_loserRank"] = resolve_rank_by_ids(
                row.get("_loserName", ""), row.get("_loserId", ""), idx_by_name, idx_by_id
            )

    return cleaned_history


def create_driver():
    if uc is not None and os.getenv("USE_UNDETECTED_CHROME", "1").strip().lower() not in {"0", "false", "no"}:
        try:
            chrome_options = uc.ChromeOptions()
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-dev-shm-usage")
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument("window-size=1920,1080")
            version_main = _get_chrome_major_version()
            chrome_exe = _get_chrome_executable_path()
            kwargs = {"options": chrome_options, "headless": True}
            if version_main:
                kwargs["version_main"] = version_main
            if chrome_exe:
                kwargs["browser_executable_path"] = chrome_exe
            driver = uc.Chrome(**kwargs)
            logger.debug("Using undetected Chrome driver.")
        except Exception as e:
            logger.warning(f"Warning creating undetected Chrome driver, falling back to Selenium: {e}")
            driver = None
    else:
        driver = None

    if driver is None:
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
        chrome_options.add_experimental_option(
            "prefs",
            {
                "plugins.always_open_pdf_externally": True,
                "download.prompt_for_download": False,
                "download.directory_upgrade": True,
                "safebrowsing.enabled": True,
            },
        )
        chromedriver_path = _find_local_chromedriver()
        if chromedriver_path:
            driver = webdriver.Chrome(service=Service(chromedriver_path), options=chrome_options)
        else:
            driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
    driver.set_page_load_timeout(60)
    driver.set_script_timeout(30)
    # Selenium's async-script timeout only covers the browser-side JS.
    # The HTTP transport to ChromeDriver can still hang longer, so cap it too.
    if hasattr(driver, "command_executor") and hasattr(driver.command_executor, "client_config"):
        driver.command_executor.client_config.timeout = 20
    return driver


def _quit_driver(driver, operation="quit browser"):
    if driver is None:
        return
    try:
        driver.quit()
    except Exception as exc:
        report_run_issue("browser", operation, exc, severity="degraded")


def build_all_tournament_groups(driver):
    """Merge WTA tournament groups with ITF calendar and save snapshot."""
    tournament_groups = build_tournament_groups()
    monday_map = generate_dynamic_monday_map(num_weeks=4)
    itf_monday_map = generate_dynamic_monday_map(num_weeks=3)

    # Add the current week's Monday only on Monday, dropping the last future
    # week to keep the total at 4.
    today = madrid_now()
    current_monday = today - timedelta(days=today.weekday())
    current_monday_str = current_monday.strftime("%Y-%m-%d")
    current_monday_label = format_week_label(current_monday)
    next_monday_str = (current_monday + timedelta(days=7)).strftime("%Y-%m-%d")
    itf_items = get_dynamic_itf_calendar(driver, num_weeks=3)
    has_current_wta = bool(tournament_groups.get(current_monday_label))
    has_current_itf = any(
        current_monday_str <= str(item.get("startDate") or "")[:10] < next_monday_str for item in itf_items
    )
    if today.weekday() == 0 and (has_current_wta or has_current_itf):
        m_keys = list(monday_map.keys())
        monday_map = {k: monday_map[k] for k in m_keys[:-1]}
        monday_map = {current_monday_str: current_monday_label, **monday_map}
        itf_keys = list(itf_monday_map.keys())
        itf_monday_map = {k: itf_monday_map[k] for k in itf_keys[:-1]}
        itf_monday_map = {current_monday_str: current_monday_label, **itf_monday_map}

    for label in monday_map.values():
        if label not in tournament_groups:
            tournament_groups[label] = {}

    for item in itf_items:
        t_name = item["tournamentName"]
        if "cancel" in t_name.lower():
            continue
        s_date = pd.to_datetime(item["startDate"])
        monday_date = (s_date - timedelta(days=s_date.weekday())).strftime("%Y-%m-%d")
        if monday_date in itf_monday_map:
            week_label = itf_monday_map[monday_date]
            tournament_groups[week_label][item["tournamentKey"].lower()] = {
                "name": t_name,
                "level": get_itf_level(t_name),
                "surface": item.get("surfaceDesc") or item.get("surface") or "",
                "country": item.get("hostNationCode") or item.get("hostNation") or item.get("countryCode") or "",
                "startDate": item["startDate"],
                "endDate": item.get("endDate", None),
            }

    tournament_snapshot: dict[str, TournamentSnapshotRecord] = {}
    for week, tourneys in tournament_groups.items():
        for key, info in tourneys.items():
            if "cancel" in info.get("name", "").lower():
                continue
            normalized_key = normalize_tournament_snapshot_key(key)
            tournament_snapshot[normalized_key] = normalize_tournament_snapshot_record(
                {
                    "name": info.get("name", key),
                    "level": info.get("level", ""),
                    "surface": info.get("surface", ""),
                    "country": info.get("country", ""),
                    "startDate": info.get("startDate"),
                    "endDate": info.get("endDate"),
                    "week": week,
                }
            )
    save_json_file(
        TOURNAMENT_SNAPSHOT_FILE,
        compress_tournament_snapshot(tournament_snapshot),
        formatter=dumps_tournament_snapshot,
    )

    return tournament_groups, monday_map


def fetch_arg_players():
    """Fetch WTA+ITF rankings and return deduplicated ARG player list."""
    eastern_now = new_york_now()
    ranking_status_file = os.path.join(DATA_DIR, "wta_ranking_refresh_status.json")
    try:
        with open(ranking_status_file, encoding="utf-8-sig") as source:
            ranking_status = json.load(source)
    except (OSError, UnicodeError, json.JSONDecodeError):
        ranking_status = {}
    if not isinstance(ranking_status, dict):
        ranking_status = {}
    ranking_monday = effective_wta_ranking_date(eastern_now, ranking_status).isoformat()

    all_wta_players, wta_status = get_wta_rankings_cached(ranking_monday, nationality=None, with_status=True)
    normalize_country_overrides(all_wta_players, "Player", "Country")

    wta_players_arg = [p for p in all_wta_players if p["Country"] == "ARG"]
    itf_players_arg, itf_status = get_itf_rankings_cached(ranking_monday, nationality="ARG", with_status=True)

    wta_names_arg = {p["Player"] for p in wta_players_arg}
    itf_only_arg = [p for p in itf_players_arg if p["Player"] not in wta_names_arg]

    players_data = wta_players_arg + itf_only_arg
    arg_names_set = {p["Player"] for p in players_data}

    data_status = {
        "generatedAt": utc_now_iso(),
        "sources": {
            "wtaRankings": wta_status,
            "itfRankings": itf_status,
        },
    }

    return players_data, arg_names_set, all_wta_players, data_status


def log_data_status_warnings(data_status):
    """Print operator-only freshness warnings without exposing them in the site."""
    sources = (data_status or {}).get("sources")
    if not isinstance(sources, dict):
        return
    for source_status in sources.values():
        if not isinstance(source_status, dict):
            continue
        source = source_status.get("source") or "Data"
        status = source_status.get("status")
        requested = source_status.get("requestedDate")
        effective = source_status.get("effectiveDate")
        if status == "error":
            logger.warning(f"Data status warning: {source} unavailable for {requested or 'requested date'}.")
        elif source_status.get("stale"):
            if requested and effective and requested != effective:
                logger.warning(f"Data status warning: {source} using {effective} instead of {requested}.")
            else:
                logger.warning(f"Data status warning: {source} using cached data.")


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
    key_fields = sorted((p.get("name", ""), p.get("type", ""), p.get("pos_num", 9999)) for p in players)
    return json.dumps(key_fields, ensure_ascii=False)


def _load_acceptance_state():
    """Load per-tournament acceptance polling and lifecycle state from disk."""
    if not os.path.exists(ITF_ACCEPTANCE_STATE_FILE):
        return {}
    try:
        with open(ITF_ACCEPTANCE_STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return {}


def _save_acceptance_state(state):
    """Persist per-tournament acceptance-check state to disk."""
    try:
        save_json_file(ITF_ACCEPTANCE_STATE_FILE, state)
    except Exception as e:
        logger.warning(f"Warning: could not save ITF acceptance state: {e}")


def _record_entry_draw_availability(
    qualifying_draw_keys=None,
    main_draw_keys=None,
    *,
    observed_date=None,
):
    """Persist draw publication milestones that close entry-list polling."""
    state = _load_acceptance_state()
    changed = False
    date_text = str(observed_date or madrid_today().isoformat())
    milestones = (
        (qualifying_draw_keys or (), "qualifying_draw_available_date"),
        (main_draw_keys or (), "main_draw_available_date"),
    )
    for keys, field in milestones:
        for raw_key in keys:
            key = _canonical_draw_store_key(raw_key)
            if not key:
                continue
            entry = state.setdefault(key, {})
            if not entry.get(field):
                entry[field] = date_text
                changed = True
    if changed:
        _save_acceptance_state(state)
    return state


def process_tournaments(
    driver,
    tournament_groups,
    monday_map,
    arg_names_set,
    entry_cache,
    force_itf_acceptance=False,
    qualifying_draw_available_keys=None,
    main_draw_available_keys=None,
    itf_main_draw_available_keys=None,
):
    """Process WTA & ITF tournaments: scrape entry lists, build schedule map."""
    schedule_map = {}
    tournament_store = {}
    ranking_cache = {}
    unranked_schedule = {}
    itf_schedule_pending = {}
    unranked_itf_pending = {}

    _utc_now = utc_now()
    _now = _utc_now.astimezone(MADRID)
    today_str = _now.strftime("%Y-%m-%d")
    # After noon UTC, give up re-fetching acceptance lists we already tried today
    # without detecting a change. Pre-noon keeps retrying to catch morning updates.
    _past_noon_utc = _utc_now.hour >= 12
    # On Sat/Sun/Mon allow a second check after 6 pm Europe/Madrid time.
    # ZoneInfo applies the real CET/CEST transition instants.
    _past_6pm_spain = _now.hour >= 18
    _is_double_check_day = _now.weekday() in (5, 6, 0)  # Sat, Sun, Mon
    current_monday_str = (_now - timedelta(days=_now.weekday())).strftime("%Y-%m-%d")
    acceptance_state = _load_acceptance_state()
    acceptance_state_dirty = False
    qualifying_draw_available_keys = {
        _canonical_draw_store_key(key) for key in (qualifying_draw_available_keys or set())
    }
    supplied_main_draw_keys = set(main_draw_available_keys or ())
    supplied_main_draw_keys.update(itf_main_draw_available_keys or ())
    main_draw_available_keys = {_canonical_draw_store_key(key) for key in supplied_main_draw_keys}
    if force_itf_acceptance:
        logger.info("Forcing refresh of all open ITF acceptance lists.")

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
        except (TypeError, ValueError):
            return 9999

    def _suffix_from_itf_player(player_row):
        p_type = (player_row or {}).get("type", "")
        if p_type == "MAIN":
            return ""
        if p_type == "QUAL":
            return " (Q)"
        pos = (player_row or {}).get("pos", "")
        return f" (ALT {pos})" if pos else " (ALT)"

    def _queue_itf_entry(
        container, player_key, week_label, tournament_key, tournament_name, suffix, priority, entry_type, pos_num
    ):
        if not player_key or not week_label:
            return
        by_week = container.setdefault(player_key, {})
        items = by_week.setdefault(week_label, [])
        if any(item.get("tournament_key") == tournament_key for item in items):
            return
        items.append(
            {
                "tournament_key": tournament_key,
                "name": tournament_name,
                "suffix": suffix or "",
                "priority": priority,
                "entry_type": entry_type,
                "pos_num": _safe_pos_num(pos_num),
            }
        )

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

    def _apply_argless_itf_entry_policy(store_key, players, week_monday):
        nonlocal acceptance_state_dirty
        if not _entry_list_proves_no_arg(players):
            return
        state_entry = acceptance_state.setdefault(_canonical_draw_store_key(store_key), {})
        # Draw publication must never control a confirmed no-ARG list.
        for field in ("qualifying_draw_available_date", "main_draw_available_date"):
            if state_entry.pop(field, None):
                acceptance_state_dirty = True
        if _now.weekday() != 0 or week_monday != current_monday_str:
            return
        if not state_entry.get("argless_entry_list_removed_date"):
            state_entry["argless_entry_list_removed_date"] = today_str
            acceptance_state_dirty = True

    mondays = sorted(monday_map.keys())
    total_weeks = len(mondays) or 4

    for i, week_monday in enumerate(mondays, start=1):
        logger.debug(f"Processing Tournaments ({i}/{total_weeks})")
        week = monday_map.get(week_monday)
        if not week:
            continue
        tourneys = tournament_groups.get(week, {})
        md_date = get_monday_offset(week_monday, 4)
        q_date = get_monday_offset(week_monday, 3)

        today_date = _now.date()
        md_datetime = datetime.strptime(md_date, "%Y-%m-%d").date()
        q_datetime = datetime.strptime(q_date, "%Y-%m-%d").date()

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
        for key, t_info in list(tourneys.items()):
            t_name = t_info["name"]
            schedule_name = _schedule_tournament_name(key, t_name)
            if key.startswith("http"):
                if _is_excluded_entry_list_tournament(key, t_info):
                    continue
                store_key = _canonical_draw_store_key(key)
                cached_players = entry_cache.get(key, [])
                if not isinstance(cached_players, list):
                    cached_players = []
                state_entry = acceptance_state.get(store_key, {}) or {}
                qualifying_draw_available = bool(state_entry.get("qualifying_draw_available_date"))
                main_draw_available = bool(state_entry.get("main_draw_available_date"))
                if not qualifying_draw_available and store_key in qualifying_draw_available_keys:
                    state_entry = acceptance_state.setdefault(store_key, {})
                    state_entry["qualifying_draw_available_date"] = today_str
                    acceptance_state_dirty = True
                    qualifying_draw_available = True
                if not main_draw_available and store_key in main_draw_available_keys:
                    state_entry = acceptance_state.setdefault(store_key, {})
                    state_entry["main_draw_available_date"] = today_str
                    acceptance_state_dirty = True
                    main_draw_available = True
                is_manual_entry = _is_manual_entry_list_tournament(key)
                is_pdf_entry = key in _get_pdf_cache_keys()
                entry_refresh_closed = qualifying_draw_available or main_draw_available
                if is_manual_entry or (is_pdf_entry and cached_players) or entry_refresh_closed:
                    if entry_refresh_closed:
                        draw_name = "main" if main_draw_available else "qualifying"
                        logger.debug(f"  WTA {draw_name} draw published, entry list closed: {t_name}")
                    t_list = copy.deepcopy(cached_players)
                    status_dict = {}
                    for _p in t_list:
                        _p_name = str(_p.get("name") or "").strip().upper()
                        if not _p_name:
                            continue
                        _p_type = str(_p.get("type") or "").upper()
                        if _p_type == "MAIN":
                            status_dict[_p_name] = ""
                        elif _p_type == "QUAL":
                            status_dict[_p_name] = " (Q)"
                        else:
                            _p_pos = str(_p.get("pos") or "").strip()
                            status_dict[_p_name] = f" (ALT {_p_pos})" if _p_pos else " (ALT)"
                else:
                    t_list, status_dict = scrape_tournament_players(
                        key,
                        ranking_cache[md_date],
                        ranking_cache[q_date],
                        cached_players,
                    )
                    t_list = merge_entry_list(cached_players, t_list)
                if not is_manual_entry:
                    _canonicalize_player_names(t_list, source="wta", names_only=True)
                    normalize_country_overrides(t_list, "name", "country")
                entry_cache[key] = t_list
                tournament_store[key] = t_list
                if is_pdf_entry and not str(key).endswith("#qual"):
                    qual_key = key + "#qual"
                    qual_players = copy.deepcopy(entry_cache.get(qual_key, []))
                    if not qual_players:
                        qual_players = [copy.deepcopy(p) for p in t_list if p.get("type") == "QUAL"]
                    if qual_players:
                        _canonicalize_player_names(qual_players, source="wta", names_only=True)
                        normalize_country_overrides(qual_players, "name", "country")
                        entry_cache[qual_key] = qual_players
                        tournament_store[qual_key] = qual_players
                        qual_already_grouped = any(
                            qual_key in week_tourneys for week_tourneys in tournament_groups.values()
                        )
                        if not qual_already_grouped:
                            tournament_groups.setdefault(week, {})[qual_key] = {
                                "name": f"{t_name.replace('Grand Slam ', '').strip()} Qualifying",
                                "level": t_info.get("level", ""),
                                "surface": t_info.get("surface", ""),
                                "country": t_info.get("country", ""),
                                "startDate": t_info.get("startDate"),
                                "endDate": t_info.get("endDate", None),
                            }
                # If the live WTA page disappears or only partially loads, rebuild
                # the schedule labels from the merged cached list so players saved
                # in Entry Lists still appear in Schedule.
                merged_status_dict = dict(status_dict or {})
                for _p in t_list:
                    if _p.get("type") not in ("MAIN", "QUAL"):
                        continue
                    _p_name = str(_p.get("name") or "").strip()
                    if not _p_name:
                        continue
                    _p_key = _p_name.upper()
                    if _p_key in merged_status_dict:
                        continue
                    merged_status_dict[_p_key] = "" if _p.get("type") == "MAIN" else " (Q)"

                # Compute seeds for WTA tournaments based on level and draw size.
                # Grand Slams: 32 seeds for main draw and qualifying independently.
                # Other WTA: seed count by main draw player count.
                _level = t_info.get("level", "")
                _is_gs = "grand slam" in _level.lower()
                _main_players = [_p for _p in t_list if _p.get("type") == "MAIN"]
                _qual_players = [_p for _p in t_list if _p.get("type") == "QUAL"]
                _main_count = len(_main_players)
                if _is_gs or _main_count > 70:
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
                    _sname = _map_to_display_name_upper(_sp.get("Player", ""))
                    _srank = _sp.get("Rank")
                    if _sname and _srank is not None:
                        _name_to_rank[_sname] = int(_srank)
                for _p in t_list:
                    _pname_up = _map_to_display_name_upper(_p["name"])
                    _r = _name_to_rank.get(_pname_up)
                    _p["seed_rank"] = _r if _r is not None else ""

                def _build_seed_map(player_list, n):
                    candidates = []
                    for _p in player_list:
                        _r = _p.get("seed_rank")
                        if isinstance(_r, int):
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

                for p_name, suffix in merged_status_dict.items():
                    p_key = p_name.upper()
                    if p_key not in arg_names_set:
                        continue
                    _append_schedule_label(schedule_map, p_key, week, f"{schedule_name}{suffix}", style="append_div")
                for p in t_list:
                    p_upper = p["name"].upper()
                    if p_upper in arg_names_set:
                        continue
                    if p.get("country", "") != "ARG":
                        continue
                    suffix = "" if p.get("type") == "MAIN" else " (Q)"
                    _append_schedule_label(
                        unranked_schedule, p_upper, week, f"{schedule_name}{suffix}", style="append_div"
                    )

        # ITF tournaments
        for key, t_info in list(tourneys.items()):
            t_name = t_info["name"]
            if "cancel" in t_name.lower():
                continue
            if not key.startswith("http"):
                cached_players = entry_cache.get(key, [])
                if not isinstance(cached_players, list):
                    cached_players = []

                store_key = _canonical_draw_store_key(key)
                cached_list_has_no_arg = _entry_list_proves_no_arg(cached_players)
                state_entry = acceptance_state.get(store_key, {}) or {}
                qualifying_draw_available = (
                    bool(state_entry.get("qualifying_draw_available_date")) and not cached_list_has_no_arg
                )
                main_draw_available = bool(state_entry.get("main_draw_available_date")) and not cached_list_has_no_arg
                if (
                    not cached_list_has_no_arg
                    and not qualifying_draw_available
                    and store_key in qualifying_draw_available_keys
                ):
                    state_entry = acceptance_state.setdefault(store_key, {})
                    state_entry["qualifying_draw_available_date"] = today_str
                    acceptance_state_dirty = True
                    qualifying_draw_available = True
                if not cached_list_has_no_arg and not main_draw_available and store_key in main_draw_available_keys:
                    state_entry = acceptance_state.setdefault(store_key, {})
                    state_entry["main_draw_available_date"] = today_str
                    acceptance_state_dirty = True
                    main_draw_available = True
                already_updated_today = state_entry.get("last_changed_date") == today_str
                fetched_today_no_change = (
                    state_entry.get("last_fetched_date") == today_str and not already_updated_today
                )
                evening_already_fetched = state_entry.get("last_fetched_evening_date") == today_str
                start_date_str = t_info.get("startDate", "")
                list_available = _itf_acceptance_list_available(start_date_str, _now)
                fresh_players = []

                if main_draw_available:
                    # The published main draw is the final roster boundary.
                    # Never fetch this tournament's acceptance list again.
                    logger.debug(f"  ITF main draw published, acceptance list closed: {t_name}")
                    tourney_players_list = list(cached_players)
                    itf_name_map = {}
                elif qualifying_draw_available:
                    # Qualifying publication freezes the acceptance list. Keep
                    # showing the last cached list until the main draw replaces it.
                    logger.debug(f"  ITF qualifying draw published, acceptance list closed: {t_name}")
                    tourney_players_list = list(cached_players)
                    itf_name_map = {}
                elif already_updated_today and not force_itf_acceptance:
                    logger.debug(f"  ITF acceptance list already updated today, skipping fetch: {t_name}")
                    tourney_players_list = list(cached_players)
                    itf_name_map = {}
                elif (
                    not force_itf_acceptance
                    and fetched_today_no_change
                    and _past_noon_utc
                    and (not _is_double_check_day or not _past_6pm_spain or evening_already_fetched)
                ):
                    if _is_double_check_day and not _past_6pm_spain:
                        logger.debug(
                            f"  ITF acceptance list unchanged this morning, will re-check after 6 pm Spain: {t_name}"
                        )
                    else:
                        logger.debug(f"  ITF acceptance list unchanged through noon UTC, skipping: {t_name}")
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
                    state_entry = acceptance_state.setdefault(store_key, {})
                    if state_entry.get("last_fetched_date") != today_str:
                        state_entry["last_fetched_date"] = today_str
                        acceptance_state_dirty = True
                    if (
                        _is_double_check_day
                        and _past_6pm_spain
                        and state_entry.get("last_fetched_evening_date") != today_str
                    ):
                        state_entry["last_fetched_evening_date"] = today_str
                        acceptance_state_dirty = True
                    if fresh_players:
                        cached_fp = _acceptance_fingerprint(cached_players)
                        fresh_fp = _acceptance_fingerprint(fresh_players)
                        if cached_fp != fresh_fp:
                            logger.info(f"  ITF acceptance list updated for: {t_name}")
                            state_entry["last_changed_date"] = today_str
                            acceptance_state_dirty = True
                        else:
                            logger.debug(f"  No changes in ITF acceptance list yet for: {t_name}")
                    else:
                        logger.warning(f"  Using cached ITF acceptance list (fetch failed): {t_name}")

                # Preserve the saved cache when ITF returns nothing. We still
                # use a working copy for ranking/seeding/schedule generation,
                # but we only write back when the live fetch actually produced
                # rows.
                if not fresh_players and not cached_players:
                    continue

                tourney_players_list = copy.deepcopy(cached_players)
                if fresh_players:
                    tourney_players_list = merge_entry_list(tourney_players_list, fresh_players)

                _canonicalize_player_names(tourney_players_list, source="itf", names_only=True)

                normalize_country_overrides(tourney_players_list, "name", "country")
                if fresh_players or tourney_players_list != cached_players:
                    entry_cache[key] = tourney_players_list
                tournament_store[key] = tourney_players_list
                _apply_argless_itf_entry_policy(store_key, tourney_players_list, week_monday)

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
                    _sname = _map_to_display_name_upper(_sp.get("Player", ""))
                    _srank = _sp.get("Rank")
                    if _sname and _srank is not None:
                        _name_to_rank[_sname] = int(_srank)
                for _p in tourney_players_list:
                    _pname_up = _map_to_display_name_upper(_p["name"])
                    _wta_rank = _name_to_rank.get(_pname_up)
                    _p["seed_rank"] = _wta_rank if _wta_rank is not None else ""
                _main_candidates = []
                for _p in tourney_players_list:
                    if _p.get("type") != "MAIN":
                        continue
                    _wta_rank = _p.get("seed_rank")
                    if isinstance(_wta_rank, int):
                        _main_candidates.append((_wta_rank, _p["name"]))
                _main_candidates.sort()
                _seed_map = {name: i + 1 for i, (_, name) in enumerate(_main_candidates[:_num_seeds])}
                for _p in tourney_players_list:
                    if _p.get("type") == "MAIN":
                        _sv = _seed_map.get(_p["name"])
                        _p["seed"] = _sv if _sv is not None else ""

                itf_player_meta = {}
                for p in tourney_players_list:
                    raw_upper = p.get("name", "").upper()
                    p_key = NAME_LOOKUP.get(raw_upper, raw_upper)
                    candidate = {
                        "priority": str(p.get("priority", "")).strip(),
                        "entry_type": p.get("type", ""),
                        "pos_num": p.get("pos_num", 9999),
                    }
                    prev = itf_player_meta.get(p_key)
                    if not prev:
                        itf_player_meta[p_key] = candidate
                        continue
                    prev_key = (
                        _entry_type_rank(prev.get("entry_type")),
                        _priority_num(prev.get("priority")),
                        _safe_pos_num(prev.get("pos_num")),
                    )
                    cand_key = (
                        _entry_type_rank(candidate.get("entry_type")),
                        _priority_num(candidate.get("priority")),
                        _safe_pos_num(candidate.get("pos_num")),
                    )
                    if cand_key < prev_key:
                        itf_player_meta[p_key] = candidate

                for p_name, suffix in itf_name_map.items():
                    if p_name not in arg_names_set:
                        continue
                    suffix_text = suffix.get("suffix", "") if isinstance(suffix, dict) else str(suffix or "")
                    p_meta = itf_player_meta.get(p_name, {})
                    _queue_itf_entry(
                        itf_schedule_pending,
                        p_name,
                        week,
                        key,
                        t_name,
                        suffix_text,
                        p_meta.get("priority", ""),
                        p_meta.get("entry_type", ""),
                        p_meta.get("pos_num", 9999),
                    )
                if not itf_name_map:
                    for p in tourney_players_list:
                        raw_upper = p.get("name", "").upper()
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
                            p_meta.get("priority", ""),
                            p_meta.get("entry_type", ""),
                            p_meta.get("pos_num", 9999),
                        )
                for p in tourney_players_list:
                    raw_upper = p["name"].upper()
                    p_key = NAME_LOOKUP.get(raw_upper, raw_upper)
                    if p_key in arg_names_set:
                        continue
                    if p.get("country", "") != "ARG":
                        continue
                    p_type = p.get("type", "")
                    suffix = _suffix_from_itf_player(p)
                    _queue_itf_entry(
                        unranked_itf_pending,
                        p_key,
                        week,
                        key,
                        t_name,
                        suffix,
                        str(p.get("priority", "")).strip(),
                        p_type,
                        p.get("pos_num", 9999),
                    )

    if acceptance_state_dirty:
        _save_acceptance_state(acceptance_state)

    _flush_itf_pending(schedule_map, itf_schedule_pending)
    _flush_itf_pending(unranked_schedule, unranked_itf_pending)

    # Remove tournaments no longer in the visible Schedule/Entry Lists window.
    active_keys = set()
    active_itf_keys = set()
    for tourneys in tournament_groups.values():
        for t_key, t_info in tourneys.items():
            if _is_excluded_entry_list_tournament(t_key, t_info):
                continue
            active_keys.add(t_key)
            if not str(t_key).startswith("http"):
                active_itf_keys.add(t_key)

    # Defensive fallback: when ITF calendar fetch fails for a run, keep previous ITF
    # entry lists instead of deleting them from cache.
    if not active_itf_keys:
        for cached_key in entry_cache:
            if not str(cached_key).startswith("http"):
                active_keys.add(cached_key)

    entry_cache = {k: v for k, v in entry_cache.items() if k in active_keys}

    return schedule_map, tournament_store, entry_cache, unranked_schedule


def normalize_history_round(raw_round, draw_type):
    """Return the canonical match-history label for qualifying rounds."""
    round_name = str(raw_round or "").strip()
    compact = round_name.upper().replace(" ", "")
    qualifying_match = re.fullmatch(r"Q(?:R)?(\d+)", compact)
    if qualifying_match:
        return f"QR{qualifying_match.group(1)}"

    draw_name = str(draw_type or "").strip().upper()
    if draw_name != "Q" and "QUAL" not in draw_name:
        return round_name

    return {
        "1st Round": "QR1",
        "2nd Round": "QR2",
        "3rd Round": "QR3",
        "4th Round": "QR4",
    }.get(round_name, round_name)


def normalize_history_category(raw_category):
    """Return the canonical match-history tournament category label."""
    category = str(raw_category or "").strip()
    if category.upper() == "WT":
        return "World Tour"
    if re.fullmatch(r"Tier\s*2", category, flags=re.IGNORECASE):
        return "Tier II"
    return category


def load_match_history(data_dir=None):
    """Read all match CSV files and return raw + cleaned/normalized rows."""
    source_data_dir = os.fspath(data_dir or DATA_DIR)
    match_history_data = []
    matches_files = [
        os.path.join(source_data_dir, "itf_matches_arg.csv"),
        os.path.join(source_data_dir, "wta_matches_arg.csv"),
        os.path.join(source_data_dir, "gs_matches_arg.csv"),
        os.path.join(source_data_dir, "og_matches_arg.csv"),
        os.path.join(source_data_dir, "bjkc_matches_arg.csv"),
        os.path.join(source_data_dir, "united_cup_matches_arg.csv"),
        os.path.join(source_data_dir, "manually_added_matches.csv"),
    ]
    for file_path in matches_files:
        try:
            with open(file_path, encoding="utf-8-sig") as file_obj:
                reader = csv.DictReader(file_obj, delimiter=",")
                for row in reader:
                    match_history_data.append(row)
        except (OSError, UnicodeError, csv.Error) as e:
            logger.error(f"Error reading matches data from {file_path}: {e}")

    def _history_identity_source(match_type):
        value = str(match_type or "").strip().upper()
        if value == "ITF":
            return "itf"
        if value in {"WTA", "GS", "OG", "UNITED CUP"}:
            return "wta"
        if "BJK" in value or "FED CUP" in value:
            return "bjkc"
        return value.casefold()

    def normalize_history_player_name(raw_name, *, player_id="", source=""):
        name = fix_encoding_keep_accents(str(raw_name or "")).strip()
        if not name:
            return name
        if "/" in name:
            return " / ".join(
                normalize_history_player_name(part, source=source) if part.strip() else part.strip()
                for part in name.split("/")
            )
        mapped = resolve_player_display_name(source, player_id=player_id, name=name)
        return format_player_name(mapped)

    cleaned_history = []
    for m in match_history_data:
        fecha = m.get("date") or m.get("Date") or m.get("matchDate") or m.get("match_date") or m.get("FECHA") or ""

        winner_entry = m.get("winnerEntry") or m.get("winner_entry") or m.get("WinnerEntry") or ""
        loser_entry = m.get("loserEntry") or m.get("loser_entry") or m.get("LoserEntry") or ""
        winner_entry = winner_entry.strip().upper()
        loser_entry = loser_entry.strip().upper()
        winner_entry = "LL" if winner_entry == "L" else ("" if winner_entry == "DA" else winner_entry)
        loser_entry = "LL" if loser_entry == "L" else ("" if loser_entry == "DA" else loser_entry)

        raw_round = m.get("roundName") or m.get("round_name") or m.get("RoundName") or ""
        draw_type = m.get("draw") or m.get("Draw") or m.get("DRAW") or ""
        match_type_value = (m.get("matchType") or m.get("MatchType") or m.get("MATCH_TYPE") or "").strip()
        tournament_category_value = normalize_history_category(
            m.get("tournamentCategory") or m.get("tournament_category") or m.get("TournamentCategory") or ""
        )
        tournament_name_value = (
            m.get("tournamentName") or m.get("tournament_name") or m.get("TournamentName") or ""
        ).strip()
        identity_source = _history_identity_source(match_type_value)

        final_round = normalize_history_round(raw_round, draw_type)

        raw_surface = m.get("surface") or m.get("Surface") or ""
        in_or_outdoor = m.get("inOrOutdoor") or m.get("InOrOutdoor") or ""
        if raw_surface.startswith("I."):
            formatted_surface = "Ind. " + raw_surface[2:].capitalize()
        elif in_or_outdoor == "I":
            formatted_surface = "Ind. " + raw_surface
        else:
            formatted_surface = raw_surface

        tournament_id_value = (m.get("tournamentId") or m.get("tournament_id") or m.get("TournamentId") or "").strip()
        winner_id_value = (m.get("winnerId") or m.get("winner_id") or m.get("WinnerId") or "").strip()
        loser_id_value = (m.get("loserId") or m.get("loser_id") or m.get("LoserId") or "").strip()

        cleaned_history.append(
            {
                "DATE": fecha,
                "TOURNAMENT": fix_encoding(tournament_name_value),
                "TOURNAMENT_ID": tournament_id_value,
                "CATEGORY": fix_encoding(tournament_category_value),
                "SURFACE": formatted_surface,
                "MATCH_TYPE": match_type_value,
                "DRAW": draw_type,
                "ROUND": final_round,
                "PLAYER": "",
                "ENTRY": "",
                "SEED": "",
                "RESULT": "",
                "SCORE": m.get("result") or m.get("Result") or "",
                "RIVAL_ENTRY": "",
                "RIVAL_SEED": "",
                "RIVAL": "",
                "RIVAL_COUNTRY": "",
                "_winnerId": winner_id_value,
                "_loserId": loser_id_value,
                "_winnerName": normalize_history_player_name(
                    m.get("winnerName") or m.get("winner_name") or m.get("WinnerName") or "",
                    player_id=winner_id_value,
                    source=identity_source,
                ),
                "_loserName": normalize_history_player_name(
                    m.get("loserName") or m.get("loser_name") or m.get("LoserName") or "",
                    player_id=loser_id_value,
                    source=identity_source,
                ),
                "_winnerCountry": m.get("winnerCountry") or m.get("winner_country") or m.get("WinnerCountry") or "",
                "_loserCountry": m.get("loserCountry") or m.get("loser_country") or m.get("LoserCountry") or "",
                "_winnerEntry": winner_entry,
                "_loserEntry": loser_entry,
                "_winnerSeed": m.get("winnerSeed") or m.get("winner_seed") or m.get("WinnerSeed") or "",
                "_loserSeed": m.get("loserSeed") or m.get("loser_seed") or m.get("LoserSeed") or "",
                "_resultStatusDesc": m.get("resultStatusDesc")
                or m.get("result_status_desc")
                or m.get("ResultStatusDesc")
                or "",
            }
        )

    def parse_match_date(item):
        d = item.get("DATE") or "1900-01-01"
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
                    calendar_key = get_calendar_tournament_key(t)
                    key = (week_label, column_name, continent, calendar_key)
                    if key in seen:
                        continue
                    seen.add(key)
                    calendar_snapshot.append(
                        {
                            "week_label": week_label,
                            "column": column_name,
                            "continent": continent,
                            "name": t.get("name", ""),
                            "level": t.get("level", ""),
                            "surface": t.get("surface", ""),
                            "country": t.get("country", ""),
                            "startDate": t.get("startDate", ""),
                            "endDate": t.get("endDate", ""),
                            "source": t.get("source", ""),
                            "tournamentKey": t.get("tournamentKey", ""),
                            "tournamentId": t.get("tournamentId", ""),
                            "calendarKey": calendar_key,
                        }
                    )
    # Store week labels once and keep the tournament rows compact.
    save_json_file(
        CALENDAR_SNAPSHOT_FILE,
        compress_calendar_snapshot(calendar_snapshot),
        formatter=dumps_calendar_snapshot,
    )


def main():
    parser = argparse.ArgumentParser(description="Refresh site data and regenerate app.html.")
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Skip the hourly preflight refresh scripts and only run the main update flow.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show per-tournament and cache-level diagnostic progress.",
    )
    parser.add_argument(
        "--force-itf-acceptance",
        action="store_true",
        help="Fetch available ITF acceptance lists except tournaments whose main draw is already published.",
    )
    parser.add_argument(
        "--entry-lists-only",
        action="store_true",
        help="Apply manual entry-list overrides from cached data and rebuild without external refreshes.",
    )
    args = parser.parse_args()

    if args.verbose:
        os.environ["WTARG_VERBOSE"] = "1"
    configure_logging(verbose=True if args.verbose else None)

    if args.entry_lists_only:
        with open(GS_PDF_URLS_FILE, encoding="utf-8") as source:
            pdf_urls = json.load(source)
        entry_cache = expand_entry_lists_cache(load_cache(ENTRY_LISTS_CACHE_FILE))
        _apply_cached_manual_entry_list_overrides(entry_cache, pdf_urls)
        save_cache(ENTRY_LISTS_CACHE_FILE, entry_cache, formatter=dumps_entry_lists_cache)
        logger.info("Applied cached manual entry-list overrides.")
        return

    if not args.fast:
        _run_hourly_preflight()

    skip_draws_fetch = os.getenv("SKIP_DRAWS_FETCH", "").strip().lower() in {"1", "true", "yes", "on"}

    # Calendar and drawsheet APIs have direct HTTP/cache paths. Keep Chrome
    # dormant unless an ITF acceptance-list request actually needs browser
    # functionality later in process_tournaments().
    driver = LazyBrowserSession(create_driver)
    itf_draws_tournaments = {}
    prefetched_itf_draws = {}
    try:
        # 1. Fetch full-year ITF calendar first (populates cache for dynamic subset)
        full_itf = get_full_itf_calendar(driver)

        # 2. Build tournament groups (WTA + ITF) — uses cached ITF data
        tournament_groups, monday_map = build_all_tournament_groups(driver)

        # 2b. Fetch ITF draws tournament list and prefetch draw payloads before
        # heavier ITF traffic later in the run.
        if skip_draws_fetch:
            logger.info("Skipping ITF draws tournament list fetch (SKIP_DRAWS_FETCH=1).")
            itf_draws_tournaments = {}
        else:
            logger.info("Fetching ITF draws tournament list...")
            itf_draws_tournaments = get_draws_itf_tournament_list(driver)
        if ENABLE_ITF_DRAWS_PREFETCH and not skip_draws_fetch:
            itf_prefetch_jobs = []
            for week, tourneys in (itf_draws_tournaments or {}).items():
                for t_key, t_info in (tourneys or {}).items():
                    tid = (t_info or {}).get("tournamentId")
                    if not tid:
                        continue
                    if is_draw_completed(_canonical_draw_store_key(t_key)):
                        continue
                    itf_prefetch_jobs.append((week, t_key, t_info))

            total_itf_prefetch = len(itf_prefetch_jobs) or 1
            for i, (_week, t_key, t_info) in enumerate(itf_prefetch_jobs, start=1):
                logger.debug(f"Prefetching ITF Draws ({i}/{total_itf_prefetch})")
                tid = t_info.get("tournamentId")
                is_multiweek = t_info.get("is_multiweek", False)
                t_draws = fetch_itf_tournament_draws(tid, is_multiweek=is_multiweek) or {}
                if t_draws:
                    prefetched_itf_draws[_canonical_draw_store_key(t_key)] = t_draws

        # 3. Fetch ARG player rankings
        players_data, arg_names_set, all_wta_players, data_status = fetch_arg_players()
        log_data_status_warnings(data_status)

        # 4. Refresh authoritative Grand Slam PDF lists, then process all entry lists.
        entry_cache = expand_entry_lists_cache(load_cache(ENTRY_LISTS_CACHE_FILE))
        entry_cache_before_pdf_override = copy.deepcopy(entry_cache)
        _refresh_entry_lists_from_pdfs(
            entry_cache,
            {},
            tournament_groups,
            monday_map,
            original_entry_cache=entry_cache_before_pdf_override,
        )
        cached_draws_for_acceptance = expand_draws_store_cache(load_cache(DRAWS_STORE_CACHE_FILE)) or {}
        qualifying_draw_available_keys = _itf_keys_with_published_qualifying_draw(
            itf_draws_tournaments,
            _normalize_draws_store_keys(cached_draws_for_acceptance),
            prefetched_itf_draws,
        )
        main_draw_available_keys = _itf_keys_with_published_main_draw(
            itf_draws_tournaments,
            _normalize_draws_store_keys(cached_draws_for_acceptance),
            prefetched_itf_draws,
        )
        schedule_map, tournament_store, entry_cache, unranked_schedule = process_tournaments(
            driver,
            tournament_groups,
            monday_map,
            arg_names_set,
            entry_cache,
            force_itf_acceptance=args.force_itf_acceptance,
            qualifying_draw_available_keys=qualifying_draw_available_keys,
            main_draw_available_keys=main_draw_available_keys,
        )

        # Persist the refreshed entry lists after the tournament pass.
        save_cache(ENTRY_LISTS_CACHE_FILE, entry_cache, formatter=dumps_entry_lists_cache)

        # 4b-seed. Compute seeds for separate qualifying-only Wimbledon lists.
        _gs_now = madrid_now()
        _gs_seed_date = (_gs_now - timedelta(days=_gs_now.weekday())).strftime("%Y-%m-%d")
        _gs_rankings = get_wta_rankings_cached(_gs_seed_date, nationality=None)
        _gs_name_to_rank = {}
        for _sp in _gs_rankings:
            _sname = _map_to_display_name_upper(_sp.get("Player") or "")
            _srank = _sp.get("Rank")
            if _sname and _srank is not None:
                _gs_name_to_rank[_sname] = int(_srank)
        for _gs_key in [k for k in tournament_store if str(k).endswith("#qual")]:
            _gs_players = tournament_store.get(_gs_key)
            if not _gs_players:
                continue
            _candidates = []
            for _p in _gs_players:
                if _p.get("type") != "QUAL":
                    continue
                _pname_up = _map_to_display_name_upper(_p["name"])
                _r = _gs_name_to_rank.get(_pname_up)
                if _r is not None:
                    _candidates.append((_r, _p["name"]))
            _candidates.sort()
            _gs_seed_map = {name: i + 1 for i, (_, name) in enumerate(_candidates[:32])}
            for _p in _gs_players:
                if _p.get("type") == "QUAL":
                    _sv = _gs_seed_map.get(_p["name"])
                    _p["seed"] = _sv if _sv is not None else ""

        # Add unranked ARG players found in entry lists to players_data and schedule_map
        existing_player_keys = {p["Player"] for p in players_data}
        for name_upper, weeks in unranked_schedule.items():
            schedule_map[name_upper] = weeks
            if name_upper not in existing_player_keys:
                players_data.append({"Player": name_upper, "Key": name_upper, "Rank": "-"})

    except Exception:
        _quit_driver(driver, "quit browser after pipeline failure")
        raise

    # 6. Fetch draws (WTA + ITF). Keep a persistent cache so draws don't "disappear"
    # when a fetch fails temporarily.
    draws_store = expand_draws_store_cache(load_cache(DRAWS_STORE_CACHE_FILE)) or {}
    if not isinstance(draws_store, dict):
        draws_store = {}
    draws_store = _normalize_draws_store_keys(draws_store)
    today_date = madrid_today()
    for store_key, entry in (draws_store or {}).items():
        if not isinstance(entry, dict):
            continue
        end_date = str(entry.get("endDate") or "")[:10]
        try:
            entry_end = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else None
        except (TypeError, ValueError):
            entry_end = None
        if entry_end and entry_end <= today_date and _draw_is_complete((entry.get("draws") or {}).get("MDS")):
            mark_draw_completed(store_key)
    if skip_draws_fetch:
        logger.info("Skipping draws fetch (SKIP_DRAWS_FETCH=1). Using cached draws store.")
        draws_tournaments = {}
        itf_draws_tournaments = {}
    else:
        draws_tournaments = get_draws_tournament_list()
    current_year = str(madrid_today().year)
    active_draw_keys = set()
    wta_draw_jobs = []
    today = madrid_now()
    for week, tourneys in (draws_tournaments or {}).items():
        for t_key, t_info in (tourneys or {}).items():
            if _is_excluded_draw_tournament(t_key, t_info):
                continue
            store_key = _canonical_draw_store_key(t_key)
            if is_draw_completed(store_key):
                logger.debug(f"  Skipping completed WTA draw: {t_info.get('name', '')}")
                continue
            if not wta_draw_polling_open(t_info.get("startDate"), today=today.date()):
                logger.debug(
                    f"  Skipping WTA draw until two days before start: {t_info.get('name', '')} "
                    f"({t_info.get('startDate', '')})"
                )
                continue
            cached_entry = draws_store.get(store_key) if isinstance(draws_store.get(store_key), dict) else {}
            requested_draw_types = _itf_requested_draw_types((cached_entry or {}).get("draws") or {})
            if not requested_draw_types:
                logger.debug(f"  Skipping completed WTA draw types: {t_info.get('name', '')}")
                continue
            active_draw_keys.add(store_key)
            wta_draw_jobs.append((week, t_key, t_info, requested_draw_types))

    total_wta_draws = len(wta_draw_jobs) or 1
    logger.info(f"Fetching WTA Draws (0/{total_wta_draws}) — parallel")

    def _fetch_wta_draw_job(job):
        week, t_key, t_info, requested_draw_types = job
        return (
            week,
            t_key,
            t_info,
            fetch_tournament_draws(
                t_key,
                current_year,
                start_date=t_info.get("startDate"),
                draw_types=requested_draw_types,
            )
            or {},
        )

    from concurrent.futures import ThreadPoolExecutor, as_completed

    wta_draw_results = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_fetch_wta_draw_job, job): job for job in wta_draw_jobs}
        for done, fut in enumerate(as_completed(futures), start=1):
            try:
                week, t_key, t_info, t_draws = fut.result()
            except Exception as e:
                week, t_key, t_info, _requested_draw_types = futures[fut]
                t_draws = {}
                logger.warning(f"  [!] WTA draw fetch failed for {t_info.get('name', '')}: {e}")
            wta_draw_results[_canonical_draw_store_key(t_key)] = (week, t_key, t_info, t_draws)
            logger.debug(f"  WTA draw fetched ({done}/{total_wta_draws}): {t_info.get('name', '')}")

    for store_key, (week, _t_key, t_info, t_draws) in wta_draw_results.items():
        prev = draws_store.get(store_key) if isinstance(draws_store.get(store_key), dict) else {}
        prev_draws = (prev or {}).get("draws") or {}
        merged_draws = {}
        if isinstance(prev_draws, dict):
            merged_draws.update(prev_draws)
        if isinstance(t_draws, dict):
            for dtype_code, new_draw in t_draws.items():
                old_draw = merged_draws.get(dtype_code)
                # Don't overwrite a non-empty cached draw with an empty new fetch
                if (
                    isinstance(old_draw, dict)
                    and old_draw.get("players")
                    and isinstance(new_draw, dict)
                    and not new_draw.get("players")
                ):
                    logger.warning(
                        f"  Keeping cached {dtype_code} for {t_info.get('name', '')} (new fetch returned empty)"
                    )
                    continue
                merged_draws[dtype_code] = new_draw
        if merged_draws:
            fetched_at = utc_timestamp() if t_draws else get_cache_timestamp(DRAWS_STORE_CACHE_FILE, store_key, prev)
            if not t_draws and prev_draws:
                logger.warning(f"  Using cached WTA draws for: {t_info.get('name', '')}")
            arg_visibility = _itf_cached_draw_arg_visibility({"draws": merged_draws})
            draws_store[store_key] = {
                "name": t_info["name"],
                "level": t_info.get("level", ""),
                "week": week,
                "startDate": t_info.get("startDate"),
                "endDate": t_info.get("endDate"),
                "draws": merged_draws,
                "arg_visibility": arg_visibility,
            }
            set_cache_entry_meta(DRAWS_STORE_CACHE_FILE, store_key, fetchedAt=fetched_at)
            if (not t_info.get("is_multiweek")) and _draw_is_complete(merged_draws.get("MDS")):
                mark_draw_completed(store_key)

    # 6b. Fetch ITF draws (prefer prefetched payloads captured earlier in the run)
    # process_tournaments (step 4) has already fetched and cached tournament IDs
    # via get_itf_players → itf_event_filters_cache.json. Use that cache to fill
    # any IDs that were missing when get_draws_itf_tournament_list ran earlier.
    _event_filters_cache = _load_itf_event_filters_cache()
    _regular_itf_draw_ids = set()
    for _tourneys in (itf_draws_tournaments or {}).values():
        for _t_key, _t_info in (_tourneys or {}).items():
            if (_t_info or {}).get("is_multiweek"):
                continue
            _tid = (_t_info or {}).get("tournamentId")
            if not _tid and isinstance(_t_key, str) and _t_key.lower().startswith("w-itf-"):
                _tid = _event_filters_cache.get(_t_key.lower())
            if _tid:
                _regular_itf_draw_ids.add(str(_tid).strip())
    _no_arg_draw_codes_by_id = tournament_draw_codes_with_definitive_no_nationality(
        _regular_itf_draw_ids,
        "ARG",
    )
    _definitive_no_arg_draw_ids = {
        tournament_id
        for tournament_id, excluded_codes in _no_arg_draw_codes_by_id.items()
        if excluded_codes == {"Q", "M"}
    }
    itf_draw_jobs = []
    today = madrid_now()
    for week, tourneys in (itf_draws_tournaments or {}).items():
        for t_key, t_info in (tourneys or {}).items():
            if _is_excluded_draw_tournament(t_key, t_info):
                continue
            store_key = _canonical_draw_store_key(t_key)
            tid = (t_info or {}).get("tournamentId")
            if not tid and isinstance(t_key, str) and t_key.lower().startswith("w-itf-"):
                cached_tid = _event_filters_cache.get(t_key.lower())
                if isinstance(cached_tid, int) and cached_tid > 0:
                    tid = cached_tid
                    t_info["tournamentId"] = cached_tid
            acceptance_players = (
                tournament_store.get(t_key)
                or entry_cache.get(t_key)
                or tournament_store.get(store_key)
                or entry_cache.get(store_key)
                or []
            )
            cached_entry = draws_store.get(store_key) if isinstance(draws_store.get(store_key), dict) else {}
            tid_text = str(tid or "").strip()
            skip_reason = _itf_draw_skip_reason(
                store_key,
                t_info,
                acceptance_players,
                cached_entry,
                today,
                definitive_no_arg_draw=tid_text in _definitive_no_arg_draw_ids,
            )
            if skip_reason is not None:
                logger.debug(f"  Skipping ITF draw for: {t_info.get('name', '')} ({skip_reason})")
                if cached_entry and _itf_cached_draw_arg_visibility(cached_entry).get("has_arg_any"):
                    logger.debug(f"  Keeping cached ARG ITF draws for: {t_info.get('name', '')}")
                continue
            active_draw_keys.add(store_key)
            if not tid:
                existing = draws_store.get(store_key) if isinstance(draws_store.get(store_key), dict) else {}
                existing_draws = existing.get("draws") if isinstance(existing.get("draws"), dict) else {}
                if existing_draws:
                    logger.warning(f"  Keeping cached ITF draws for: {t_info.get('name', '')} (missing tournamentId)")
                    arg_visibility = _itf_cached_draw_arg_visibility({"draws": existing_draws})
                    draws_store[store_key] = _merge_draw_store_entry(
                        existing,
                        {
                            "name": t_info.get("name", ""),
                            "level": t_info.get("level", ""),
                            "week": week,
                            "startDate": t_info.get("startDate"),
                            "endDate": t_info.get("endDate"),
                            "draws": existing_draws,
                            "arg_visibility": arg_visibility,
                        },
                    )
                continue
            count_empty_for_backoff = _itf_empty_draw_counts_toward_backoff(acceptance_players, cached_entry)
            cached_draws = (cached_entry or {}).get("draws") or {}
            requested_draw_types = _itf_requested_draw_types(cached_draws)
            excluded_codes = _no_arg_draw_codes_by_id.get(tid_text, set())
            requested_draw_types = [
                dtype_code
                for dtype_code in requested_draw_types
                if not (
                    (dtype_code == "QS" and "Q" in excluded_codes) or (dtype_code == "MDS" and "M" in excluded_codes)
                )
            ]
            if not requested_draw_types:
                logger.debug(f"  Skipping ITF draw for: {t_info.get('name', '')} (no ARG-relevant draw types remain)")
                continue
            itf_draw_jobs.append((week, t_key, t_info, count_empty_for_backoff, requested_draw_types))

    total_itf_draws = len(itf_draw_jobs) or 1
    itf_cooloff_applied = False
    itf_consecutive_empty = 0
    itf_consecutive_blocked = 0
    draw_fetch_errors = []
    itf_blocked_responses = []
    itf_blocked_response_keys = set()

    def _record_itf_blocked_responses(meta):
        for item in (meta or {}).get("blocked_responses") or []:
            if not isinstance(item, dict):
                continue
            record = {
                "endpoint": item.get("endpoint") or "itf",
                "tournament_id": str(item.get("tournament_id") or ""),
                "tournament_name": item.get("tournament_name") or "",
                "code": item.get("code") or "",
                "week_number": str(item.get("week_number") or ""),
            }
            record_key = (
                record["endpoint"],
                record["tournament_id"],
                record["tournament_name"],
                record["code"],
                record["week_number"],
            )
            if record_key in itf_blocked_response_keys:
                continue
            itf_blocked_response_keys.add(record_key)
            itf_blocked_responses.append(record)

    def _fetch_itf_draws_with_meta(
        tid,
        is_multiweek,
        cached_draws,
        tournament_name,
        requested_draw_types,
    ):
        draws_result, meta = fetch_itf_tournament_draws(
            tid,
            is_multiweek=is_multiweek,
            driver=driver,
            cached_draws=cached_draws,
            tournament_name=tournament_name,
            return_meta=True,
            draw_types=requested_draw_types,
        )
        _record_itf_blocked_responses(meta)
        return draws_result, meta

    for i, (week, t_key, t_info, count_empty_for_backoff, requested_draw_types) in enumerate(itf_draw_jobs, start=1):
        logger.debug(f"Fetching ITF Draws ({i}/{total_itf_draws})")
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
            logger.debug(f"  Draws complete, using cache: {t_info.get('name', '')}")
            arg_visibility = _itf_cached_draw_arg_visibility({"draws": prev_draws})
            draws_store[store_key] = _merge_draw_store_entry(
                prev,
                {
                    "name": t_info["name"],
                    "level": t_info.get("level", ""),
                    "week": week,
                    "startDate": t_info.get("startDate"),
                    "endDate": t_info.get("endDate"),
                    "draws": prev_draws,
                    "arg_visibility": arg_visibility,
                },
            )
            continue

        t_draws = prefetched_itf_draws.get(store_key) if isinstance(prefetched_itf_draws.get(store_key), dict) else {}
        fetch_had_block = False
        if not t_draws:
            t_draws, meta = _fetch_itf_draws_with_meta(
                tid,
                is_multiweek,
                prev_draws,
                t_info.get("name", ""),
                requested_draw_types,
            )
            fetch_had_block = bool((meta or {}).get("blocked_responses"))
        if not t_draws and not itf_cooloff_applied and i == 1 and len(itf_draw_jobs) >= ITF_FIRST_BURST_MIN_JOBS:
            # ITF often enforces a short temporary block after the tournament-id burst.
            # Wait once, then retry the same event with a fresh session.
            logger.warning(f"  ITF cooldown triggered ({ITF_FIRST_BURST_COOLDOWN_SEC}s) before retrying draw fetch...")
            time.sleep(ITF_FIRST_BURST_COOLDOWN_SEC)
            itf_cooloff_applied = True
            t_draws, meta = _fetch_itf_draws_with_meta(
                tid,
                is_multiweek,
                prev_draws,
                t_info.get("name", ""),
                requested_draw_types,
            )
            fetch_had_block = fetch_had_block or bool((meta or {}).get("blocked_responses"))

        merged_draws = {}
        if isinstance(prev_draws, dict):
            merged_draws.update(prev_draws)
        if isinstance(t_draws, dict):
            merged_draws.update(t_draws)
        # Extra gap-fill pass for ordinary empty responses. A blocked request
        # has already had its quiet-period retry; an immediate gap-fill burst
        # only extends Imperva's block.
        expected_draw_types = set(requested_draw_types)
        if not expected_draw_types.issubset(merged_draws.keys()) and not fetch_had_block:
            for _ in range(2):
                extra_draws, meta = _fetch_itf_draws_with_meta(
                    tid,
                    is_multiweek,
                    merged_draws,
                    t_info.get("name", ""),
                    requested_draw_types,
                )
                fetch_had_block = fetch_had_block or bool((meta or {}).get("blocked_responses"))
                if isinstance(extra_draws, dict):
                    merged_draws.update(extra_draws)
                if expected_draw_types.issubset(merged_draws.keys()):
                    break
        if merged_draws:
            itf_consecutive_empty = 0
            itf_consecutive_blocked = 0
            fetched_at = utc_timestamp() if t_draws else get_cache_timestamp(DRAWS_STORE_CACHE_FILE, store_key, prev)
            if not t_draws and prev_draws:
                logger.warning(f"  Using cached ITF draws for: {t_info.get('name', '')}")
            arg_visibility = _itf_cached_draw_arg_visibility({"draws": merged_draws})
            draws_store[store_key] = {
                "name": t_info["name"],
                "level": t_info.get("level", ""),
                "week": week,
                "startDate": t_info.get("startDate"),
                "endDate": t_info.get("endDate"),
                "draws": merged_draws,
                "arg_visibility": arg_visibility,
            }
            set_cache_entry_meta(DRAWS_STORE_CACHE_FILE, store_key, fetchedAt=fetched_at)
            if (not t_info.get("is_multiweek")) and _draw_is_complete(merged_draws.get("MDS")):
                mark_draw_completed(store_key)
        else:
            if fetch_had_block:
                itf_consecutive_blocked += 1
                itf_consecutive_empty = 0
                if (
                    itf_consecutive_blocked >= ITF_CONSECUTIVE_BLOCKED_THRESHOLD
                    and i < total_itf_draws
                ):
                    logger.warning(
                        f"  ITF backoff triggered ({ITF_CONSECUTIVE_BLOCKED_BACKOFF_SEC}s) "
                        "after consecutive 403 blocks."
                    )
                    time.sleep(ITF_CONSECUTIVE_BLOCKED_BACKOFF_SEC)
                    itf_consecutive_blocked = 0
                    itf_consecutive_empty = 0
            elif count_empty_for_backoff:
                itf_consecutive_empty += 1
                itf_consecutive_blocked = 0
                draw_fetch_errors.append(
                    {
                        "key": t_key,
                        "name": t_info.get("name", t_key),
                        "startDate": (t_info.get("startDate") or "")[:10],
                        "drawTypes": requested_draw_types,
                        "reason": _itf_empty_draw_status(requested_draw_types),
                    }
                )
                if itf_consecutive_empty >= ITF_CONSECUTIVE_EMPTY_THRESHOLD and i < total_itf_draws:
                    # Back off only for ARG-relevant empties; no-ARG events often
                    # legitimately return nothing and should not look like a block.
                    draw_label = _itf_draw_types_label(requested_draw_types)
                    draw_noun = (
                        f"{draw_label} draw" if draw_label in {"main", "qualifying"} else f"{draw_label} draws"
                    )
                    logger.warning(
                        f"  ITF backoff triggered ({ITF_CONSECUTIVE_EMPTY_BACKOFF_SEC}s) after consecutive "
                        f"ARG-relevant empty {draw_noun} - refreshing session."
                    )
                    time.sleep(ITF_CONSECUTIVE_EMPTY_BACKOFF_SEC)
                    driver.reset()
                    itf_consecutive_empty = 0

    # Write draw fetch errors for this run (always overwrite so stale errors are cleared).
    save_json_file(DRAW_FETCH_ERRORS_FILE, draw_fetch_errors)
    save_json_file(ITF_BLOCKED_RESPONSES_FILE, itf_blocked_responses)

    # Prune draws for tournaments that are definitely over (endDate < today).
    today = madrid_today()
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
        except (TypeError, ValueError):
            continue
        if end_date < today:
            keys_to_delete.append(t_key)
    for t_key in keys_to_delete:
        draws_store.pop(t_key, None)

    # Always remove explicitly excluded draws (e.g., Roland Garros) from cache/output.
    excluded_draw_keys = [
        k for k, v in (draws_store or {}).items() if _is_excluded_draw_tournament(k, v if isinstance(v, dict) else None)
    ]
    for t_key in excluded_draw_keys:
        draws_store.pop(t_key, None)

    # Capture draw publication for every WTA Entry List and ARG-relevant ITF
    # lists. Confirmed no-ARG ITF lists use the Monday-removal rule instead.
    qualifying_draw_available_keys = _itf_keys_with_published_qualifying_draw(
        itf_draws_tournaments,
        draws_store,
        prefetched_itf_draws,
    )
    main_draw_available_keys = _itf_keys_with_published_main_draw(
        itf_draws_tournaments,
        draws_store,
        prefetched_itf_draws,
    )
    qualifying_draw_available_keys = {
        key
        for key in qualifying_draw_available_keys
        if not (
            str(key).lower().startswith("w-itf-")
            and _entry_list_proves_no_arg(
                entry_cache.get(key) or tournament_store.get(key) or []
            )
        )
    }
    main_draw_available_keys = {
        key
        for key in main_draw_available_keys
        if not (
            str(key).lower().startswith("w-itf-")
            and _entry_list_proves_no_arg(
                entry_cache.get(key) or tournament_store.get(key) or []
            )
        )
    }
    _record_entry_draw_availability(
        qualifying_draw_available_keys,
        main_draw_available_keys,
        observed_date=today.isoformat(),
    )

    # Remove any ITF draw entries that do not contain ARG players.
    argless_draw_keys = [
        k
        for k, v in (draws_store or {}).items()
        if str(k).lower().startswith("w-itf-") and not _itf_cached_draw_arg_visibility(v).get("has_arg_any")
    ]
    for t_key in argless_draw_keys:
        draws_store.pop(t_key, None)

    # Persist draws cache so a successful draw doesn't disappear on a later failed run.
    save_json_file(DRAWS_STORE_CACHE_FILE, draws_store, formatter=dumps_draws_store_cache)

    # Save draws snapshot (tournament key -> list of draw types available)
    draws_snapshot = {}
    for t_key, tdata in draws_store.items():
        draws_snapshot[t_key] = {
            "name": tdata["name"],
            "types": list(tdata.get("draws", {}).keys()),
        }
    save_json_file(os.path.join(DATA_DIR, "draws_snapshot.json"), compress_draws_snapshot(draws_snapshot))

    _quit_driver(driver)

    # 7. Build calendar — uses cached WTA data
    full_wta = get_full_wta_calendar()
    _manual_entries_file = os.path.join(DATA_DIR, "manual_calendar_entries.json")
    if os.path.exists(_manual_entries_file):
        with open(_manual_entries_file, encoding="utf-8") as source:
            _manual_entries = json.load(source)
    else:
        _manual_entries = []
    calendar_data = build_calendar_data(full_wta + full_itf + _manual_entries)
    build_calendar_snapshot(calendar_data)

    # 7b. Build tournament strength data (cached)
    logger.info("Processing WTA Tournament Strength")
    build_tstrength_data()


if __name__ == "__main__":
    from pipeline_transaction import run_current_script_transaction, transaction_is_active

    if transaction_is_active():
        main()
    else:
        raise SystemExit(run_current_script_transaction(__file__, include_generated_site=True))
