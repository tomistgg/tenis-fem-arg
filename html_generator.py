import base64
import contextlib
import hashlib
import json
import math
import os
import re
from datetime import datetime, timedelta
from html import escape, unescape
from pathlib import Path

from calendar_builder import format_week_label
from config import (
    CONTINENT_KEYS,
    CONTINENT_LABELS,
    MOBILE_CONTINENT_LABELS,
    NAME_LOOKUP,
    PLAYER_IDENTITIES,
    PLAYER_MAPPING,
    load_player_mapping,
    player_name_only,
    resolve_player_display_name,
    resolve_player_presentation_name,
)
from pipeline_errors import DataValidationError
from run_state import report_run_issue
from runtime_logging import get_logger
from runtime_paths import DATA_DIR as RUNTIME_DATA_DIR
from runtime_paths import SITE_ROOT as RUNTIME_SITE_ROOT
from time_utils import madrid_today
from utils import (
    compact_tournament_name,
    compress_history_data,
    dumps_history_data,
    dumps_readable,
    dumps_wta_rankings_bundle,
    expand_points_distribution,
    expand_tournament_draw_sizes,
    expand_wta_calendar_cache,
    fix_encoding_keep_accents,
    format_player_name,
    get_surface_class,
    get_tournament_sort_order,
    write_text_if_changed,
)
from wta import _load_wta_csv

logger = get_logger("html-generator")

_FRONTEND_SOURCE_ROOT = Path(__file__).resolve().parent / "web"
_FRONTEND_TOKEN_RE = re.compile(r"@@WTARG_([A-Z0-9_]+)@@")
_FRONTEND_ASSETS = {
    "css/app.css": "assets/app.css",
    "js/app.js": "assets/js/app.js",
    "js/data-loader.js": "assets/js/data-loader.js",
    "js/router.js": "assets/js/router.js",
    "js/tabs/draws.js": "assets/js/tabs/draws.js",
    "js/tabs/roadtogs.js": "assets/js/tabs/roadtogs.js",
    "js/tabs/tstrength.js": "assets/js/tabs/tstrength.js",
}
_CSP_PLACEHOLDER = "__WTARG_CSP_META__"
_CSP_META_RE = re.compile(
    r'\s*<meta\s+http-equiv=["\']Content-Security-Policy["\']\s+content=(?:"[^"]*"|\'[^\']*\')\s*/?>',
    re.IGNORECASE,
)


def _week_label_sort_key(label):
    """Return a chronological key for labels such as ``Week of August 17``."""
    if not label:
        return datetime.max
    match = re.search(
        r"Week of\s+([A-Za-z]+)\s+(\d{1,2})(?:,?\s+(\d{4}))?",
        str(label),
        re.IGNORECASE,
    )
    if not match:
        return datetime.max
    month = match.group(1)
    day = int(match.group(2))
    year = int(match.group(3)) if match.group(3) else madrid_today().year
    for month_format in ("%B", "%b"):
        try:
            return datetime.strptime(f"{month} {day} {year}", f"{month_format} %d %Y")
        except ValueError:
            continue
    return datetime.max


def _schedule_tournament_base_name(entry):
    """Strip Schedule position labels before looking up tournament metadata."""
    plain = re.sub(r"<[^>]+>", "", entry or "").strip()
    return re.sub(
        r"\s*\((?:Q|ALT(?:\s+[^)]+)?)\)\s*$",
        "",
        plain,
        flags=re.IGNORECASE,
    ).strip()


def _display_tournament_name(name):
    """Hide source relocation notes while preserving the canonical name."""
    return re.sub(
        r"\s*\(\s*moved\s+from\b[^)]*\)",
        "",
        str(name or ""),
        flags=re.IGNORECASE,
    ).strip()


_CALENDAR_TOURNAMENT_NAME_REPLACEMENTS = {
    "Alcala de Henares": "Alcala de H.",
    "Campos do Jordao": "Campos do J.",
    "Campos do Jordão": "Campos do J.",
    "Cherbourg-en-Cotentin": "Cherbourg",
    "Grodzisk Mazowiecki": "Grodzisk M.",
    "Kursumlijska Banja": "K. Banja",
    "Saint-Palais-sur-Mer": "Saint-Palais",
    "Santa Margherita di Pula": "St. Marg. di Pula",
    "Sharm ElSheikh": "Sharm ES.",
    "Caldas Da Rainha": "Caldas Da R.",
}


def _display_calendar_tournament_name(name):
    """Return the compact Calendar-only label without edition numbers."""
    return compact_tournament_name(name)


_INLINE_SCRIPT_RE = re.compile(
    r"<script\b(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
_HTML_TAG_RE = re.compile(r"<[^<>]+>", re.DOTALL)
_EVENT_HANDLER_ATTR_RE = re.compile(
    r"\s(on[a-z][\w:-]*)\s*=\s*(?P<quote>['\"])(?P<value>.*?)(?P=quote)",
    re.IGNORECASE | re.DOTALL,
)


def _csp_hash(value):
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return "'sha256-" + base64.b64encode(digest).decode("ascii") + "'"


def _script_safe_json(payload, **options):
    """Serialize data for an inline script without allowing a closing tag."""

    serialized = json.dumps(payload, **options)
    return (
        serialized.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _roll_forward_passed_gs_cutoffs(gs_data, today):
    """Advance a Slam only after its qualifying cutoff date has passed."""

    today_str = today.isoformat()
    for gs in gs_data:
        q_cutoff = gs.get("qCutoff")
        if q_cutoff in ("N/A", "") or q_cutoff >= today_str:
            continue
        gs["qCutoff"] = (datetime.strptime(q_cutoff, "%Y-%m-%d") + timedelta(weeks=52)).strftime("%Y-%m-%d")
        gs["mdCutoff"] = (datetime.strptime(gs["mdCutoff"], "%Y-%m-%d") + timedelta(weeks=52)).strftime("%Y-%m-%d")
        if isinstance(gs.get("year"), int):
            gs["year"] += 1


def _apply_special_gs_cutoff_overrides(gs_name, gs_year, q_cutoff, md_cutoff):
    """Keep the Australian Open fixed at the Nov 15 entry cutoff."""

    if gs_name == "Australian Open" and isinstance(gs_year, int):
        fixed_cutoff = datetime(gs_year - 1, 11, 15).strftime("%Y-%m-%d")
        return fixed_cutoff, fixed_cutoff
    return q_cutoff, md_cutoff


def _calendar_week_label_for_date(date_value):
    """Return the visible calendar week label for a cutoff date."""
    if date_value in (None, "", "N/A"):
        return ""
    dt = datetime.strptime(str(date_value)[:10], "%Y-%m-%d")
    week_start = dt - timedelta(days=dt.weekday())
    return format_week_label(week_start)


def _register_cutoff_box(boxes, date_value, sort_key, label):
    """Store a cutoff box under date, Monday, and visible week-label keys."""
    if date_value in (None, "", "N/A"):
        return

    iso_key = str(date_value)[:10]
    cutoff_dt = datetime.strptime(iso_key, "%Y-%m-%d")
    monday_key = (cutoff_dt - timedelta(days=cutoff_dt.weekday())).strftime("%Y-%m-%d")
    label_key = _calendar_week_label_for_date(iso_key)
    for key in {iso_key, monday_key, label_key}:
        if key:
            boxes.setdefault(key, []).append((sort_key, label))


def _build_gs_cutoff_boxes(gs_data, frozen_mondays):
    """Summarize calendar cutoff labels for each GS by last valid points week."""

    boxes = {}
    for gi, gs in enumerate(gs_data):
        if gs.get("mdCutoff") in ("N/A", "") and gs.get("name") != "Australian Open":
            continue

        gs_label = {"Australian Open": "AO", "Roland Garros": "RG", "Wimbledon": "WMB", "US Open": "USO"}.get(
            gs.get("name"), gs.get("name")
        )

        if gs.get("name") == "Australian Open":
            default_year = gs.get("year") or madrid_today().year
            ao_dt = datetime.strptime(f"{default_year - 1}-11-09", "%Y-%m-%d")
            _register_cutoff_box(boxes, ao_dt.strftime("%Y-%m-%d"), gi * 3, "Last week for AO MD/Q")
            continue

        start_dt = datetime.strptime(gs["mdCutoff"], "%Y-%m-%d") + timedelta(weeks=6)
        for di, (draw_type, wks) in enumerate([("MD", 6), ("Q", 4)]):
            cutoff_dt = start_dt - timedelta(weeks=wks)
            cutoff_str = cutoff_dt.strftime("%Y-%m-%d")
            last_dt = (
                (cutoff_dt - timedelta(weeks=2))
                if cutoff_str in frozen_mondays
                else (cutoff_dt - timedelta(weeks=1))
            )
            _register_cutoff_box(
                boxes,
                last_dt.strftime("%Y-%m-%d"),
                gi * 3 + di * 2,
                f"Last week for {gs_label} {draw_type}{' in W50+' if draw_type == 'Q' else ''}",
            )
            if draw_type == "Q":
                w1535_dt = last_dt - timedelta(weeks=1)
                _register_cutoff_box(
                    boxes,
                    w1535_dt.strftime("%Y-%m-%d"),
                    gi * 3 + di * 2 + 1,
                    f"Last week for {gs_label} Q in W15/W35",
                )
    return boxes


def _html_for_csp_hashing(html_text):
    html_text = _CSP_META_RE.sub("", html_text)
    return html_text.replace(_CSP_PLACEHOLDER, "")


def _script_hash_sources(html_text):
    html_text = _html_for_csp_hashing(html_text)
    hashes = set()
    for match in _INLINE_SCRIPT_RE.finditer(html_text):
        script_text = match.group(1)
        if script_text.strip():
            hashes.add(_csp_hash(script_text))

    for tag in _HTML_TAG_RE.finditer(html_text):
        for attr in _EVENT_HANDLER_ATTR_RE.finditer(tag.group(0)):
            handler_text = unescape(attr.group("value")).strip()
            if handler_text:
                hashes.add(_csp_hash(handler_text))

    return sorted(hashes)


def _content_security_policy_meta(html_text):
    script_sources = " ".join(_script_hash_sources(html_text))
    script_src = "'self' 'unsafe-hashes'"
    if script_sources:
        script_src = f"{script_src} {script_sources}"
    policy = (
        "default-src 'self'; "
        f"script-src {script_src}; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob: https://purecatamphetamine.github.io; "
        "font-src 'self' data:; "
        "connect-src 'self' https://script.google.com https://script.googleusercontent.com "
        "https://api.country.is; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "manifest-src 'self';"
    )
    return f'<meta http-equiv="Content-Security-Policy" content="{escape(policy, quote=True)}">'


def _apply_content_security_policy(html_text):
    html_text = _CSP_META_RE.sub("", html_text)
    if _CSP_PLACEHOLDER not in html_text:
        return html_text
    return html_text.replace(_CSP_PLACEHOLDER, _content_security_policy_meta(html_text), 1)


def _frontend_source_path(relative_path):
    source_path = (_FRONTEND_SOURCE_ROOT / relative_path).resolve()
    try:
        source_path.relative_to(_FRONTEND_SOURCE_ROOT.resolve())
    except ValueError as exc:
        raise DataValidationError(
            component="html-generator",
            operation="resolve frontend source",
            message="frontend source path escapes the web directory",
            context={"path": str(relative_path)},
        ) from exc
    return source_path


def _read_frontend_source(relative_path):
    source_path = _frontend_source_path(relative_path)
    try:
        return source_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DataValidationError(
            component="html-generator",
            operation="read frontend source",
            message="required frontend source is unavailable",
            context={"path": str(source_path)},
        ) from exc


def _render_frontend_source(relative_path, context):
    source = _read_frontend_source(relative_path)
    required = set(_FRONTEND_TOKEN_RE.findall(source))
    missing = sorted(required.difference(context))
    if missing:
        raise DataValidationError(
            component="html-generator",
            operation="render frontend template",
            message="frontend template context is incomplete",
            context={"path": relative_path, "missing": missing},
        )
    return _FRONTEND_TOKEN_RE.sub(
        lambda match: str(context[match.group(1)]),
        source,
    )


def _write_frontend_assets(site_root, frontend_data):
    site_root = Path(site_root)
    for source_name, output_name in _FRONTEND_ASSETS.items():
        destination = site_root / output_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        write_text_if_changed(
            str(destination),
            _read_frontend_source(source_name),
            encoding="utf-8",
        )

    data_destination = site_root / "assets/js/generated-data.js"
    data_destination.parent.mkdir(parents=True, exist_ok=True)
    serialized = _script_safe_json(
        frontend_data,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    write_text_if_changed(
        str(data_destination),
        f"window.__WTARG_GENERATED_DATA__ = {serialized};\n",
        encoding="utf-8",
    )


IOC_TO_ISO2 = {
    "ALB": "al",
    "ALG": "dz",
    "AND": "ad",
    "ANG": "ao",
    "ARG": "ar",
    "ARM": "am",
    "ASA": "as",
    "AUS": "au",
    "AUT": "at",
    "AZE": "az",
    "BAH": "bs",
    "BAR": "bb",
    "BDI": "bi",
    "BEL": "be",
    "BEN": "bj",
    "BIH": "ba",
    "BLR": "by",
    "BOL": "bo",
    "BOT": "bw",
    "BRA": "br",
    "BUL": "bg",
    "CAL": "nc",
    "CAM": "kh",
    "CAN": "ca",
    "CHI": "cl",
    "CHL": "cl",
    "CHN": "cn",
    "CIV": "ci",
    "CMR": "cm",
    "COD": "cd",
    "COL": "co",
    "CRC": "cr",
    "CRO": "hr",
    "CUB": "cu",
    "CUW": "cw",
    "CYP": "cy",
    "CZE": "cz",
    "CZS": "cz",
    "DEN": "dk",
    "DOM": "do",
    "DZA": "dz",
    "ECU": "ec",
    "EGY": "eg",
    "ERI": "er",
    "ESA": "sv",
    "ESP": "es",
    "EST": "ee",
    "FIJ": "fj",
    "FIN": "fi",
    "FRA": "fr",
    "FRG": "de",
    "GAB": "ga",
    "GBR": "gb",
    "GEO": "ge",
    "GER": "de",
    "GHA": "gh",
    "GLP": "gp",
    "GRB": "gb",
    "GRE": "gr",
    "GRC": "gr",
    "GUA": "gt",
    "HAI": "ht",
    "HKG": "hk",
    "HRV": "hr",
    "HUN": "hu",
    "INA": "id",
    "IND": "in",
    "IRI": "ir",
    "IRL": "ie",
    "IRN": "ir",
    "ISR": "il",
    "ITA": "it",
    "JAM": "jm",
    "JOR": "jo",
    "JPN": "jp",
    "KAZ": "kz",
    "KEN": "ke",
    "KGZ": "kg",
    "KHM": "kh",
    "KOR": "kr",
    "KOS": "xk",
    "KSA": "sa",
    "LAO": "la",
    "LAT": "lv",
    "LIE": "li",
    "LTU": "lt",
    "LUX": "lu",
    "MAD": "mg",
    "MAR": "ma",
    "MAS": "my",
    "MDA": "md",
    "MEX": "mx",
    "MKD": "mk",
    "MLI": "ml",
    "MLT": "mt",
    "MNE": "me",
    "MON": "mc",
    "MRI": "mu",
    "MOZ": "mz",
    "NAM": "na",
    "NCA": "ni",
    "NCD": "nc",
    "NED": "nl",
    "NEP": "np",
    "NET": "nl",
    "NGA": "ng",
    "NGR": "ng",
    "NOR": "no",
    "NZL": "nz",
    "OMA": "om",
    "OMN": "om",
    "PAK": "pk",
    "PAN": "pa",
    "PAR": "py",
    "PER": "pe",
    "PHI": "ph",
    "PLE": "ps",
    "PNG": "pg",
    "POL": "pl",
    "POR": "pt",
    "PUR": "pr",
    "QAT": "qa",
    "ROC": "ru",
    "ROM": "ro",
    "ROU": "ro",
    "RSA": "za",
    "RUS": "ru",
    "SAF": "za",
    "SAM": "ws",
    "SEN": "sn",
    "SGP": "sg",
    "SIN": "sg",
    "SLO": "si",
    "SMR": "sm",
    "SRB": "rs",
    "SRI": "lk",
    "SUI": "ch",
    "SVK": "sk",
    "SWE": "se",
    "SYR": "sy",
    "TCH": "cz",
    "THA": "th",
    "TKM": "tm",
    "TOG": "tg",
    "TPE": "tw",
    "TRI": "tt",
    "TTO": "tt",
    "TUN": "tn",
    "TUR": "tr",
    "UAE": "ae",
    "UKR": "ua",
    "URU": "uy",
    "USA": "us",
    "UZB": "uz",
    "VEN": "ve",
    "VIE": "vn",
    "XKX": "xk",
    "ZAM": "zm",
    "ZIM": "zw",
}

# ``national_team_order.csv`` stores the opposing nation as a display name in
# its legacy Tie column. Keep that source value available for the flag even
# though the column itself is no longer rendered in Player Debuts.
_BJKC_TIE_COUNTRY_CODES = {
    name.casefold(): code
    for name, code in {
        "Australia": "AUS",
        "Austria": "AUT",
        "Belgium": "BEL",
        "Bolivia": "BOL",
        "Bulgaria": "BUL",
        "Chile": "CHI",
        "Colombia": "COL",
        "Croatia": "CRO",
        "Cuba": "CUB",
        "Denmark": "DEN",
        "Dominican Republic": "DOM",
        "Ecuador": "ECU",
        "Estonia": "EST",
        "Finland": "FIN",
        "France": "FRA",
        "Germany F.R.": "FRG",
        "Greece": "GRE",
        "Guatemala": "GUA",
        "Hungary": "HUN",
        "Japan": "JPN",
        "Korea, Rep.": "KOR",
        "Netherlands": "NED",
        "New Zealand": "NZL",
        "Paraguay": "PAR",
        "Peru": "PER",
        "Philippines": "PHI",
        "Poland": "POL",
        "Russia": "RUS",
        "Slovenia": "SLO",
        "Sweden": "SWE",
        "Switzerland": "SUI",
        "Ukraine": "UKR",
        "USA": "USA",
        "Venezuela": "VEN",
    }.items()
}

# Dissolved countries with local SVG flags
LOCAL_FLAGS = {"AHO", "YUG", "SCG", "CIS", "URS"}

FLAG_STYLE = "vertical-align:middle;margin-right:3px;width:16px;height:11px;outline:0.3px solid #000"

# Road-to-GS thresholds â€” single source of truth shared between JS logic and the
# user-facing legend text so the displayed numbers can't drift from the calculation.
GS_THRESHOLD_Q = 330
GS_THRESHOLD_MD = 780


def country_flag_html(code, show_code=True):
    if not code or code == "-":
        return code or ""
    upper = code.upper()
    if upper == "GRC":
        upper = "GRE"
        code = "GRE"
    if upper in LOCAL_FLAGS:
        img = f'<img src="data/flags/{upper.lower()}.svg" alt="{code}" title="{code}" style="{FLAG_STYLE}">'
        return f"{img}{code}" if show_code else img
    iso = IOC_TO_ISO2.get(upper)
    if not iso:
        return code
    img = f'<img src="https://purecatamphetamine.github.io/country-flag-icons/3x2/{iso.upper()}.svg" alt="{code}" title="{code}" style="{FLAG_STYLE}">'
    return f"{img}{code}" if show_code else img


def _bjkc_tie_country_code(value):
    raw = str(value or "").strip()
    if not raw:
        return ""
    upper = raw.upper()
    if upper in IOC_TO_ISO2:
        return upper
    return _BJKC_TIE_COUNTRY_CODES.get(raw.casefold(), "")


def _player_display_name(raw_name):
    name = fix_encoding_keep_accents(str(raw_name or "")).strip()
    if not name:
        return ""
    mapped = NAME_LOOKUP.get(name.upper(), name)
    return format_player_name(mapped)


def _write_js_bundle_file(bundle_path, global_name, data, formatter=None):
    """Write a simple classic-script bundle that assigns data to a window global."""
    os.makedirs(os.path.dirname(bundle_path), exist_ok=True)
    dump_func = formatter or dumps_readable
    payload = dump_func(data, ensure_ascii=False)
    write_text_if_changed(bundle_path, f"window.{global_name} = {payload};\n", encoding="utf-8")


_LEGACY_WTA_RANKING_BUNDLES = {
    "wta_rankings_20_29_bundle.js",
    "wta_rankings_10_19_bundle.js",
    "wta_rankings_00_09_bundle.js",
    "wta_rankings_83_99_bundle.js",
}
_YEAR_WTA_RANKING_BUNDLE_RE = re.compile(r"^wta_rankings_\d{4}_bundle\.js$")


def _ranking_display_name(player):
    """Resolve a ranking identity to its explicitly configured public name.

    The canonical ``Player`` value remains differentiated for matching, while
    ``presentation_name`` metadata controls display without parsing suffixes.
    """
    raw_name = str(player.get("Player", "") or "")
    resolved = resolve_player_presentation_name(
        "wta",
        player_id=player.get("Id", ""),
        name=raw_name,
    )
    return resolved.upper() if raw_name.isupper() else resolved


def _ranking_bundle_rows(players):
    rows = []
    identity_indexes = {}
    for player in players or []:
        row = {
            "r": player.get("Rank") if player.get("Rank") is not None else None,
            "pts": player.get("Points", 0),
            "n": _ranking_display_name(player),
            "c": player.get("Country", ""),
            "d": str(player.get("DOB", "") or "").replace("\r", "").strip(),
        }
        identity = (
            row["n"].casefold(),
            row["d"] or row["c"].casefold(),
        )
        existing_index = identity_indexes.get(identity)
        if existing_index is None:
            identity_indexes[identity] = len(rows)
            rows.append(row)
            continue

        existing = rows[existing_index]
        existing_rank = existing["r"] if isinstance(existing["r"], int) else math.inf
        candidate_rank = row["r"] if isinstance(row["r"], int) else math.inf
        if (candidate_rank, -int(row["pts"] or 0)) < (
            existing_rank,
            -int(existing["pts"] or 0),
        ):
            rows[existing_index] = row
    return rows


def _write_wta_ranking_bundles(rankings_by_date, data_dir, dates=None):
    """Write one lazy-load bundle per year plus a tiny latest-week bundle."""

    all_dates = sorted(dates if dates is not None else rankings_by_date.keys())
    dates_by_year = {}
    for date_str in all_dates:
        try:
            year = int(date_str[:4])
        except (TypeError, ValueError):
            continue
        dates_by_year.setdefault(year, []).append(date_str)

    expected_files = {"wta_rankings_latest_bundle.js"}
    for year, year_dates in sorted(dates_by_year.items()):
        filename = f"wta_rankings_{year}_bundle.js"
        payload = {date_str: _ranking_bundle_rows(rankings_by_date.get(date_str) or []) for date_str in year_dates}
        _write_js_bundle_file(
            os.path.join(data_dir, filename),
            f"__WTA_RANKINGS_{year}__",
            payload,
            formatter=dumps_wta_rankings_bundle,
        )
        expected_files.add(filename)

    latest_date = all_dates[-1] if all_dates else ""
    latest_payload = {latest_date: _ranking_bundle_rows(rankings_by_date.get(latest_date) or [])} if latest_date else {}
    _write_js_bundle_file(
        os.path.join(data_dir, "wta_rankings_latest_bundle.js"),
        "__WTA_RANKINGS_LATEST__",
        latest_payload,
        formatter=dumps_wta_rankings_bundle,
    )

    for filename in os.listdir(data_dir):
        if filename in _LEGACY_WTA_RANKING_BUNDLES or (
            _YEAR_WTA_RANKING_BUNDLE_RE.fullmatch(filename) and filename not in expected_files
        ):
            os.remove(os.path.join(data_dir, filename))
    return latest_date, expected_files


_ORDINAL_ROUND_TO_DEPTH = {
    "1st Round": 1,
    "2nd Round": 2,
    "3rd Round": 3,
    "4th Round": 4,
    "5th Round": 5,
}


def _history_draw_slot_size(max_ordinal_depth, seed_implies_128):
    if max_ordinal_depth >= 5:
        return 256
    if max_ordinal_depth >= 4 or seed_implies_128:
        return 128
    if max_ordinal_depth >= 3:
        return 64
    return None


def _history_draw_keys(tournament_id, date_value, tournament_name):
    year = str(date_value or "")[:4]
    if not year:
        return []
    keys = []
    norm_id = str(tournament_id or "").strip()
    if norm_id:
        with contextlib.suppress(ValueError):
            norm_id = str(int(norm_id))
        keys.append(f"id|{norm_id}|{year}")
    norm_name = str(tournament_name or "").strip().upper()
    if norm_name:
        keys.append(f"name|{norm_name}|{year}")
    return keys


def _build_history_draw_slot_lookup(cleaned_history):
    """Infer historical bracket slots from observed ordinal round depth.

    Older WTA and ITF rows (for example 56- and 48-player draws) are not always
    present in the modern draw-size cache. When a tournament reaches a 3rd Round
    before QF, its first round should display as the 64-slot round (R64), not as
    a 32 draw.
    """
    event_state = {}
    for row in cleaned_history or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("DRAW") or row.get("draw") or "").strip().upper() != "M":
            continue
        match_type = str(row.get("MATCH_TYPE") or row.get("matchType") or "").strip().upper()
        if match_type not in {"WTA", "ITF"}:
            continue
        round_name = str(row.get("ROUND") or row.get("roundName") or "").strip()
        depth = _ORDINAL_ROUND_TO_DEPTH.get(round_name, 0)
        seed_values = []
        for seed_field in ("_winnerSeed", "_loserSeed", "winnerSeed", "loserSeed"):
            seed = str(row.get(seed_field) or "").strip()
            if seed.isdigit():
                seed_values.append(int(seed))
        max_seed = max(seed_values, default=0)
        category = str(row.get("CATEGORY") or row.get("tournamentCategory") or "").strip().upper()
        seed_implies_128 = max_seed > 16 and category in {"WTA 1000", "PREMIER MANDATORY"}
        if not depth and not max_seed:
            continue
        tournament_id = row.get("TOURNAMENT_ID") or row.get("tournamentId") or ""
        date_value = row.get("DATE") or row.get("date") or ""
        tournament_name = row.get("TOURNAMENT") or row.get("tournamentName") or ""
        for key in _history_draw_keys(tournament_id, date_value, tournament_name):
            state = event_state.setdefault(key, {"depth": 0, "seed_implies_128": False})
            state["depth"] = max(state["depth"], depth)
            state["seed_implies_128"] = state["seed_implies_128"] or seed_implies_128

    return {
        key: slot_size
        for key, state in event_state.items()
        for slot_size in [_history_draw_slot_size(state["depth"], state["seed_implies_128"])]
        if slot_size
    }


def generate_html(
    tournament_groups,
    tournament_store,
    players_data,
    schedule_map,
    cleaned_history,
    calendar_data,
    match_history_data,
    wta_rankings=None,
    national_team_data=None,
    captains_data=None,
    draws_data=None,
    tstrength_data=None,
    monday_map=None,
    *,
    data_dir=None,
    site_root=None,
):
    """Generate the full app page (app.html) + a lightweight launcher (index.html)."""

    source_data_dir = os.fspath(data_dir or RUNTIME_DATA_DIR)
    output_site_root = os.fspath(site_root or RUNTIME_SITE_ROOT)
    output_data_dir = os.path.join(output_site_root, "data")
    os.makedirs(output_data_dir, exist_ok=True)

    # Load points distribution
    points_dist_path = os.path.join(source_data_dir, "points_distribution.json")
    with open(points_dist_path, encoding="utf-8") as f:
        points_distribution = expand_points_distribution(json.load(f))

    def _history_identity_source(match_type):
        value = str(match_type or "").strip().upper()
        if value == "ITF":
            return "itf"
        if value in {"WTA", "GS", "OG", "UNITED CUP"}:
            return "wta"
        if "BJK" in value or "FED CUP" in value:
            return "bjkc"
        return value.casefold()

    def _normalize_history_player_name(raw_name, *, player_id="", source=""):
        name = fix_encoding_keep_accents(str(raw_name or "")).strip()
        if not name:
            return name
        if "/" in name:
            return " / ".join(
                _normalize_history_player_name(part, source=source) if part.strip() else part.strip()
                for part in name.split("/")
            )
        mapped = resolve_player_display_name(source, player_id=player_id, name=name)
        return format_player_name(mapped)

    def _normalize_history_row(row):
        if not isinstance(row, dict):
            return row
        normalized = dict(row)
        source = _history_identity_source(normalized.get("MATCH_TYPE"))
        for field in (
            "_winnerName",
            "_loserName",
            "winnerName",
            "loserName",
            "winner_name",
            "loser_name",
            "PLAYER",
            "OPPONENT",
            "RIVAL",
            "player",
            "opponent",
            "rival",
        ):
            value = normalized.get(field)
            if isinstance(value, str) and value.strip() and "/" not in value:
                side = "winner" if "winner" in field.lower() else "loser" if "loser" in field.lower() else ""
                player_id = normalized.get(f"_{side}Id") if side else ""
                normalized[field] = _normalize_history_player_name(value, player_id=player_id, source=source)
        return normalized

    cleaned_history = [_normalize_history_row(row) for row in (cleaned_history or [])]
    historical_draw_slots = _build_history_draw_slot_lookup(cleaned_history)
    compact_history = compress_history_data(cleaned_history or [])

    # The browser lazy-loads this classic-script bundle. Do not also persist an
    # identical JSON copy: it doubled the 14.5 MB payload without any consumer.
    history_bundle_path = os.path.join(output_data_dir, "history_data_bundle.js")
    try:
        _write_js_bundle_file(
            history_bundle_path, "__WTA_HISTORY_DATA__", compact_history, formatter=dumps_history_data
        )
    except (OSError, TypeError, ValueError) as e:
        raise DataValidationError(
            component="html_generator",
            operation="write history bundle",
            message="could not write the browser history bundle",
            context={"cause": str(e), "path": history_bundle_path},
        ) from e
    redundant_history_path = os.path.join(output_data_dir, "history_data.json")
    if os.path.exists(redundant_history_path):
        os.remove(redundant_history_path)

    player_aliases_bundle_path = os.path.join(output_data_dir, "player_aliases_wta_itf_bundle.js")
    try:
        # The ranking preflight may add newly seen WTA IDs in a subprocess, so
        # reload from disk instead of relying on this process's import-time copy.
        identities_path = os.path.join(source_data_dir, "player_aliases_wta_itf.json")
        player_mapping_bundle = None
        try:
            with open(identities_path, encoding="utf-8-sig") as identities_file:
                candidate = json.load(identities_file)
            if isinstance(candidate, list):
                player_mapping_bundle = candidate
        except (OSError, json.JSONDecodeError):
            pass
        if not player_mapping_bundle:
            player_mapping_bundle = PLAYER_IDENTITIES or load_player_mapping() or PLAYER_MAPPING
        if isinstance(player_mapping_bundle, list):
            player_mapping_bundle = [
                {
                    **identity,
                    "display_name": player_name_only(identity.get("display_name")),
                }
                if isinstance(identity, dict)
                else identity
                for identity in player_mapping_bundle
            ]
        _write_js_bundle_file(player_aliases_bundle_path, "__WTA_PLAYER_MAPPING__", player_mapping_bundle)
    except (OSError, TypeError, ValueError) as e:
        logger.warning(f"[warn] could not write player_aliases_wta_itf_bundle.js: {e}")

    # Load tournament draw sizes (combined WTA + ITF)
    draw_sizes_path = os.path.join(source_data_dir, "tournament_draw_sizes.json")
    try:
        with open(draw_sizes_path, encoding="utf-8") as f:
            all_draw_sizes = expand_tournament_draw_sizes(json.load(f))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"[warn] could not load tournament_draw_sizes.json: {e}")
        all_draw_sizes = []
    itf_draw_sizes = [t for t in all_draw_sizes if t.get("source") == "ITF"]
    wta_draw_sizes = [t for t in all_draw_sizes if t.get("source") == "WTA"]

    # Build tournament name â†’ surface lookup (used by Schedule and Entry Lists)
    _SCHED_SURFACE_COLORS = {
        "clay": "#f97316",
        "hard": "#3b82f6",
        "grass": "#22c55e",
        "carpet": "#a855f7",
    }
    _name_to_surface = {}

    def _normalize_surface_key(surface_value):
        s = (surface_value or "").strip().lower()
        if "clay" in s:
            return "clay"
        if "grass" in s:
            return "grass"
        if "carpet" in s:
            return "carpet"
        if "hard" in s:
            return "hard"
        return ""

    def _register_surface(name_value, surface_value):
        _n = (name_value or "").strip()
        _s = (surface_value or "").strip()
        if not _n or not _s:
            return
        _name_to_surface[_n.lower()] = _s
        _base_n = re.sub(r"\s+\d+$", "", _n).strip()
        if _base_n != _n:
            _name_to_surface.setdefault(_base_n.lower(), _s)

    for _cw in calendar_data or []:
        for _ck in ["gs", "wta_tour", "wta_125", "itf"]:
            for _ct in _cw.get("columns", {}).get(_ck, {}).values():
                for _t in _ct:
                    _register_surface(_t.get("name", ""), _t.get("surface", ""))

    for _week_tourneys in (tournament_groups or {}).values():
        for _t_info in _week_tourneys.values():
            _register_surface(_t_info.get("name", ""), _t_info.get("surface", ""))

    def _normalize_entry_country(code):
        upper = str(code or "").strip().upper()
        if not upper:
            return ""
        if upper in {"GREAT BRITAIN", "UNITED KINGDOM"}:
            return "GBR"
        return upper

    wta_country_map = {}
    wta_cache_path = os.path.join(source_data_dir, "wta_full_calendar_cache.json")
    try:
        with open(wta_cache_path, encoding="utf-8") as f:
            wta_cache = json.load(f)
        wta_cache = expand_wta_calendar_cache(wta_cache)
        wta_items = wta_cache.get("items", wta_cache) if isinstance(wta_cache, dict) else wta_cache
        if isinstance(wta_items, list):
            for _t in wta_items:
                try:
                    _id = str((_t.get("tournamentGroup") or {}).get("id") or "")
                except Exception:
                    _id = ""
                _country = _normalize_entry_country(_t.get("country", ""))
                if _id and _country:
                    wta_country_map[_id] = _country
    except Exception:
        wta_country_map = {}

    def _entry_country_from_key(t_key, t_info):
        country = _normalize_entry_country(
            (t_info or {}).get("country", "") or (t_info or {}).get("countryCode", "") or ""
        )
        if country:
            return country
        key = str(t_key or "")
        if "wimbledon/2026/player-list#qual" in key:
            return "GBR"
        if key.startswith("http"):
            m = re.search(r"/tournaments/(\d+)/", key)
            if m:
                return _normalize_entry_country(wta_country_map.get(m.group(1), ""))
        parts = key.split("-")
        if len(parts) >= 4 and parts[0] == "w" and parts[1] == "itf":
            return _normalize_entry_country(parts[2])
        return ""

    def _hide_entry_list_menu_key(t_key):
        key = str(t_key or "")
        return "wimbledon/2026/player-list#qual" in key

    def _sched_dot(entry):
        base = _schedule_tournament_base_name(entry)
        color = _SCHED_SURFACE_COLORS.get(_normalize_surface_key(_name_to_surface.get(base.lower(), "")), "")
        if not color:
            return ""
        return f'<span class="tournament-surface-dot" style="background:{color};"></span>'

    # Build tournament side menu HTML for Entry Lists
    entry_menu_html = ""
    first_key = None
    legend_html = '<div class="entry-menu-legend"><span class="entry-menu-gm-sample">99.9</span> Geometric Mean: Overall draw quality across all players.</div>'
    for week, tourneys in sorted(tournament_groups.items(), key=lambda item: _week_label_sort_key(item[0])):
        week_has_data = False
        for t_key in tourneys:
            if t_key in tournament_store and tournament_store[t_key]:
                week_has_data = True
                break
        if not week_has_data:
            continue

        entry_menu_html += f'<div class="entry-menu-week">{week.upper()}</div>'
        sorted_tourneys = sorted(tourneys.items(), key=lambda x: get_tournament_sort_order(x[1]["level"]))

        for t_key, t_info in sorted_tourneys:
            if _hide_entry_list_menu_key(t_key):
                continue
            if t_key in tournament_store and tournament_store[t_key]:
                t_source_name = t_info["name"]
                t_name = _display_tournament_name(t_source_name)
                t_level = escape(str(t_info.get("level", "") or ""))
                t_country = _entry_country_from_key(t_key, t_info)
                t_flag = country_flag_html(t_country, show_code=False) if t_country else ""
                t_flag_html = f'<span class="entry-menu-flag">{t_flag}</span>' if t_flag else ""
                t_dot = _sched_dot(t_source_name)
                active = " active" if first_key is None else ""
                if first_key is None:
                    first_key = t_key
                entry_menu_html += (
                    f'<div class="entry-menu-item{active}" data-key="{t_key}" data-country="{escape(t_country)}" '
                    f'data-level="{t_level}" onclick="selectEntryTournament(this)">'
                    f'<div class="entry-menu-top">{t_dot}<span class="entry-menu-level">{t_level}</span>'
                    f"{t_flag_html}"
                    f'<span class="entry-menu-gm"><span class="entry-menu-gm-value">-</span></span>'
                    f"</div>"
                    f'<div class="entry-menu-name">{escape(t_name)}</div></div>'
                )

    if entry_menu_html:
        entry_menu_html = legend_html + entry_menu_html

    # Build draws dropdown and data
    if draws_data is None:
        draws_data = {}
    draws_dropdown_html = ""
    first_draw_tkey = None
    draws_by_week = {}
    for t_key, tdata in draws_data.items():
        week = tdata.get("week", "")
        if week not in draws_by_week:
            draws_by_week[week] = []
        draws_by_week[week].append((t_key, tdata))
    for week in sorted(draws_by_week.keys(), key=_week_label_sort_key):
        items = draws_by_week[week]
        items.sort(key=lambda x: get_tournament_sort_order(x[1].get("level", "")))
        draws_dropdown_html += f'<optgroup label="{week.upper()}">'
        for t_key, tdata in items:
            t_name = tdata["name"]
            selected = ""
            if first_draw_tkey is None:
                first_draw_tkey = t_key
                selected = " selected"
            draws_dropdown_html += f'<option value="{t_key}"{selected}>{t_name}</option>'
        draws_dropdown_html += "</optgroup>"

    draws_tournament_info = {}
    for t_key, tdata in draws_data.items():
        draw_types = [dt for dt, di in tdata.get("draws", {}).items() if isinstance(di, dict) and di.get("players")]
        draws_tournament_info[t_key] = {"name": tdata["name"], "types": draw_types}

    draws_js_data = {}
    for t_key, tdata in draws_data.items():
        for dtype_code, draw_info in tdata.get("draws", {}).items():
            js_key = f"{t_key}|{dtype_code}"
            draws_js_data[js_key] = draw_info

    # Build table rows
    table_rows = ""
    week_keys = list(monday_map.values()) if monday_map else list(tournament_groups.keys())
    schedule_week_headers = "".join(
        f'<th class="col-week">{week_label}</th>'
        for week_label in (re.sub(r"^Week of\s+", "", week, flags=re.IGNORECASE) for week in week_keys)
    )

    def get_sort_key(player_name):
        p = next(item for item in players_data if item["Player"] == player_name)
        rank = p["Rank"]
        if isinstance(rank, int):
            return (0, rank)
        else:
            itf_rank = int(rank.replace("ITF ", "")) if isinstance(rank, str) and "ITF" in rank else 999999
            return (1, itf_rank)

    for p_name in sorted([p["Player"] for p in players_data], key=get_sort_key):
        p = next(item for item in players_data if item["Player"] == p_name)
        player_display = _player_display_name(p["Player"])
        row = f'<tr data-name="{player_display.lower()}">'
        row += f'<td class="sticky-col col-rank">{p["Rank"]}</td>'
        mobile_name = "<br>".join(player_display.split())
        row += f'<td class="sticky-col col-name"><span class="desktop-only">{player_display}</span><span class="mobile-only">{mobile_name}</span></td>'
        for week in week_keys:
            val = schedule_map.get(p["Key"], {}).get(week, "\u2014")
            val = val.replace("Sharm ElSheikh", "Sharm ES")
            val = re.sub(r"</div>\s*<div[^>]*>", "<br>", val, flags=re.IGNORECASE)
            val = re.sub(r"^<div[^>]*>\s*", "", val, flags=re.IGNORECASE)
            val = re.sub(r"\s*</div>$", "", val, flags=re.IGNORECASE)
            parts = [part for part in val.split("<br>") if part]
            rendered = "<br>".join(
                (
                    _sched_dot(e)
                    + (
                        f"<b>{e}</b>"
                        if "(Q)" not in re.sub(r"<[^>]+>", "", e) and re.sub(r"<[^>]+>", "", e).strip() != "\u2014"
                        else e
                    )
                )
                for e in parts
            )
            row += f'<td class="col-week">{rendered}</td>'
        table_rows += row + "</tr>"

    # Build history players list
    history_arg_players = set()
    for m in match_history_data:
        if m.get("winnerCountry") == "ARG" or m.get("winner_country") == "ARG":
            name = m.get("winnerName") or m.get("winner_name")
            if name:
                if "/" in name:
                    continue
                name_upper = name.strip().upper()
                display_name = NAME_LOOKUP.get(name_upper, name_upper)
                history_arg_players.add(format_player_name(display_name))
        if m.get("loserCountry") == "ARG" or m.get("loser_country") == "ARG":
            name = m.get("loserName") or m.get("loser_name")
            if name:
                if "/" in name:
                    continue
                name_upper = name.strip().upper()
                display_name = NAME_LOOKUP.get(name_upper, name_upper)
                history_arg_players.add(format_player_name(display_name))

    history_players_sorted = sorted(list(history_arg_players))

    # Build roadtogs player list: only players present in the WTA rankings, sorted by rank
    wta_rank_lookup = {
        format_player_name(p.get("Player", "")).upper(): int(p.get("Rank") or 9999) for p in (wta_rankings or [])
    }
    roadtogs_players_sorted = sorted(
        [name for name in history_arg_players if name.upper() in wta_rank_lookup],
        key=lambda name: wta_rank_lookup.get(name.upper(), 9999),
    )

    # Compute GS cutoff dates
    current_year = str(madrid_today().year)
    gs_list_raw = [
        ("Australian Open", "#0066B3", "AO"),
        ("Roland Garros", "#C8602A", "RG"),
        ("Wimbledon", "#3D7A3D", "WIM"),
        ("US Open", "#003087", "USO"),
    ]
    gs_data = []
    for gs_name, gs_color, gs_id in gs_list_raw:
        monday_date = None
        gs_year = None
        for week in calendar_data:
            for col_key in ["gs", "wta_tour", "wta_125", "itf"]:
                for tournaments in week.get("columns", {}).get(col_key, {}).values():
                    if any(t["name"] == gs_name for t in tournaments):
                        monday_date = week.get("monday_date")
                        break
                if monday_date:
                    break
            if monday_date:
                break
        if monday_date:
            gs_dt = datetime.strptime(monday_date, "%Y-%m-%d")
            gs_year = gs_dt.year
            md_cutoff = (gs_dt - timedelta(weeks=6)).strftime("%Y-%m-%d")
            q_cutoff = (gs_dt - timedelta(weeks=4)).strftime("%Y-%m-%d")
        else:
            gs_upper = gs_name.upper()
            year_rows = [
                r
                for r in cleaned_history
                if gs_upper in (r.get("TOURNAMENT") or "").upper() and (r.get("DATE") or "").startswith(current_year)
            ]
            # Use the first main-draw week, not qualifying, when we have to
            # project the next season from current-year history.
            ref_rows = [r for r in year_rows if (r.get("DRAW") or "").strip().upper() != "Q"] or year_rows
            if ref_rows:
                earliest = min(r["DATE"] for r in ref_rows if r.get("DATE"))
                gs_dt = datetime.strptime(earliest, "%Y-%m-%d")
                gs_dt -= timedelta(days=gs_dt.weekday())
                gs_dt += timedelta(weeks=52)
                gs_year = gs_dt.year
                md_cutoff = (gs_dt - timedelta(weeks=6)).strftime("%Y-%m-%d")
                q_cutoff = (gs_dt - timedelta(weeks=4)).strftime("%Y-%m-%d")
            else:
                md_cutoff = "N/A"
                q_cutoff = "N/A"
        q_cutoff, md_cutoff = _apply_special_gs_cutoff_overrides(gs_name, gs_year, q_cutoff, md_cutoff)
        gs_data.append(
            {
                "id": gs_id,
                "name": gs_name,
                "color": gs_color,
                "qCutoff": q_cutoff,
                "mdCutoff": md_cutoff,
                "year": gs_year,
            }
        )

    # Keep the current edition visible through its qualifying cutoff date.
    _roll_forward_passed_gs_cutoffs(gs_data, madrid_today())

    # Sort: soonest upcoming GS first (by qCutoff ascending); N/A last
    gs_data.sort(key=lambda g: g["qCutoff"] if g["qCutoff"] != "N/A" else "9999-99-99")

    def _format_cutoff_display(cutoff_value):
        if cutoff_value in ("N/A", ""):
            return cutoff_value or "N/A"
        try:
            cutoff_dt = datetime.strptime(cutoff_value, "%Y-%m-%d")
        except ValueError:
            return cutoff_value
        return f"{cutoff_dt.strftime('%b')} {cutoff_dt.day}"

    gs_tables_html = ""
    for gs in gs_data:
        gs_id = gs["id"]
        gs_name = gs["name"]
        gs_color = gs["color"]
        q_cutoff = gs["qCutoff"]
        md_cutoff = gs["mdCutoff"]
        gs_year = gs.get("year")
        table_title = f"{gs_name.upper()} {gs_year}" if gs_year else gs_name.upper()
        q_cutoff_display = _format_cutoff_display(q_cutoff)
        md_cutoff_display = _format_cutoff_display(md_cutoff)
        gs_tables_html += (
            f'<table class="gs-cutoff-table">'
            f"<colgroup>"
            f'<col class="gs-col-d"><col class="gs-col-cutoff"><col class="gs-col-acc"><col class="gs-col-est">'
            f"</colgroup>"
            f"<thead>"
            f'<tr><th colspan="4" style="background:{gs_color} !important;color:white !important;">{table_title}</th></tr>'
            f"<tr><th>D</th><th>Cut Off</th><th>Acc. Pts</th><th>Est. Need</th></tr>"
            f"</thead>"
            f"<tbody>"
            f'<tr><td>Q</td><td>{q_cutoff_display}</td><td id="gs-acc-q-{gs_id}">-</td><td id="gs-est-q-{gs_id}">-</td></tr>'
            f'<tr><td>MD</td><td>{md_cutoff_display}</td><td id="gs-acc-md-{gs_id}">-</td><td id="gs-est-md-{gs_id}">-</td></tr>'
            f"</tbody>"
            f"</table>"
        )

    # Build GS "last week to get points" boxes for the calendar GS row
    _GS_DISPLAY = {"Australian Open": "AO", "Roland Garros": "RG", "Wimbledon": "WMB", "US Open": "USO"}

    # Find frozen weeks: second calendar week of 2-week main-draw tournaments (GS & WTA1000)
    _frozen_mondays = set()
    _tourn_weeks = {}
    for _w in calendar_data:
        _wmon = _w["monday_date"]
        for _ck in ("gs", "wta_tour"):
            for _cl in _w.get("columns", {}).get(_ck, {}).values():
                for _t in _cl or []:
                    _tlv = (_t.get("level") or "").lower().replace(" ", "")
                    if _tlv in ("grandslam", "wta1000") and "qualifying" not in _t["name"].lower():
                        _tourn_weeks.setdefault(_t["name"], []).append(_wmon)
    for _mons in _tourn_weeks.values():
        if len(_mons) >= 2:
            _frozen_mondays.add(sorted(_mons)[1])

    # Map each monday_date -> list of (sort_key, label) cutoff boxes
    _gs_cutoff_boxes = _build_gs_cutoff_boxes(gs_data, _frozen_mondays)

    # Build calendar HTML
    def get_calendar_filter_key(level):
        lvl = (level or "").strip().lower().replace(" ", "")
        if lvl == "grandslam":
            return "gs"
        if "wta125" in lvl or lvl == "125" or lvl.endswith("wta125"):
            return "wta125"
        if lvl.startswith("wta"):
            if "125" in lvl:
                return "wta125"
            if any(x in lvl for x in ["250", "500", "1000", "wtafinals", "finals"]):
                return "wta_tour"
            return "wta_tour"
        if lvl in {"w15", "w35", "w50", "w75", "w100"}:
            return lvl
        if lvl.startswith("w") and lvl[1:].isdigit():
            return "itf_other"
        return "other"

    def get_calendar_surface_key(surface: str) -> str:
        s = (surface or "").lower()
        if "clay" in s:
            return "clay"
        if "carpet" in s:
            return "carpet"
        if "grass" in s:
            return "grass"
        return "hard"

    # Build 2025 GM lookup for WTA 125/250/500 only: (level, city_upper) -> [(startDate, gm), ...]
    _CAL_GM_LEVELS = {"WTA 125", "WTA 250", "WTA 500"}
    _ts_2025 = [
        e
        for e in (tstrength_data or [])
        if str(e.get("year", "")) == "2025"
        and e.get("draw", "MD") in ("MD", "M", "MAIN")
        and e.get("gm", 0) > 0
        and e.get("level", "") in _CAL_GM_LEVELS
    ]
    _gm_lookup = {}
    for _e in _ts_2025:
        _key = (_e.get("level", "").upper(), (_e.get("city") or "").upper())
        _gm_lookup.setdefault(_key, []).append((_e.get("startDate", ""), _e["gm"]))
    _gm_vals = [g for entries in _gm_lookup.values() for _, g in entries]
    _gm_min, _gm_max = (min(_gm_vals), max(_gm_vals)) if _gm_vals else (0, 1)

    def _gm_color(gm):
        if _gm_max <= _gm_min:
            return "#f1f5f9"
        t = max(0.0, min(1.0, (gm - _gm_min) / (_gm_max - _gm_min)))
        if t < 0.5:
            p = t * 2
            r, g, b = round(p * 255), round(200 + p * 20), 0
        else:
            p = (t - 0.5) * 2
            r, g, b = round(255 + p * (220 - 255)), round(220 * (1 - p)), 0
        return f"rgba({r},{g},{b},0.65)"

    def _day_of_year(date_str):
        try:
            return datetime.strptime(date_str[:10], "%Y-%m-%d").timetuple().tm_yday
        except Exception:
            return 0

    def _get_gm_badge(t_name, t_level, week_monday):
        if t_level not in _CAL_GM_LEVELS:
            return ""
        city_part = t_name[len(t_level) :].strip().split("#")[0].strip().upper()
        entries = _gm_lookup.get((t_level.upper(), city_part))
        if not entries:
            return ""
        if len(entries) == 1:
            gm = entries[0][1]
        else:
            cal_doy = _day_of_year(week_monday)
            gm = min(entries, key=lambda x: abs(_day_of_year(x[0]) - cal_doy))[1]
        color = _gm_color(gm)
        return f'<span class="cal-gm-badge" style="background:{color}">{gm}</span>'

    col_groups = [
        {"label": "GS", "keys": ["gs"], "single_row": True},
        {"label": "WTA", "keys": ["wta_tour", "wta_125"]},
        {"label": "ITF", "keys": ["itf"]},
    ]
    _wta_keys = {"wta_tour", "wta_125"}
    cont_labels = CONTINENT_LABELS
    mobile_cont_labels = MOBILE_CONTINENT_LABELS

    calendar_html = '<table class="calendar-table"><thead><tr>'
    calendar_html += '<th class="cal-cat-header"></th><th class="cal-cont-header"></th>'
    for week in calendar_data:
        calendar_html += f'<th class="cal-week-header">{week["week_label"]}</th>'
    calendar_html += "</tr></thead><tbody>"

    for group in col_groups:
        if group.get("single_row"):
            calendar_html += '<tr class="cal-group-first cal-group-last">'
            calendar_html += f'<td class="cal-cat-label" rowspan="1">{group["label"]}</td>'
            calendar_html += '<td class="cal-cont-label"></td>'
            for week in calendar_data:
                calendar_html += '<td class="cal-cell">'
                tournaments = []
                for ck in group["keys"]:
                    for cont_list in week.get("columns", {}).get(ck, {}).values():
                        tournaments.extend(cont_list or [])
                if tournaments:
                    tournaments.sort(key=lambda x: get_tournament_sort_order(x.get("level", "")))
                    for t in tournaments:
                        sc = get_surface_class(t.get("surface", ""))
                        fk = get_calendar_filter_key(t.get("level", ""))
                        sk = get_calendar_surface_key(t.get("surface", ""))
                        flag = country_flag_html(t.get("country", ""), show_code=False)
                        flag_prefix = f"{flag} " if flag else ""
                        display_name = escape(_display_calendar_tournament_name(t["name"]))
                        calendar_html += f'<span class="calendar-tournament {sc}" data-cal-filter="{fk}" data-cal-surface="{sk}">{flag_prefix}{display_name}</span>'
                _week_cutoff_boxes = _gs_cutoff_boxes.get(week["monday_date"], [])
                if not _week_cutoff_boxes:
                    _week_cutoff_boxes = _gs_cutoff_boxes.get(week["week_label"], [])
                for _, _box_label in sorted(_week_cutoff_boxes):
                    calendar_html += f'<span class="cal-cutoff-box">{_box_label}</span>'
                calendar_html += "</td>"
            calendar_html += "</tr>"
        else:
            for ci, cont in enumerate(CONTINENT_KEYS):
                row_cls = "cal-group-first" if ci == 0 else ("cal-group-last" if ci == len(CONTINENT_KEYS) - 1 else "")
                if row_cls:
                    calendar_html += f'<tr class="{row_cls}" data-cal-row-continent="{cont}">'
                else:
                    calendar_html += f'<tr data-cal-row-continent="{cont}">'
                if ci == 0:
                    calendar_html += f'<td class="cal-cat-label" rowspan="{len(CONTINENT_KEYS)}">{group["label"]}</td>'
                calendar_html += (
                    '<td class="cal-cont-label">'
                    f'<span class="cal-cont-label-desktop">{cont_labels[cont]}</span>'
                    f'<span class="cal-cont-label-mobile">{mobile_cont_labels[cont]}</span>'
                    "</td>"
                )
                for week in calendar_data:
                    calendar_html += '<td class="cal-cell">'
                    tournaments = []
                    for ck in group["keys"]:
                        tournaments.extend(week.get("columns", {}).get(ck, {}).get(cont, []) or [])
                    if tournaments:
                        tournaments.sort(key=lambda x: get_tournament_sort_order(x.get("level", "")))
                        for t in tournaments:
                            sc = get_surface_class(t.get("surface", ""))
                            fk = get_calendar_filter_key(t.get("level", ""))
                            sk = get_calendar_surface_key(t.get("surface", ""))
                            flag = country_flag_html(t.get("country", ""), show_code=False)
                            flag_prefix = f"{flag} " if flag else ""
                            is_wta = any(ck in _wta_keys for ck in group["keys"])
                            gm_badge = (
                                _get_gm_badge(t["name"], t.get("level", ""), week["monday_date"]) if is_wta else ""
                            )
                            display_name = escape(_display_calendar_tournament_name(t["name"]))
                            calendar_html += f'<span class="calendar-tournament {sc}" data-cal-filter="{fk}" data-cal-continent="{cont}" data-cal-surface="{sk}">{flag_prefix}{display_name}{gm_badge}</span>'
                    calendar_html += "</td>"
                calendar_html += "</tr>"

    calendar_html += "</tbody></table>"

    # Build cascading year/month/day selects for ranking week picker
    _all_csv = _load_wta_csv(source_data_dir)
    _all_dates = sorted(_all_csv.keys())
    _latest_date = _all_dates[-1] if _all_dates else ""

    # Build nested date index: year(str) -> month(int) -> [day(int), ...]
    _date_index = {}
    for _d in _all_dates:
        try:
            _dt = datetime.strptime(_d, "%Y-%m-%d")
            _y = str(_dt.year)
            if _y not in _date_index:
                _date_index[_y] = {}
            _m = _dt.month
            if _m not in _date_index[_y]:
                _date_index[_y][_m] = []
            _date_index[_y][_m].append(_dt.day)
        except ValueError:
            pass  # malformed date string in rankings_dates_index â€” skip silently, common for older data

    _latest_year = _latest_month_int = _latest_day_int = 0
    _latest_year_str = ""
    if _latest_date:
        try:
            _ldt = datetime.strptime(_latest_date, "%Y-%m-%d")
            _latest_year_str = str(_ldt.year)
            _latest_year = _ldt.year
            _latest_month_int = _ldt.month
            _latest_day_int = _ldt.day
        except ValueError as exc:
            raise DataValidationError(
                component="html_generator",
                operation="parse latest rankings date",
                message=f"invalid latest rankings date: {_latest_date}",
                context={"value": _latest_date},
            ) from exc

    _all_years = sorted(_date_index.keys(), reverse=True)
    rankings_year_options = ""
    for _y in _all_years:
        _sel = " selected" if _y == _latest_year_str else ""
        rankings_year_options += f'<option value="{_y}"{_sel}>{_y}</option>'

    rankings_latest_month = _latest_month_int
    rankings_latest_day = _latest_day_int

    try:
        _write_wta_ranking_bundles(_all_csv, output_data_dir, _all_dates)
    except (OSError, TypeError, ValueError) as e:
        raise DataValidationError(
            component="html_generator",
            operation="write ranking bundles",
            message="could not write lazy WTA ranking bundles",
            context={"cause": str(e), "data_dir": str(output_data_dir)},
        ) from e

    # Build rankings table rows (initial: latest week)
    rankings_rows = ""
    for p in wta_rankings or []:
        dob = p.get("DOB", "")
        if dob and "T" in dob:
            dob = dob.split("T")[0]
        name = format_player_name(_ranking_display_name(p))
        country_code = p.get("Country") or ""
        rankings_rows += f'<tr data-country="{country_code.upper()}"><td>{p.get("Rank", "")}</td><td style="text-align:left;font-weight:bold;">{country_flag_html(country_code, show_code=False)} {name}</td><td>{p.get("Points", "")}</td><td>{dob}</td></tr>'

    default_national_columns = ["N", "Player", "Date", "Event", "Partner", "Opponent", "Score"]
    source_national_columns = list(national_team_data[0].keys()) if national_team_data else default_national_columns
    national_columns = [col for col in source_national_columns if col not in ("Tie", "Result", "Round")]

    header_label_map = {"N": "#"}
    header_style_map = {
        "N": ' style="width:30px"',
        "Player": ' style="width:140px"',
        "Date": ' style="width:90px"',
        "Event": ' style="width:190px"',
        "Partner": ' style="width:160px"',
        "Opponent": "",
        "Score": ' style="width:110px"',
    }
    national_header_html = "".join(
        f"<th>{escape(header_label_map.get(col, col.upper()))}</th>" for col in national_columns
    )

    national_rows = ""
    for row in national_team_data or []:
        national_rows += "<tr>"
        opponent_country = _bjkc_tie_country_code(row.get("Tie", ""))
        opponent_flag = country_flag_html(opponent_country, show_code=False)
        for col in national_columns:
            value = str(row.get(col, "") or "")
            if col == "Event":
                value = "G1 Am" if value == "G1 Americas" else value
            cell_style = ""
            cell_class = ""

            if col == "Player":
                value = format_player_name(value)
                cell_style = ' style="font-weight:bold;"'
            if col == "Score":
                result = str(row.get("Result", "") or "").upper()
                if result == "W":
                    cell_class = ' class="score-win"'
                elif result == "L":
                    cell_class = ' class="score-loss"'
                display_value = f'<span class="score-badge">{escape(value)}</span>'
            elif col == "Player":
                desktop_value = escape(value)
                mobile_value = escape(value)
                display_value = (
                    f'<span class="desktop-only">{desktop_value}</span><span class="mobile-only">{mobile_value}</span>'
                )
            elif col == "Partner":
                desktop_value = escape(value)
                mobile_value = "<br>".join(escape(value).split())
                display_value = (
                    f'<span class="desktop-only">{desktop_value}</span><span class="mobile-only">{mobile_value}</span>'
                )
            elif col == "Opponent":
                cell_class = ' class="national-opponent-cell"'
                opponent_flag_cell = f'<span class="national-opponent-flag">{opponent_flag}</span>'
                desktop_value = (
                    '<span class="national-opponent-content">'
                    f'{opponent_flag_cell}<span class="national-opponent-name">{escape(value)}</span>'
                    "</span>"
                )
                parts = value.split("/")
                mobile_players = [
                    f'<span class="national-opponent-player">{escape(part.strip())}</span>' for part in parts
                ]
                opponent_mobile_name = "".join(mobile_players)
                mobile_value = (
                    '<span class="national-opponent-content">'
                    f'{opponent_flag_cell}<span class="national-opponent-name">{opponent_mobile_name}</span>'
                    "</span>"
                )
                display_value = (
                    f'<span class="desktop-only">{desktop_value}</span><span class="mobile-only">{mobile_value}</span>'
                )
            else:
                display_value = escape(value)
            national_rows += f"<td{cell_class}{cell_style}>{display_value}</td>"
        national_rows += "</tr>"

    default_captains_columns = ["N", "Captain", "Year"]
    captains_columns = list(captains_data[0].keys()) if captains_data else default_captains_columns

    captains_header_html = "".join(
        f"<th{header_style_map.get(col, '')}>{escape(header_label_map.get(col, col.upper()))}</th>"
        for col in captains_columns
    )

    captains_rows = ""
    for row in captains_data or []:
        captains_rows += "<tr>"
        for col in captains_columns:
            value = str(row.get(col, "") or "")
            cell_style = ""

            if col == "Captain":
                value = format_player_name(value)
                cell_style = ' style="font-weight:bold;"'

            captains_rows += f"<td{cell_style}>{escape(value)}</td>"
        captains_rows += "</tr>"

    # Build BJK Cup Series HTML
    _bjkc_iso_to_name = {
        "ARG": "Argentina",
        "AUS": "Australia",
        "AUT": "Austria",
        "BAH": "Bahamas",
        "BEL": "Belgium",
        "BOL": "Bolivia",
        "BRA": "Brazil",
        "BUL": "Bulgaria",
        "CAN": "Canada",
        "CHI": "Chile",
        "CHN": "China",
        "COL": "Colombia",
        "CRO": "Croatia",
        "CUB": "Cuba",
        "CZE": "Czechia",
        "DEN": "Denmark",
        "DOM": "Dominican Republic",
        "ECU": "Ecuador",
        "ESP": "Spain",
        "EST": "Estonia",
        "FIN": "Finland",
        "FRA": "France",
        "FRG": "West Germany",
        "GBR": "Great Britain",
        "GER": "Germany",
        "GRE": "Greece",
        "GUA": "Guatemala",
        "HUN": "Hungary",
        "INA": "Indonesia",
        "JPN": "Japan",
        "KAZ": "Kazakhstan",
        "KOR": "South Korea",
        "MEX": "Mexico",
        "NED": "Netherlands",
        "NOR": "Norway",
        "NZL": "New Zealand",
        "PAR": "Paraguay",
        "PER": "Peru",
        "PHI": "Philippines",
        "POL": "Poland",
        "PUR": "Puerto Rico",
        "ROU": "Romania",
        "RUS": "Russia",
        "SEN": "Senegal",
        "SLO": "Slovenia",
        "SUI": "Switzerland",
        "SVK": "Slovakia",
        "SWE": "Sweden",
        "TCH": "Czechoslovakia",
        "TPE": "Chinese Taipei",
        "UKR": "Ukraine",
        "URU": "Uruguay",
        "USA": "USA",
        "VEN": "Venezuela",
        "YUG": "Yugoslavia",
    }

    def _bjkc_flip_score(s):
        if not s:
            return ""
        out = []
        for part in s.split():
            tb = ""
            if "(" in part:
                tb = part[part.index("(") :]
                part = part[: part.index("(")]
            ab = part.split("-")
            out.append(f"{ab[1]}-{ab[0]}{tb}" if len(ab) == 2 else part + tb)
        return " ".join(out)

    bjkc_series_html = ""
    _all_bjkc_players = set()
    try:
        import pandas as _pd

        _bjkc_path = os.path.join(source_data_dir, "bjkc_matches_arg.csv")
        _bjkc_df = _pd.read_csv(_bjkc_path)
        _manual_path = os.path.join(source_data_dir, "manually_added_matches.csv")
        _manual_df = _pd.read_csv(_manual_path)
        if "matchType" in _manual_df.columns:
            _manual_bjkc = _manual_df[_manual_df["matchType"].astype(str).str.strip().str.lower() == "fed/bjk cup"]
            if not _manual_bjkc.empty:
                _bjkc_df = _pd.concat([_bjkc_df, _manual_bjkc], ignore_index=True)

        # Build alias reverse map: raw_name_upper -> display_name
        _alias_reverse = {}
        for _display_name, _raw_list in (PLAYER_MAPPING or {}).items():
            if not isinstance(_display_name, str):
                continue
            _display_clean = _display_name.strip()
            if not _display_clean:
                continue
            _alias_reverse[_display_clean.upper()] = _display_clean
            if isinstance(_raw_list, list):
                for _raw in _raw_list:
                    if isinstance(_raw, str) and _raw.strip():
                        _alias_reverse[_raw.strip().upper()] = _display_clean

        def _apply_alias(name_str):
            """Apply alias lookup to a player name or 'P1 / P2' doubles string."""
            parts = name_str.split(" / ")
            return " / ".join(_alias_reverse.get(p.strip().upper(), p.strip()) for p in parts)

        def _fmt_name(name_str):
            """Format player name; doubles get a desktop slash + mobile line-break."""
            if " / " in name_str:
                p = name_str.split(" / ", 1)
                return escape(p[0]) + '<span class="doubles-slash"> / </span><br class="doubles-br">' + escape(p[1])
            return escape(name_str)

        # Sort ties by earliest date (newest first), then by best round (best first).
        _tie_round_order = {
            "Round Robin": 1,
            "Last 128": 2,
            "Last 64": 3,
            "Last 32": 4,
            "Last 16": 5,
            "Quarter Finals": 6,
            "Semi Finals": 7,
            "Final": 8,
        }
        _tie_draw_order = {
            "Main Draw": 1,
            "Consolation Round": 2,
        }

        def _round_rank(v):
            return _tie_round_order.get(str(v or "").strip(), 0)

        def _draw_rank(v):
            return _tie_draw_order.get(str(v or "").strip(), 0)

        _tie_meta = _bjkc_df.groupby("tournamentId", as_index=False).agg(
            tieDate=("date", "min"),
            roundRank=("roundName", lambda s: max((_round_rank(x) for x in s), default=0)),
            drawRank=("draw", lambda s: max((_draw_rank(x) for x in s), default=0)),
        )
        _tie_meta["tieDateDt"] = _pd.to_datetime(_tie_meta["tieDate"], errors="coerce")
        _tie_meta = _tie_meta.sort_values(
            by=["tieDateDt", "drawRank", "roundRank", "tournamentId"], ascending=[False, False, False, True]
        )

        for _tie_index, _tid in enumerate(_tie_meta["tournamentId"].tolist()):
            _grp = _bjkc_df[_bjkc_df["tournamentId"] == _tid].copy()
            _first = _grp.iloc[0]

            # Determine opponent ISO -> name
            _opp_iso = None
            for _, _mr in _grp.iterrows():
                if str(_mr.get("winnerCountry", "")) != "ARG":
                    _opp_iso = str(_mr["winnerCountry"])
                    break
                if str(_mr.get("loserCountry", "")) != "ARG":
                    _opp_iso = str(_mr["loserCountry"])
                    break
            _opp_name = _bjkc_iso_to_name.get(_opp_iso or "", _opp_iso or "?")

            _t_name = str(_first.get("tournamentName", ""))
            _opp_flag = country_flag_html(_opp_iso or "", show_code=False)
            _header_text = _t_name if " vs " in _t_name.lower() else f"{_t_name} vs {_opp_name}"

            # Overall tie result: only count played matches
            _arg_wins = 0
            _arg_losses = 0
            for _, _mr in _grp.iterrows():
                _r = str(_mr.get("result", "") or "")
                if not _r or _r.lower() == "nan":
                    continue
                if str(_mr.get("winnerCountry", "")) == "ARG":
                    _arg_wins += 1
                else:
                    _arg_losses += 1
            _tie_won = _arg_wins > _arg_losses
            _badge_bg = "#dcfce7" if _tie_won else "#fee2e2"
            _badge_color = "#166534" if _tie_won else "#991b1b"
            _tie_res_label = f"{_arg_wins}-{_arg_losses}"

            _tie_date = str(_grp["date"].dropna().min()) if not _grp["date"].dropna().empty else ""

            _rows_html = ""

            def _sort_key(row):
                mo = row.get("matchOrder")
                try:
                    if mo is None or (isinstance(mo, float) and math.isnan(mo)):
                        raise ValueError
                    return int(mo)
                except (ValueError, TypeError):
                    is_d = " / " in str(row.get("winnerName", "")) or " / " in str(row.get("loserName", ""))
                    return 999 if is_d else 998

            _grp_sorted = _grp.copy()
            _grp_sorted["_sk"] = _grp_sorted.apply(_sort_key, axis=1)
            _grp_sorted = _grp_sorted.sort_values("_sk").drop(columns=["_sk"])
            for _, _mr in _grp_sorted.iterrows():
                _result_raw = str(_mr.get("result", "") or "")
                _has_result = bool(_result_raw) and _result_raw.lower() != "nan"
                _arg_won = str(_mr.get("winnerCountry", "")) == "ARG"

                _arg_player = _apply_alias(str(_mr["winnerName"] if _arg_won else _mr["loserName"]))
                _opp_player = str(_mr["loserName"] if _arg_won else _mr["winnerName"])

                if not _has_result:
                    _score_display = '<em class="text-muted">Not Played</em>'
                    _res_label = "-"
                    _res_class = "text-muted"
                    _res_extra_style = "font-weight:bold;"
                else:
                    _score = _result_raw if _arg_won else _bjkc_flip_score(_result_raw)
                    _status = str(_mr.get("resultStatusDesc", "") or "")
                    _score_display = escape(_score)
                    if _status and _status.lower() != "nan":
                        _score_display += (
                            f' <span class="text-muted" style="font-size:0.85em;">({escape(_status)})</span>'
                        )
                    _res_label = "W" if _arg_won else "L"
                    _res_class = "res-win" if _arg_won else "res-loss"
                    _res_extra_style = ""

                _is_doubles = " / " in _arg_player
                _data_type = "D" if _is_doubles else "S"
                if _is_doubles:
                    _player_parts = [p.strip() for p in _arg_player.split(" / ", 1)]
                    _data_player = "|".join(_player_parts)
                    for _pp in _player_parts:
                        if _pp:
                            _all_bjkc_players.add(_pp)
                else:
                    _data_player = _arg_player.strip()
                    if _data_player:
                        _all_bjkc_players.add(_data_player)
                _data_result = ("W" if _arg_won else "L") if _has_result else ""

                _rows_html += f"""<tr data-player="{escape(_data_player)}" data-type="{_data_type}" data-result="{_data_result}">
                        <td style="font-weight:bold;white-space:nowrap;">{_fmt_name(_arg_player)}</td>
                        <td class="{_res_class}" style="{_res_extra_style}text-align:center;">{_res_label}</td>
                        <td style="white-space:nowrap;">{_score_display}</td>
                        <td style="white-space:nowrap;">{_fmt_name(_opp_player)}</td>
                    </tr>"""

            _open_attr = " open" if _tie_index == 0 else ""
            bjkc_series_html += f"""<details class="bjkc-series-block"{_open_attr}>
                <summary class="bjkc-series-header">
                    <span class="bjkc-header-date">{escape(_tie_date)}</span>
                    <span class="bjkc-header-title">{escape(_header_text)} {_opp_flag}</span>
                    <span class="bjkc-header-side"><span class="bjkc-tie-score" style="background:{_badge_bg};color:{_badge_color};">{_tie_res_label}</span></span>
                    <span class="bjkc-header-arrow" aria-hidden="true"></span>
                </summary>
                <div class="content-card">
                    <div class="table-wrapper">
                        <table class="bjkc-series-table">
                            <thead><tr>
                                <th>ARGENTINA</th><th>RES.</th><th>SCORE</th><th>OPPONENT</th>
                            </tr></thead>
                            <tbody>{_rows_html}</tbody>
                        </table>
                    </div>
                </div>
            </details>"""
    except Exception as _e:
        report_run_issue("html_generator", "render BJK Cup section", _e, severity="partial")
        bjkc_series_html = f'<p style="color:red;">Error loading BJK Cup data: {escape(str(_e))}</p>'

    # Build T-Strength data as JSON for JS rendering
    if tstrength_data is None:
        tstrength_data = []
    tstrength_json_list = [t for t in tstrength_data if t.get("gm", 0) > 0]

    # Generate the full HTML template
    frontend_context = {
        "SCHEDULE_WEEK_HEADERS": schedule_week_headers,
        "TABLE_ROWS": table_rows,
        "ENTRY_MENU_HTML": entry_menu_html,
        "RANKINGS_YEAR_OPTIONS": rankings_year_options,
        "RANKINGS_ROWS": rankings_rows,
        "HISTORY_PLAYER_OPTIONS": "".join(f'<option value="{name}">{name}</option>' for name in history_players_sorted),
        "NATIONAL_HEADER_HTML": national_header_html,
        "NATIONAL_ROWS": national_rows,
        "CAPTAINS_HEADER_HTML": captains_header_html,
        "CAPTAINS_ROWS": captains_rows,
        "BJKC_SERIES_HTML": bjkc_series_html,
        "CALENDAR_HTML": calendar_html,
        "ROADTOGS_PLAYER_OPTIONS": "".join(
            f'<option value="{escape(name, quote=True)}">{escape(name)}</option>' for name in roadtogs_players_sorted
        ),
        "GS_TABLES_HTML": gs_tables_html,
        "GS_THRESHOLD_Q": GS_THRESHOLD_Q,
        "GS_THRESHOLD_MD": GS_THRESHOLD_MD,
        "DRAWS_DROPDOWN_HTML": draws_dropdown_html,
    }
    frontend_data = {
        "schemaVersion": 1,
        "tstrength": tstrength_json_list,
        "tournaments": tournament_store,
        "pointsDistribution": points_distribution,
        "itfDrawSizes": itf_draw_sizes,
        "wtaDrawSizes": wta_draw_sizes,
        "historicalDrawSlots": historical_draw_slots,
        "gsCutoffs": gs_data,
        "draws": draws_js_data,
        "drawsTournamentInfo": draws_tournament_info,
        "bjkcPlayers": sorted(_all_bjkc_players),
        "rankingsLatestDate": _latest_date,
        "rankingsDatesIndex": _date_index,
        "rankingsLatestYear": _latest_year_str,
        "rankingsLatestMonth": rankings_latest_month,
        "rankingsLatestDay": rankings_latest_day,
        "gsThresholdQ": GS_THRESHOLD_Q,
        "gsThresholdMd": GS_THRESHOLD_MD,
    }
    html_template = _render_frontend_source("templates/app.html", frontend_context)
    html_template = _apply_content_security_policy(html_template).rstrip() + "\n"
    # Always write generated site files beside this module.  main.py may be
    # launched with a different working directory (for example from an IDE),
    # but build_deploy_site.py and the local file URL use the project root.
    site_root = output_site_root
    _write_frontend_assets(site_root, frontend_data)
    write_text_if_changed(os.path.join(site_root, "app.html"), html_template, encoding="utf-8-sig")

    launcher_template = _read_frontend_source("templates/index.html")
    launcher_template = _apply_content_security_policy(launcher_template).rstrip() + "\n"
    write_text_if_changed(os.path.join(site_root, "index.html"), launcher_template, encoding="utf-8-sig")

    # SPA fallback: GitHub Pages serves this for any unknown path.
    not_found_template = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  __WTARG_CSP_META__
  <title>WTARG</title>
  <link rel="apple-touch-icon" sizes="180x180" href="/assets/apple-touch-icon.png?v=20260709">
  <link rel="manifest" href="/site.webmanifest">
  <link rel="icon" type="image/png" sizes="192x192" href="/assets/favicon-192.png">
  <link rel="icon" type="image/png" sizes="32x32" href="/assets/favicon-32.png">
  <link rel="icon" type="image/png" sizes="16x16" href="/assets/favicon-16.png">
  <script>
    (function() {
      location.replace('/');
    })();
  </script>
</head>
<body></body>
</html>
"""
    not_found_template = _apply_content_security_policy(not_found_template).rstrip() + "\n"
    write_text_if_changed(os.path.join(site_root, "404.html"), not_found_template, encoding="utf-8-sig")

    route_template = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
  __WTARG_CSP_META__
  <title>WTARG</title>
  <link rel="apple-touch-icon" sizes="180x180" href="../assets/apple-touch-icon.png?v=20260709">
  <link rel="manifest" href="../site.webmanifest">
  <link rel="icon" type="image/png" sizes="192x192" href="../assets/favicon-192.png">
  <link rel="icon" type="image/png" sizes="32x32" href="../assets/favicon-32.png">
  <link rel="icon" type="image/png" sizes="16x16" href="../assets/favicon-16.png">
  <script>
    (function() {{
      location.replace('../app.html' + (location.search || '') + '#{tab}');
    }})();
  </script>
</head>
<body></body>
</html>
"""

    route_tabs = [
        "upcoming",
        "entrylists",
        "draws",
        "calendar",
        "rankings",
        "roadtogs",
        "history",
        "fedbcup",
        "tstrength",
    ]
    for tab in route_tabs:
        folder = os.path.join(site_root, tab)
        os.makedirs(folder, exist_ok=True)
        route_html = _apply_content_security_policy(route_template.format(tab=tab)).rstrip() + "\n"
        write_text_if_changed(os.path.join(folder, "index.html"), route_html, encoding="utf-8")
