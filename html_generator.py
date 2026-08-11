import json
import base64
import hashlib
import math
import re
from html import escape, unescape
import os
from datetime import datetime, timedelta
from config import (
    PLAYER_IDENTITIES,
    PLAYER_MAPPING,
    CONTINENT_KEYS,
    CONTINENT_LABELS,
    MOBILE_CONTINENT_LABELS,
    NAME_LOOKUP,
    load_player_mapping,
    player_name_only,
    resolve_player_display_name,
    resolve_player_presentation_name,
)
from runtime_paths import DATA_DIR as RUNTIME_DATA_DIR, SITE_ROOT as RUNTIME_SITE_ROOT
from time_utils import madrid_today
from pipeline_errors import DataValidationError
from run_state import report_run_issue
from runtime_logging import get_logger
from utils import (
    dumps_readable, format_player_name, get_tournament_sort_order,
    get_surface_class, fix_encoding_keep_accents,
    compress_history_data, dumps_history_data,
    dumps_wta_rankings_bundle,
    expand_wta_calendar_cache,
    expand_points_distribution, expand_tournament_draw_sizes,
    write_text_if_changed,
)
from wta import _load_wta_csv


logger = get_logger("html-generator")

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
    display_name = re.sub(r"\s+\d+\s*$", "", _display_tournament_name(name))
    for full_name, short_name in _CALENDAR_TOURNAMENT_NAME_REPLACEMENTS.items():
        display_name = display_name.replace(full_name, short_name)
    return display_name.strip()


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
        serialized
        .replace("<", "\\u003c")
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
        gs["qCutoff"] = (
            datetime.strptime(q_cutoff, "%Y-%m-%d") + timedelta(weeks=52)
        ).strftime("%Y-%m-%d")
        gs["mdCutoff"] = (
            datetime.strptime(gs["mdCutoff"], "%Y-%m-%d") + timedelta(weeks=52)
        ).strftime("%Y-%m-%d")
        if isinstance(gs.get("year"), int):
            gs["year"] += 1


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


IOC_TO_ISO2 = {
    'ALB':'al','ALG':'dz','AND':'ad','ANG':'ao','ARG':'ar','ARM':'am','ASA':'as','AUS':'au','AUT':'at','AZE':'az',
    'BAH':'bs','BAR':'bb','BDI':'bi','BEL':'be','BEN':'bj','BIH':'ba','BLR':'by','BOL':'bo',
    'BOT':'bw','BRA':'br','BUL':'bg','CAL':'nc','CAM':'kh','CAN':'ca','CHI':'cl','CHL':'cl','CHN':'cn',
    'CIV':'ci','CMR':'cm','COD':'cd','COL':'co','CRC':'cr','CRO':'hr','CUB':'cu','CUW':'cw','CYP':'cy','CZE':'cz','CZS':'cz',
    'DEN':'dk','DOM':'do','DZA':'dz','ECU':'ec','EGY':'eg','ESA':'sv','ESP':'es','EST':'ee',
    'FIJ':'fj','FIN':'fi','FRA':'fr','FRG':'de',
    'GAB':'ga','GBR':'gb','GEO':'ge','GER':'de','GHA':'gh','GLP':'gp','GRB':'gb','GRE':'gr','GRC':'gr','GUA':'gt',
    'HAI':'ht','HKG':'hk','HRV':'hr','HUN':'hu',
    'INA':'id','IND':'in','IRI':'ir','IRL':'ie','IRN':'ir','ISR':'il','ITA':'it',
    'JAM':'jm','JOR':'jo','JPN':'jp',
    'KAZ':'kz','KEN':'ke','KGZ':'kg','KHM':'kh','KOR':'kr','KOS':'xk','KSA':'sa',
    'LAO':'la','LAT':'lv','LIE':'li','LTU':'lt','LUX':'lu',
    'MAD':'mg','MAR':'ma','MAS':'my','MDA':'md','MEX':'mx','MKD':'mk','MLI':'ml','MLT':'mt','MNE':'me','MON':'mc',
    'MRI':'mu','MOZ':'mz','NAM':'na','NCA':'ni','NCD':'nc','NED':'nl','NEP':'np','NET':'nl','NGA':'ng','NGR':'ng','NOR':'no','NZL':'nz',
    'OMA':'om','OMN':'om','PAK':'pk','PAN':'pa','PAR':'py','PER':'pe','PHI':'ph','PLE':'ps','PNG':'pg',
    'POL':'pl','POR':'pt','PUR':'pr','QAT':'qa',
    'ROC':'ru','ROM':'ro','ROU':'ro','RSA':'za','RUS':'ru',
    'SAF':'za','SAM':'ws','SEN':'sn','SGP':'sg','SIN':'sg','SLO':'si','SMR':'sm',
    'SRB':'rs','SRI':'lk','SUI':'ch','SVK':'sk','SWE':'se','SYR':'sy',
    'TCH':'cz',
    'THA':'th','TKM':'tm','TOG':'tg','TPE':'tw','TRI':'tt','TTO':'tt','TUN':'tn','TUR':'tr',
    'UAE':'ae','UKR':'ua','URU':'uy','USA':'us','UZB':'uz','VEN':'ve','VIE':'vn',
    'XKX':'xk','ZAM':'zm','ZIM':'zw',
}

# ``national_team_order.csv`` stores the opposing nation as a display name in
# its legacy Tie column. Keep that source value available for the flag even
# though the column itself is no longer rendered in Player Debuts.
_BJKC_TIE_COUNTRY_CODES = {
    name.casefold(): code for name, code in {
        'Australia': 'AUS', 'Austria': 'AUT', 'Belgium': 'BEL', 'Bolivia': 'BOL',
        'Bulgaria': 'BUL', 'Chile': 'CHI', 'Colombia': 'COL', 'Croatia': 'CRO',
        'Cuba': 'CUB', 'Denmark': 'DEN', 'Dominican Republic': 'DOM', 'Ecuador': 'ECU',
        'Estonia': 'EST', 'Finland': 'FIN', 'France': 'FRA', 'Germany F.R.': 'FRG',
        'Greece': 'GRE', 'Guatemala': 'GUA', 'Hungary': 'HUN', 'Japan': 'JPN',
        'Korea, Rep.': 'KOR', 'Netherlands': 'NED', 'New Zealand': 'NZL',
        'Paraguay': 'PAR', 'Peru': 'PER', 'Philippines': 'PHI', 'Poland': 'POL',
        'Russia': 'RUS', 'Slovenia': 'SLO', 'Sweden': 'SWE', 'Switzerland': 'SUI',
        'Ukraine': 'UKR', 'USA': 'USA', 'Venezuela': 'VEN',
    }.items()
}

# Dissolved countries with local SVG flags
LOCAL_FLAGS = {'AHO', 'YUG', 'SCG', 'CIS', 'URS'}

FLAG_STYLE = 'vertical-align:middle;margin-right:3px;width:16px;height:11px;outline:0.3px solid #000'

# Road-to-GS thresholds â€” single source of truth shared between JS logic and the
# user-facing legend text so the displayed numbers can't drift from the calculation.
GS_THRESHOLD_Q = 330
GS_THRESHOLD_MD = 780

def country_flag_html(code, show_code=True):
    if not code or code == '-':
        return code or ''
    upper = code.upper()
    if upper == "GRC":
        upper = "GRE"
        code = "GRE"
    if upper in LOCAL_FLAGS:
        img = f'<img src="data/flags/{upper.lower()}.svg" alt="{code}" title="{code}" style="{FLAG_STYLE}">'
        return f'{img}{code}' if show_code else img
    iso = IOC_TO_ISO2.get(upper)
    if not iso:
        return code
    img = f'<img src="https://purecatamphetamine.github.io/country-flag-icons/3x2/{iso.upper()}.svg" alt="{code}" title="{code}" style="{FLAG_STYLE}">'
    return f'{img}{code}' if show_code else img


def _bjkc_tie_country_code(value):
    raw = str(value or '').strip()
    if not raw:
        return ''
    upper = raw.upper()
    if upper in IOC_TO_ISO2:
        return upper
    return _BJKC_TIE_COUNTRY_CODES.get(raw.casefold(), '')


def _player_display_name(raw_name):
    name = fix_encoding_keep_accents(str(raw_name or '')).strip()
    if not name:
        return ""
    mapped = NAME_LOOKUP.get(name.upper(), name)
    return format_player_name(mapped)


def _write_js_bundle_file(bundle_path, global_name, data, formatter=None):
    """Write a simple classic-script bundle that assigns data to a window global."""
    os.makedirs(os.path.dirname(bundle_path), exist_ok=True)
    dump_func = formatter or dumps_readable
    payload = dump_func(data, ensure_ascii=False)
    write_text_if_changed(bundle_path, f'window.{global_name} = {payload};\n', encoding='utf-8')


_LEGACY_WTA_RANKING_BUNDLES = {
    'wta_rankings_20_29_bundle.js',
    'wta_rankings_10_19_bundle.js',
    'wta_rankings_00_09_bundle.js',
    'wta_rankings_83_99_bundle.js',
}
_YEAR_WTA_RANKING_BUNDLE_RE = re.compile(r'^wta_rankings_\d{4}_bundle\.js$')
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
            "d": str(player.get("DOB", "") or "").replace('\r', '').strip(),
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

    expected_files = {'wta_rankings_latest_bundle.js'}
    for year, year_dates in sorted(dates_by_year.items()):
        filename = f'wta_rankings_{year}_bundle.js'
        payload = {
            date_str: _ranking_bundle_rows(rankings_by_date.get(date_str) or [])
            for date_str in year_dates
        }
        _write_js_bundle_file(
            os.path.join(data_dir, filename),
            f'__WTA_RANKINGS_{year}__',
            payload,
            formatter=dumps_wta_rankings_bundle,
        )
        expected_files.add(filename)

    latest_date = all_dates[-1] if all_dates else ''
    latest_payload = (
        {latest_date: _ranking_bundle_rows(rankings_by_date.get(latest_date) or [])}
        if latest_date
        else {}
    )
    _write_js_bundle_file(
        os.path.join(data_dir, 'wta_rankings_latest_bundle.js'),
        '__WTA_RANKINGS_LATEST__',
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
        try:
            norm_id = str(int(norm_id))
        except ValueError:
            pass
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

def generate_html(tournament_groups, tournament_store, players_data, schedule_map,
                  cleaned_history, calendar_data, match_history_data, wta_rankings=None,
                  national_team_data=None, captains_data=None, draws_data=None,
                  tstrength_data=None, monday_map=None):
    """Generate the full app page (app.html) + a lightweight launcher (index.html)."""

    # Load points distribution
    points_dist_path = os.path.join(RUNTIME_DATA_DIR, 'points_distribution.json')
    with open(points_dist_path, 'r', encoding='utf-8') as f:
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
        name = fix_encoding_keep_accents(str(raw_name or '')).strip()
        if not name:
            return name
        if '/' in name:
            return ' / '.join(
                _normalize_history_player_name(part, source=source) if part.strip() else part.strip()
                for part in name.split('/')
            )
        mapped = resolve_player_display_name(
            source, player_id=player_id, name=name
        )
        return format_player_name(mapped)

    def _normalize_history_row(row):
        if not isinstance(row, dict):
            return row
        normalized = dict(row)
        source = _history_identity_source(normalized.get('MATCH_TYPE'))
        for field in (
            '_winnerName', '_loserName', 'winnerName', 'loserName',
            'winner_name', 'loser_name', 'PLAYER', 'OPPONENT', 'RIVAL',
            'player', 'opponent', 'rival',
        ):
            value = normalized.get(field)
            if isinstance(value, str) and value.strip() and '/' not in value:
                side = 'winner' if 'winner' in field.lower() else 'loser' if 'loser' in field.lower() else ''
                player_id = normalized.get(f'_{side}Id') if side else ''
                normalized[field] = _normalize_history_player_name(
                    value, player_id=player_id, source=source
                )
        return normalized

    cleaned_history = [_normalize_history_row(row) for row in (cleaned_history or [])]
    historical_draw_slots = _build_history_draw_slot_lookup(cleaned_history)
    compact_history = compress_history_data(cleaned_history or [])

    # The browser lazy-loads this classic-script bundle. Do not also persist an
    # identical JSON copy: it doubled the 14.5 MB payload without any consumer.
    history_bundle_path = os.path.join(RUNTIME_DATA_DIR, 'history_data_bundle.js')
    try:
        _write_js_bundle_file(history_bundle_path, '__WTA_HISTORY_DATA__', compact_history, formatter=dumps_history_data)
    except (OSError, TypeError, ValueError) as e:
        raise DataValidationError(
            component="html_generator",
            operation="write history bundle",
            message="could not write the browser history bundle",
            context={"cause": str(e), "path": history_bundle_path},
        ) from e
    redundant_history_path = os.path.join(RUNTIME_DATA_DIR, 'history_data.json')
    if os.path.exists(redundant_history_path):
        os.remove(redundant_history_path)

    player_aliases_bundle_path = os.path.join(RUNTIME_DATA_DIR, 'player_aliases_wta_itf_bundle.js')
    try:
        # The ranking preflight may add newly seen WTA IDs in a subprocess, so
        # reload from disk instead of relying on this process's import-time copy.
        identities_path = os.path.join(RUNTIME_DATA_DIR, 'player_aliases_wta_itf.json')
        player_mapping_bundle = None
        try:
            with open(identities_path, 'r', encoding='utf-8-sig') as identities_file:
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
                    "display_name": (
                        identity.get("presentation_name")
                        or player_name_only(identity.get("display_name"))
                    ),
                }
                if isinstance(identity, dict)
                else identity
                for identity in player_mapping_bundle
            ]
        _write_js_bundle_file(player_aliases_bundle_path, '__WTA_PLAYER_MAPPING__', player_mapping_bundle)
    except (OSError, TypeError, ValueError) as e:
        logger.warning(f"[warn] could not write player_aliases_wta_itf_bundle.js: {e}")

    # Load tournament draw sizes (combined WTA + ITF)
    draw_sizes_path = os.path.join(RUNTIME_DATA_DIR, 'tournament_draw_sizes.json')
    try:
        with open(draw_sizes_path, 'r', encoding='utf-8') as f:
            all_draw_sizes = expand_tournament_draw_sizes(json.load(f))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"[warn] could not load tournament_draw_sizes.json: {e}")
        all_draw_sizes = []
    itf_draw_sizes = [t for t in all_draw_sizes if t.get('source') == 'ITF']
    wta_draw_sizes = [t for t in all_draw_sizes if t.get('source') == 'WTA']

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
        _base_n = re.sub(r'\s+\d+$', '', _n).strip()
        if _base_n != _n:
            _name_to_surface.setdefault(_base_n.lower(), _s)

    for _cw in (calendar_data or []):
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
    wta_cache_path = os.path.join(RUNTIME_DATA_DIR, "wta_full_calendar_cache.json")
    try:
        with open(wta_cache_path, "r", encoding="utf-8") as f:
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
            (t_info or {}).get("country", "")
            or (t_info or {}).get("countryCode", "")
            or ""
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
    for week, tourneys in sorted(
        tournament_groups.items(), key=lambda item: _week_label_sort_key(item[0])
    ):
        week_has_data = False
        for t_key in tourneys.keys():
            if t_key in tournament_store and tournament_store[t_key]:
                week_has_data = True
                break
        if not week_has_data: continue

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
                if first_key is None: first_key = t_key
                entry_menu_html += (
                    f'<div class="entry-menu-item{active}" data-key="{t_key}" data-country="{escape(t_country)}" '
                    f'data-level="{t_level}" onclick="selectEntryTournament(this)">'
                    f'<div class="entry-menu-top">{t_dot}<span class="entry-menu-level">{t_level}</span>'
                    f'{t_flag_html}'
                    f'<span class="entry-menu-gm"><span class="entry-menu-gm-value">-</span></span>'
                    f'</div>'
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
        draws_dropdown_html += '</optgroup>'

    draws_tournament_info = {}
    for t_key, tdata in draws_data.items():
        draw_types = [
            dt for dt, di in tdata.get("draws", {}).items()
            if isinstance(di, dict) and di.get("players")
        ]
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
        for week_label in (
            re.sub(r"^Week of\s+", "", week, flags=re.IGNORECASE)
            for week in week_keys
        )
    )

    def get_sort_key(player_name):
        p = next(item for item in players_data if item["Player"] == player_name)
        rank = p['Rank']
        if isinstance(rank, int): return (0, rank)
        else:
            itf_rank = int(rank.replace("ITF ", "")) if isinstance(rank, str) and "ITF" in rank else 999999
            return (1, itf_rank)

    for p_name in sorted([p['Player'] for p in players_data], key=get_sort_key):
        p = next(item for item in players_data if item["Player"] == p_name)
        player_display = _player_display_name(p['Player'])
        row = f'<tr data-name="{player_display.lower()}">'
        row += f'<td class="sticky-col col-rank">{p["Rank"]}</td>'
        mobile_name = "<br>".join(player_display.split())
        row += f'<td class="sticky-col col-name"><span class="desktop-only">{player_display}</span><span class="mobile-only">{mobile_name}</span></td>'
        for week in week_keys:
            val = schedule_map.get(p['Key'], {}).get(week, "\u2014")
            val = val.replace("Sharm ElSheikh", "Sharm ES")
            val = re.sub(r'</div>\s*<div[^>]*>', '<br>', val, flags=re.IGNORECASE)
            val = re.sub(r'^<div[^>]*>\s*', '', val, flags=re.IGNORECASE)
            val = re.sub(r'\s*</div>$', '', val, flags=re.IGNORECASE)
            parts = [part for part in val.split("<br>") if part]
            rendered = "<br>".join(
                (_sched_dot(e) + (f"<b>{e}</b>" if "(Q)" not in re.sub(r'<[^>]+>', '', e) and re.sub(r'<[^>]+>', '', e).strip() != "\u2014" else e))
                for e in parts
            )
            row += f'<td class="col-week">{rendered}</td>'
        table_rows += row + "</tr>"

    # Build history players list
    history_arg_players = set()
    for m in match_history_data:
        if m.get('winnerCountry') == 'ARG' or m.get('winner_country') == 'ARG':
            name = m.get('winnerName') or m.get('winner_name')
            if name:
                if '/' in name:
                    continue
                name_upper = name.strip().upper()
                display_name = NAME_LOOKUP.get(name_upper, name_upper)
                history_arg_players.add(format_player_name(display_name))
        if m.get('loserCountry') == 'ARG' or m.get('loser_country') == 'ARG':
            name = m.get('loserName') or m.get('loser_name')
            if name:
                if '/' in name:
                    continue
                name_upper = name.strip().upper()
                display_name = NAME_LOOKUP.get(name_upper, name_upper)
                history_arg_players.add(format_player_name(display_name))

    history_players_sorted = sorted(list(history_arg_players))

    # Build roadtogs player list: only players present in the WTA rankings, sorted by rank
    wta_rank_lookup = {format_player_name(p.get("Player", "")).upper(): int(p.get("Rank") or 9999) for p in (wta_rankings or [])}
    roadtogs_players_sorted = sorted(
        [name for name in history_arg_players if name.upper() in wta_rank_lookup],
        key=lambda name: wta_rank_lookup.get(name.upper(), 9999)
    )

    # Compute GS cutoff dates
    current_year = str(madrid_today().year)
    gs_list_raw = [
        ("Australian Open", "#0066B3", "AO"),
        ("Roland Garros",   "#C8602A", "RG"),
        ("Wimbledon",        "#3D7A3D", "WIM"),
        ("US Open",          "#003087", "USO"),
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
            q_cutoff  = (gs_dt - timedelta(weeks=4)).strftime("%Y-%m-%d")
        else:
            gs_upper = gs_name.upper()
            year_rows = [
                r for r in cleaned_history
                if gs_upper in (r.get("TOURNAMENT") or "").upper()
                and (r.get("DATE") or "").startswith(current_year)
            ]
            # Use the first main-draw week, not qualifying, when we have to
            # project the next season from current-year history.
            ref_rows = [
                r for r in year_rows
                if (r.get("DRAW") or "").strip().upper() != "Q"
            ] or year_rows
            if ref_rows:
                earliest = min(r["DATE"] for r in ref_rows if r.get("DATE"))
                gs_dt = datetime.strptime(earliest, "%Y-%m-%d")
                gs_dt -= timedelta(days=gs_dt.weekday())
                gs_dt += timedelta(weeks=52)
                gs_year = gs_dt.year
                md_cutoff = (gs_dt - timedelta(weeks=6)).strftime("%Y-%m-%d")
                q_cutoff  = (gs_dt - timedelta(weeks=4)).strftime("%Y-%m-%d")
            else:
                md_cutoff = "N/A"
                q_cutoff  = "N/A"
        gs_data.append({"id": gs_id, "name": gs_name, "color": gs_color,
                         "qCutoff": q_cutoff, "mdCutoff": md_cutoff, "year": gs_year})

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
        gs_id    = gs["id"]
        gs_name  = gs["name"]
        gs_color = gs["color"]
        q_cutoff = gs["qCutoff"]
        md_cutoff = gs["mdCutoff"]
        gs_year = gs.get("year")
        table_title = f"{gs_name.upper()} {gs_year}" if gs_year else gs_name.upper()
        q_cutoff_display = _format_cutoff_display(q_cutoff)
        md_cutoff_display = _format_cutoff_display(md_cutoff)
        gs_tables_html += (
            f'<table class="gs-cutoff-table">'
            f'<colgroup>'
            f'<col class="gs-col-d"><col class="gs-col-cutoff"><col class="gs-col-acc"><col class="gs-col-est">'
            f'</colgroup>'
            f'<thead>'
            f'<tr><th colspan="4" style="background:{gs_color} !important;color:white !important;">{table_title}</th></tr>'
            f'<tr><th>D</th><th>Cut Off</th><th>Acc. Pts</th><th>Est. Need</th></tr>'
            f'</thead>'
            f'<tbody>'
            f'<tr><td>Q</td><td>{q_cutoff_display}</td><td id="gs-acc-q-{gs_id}">-</td><td id="gs-est-q-{gs_id}">-</td></tr>'
            f'<tr><td>MD</td><td>{md_cutoff_display}</td><td id="gs-acc-md-{gs_id}">-</td><td id="gs-est-md-{gs_id}">-</td></tr>'
            f'</tbody>'
            f'</table>'
        )

    gs_cutoffs_json = _script_safe_json(gs_data)

    # Build GS "last week to get points" boxes for the calendar GS row
    _GS_DISPLAY = {"Australian Open": "AO", "Roland Garros": "RG", "Wimbledon": "WMB", "US Open": "USO"}

    # Find frozen weeks: second calendar week of 2-week main-draw tournaments (GS & WTA1000)
    _frozen_mondays = set()
    _tourn_weeks = {}
    for _w in calendar_data:
        _wmon = _w["monday_date"]
        for _ck in ("gs", "wta_tour"):
            for _cl in _w.get("columns", {}).get(_ck, {}).values():
                for _t in (_cl or []):
                    _tlv = (_t.get("level") or "").lower().replace(" ", "")
                    if _tlv in ("grandslam", "wta1000") and "qualifying" not in _t["name"].lower():
                        _tourn_weeks.setdefault(_t["name"], []).append(_wmon)
    for _mons in _tourn_weeks.values():
        if len(_mons) >= 2:
            _frozen_mondays.add(sorted(_mons)[1])

    # Map each monday_date -> list of (sort_key, label) cutoff boxes
    _gs_cutoff_boxes = {}
    for _gi, _gs in enumerate(gs_data):
        if _gs["mdCutoff"] in ("N/A", ""):
            continue
        _gslabel = _GS_DISPLAY.get(_gs["name"], _gs["name"])
        _start_dt = datetime.strptime(_gs["mdCutoff"], "%Y-%m-%d") + timedelta(weeks=6)
        for _di, (_draw_type, _wks) in enumerate([("MD", 6), ("Q", 4)]):
            _cutoff_dt = _start_dt - timedelta(weeks=_wks)
            _cutoff_str = _cutoff_dt.strftime("%Y-%m-%d")
            # Last week to add points = 1 week before the cutoff (plus 1 more if cutoff week is frozen)
            _last_dt = (_cutoff_dt - timedelta(weeks=2)) if _cutoff_str in _frozen_mondays else (_cutoff_dt - timedelta(weeks=1))
            _gs_cutoff_boxes.setdefault(_last_dt.strftime("%Y-%m-%d"), []).append(
                (_gi * 3 + _di * 2, f"Last week for {_gslabel} {_draw_type}{' in W50+' if _draw_type == 'Q' else ''}")
            )
            # W15/W35 have a 1-week processing delay, so their last week is 1 earlier â€” Q only
            if _draw_type == "Q":
                _w1535_dt = _last_dt - timedelta(weeks=1)
                _gs_cutoff_boxes.setdefault(_w1535_dt.strftime("%Y-%m-%d"), []).append(
                    (_gi * 3 + _di * 2 + 1, f"Last week for {_gslabel} Q in W15/W35")
                )

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
        e for e in (tstrength_data or [])
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
            return ''
        city_part = t_name[len(t_level):].strip().split("#")[0].strip().upper()
        entries = _gm_lookup.get((t_level.upper(), city_part))
        if not entries:
            return ''
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
    calendar_html += '</tr></thead><tbody>'

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
                        flag_prefix = f'{flag} ' if flag else ''
                        display_name = escape(_display_calendar_tournament_name(t["name"]))
                        calendar_html += f'<span class="calendar-tournament {sc}" data-cal-filter="{fk}" data-cal-surface="{sk}">{flag_prefix}{display_name}</span>'
                for _, _box_label in sorted(_gs_cutoff_boxes.get(week["monday_date"], [])):
                    calendar_html += f'<span class="cal-cutoff-box">{_box_label}</span>'
                calendar_html += '</td>'
            calendar_html += '</tr>'
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
                    '</td>'
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
                            flag_prefix = f'{flag} ' if flag else ''
                            is_wta = any(ck in _wta_keys for ck in group["keys"])
                            gm_badge = _get_gm_badge(t["name"], t.get("level", ""), week["monday_date"]) if is_wta else ''
                            display_name = escape(_display_calendar_tournament_name(t["name"]))
                            calendar_html += f'<span class="calendar-tournament {sc}" data-cal-filter="{fk}" data-cal-continent="{cont}" data-cal-surface="{sk}">{flag_prefix}{display_name}{gm_badge}</span>'
                    calendar_html += '</td>'
                calendar_html += '</tr>'

    calendar_html += '</tbody></table>'

    # Build cascading year/month/day selects for ranking week picker
    _all_csv = _load_wta_csv(RUNTIME_DATA_DIR)
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
        _sel = ' selected' if _y == _latest_year_str else ''
        rankings_year_options += f'<option value="{_y}"{_sel}>{_y}</option>'

    rankings_dates_index_json = _script_safe_json(_date_index)
    rankings_latest_date_json = _script_safe_json(_latest_date)
    rankings_latest_year_str = _latest_year_str
    rankings_latest_month = _latest_month_int
    rankings_latest_day = _latest_day_int

    try:
        _write_wta_ranking_bundles(_all_csv, RUNTIME_DATA_DIR, _all_dates)
    except (OSError, TypeError, ValueError) as e:
        raise DataValidationError(
            component="html_generator",
            operation="write ranking bundles",
            message="could not write lazy WTA ranking bundles",
            context={"cause": str(e), "data_dir": str(RUNTIME_DATA_DIR)},
        ) from e

    # Build rankings table rows (initial: latest week)
    rankings_rows = ""
    for p in (wta_rankings or []):
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
        "Score": ' style="width:110px"'
    }
    national_header_html = "".join(
        f'<th>{escape(header_label_map.get(col, col.upper()))}</th>'
        for col in national_columns
    )

    national_rows = ""
    for row in (national_team_data or []):
        national_rows += '<tr>'
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
                display_value = f'<span class="desktop-only">{desktop_value}</span><span class="mobile-only">{mobile_value}</span>'
            elif col == "Partner":
                desktop_value = escape(value)
                mobile_value = "<br>".join(escape(value).split())
                display_value = f'<span class="desktop-only">{desktop_value}</span><span class="mobile-only">{mobile_value}</span>'
            elif col == "Opponent":
                cell_class = ' class="national-opponent-cell"'
                opponent_flag_cell = f'<span class="national-opponent-flag">{opponent_flag}</span>'
                desktop_value = (
                    '<span class="national-opponent-content">'
                    f'{opponent_flag_cell}<span class="national-opponent-name">{escape(value)}</span>'
                    '</span>'
                )
                parts = value.split("/")
                mobile_players = [
                    f'<span class="national-opponent-player">{escape(part.strip())}</span>'
                    for part in parts
                ]
                opponent_mobile_name = "".join(mobile_players)
                mobile_value = (
                    '<span class="national-opponent-content">'
                    f'{opponent_flag_cell}<span class="national-opponent-name">{opponent_mobile_name}</span>'
                    '</span>'
                )
                display_value = f'<span class="desktop-only">{desktop_value}</span><span class="mobile-only">{mobile_value}</span>'
            else:
                display_value = escape(value)
            national_rows += f'<td{cell_class}{cell_style}>{display_value}</td>'
        national_rows += '</tr>'

    default_captains_columns = ["N", "Captain", "Year"]
    captains_columns = list(captains_data[0].keys()) if captains_data else default_captains_columns

    captains_header_html = "".join(
        f'<th{header_style_map.get(col, "")}>{escape(header_label_map.get(col, col.upper()))}</th>'
        for col in captains_columns
    )

    captains_rows = ""
    for row in (captains_data or []):
        captains_rows += '<tr>'
        for col in captains_columns:
            value = str(row.get(col, "") or "")
            cell_style = ""

            if col == "Captain":
                value = format_player_name(value)
                cell_style = ' style="font-weight:bold;"'

            captains_rows += f'<td{cell_style}>{escape(value)}</td>'
        captains_rows += '</tr>'

    # Build BJK Cup Series HTML
    _bjkc_iso_to_name = {
        'ARG': 'Argentina', 'AUS': 'Australia', 'AUT': 'Austria',
        'BAH': 'Bahamas', 'BEL': 'Belgium', 'BOL': 'Bolivia',
        'BRA': 'Brazil', 'BUL': 'Bulgaria', 'CAN': 'Canada',
        'CHI': 'Chile', 'CHN': 'China', 'COL': 'Colombia',
        'CRO': 'Croatia', 'CUB': 'Cuba', 'CZE': 'Czechia',
        'DEN': 'Denmark', 'DOM': 'Dominican Republic', 'ECU': 'Ecuador',
        'ESP': 'Spain', 'EST': 'Estonia', 'FIN': 'Finland',
        'FRA': 'France', 'FRG': 'West Germany', 'GBR': 'Great Britain',
        'GER': 'Germany', 'GRE': 'Greece', 'GUA': 'Guatemala',
        'HUN': 'Hungary', 'INA': 'Indonesia', 'JPN': 'Japan',
        'KAZ': 'Kazakhstan', 'KOR': 'South Korea', 'MEX': 'Mexico',
        'NED': 'Netherlands', 'NOR': 'Norway', 'NZL': 'New Zealand',
        'PAR': 'Paraguay', 'PER': 'Peru', 'PHI': 'Philippines',
        'POL': 'Poland', 'PUR': 'Puerto Rico', 'ROU': 'Romania',
        'RUS': 'Russia', 'SEN': 'Senegal', 'SLO': 'Slovenia',
        'SUI': 'Switzerland', 'SVK': 'Slovakia', 'SWE': 'Sweden',
        'TCH': 'Czechoslovakia', 'TPE': 'Chinese Taipei', 'UKR': 'Ukraine',
        'URU': 'Uruguay', 'USA': 'USA', 'VEN': 'Venezuela',
        'YUG': 'Yugoslavia',
    }

    def _bjkc_flip_score(s):
        if not s: return ""
        out = []
        for part in s.split():
            tb = ""
            if "(" in part:
                tb = part[part.index("("):]
                part = part[:part.index("(")]
            ab = part.split("-")
            out.append(f"{ab[1]}-{ab[0]}{tb}" if len(ab) == 2 else part + tb)
        return " ".join(out)

    bjkc_series_html = ""
    try:
        import pandas as _pd
        _bjkc_path = os.path.join(RUNTIME_DATA_DIR, 'bjkc_matches_arg.csv')
        _bjkc_df = _pd.read_csv(_bjkc_path)
        _manual_path = os.path.join(RUNTIME_DATA_DIR, 'manually_added_matches.csv')
        _manual_df = _pd.read_csv(_manual_path)
        if 'matchType' in _manual_df.columns:
            _manual_bjkc = _manual_df[_manual_df['matchType'].astype(str).str.strip().str.lower() == 'fed/bjk cup']
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
            parts = name_str.split(' / ')
            return ' / '.join(_alias_reverse.get(p.strip().upper(), p.strip()) for p in parts)

        def _fmt_name(name_str):
            """Format player name; doubles get a desktop slash + mobile line-break."""
            if ' / ' in name_str:
                p = name_str.split(' / ', 1)
                return escape(p[0]) + '<span class="doubles-slash"> / </span><br class="doubles-br">' + escape(p[1])
            return escape(name_str)

        # Sort ties by earliest date (newest first), then by best round (best first).
        _tie_round_order = {
            'Round Robin': 1,
            'Last 128': 2,
            'Last 64': 3,
            'Last 32': 4,
            'Last 16': 5,
            'Quarter Finals': 6,
            'Semi Finals': 7,
            'Final': 8,
        }
        _tie_draw_order = {
            'Main Draw': 1,
            'Consolation Round': 2,
        }

        def _round_rank(v):
            return _tie_round_order.get(str(v or '').strip(), 0)
        def _draw_rank(v):
            return _tie_draw_order.get(str(v or '').strip(), 0)

        _tie_meta = _bjkc_df.groupby('tournamentId', as_index=False).agg(
            tieDate=('date', 'min'),
            roundRank=('roundName', lambda s: max((_round_rank(x) for x in s), default=0)),
            drawRank=('draw', lambda s: max((_draw_rank(x) for x in s), default=0))
        )
        _tie_meta['tieDateDt'] = _pd.to_datetime(_tie_meta['tieDate'], errors='coerce')
        _tie_meta = _tie_meta.sort_values(
            by=['tieDateDt', 'drawRank', 'roundRank', 'tournamentId'],
            ascending=[False, False, False, True]
        )

        _all_bjkc_players = set()

        for _tie_index, _tid in enumerate(_tie_meta['tournamentId'].tolist()):
            _grp = _bjkc_df[_bjkc_df['tournamentId'] == _tid].copy()
            _first = _grp.iloc[0]

            # Determine opponent ISO -> name
            _opp_iso = None
            for _, _mr in _grp.iterrows():
                if str(_mr.get('winnerCountry', '')) != 'ARG':
                    _opp_iso = str(_mr['winnerCountry'])
                    break
                if str(_mr.get('loserCountry', '')) != 'ARG':
                    _opp_iso = str(_mr['loserCountry'])
                    break
            _opp_name = _bjkc_iso_to_name.get(_opp_iso or '', _opp_iso or '?')

            _t_name = str(_first.get('tournamentName', ''))
            _opp_flag = country_flag_html(_opp_iso or '', show_code=False)
            _header_text = _t_name if ' vs ' in _t_name.lower() else f"{_t_name} vs {_opp_name}"

            # Overall tie result: only count played matches
            _arg_wins = 0
            _arg_losses = 0
            for _, _mr in _grp.iterrows():
                _r = str(_mr.get('result', '') or '')
                if not _r or _r.lower() == 'nan':
                    continue
                if str(_mr.get('winnerCountry', '')) == 'ARG':
                    _arg_wins += 1
                else:
                    _arg_losses += 1
            _tie_won = _arg_wins > _arg_losses
            _badge_bg = '#dcfce7' if _tie_won else '#fee2e2'
            _badge_color = '#166534' if _tie_won else '#991b1b'
            _tie_res_label = f"{_arg_wins}-{_arg_losses}"

            _tie_date = str(_grp['date'].dropna().min()) if not _grp['date'].dropna().empty else ''

            _rows_html = ""
            def _sort_key(row):
                mo = row.get('matchOrder')
                try:
                    if mo is None or (isinstance(mo, float) and math.isnan(mo)): raise ValueError
                    return int(mo)
                except (ValueError, TypeError):
                    is_d = ' / ' in str(row.get('winnerName', '')) or ' / ' in str(row.get('loserName', ''))
                    return 999 if is_d else 998
            _grp_sorted = _grp.copy()
            _grp_sorted['_sk'] = _grp_sorted.apply(_sort_key, axis=1)
            _grp_sorted = _grp_sorted.sort_values('_sk').drop(columns=['_sk'])
            for _, _mr in _grp_sorted.iterrows():
                _result_raw = str(_mr.get('result', '') or '')
                _has_result = bool(_result_raw) and _result_raw.lower() != 'nan'
                _arg_won = str(_mr.get('winnerCountry', '')) == 'ARG'

                _arg_player = _apply_alias(str(_mr['winnerName'] if _arg_won else _mr['loserName']))
                _opp_player = str(_mr['loserName'] if _arg_won else _mr['winnerName'])

                if not _has_result:
                    _score_display = '<em class="text-muted">Not Played</em>'
                    _res_label = '-'
                    _res_class = 'text-muted'
                    _res_extra_style = 'font-weight:bold;'
                else:
                    _score = _result_raw if _arg_won else _bjkc_flip_score(_result_raw)
                    _status = str(_mr.get('resultStatusDesc', '') or '')
                    _score_display = escape(_score)
                    if _status and _status.lower() != 'nan':
                        _score_display += f' <span class="text-muted" style="font-size:0.85em;">({escape(_status)})</span>'
                    _res_label = 'W' if _arg_won else 'L'
                    _res_class = 'res-win' if _arg_won else 'res-loss'
                    _res_extra_style = ''

                _is_doubles = ' / ' in _arg_player
                _data_type = 'D' if _is_doubles else 'S'
                if _is_doubles:
                    _player_parts = [p.strip() for p in _arg_player.split(' / ', 1)]
                    _data_player = '|'.join(_player_parts)
                    for _pp in _player_parts:
                        if _pp:
                            _all_bjkc_players.add(_pp)
                else:
                    _data_player = _arg_player.strip()
                    if _data_player:
                        _all_bjkc_players.add(_data_player)
                _data_result = ('W' if _arg_won else 'L') if _has_result else ''

                _rows_html += f"""<tr data-player="{escape(_data_player)}" data-type="{_data_type}" data-result="{_data_result}">
                        <td style="font-weight:bold;white-space:nowrap;">{_fmt_name(_arg_player)}</td>
                        <td class="{_res_class}" style="{_res_extra_style}text-align:center;">{_res_label}</td>
                        <td style="white-space:nowrap;">{_score_display}</td>
                        <td style="white-space:nowrap;">{_fmt_name(_opp_player)}</td>
                    </tr>"""

            _open_attr = ' open' if _tie_index == 0 else ''
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
        bjkc_players_json = _script_safe_json(sorted(_all_bjkc_players))
    except Exception as _e:
        report_run_issue(
            "html_generator", "render BJK Cup section", _e, severity="partial"
        )
        bjkc_series_html = f'<p style="color:red;">Error loading BJK Cup data: {escape(str(_e))}</p>'
        bjkc_players_json = '[]'

    # Build T-Strength data as JSON for JS rendering
    if tstrength_data is None:
        tstrength_data = []
    tstrength_json_list = [t for t in tstrength_data if t.get("gm", 0) > 0]
    tstrength_json_str = _script_safe_json(tstrength_json_list)

    # Generate the full HTML template
    router_script = """
        <script>
        (function() {
            const VALID_TABS = new Set([
                'home',
                'upcoming',
                'entrylists',
                'draws',
                'calendar',
                'rankings',
                'roadtogs',
                'history',
                'fedbcup',
                'tstrength',
            ]);

            function normalizePath(path) {
                const raw = (path || '/').replace(/\\/+/g, '/');
                let out = raw.startsWith('/') ? raw : ('/' + raw);
                out = out.replace(/\\/+/g, '/');
                return out;
            }

            function getBasePath() {
                const baseEl = document.querySelector('base');
                if (!baseEl) return '/';
                try {
                    const u = new URL(baseEl.href, location.origin);
                    let p = normalizePath(u.pathname || '/');
                    if (!p.endsWith('/')) p += '/';
                    return p;
                } catch (e) {
                    return '/';
                }
            }

            const BASE_PATH = getBasePath();
            window.SITE_BASE_PATH = BASE_PATH;
            const baseEl = document.querySelector('base');
            if (baseEl) {
                // Freeze to an absolute path so relative fetch/src paths keep working
                // after history.replaceState() changes the visible pathname.
                baseEl.setAttribute('href', BASE_PATH);
            }

            function tabToPath(tabName) {
                if (!VALID_TABS.has(tabName)) return '';
                if (tabName === 'home') return BASE_PATH;
                return normalizePath(BASE_PATH + tabName + '/');
            }

            const originalSwitchTab = window.switchTab;
            if (typeof originalSwitchTab !== 'function') return;

            window.switchTab = function(tabName) {
                let tab = (tabName || '').trim().toLowerCase();
                if (!VALID_TABS.has(tab)) {
                    originalSwitchTab(tabName);
                    return;
                }
                try {
                    let desired = tabToPath(tab);
                    const current = normalizePath(location.pathname || '/');
                    const suffix = (current === desired || location.hash) ? (location.search || '') : '';
                    if (desired && current !== desired) {
                        history.replaceState(null, '', desired + suffix);
                    } else if (location.hash) {
                        history.replaceState(null, '', (desired || current) + suffix);
                    }
                } catch (e) {}
                originalSwitchTab(tab);
            };

            function tabFromHash() {
                const raw = (location.hash || '').replace(/^#/, '').trim().toLowerCase();
                if (!raw) return '';
                return VALID_TABS.has(raw) ? raw : '';
            }

            function tabFromPath() {
                const base = BASE_PATH.toLowerCase();
                const fullPath = normalizePath((location.pathname || '/').toLowerCase());
                let rel = fullPath.startsWith(base) ? fullPath.slice(base.length) : fullPath.replace(/^\\/+/, '');
                rel = rel.replace(/index\\.html$/, '');
                rel = rel.replace(/^\\/+|\\/+$/g, '');
                if (!rel) return 'home';
                return VALID_TABS.has(rel) ? rel : '';
            }

            function applyRoute() {
                const tab = tabFromHash() || tabFromPath();
                if (tab && tab !== 'home') {
                    window.switchTab(tab);
                    return;
                }
                try {
                    const saved = localStorage.getItem('lastTab');
                    if (saved && VALID_TABS.has(saved) && saved !== 'home') {
                        window.switchTab(saved);
                        return;
                    }
                } catch(e) {}
                if (tab) window.switchTab(tab);
            }

            document.addEventListener('DOMContentLoaded', applyRoute);
            window.addEventListener('hashchange', applyRoute);
            window.addEventListener('popstate', applyRoute);
        })();
        </script>
    """

    launcher_template = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
  <meta name="theme-color" content="#093366">
  __WTARG_CSP_META__
  <title>WTARG</title>
  <link rel="apple-touch-icon" sizes="180x180" href="assets/apple-touch-icon.png?v=20260709">
  <link rel="manifest" href="site.webmanifest">
  <link rel="icon" type="image/png" sizes="192x192" href="assets/favicon-192.png">
  <link rel="icon" type="image/png" sizes="32x32" href="assets/favicon-32.png">
  <link rel="icon" type="image/png" sizes="16x16" href="assets/favicon-16.png">
  <script>(function(){ var t=localStorage.getItem('theme'); if(t==='dark') document.documentElement.setAttribute('data-theme','dark'); })();</script>
  <style>
    @font-face { font-family: 'Montserrat'; src: url('assets/Montserrat-SemiBold.ttf'); font-weight: 600; }
    @font-face { font-family: 'Montserrat'; src: url('assets/Montserrat-ExtraBold.ttf'); font-weight: 800 900; }
    html { -webkit-text-size-adjust: 100%; text-size-adjust: 100%; overflow-x: hidden; max-width: 100vw; }
    body { font-family: 'Montserrat', sans-serif; background: #093366; margin: 0; transition: background 0.2s; }

    .home-hero { width: 100%; min-height: 90vh; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 18px; padding: 8px 12px; box-sizing: border-box; }
    .home-title { margin: 0; text-align: center; line-height: 0; }
    .home-logo { display: block; width: min(240px, 58vw); height: auto; }
    .home-note { margin: -2px 0 4px; font-size: 14px; color: #334155; text-align: center; }
    .home-note strong { color: #75AADB; }
    .home-grid { width: 100%; max-width: 1200px; display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 14px; margin: 0 auto; justify-items: center; }
    .home-btn { padding: 18px 12px; border: 2px solid #75AADB; border-radius: 12px; background: #eaf3fb; font-family: inherit; font-size: 14px; font-weight: bold; color: #1e293b; cursor: pointer; min-height: 92px; display: flex; align-items: center; justify-content: flex-start; gap: 10px; white-space: normal; line-height: 1.2; overflow: hidden; width: 100%; text-decoration: none; box-sizing: border-box; transition: background 0.15s, transform 0.15s, box-shadow 0.15s; }
    .home-icon-img { width: 30px; height: 30px; object-fit: contain; margin-left: 6px; flex-shrink: 0; }
    .home-label { flex: 1; text-align: center; padding-right: 28px; word-break: break-word; }
    .home-btn:hover { background: #d9ecf8; transform: none; box-shadow: 0 6px 16px rgba(0,0,0,0.1); }
    .home-btn:active { background: #b8d9f0; transform: scale(0.97); box-shadow: none; }
    .home-dark-btn { margin-top: 10px; padding: 10px 24px; border-radius: 20px; border: 2px solid #75AADB; background: #eaf3fb; font-family: inherit; font-size: 14px; font-weight: bold; color: #1e293b; cursor: pointer; display: flex; align-items: center; gap: 8px; transition: background 0.15s; }
    .home-dark-btn:hover { background: #d9ecf8; }
    .dm-icon { width: 18px; height: 18px; display: inline-block; vertical-align: middle; flex-shrink: 0; }
    .dm-icon-sun { display: none; }
    [data-theme="dark"] .dm-icon-moon { display: none; }
    [data-theme="dark"] .dm-icon-sun { display: inline-block; }
    *:focus-visible { outline: 2px solid rgba(117,170,219,0.55); outline-offset: 2px; }
    [data-theme="dark"] *:focus-visible { outline-color: rgba(125,197,255,0.6); }

    [data-theme="dark"] body { background: #093366; }
    [data-theme="dark"] .home-note { color: #94a3b8; }
    [data-theme="dark"] .home-btn { background: #1a3350; border-color: #3b7ec4; color: #e2e8f0; }
    [data-theme="dark"] .home-btn:hover { background: #1e3a5c; box-shadow: 0 6px 16px rgba(0,0,0,0.4); }
    [data-theme="dark"] .home-dark-btn { background: #1a3350; border-color: #3b7ec4; color: #e2e8f0; }
    [data-theme="dark"] .home-dark-btn:hover { background: #1e3a5c; }
    [data-theme="dark"] .home-icon-img:not(.no-invert) { filter: brightness(0) invert(1); }

    @media (max-width: 900px) {
      .home-hero { min-height: 0; padding: 12px 10px; gap: 10px; }
      .home-title { margin: 4px 0 2px; }
      .home-logo { width: min(190px, 52vw); }
      .home-note { margin: 0 0 2px; font-size: 13px; }
      .home-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; width: calc(100vw - 20px); max-width: 420px; padding: 0; }
      .home-btn { min-height: 78px; font-size: 13px; padding: 10px 8px; width: 100%; }
      .home-label { padding-right: 0; }
      .home-btn.last { grid-column: auto; }
    }
    body {
      min-height: 100vh;
      color: #fff;
      background: #093366;
    }
    .home-hero { position: relative; z-index: 1; min-height: 100vh; padding: 28px 18px; }
    .home-logo { width: min(210px, 52vw); filter: none; }
    .home-grid { max-width: 1050px; display: flex; flex-wrap: wrap; justify-content: center; gap: 12px; }
    .home-btn {
      flex: 0 1 calc((100% - 48px) / 5);
      min-height: 88px;
      border: 1px solid rgba(255,255,255,.24);
      border-radius: 16px;
      background: rgba(255,255,255,.93);
      box-shadow: 0 12px 30px rgba(0,0,0,.14);
      backdrop-filter: blur(8px);
    }
    html:not([data-theme="dark"]) .home-btn:hover {
      border-color: #4d89c3;
      background: #b9ddf4;
      background-image: none;
      transform: none;
      box-shadow: 0 8px 20px rgba(0,0,0,.14);
    }
    .home-dark-btn {
      min-height: 38px;
      margin-top: 8px;
      border: 1px solid rgba(255,255,255,.34);
      background: rgba(255,255,255,.10);
      color: #fff;
      backdrop-filter: blur(8px);
    }
    .home-dark-btn:hover { background: rgba(255,255,255,.18); }
    [data-theme="dark"] body { background: #093366; }
    [data-theme="dark"] .home-dark-btn { background: rgba(255,255,255,.10); color: #fff; }
    @media (max-width: 900px) {
      .home-hero { min-height: 100dvh; padding: calc(18px + env(safe-area-inset-top)) 10px calc(18px + env(safe-area-inset-bottom)); }
      .home-logo { width: min(165px, 44vw); }
      .home-grid { gap: 8px; }
      .home-btn { flex-basis: calc((100% - 8px) / 2); min-height: 70px; padding: 8px; border-radius: 13px; font-size: 10px; }
      .home-icon-img { width: 24px; height: 24px; margin-left: 2px; }
      .home-dark-btn { padding: 8px 16px; font-size: 11px; }
    }
  </style>
</head>
<body>
  <div class="home-hero">
    <h1 class="home-title"><img class="home-logo" src="assets/wtarg-app-icon.png" alt="Women's Tennis Argentina"></h1>
    <div class="home-grid">
      <a class="home-btn" href="app.html#entrylists"><img class="home-icon-img" src="assets/files.png" alt="Files icon"><span class="home-label">Entry Lists</span></a>
      <a class="home-btn" href="app.html#roadtogs"><img class="home-icon-img" src="assets/data.png" alt="Data icon"><span class="home-label">Points Breakdown</span></a>
      <a class="home-btn" href="app.html#calendar"><img class="home-icon-img" src="assets/calendar.png" alt="Calendar icon"><span class="home-label">Calendar</span></a>
      <a class="home-btn" href="app.html#upcoming"><img class="home-icon-img" src="assets/trophy.png" alt="Trophy icon"><span class="home-label">Schedule</span></a>
      <a class="home-btn" href="app.html#history"><img class="home-icon-img" src="assets/tennis-player.png" alt="Tennis player icon"><span class="home-label">Match History</span></a>
      <a class="home-btn" href="app.html#draws"><img class="home-icon-img" src="assets/tournament.png" alt="Tournament icon"><span class="home-label">Draws</span></a>
      <a class="home-btn" href="app.html#rankings"><img class="home-icon-img" src="assets/list.png" alt="List icon"><span class="home-label">WTA Rankings</span></a>
      <a class="home-btn" href="app.html#tstrength"><img class="home-icon-img" src="assets/score-board.png" alt="Analytics icon"><span class="home-label">WTA Tournament Strength</span></a>
      <a class="home-btn" href="app.html#fedbcup"><img class="home-icon-img no-invert" src="assets/argentina.png" alt="Argentina flag icon"><span class="home-label">Fed/BJK Cup</span></a>
    </div>
    <button class="home-dark-btn" id="home-dark-btn" onclick="toggleDarkMode()"><svg class="dm-icon dm-icon-moon" viewBox="0 0 24 24" aria-hidden="true"><path d="M21 12.79A9 9 0 1 1 11.21 3a7 7 0 0 0 9.79 9.79z" fill="currentColor"/></svg><svg class="dm-icon dm-icon-sun" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4" fill="currentColor"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg><span id="home-dark-label">Dark Mode</span></button>
  </div>
  <script>
    function toggleDarkMode() {
      var isDark = document.documentElement.getAttribute('data-theme') === 'dark';
      if (isDark) {
        document.documentElement.removeAttribute('data-theme');
        localStorage.setItem('theme', 'light');
      } else {
        document.documentElement.setAttribute('data-theme', 'dark');
        localStorage.setItem('theme', 'dark');
      }
      document.getElementById('home-dark-label').textContent = isDark ? 'Dark Mode' : 'Light Mode';
    }
    if (document.documentElement.getAttribute('data-theme') === 'dark') {
      document.getElementById('home-dark-label').textContent = 'Light Mode';
    }
  </script>
  <script src="assets/anonymous-analytics.js"></script>
</body>
</html>
"""

    html_template = f"""
    <!DOCTYPE html>
    <html lang="en" class="app-booting">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
        <meta name="theme-color" content="#093366">
        __WTARG_CSP_META__
        <title>WTARG</title>
        <link rel="apple-touch-icon" sizes="180x180" href="assets/apple-touch-icon.png?v=20260709">
        <link rel="manifest" href="site.webmanifest">
        <link rel="icon" type="image/png" sizes="192x192" href="assets/favicon-192.png">
        <link rel="icon" type="image/png" sizes="32x32" href="assets/favicon-32.png">
        <link rel="icon" type="image/png" sizes="16x16" href="assets/favicon-16.png">
        <script>(function(){{ var t=localStorage.getItem('theme'); if(t==='dark') document.documentElement.setAttribute('data-theme','dark'); }})();</script>
        <base href="./">
        <link href="assets/vendor/select2-4.1.0-rc.0.min.css" rel="stylesheet" />
        <script src="assets/vendor/jquery-3.6.0.min.js"></script>
        <script src="assets/vendor/select2-4.1.0-rc.0.min.js"></script>
        <style>
            html.app-booting {{ background: #f8fafc; }}
            html.app-booting[data-theme="dark"] {{ background: #111827; }}
            html.app-booting body {{ visibility: hidden; }}

            @font-face {{ font-family: 'Montserrat'; src: url('assets/Montserrat-SemiBold.ttf'); font-weight: 600; }}
            @font-face {{ font-family: 'Montserrat'; src: url('assets/Montserrat-ExtraBold.ttf'); font-weight: 800 900; }}

            /* =========================================================
               DESIGN TOKENS
               Edit values here; everything below uses var(--token).
               Dark mode only needs to override tokens, not every rule.
               ========================================================= */
            :root {{
                /* Surface layers */
                --c-bg: #f8fafc;
                --c-surface: #ffffff;
                --c-surface-alt: #f1f5f9;
                --c-surface-sunk: #e2e8f0;

                /* Borders */
                --c-border: #cbd5e1;
                --c-border-soft: #e2e8f0;
                --c-border-strong: #94a3b8;

                /* Text */
                --c-text: #0f172a;
                --c-text-primary: #1e293b;
                --c-text-secondary: #334155;
                --c-text-subtle: #475569;
                --c-text-muted: #64748b;

                /* Brand (Argentina sky blue) */
                --c-primary: #75AADB;
                --c-primary-strong: #4d89c3;
                --c-primary-hover: #5a8fb8;
                --c-primary-accent: #3B82F6;
                --c-primary-deep: #1e40af;
                --c-primary-soft: #dbeafe;
                --c-primary-softer: #eaf3fb;

                /* ARG player highlight */
                --c-arg-tint: #e0f2fe;

                /* Chrome / sidebar - stays dark in both themes. These
                   tokens are NOT overridden in [data-theme="dark"]. */
                --c-chrome-bg: #0A3366;
                --c-chrome-bg-deep: #062A55;
                --c-chrome-text: #cbd5e1;
                --c-chrome-border: #174A7A;
                --c-chrome-border-accent: #2A5F90;
                --c-chrome-hover: #134777;

                /* Semantic */
                --c-success: #059669;
                --c-success-strong: #166534;
                --c-success-soft: #bbf7d0;
                --c-success-softer: #f0fdf4;
                --c-error: #dc2626;
                --c-error-strong: #b91c1c;

                /* Typography scale */
                --fs-xs: 10px;
                --fs-sm: 11px;
                --fs-md: 12px;
                --fs-lg: 13px;
                --fs-xl: 14px;
                --fs-2xl: 16px;
                --fs-3xl: 20px;
                --fs-4xl: 22px;
                --fs-5xl: 26px;

                /* Spacing scale */
                --sp-1: 4px;
                --sp-2: 8px;
                --sp-3: 12px;
                --sp-4: 16px;
                --sp-5: 20px;
                --sp-6: 24px;
                --sp-8: 32px;

                /* Radius */
                --radius-sm: 6px;
                --radius-md: 8px;
                --radius-lg: 12px;
                --radius-xl: 16px;
                --radius-pill: 9999px;

                /* Button heights */
                --btn-h: 34px;
                --btn-h-sm: 28px;
                --btn-h-lg: 38px;

                /* Focus ring - shown on :focus-visible (keyboard nav) */
                --c-focus-ring: rgba(117,170,219,0.55);

                /* Shadow */
                --shadow-sm: 0 1px 2px rgba(15,23,42,0.06);
                --shadow-md: 0 4px 16px rgba(15,23,42,0.08);
                --shadow-lg: 0 8px 24px rgba(0,0,0,0.13);
                --shadow-hover: 0 6px 16px rgba(0,0,0,0.1);

                /* Gradients - kept as literals so they resolve the same
                   regardless of theme. */
                --grad-primary: linear-gradient(180deg, #75AADB 0%, #4d89c3 100%);
                --grad-chrome: linear-gradient(180deg, #0A3366 0%, #062A55 100%);
            }}

            [data-theme="dark"] {{
                --c-bg: #111827;
                --c-surface: #1e293b;
                --c-surface-alt: #273548;
                --c-surface-sunk: #162032;

                --c-border: #334155;
                --c-border-soft: #334155;
                --c-border-strong: #475569;

                --c-text: #e2e8f0;
                --c-text-primary: #e2e8f0;
                --c-text-secondary: #cbd5e1;
                --c-text-subtle: #94a3b8;
                --c-text-muted: #94a3b8;

                --c-primary-soft: #1e3a5c;
                --c-primary-softer: #1a3350;
                --c-arg-tint: #1e3a5c;

                --shadow-md: 0 4px 16px rgba(0,0,0,0.35);
                --shadow-lg: 0 12px 28px rgba(0,0,0,0.4);
                --shadow-hover: 0 6px 16px rgba(0,0,0,0.4);

                --c-focus-ring: rgba(125,197,255,0.6);
            }}

            /* === Button system ===
               Three styles share one grammar (height, radius, padding, weight).
               .btn-primary = filled CTA. .btn-secondary = outlined / toggle.
               .btn-ghost = low-emphasis. .btn-group wraps adjacent .btn into
               a segmented row. Home grid tiles (.home-btn / .home-dark-btn)
               are intentionally outside this system. */
            .btn {{
                display: inline-flex;
                align-items: center;
                justify-content: center;
                gap: 6px;
                height: var(--btn-h);
                padding: 0 14px;
                border-radius: var(--radius-md);
                border: 1px solid transparent;
                font-family: inherit;
                font-size: 13px;
                font-weight: 600;
                line-height: 1;
                white-space: nowrap;
                cursor: pointer;
                box-sizing: border-box;
                transition: background 0.15s, color 0.15s, border-color 0.15s;
            }}
            .btn:disabled {{ opacity: 0.35; cursor: default; }}
            .btn-primary {{ background: var(--c-primary); color: #fff; border-color: var(--c-primary); }}
            .btn-primary:hover:not(:disabled) {{ background: var(--c-primary-hover); border-color: var(--c-primary-hover); }}
            .btn-secondary {{ background: var(--c-surface); color: var(--c-text-primary); border-color: var(--c-border-strong); }}
            .btn-secondary:hover:not(:disabled) {{ background: var(--c-surface-alt); }}
            .btn-secondary.active {{ background: var(--c-primary); color: #fff; border-color: var(--c-primary); }}
            .btn-secondary.active:hover:not(:disabled) {{ background: var(--c-primary-hover); border-color: var(--c-primary-hover); }}
            .btn-ghost {{ background: var(--c-surface-sunk); color: var(--c-text-subtle); border-color: transparent; }}
            .btn-ghost:hover:not(:disabled) {{ background: var(--c-border); }}
            .btn-group {{ display: inline-flex; }}
            .btn-group > .btn {{ border-radius: 0; }}
            .btn-group > .btn:first-child {{ border-top-left-radius: var(--radius-md); border-bottom-left-radius: var(--radius-md); }}
            .btn-group > .btn:last-child {{ border-top-right-radius: var(--radius-md); border-bottom-right-radius: var(--radius-md); }}
            .btn-group > .btn + .btn {{ border-left-width: 0; }}

            .rankings-toggle-btn,
            .entry-menu-toggle-btn,
            .history-nav-btn,
            .cal-dd-btn,
            .calendar-gm-toggle {{
                display: inline-flex;
                align-items: center;
                justify-content: center;
                height: var(--btn-h);
                padding: 0 var(--sp-3);
                border-radius: var(--radius-md);
                border: 2px solid var(--c-border-strong);
                background: var(--c-surface);
                color: var(--c-text-primary);
                font-family: inherit;
                font-size: var(--fs-md);
                font-weight: 700;
                line-height: 1;
                cursor: pointer;
                white-space: nowrap;
                box-sizing: border-box;
                transition: background 0.15s, color 0.15s, border-color 0.15s, box-shadow 0.15s;
            }}
            .rankings-toggle-btn:hover,
            .entry-menu-toggle-btn:hover,
            .history-nav-btn:not(:disabled):hover,
            .cal-dd-btn:hover,
            .calendar-gm-toggle:hover,
            .cal-dd.open .cal-dd-btn {{ background: var(--c-surface-alt); }}
            .rankings-toggle-btn:disabled,
            .history-nav-btn:disabled {{ opacity: 0.35; cursor: default; }}
            .rankings-load-btn {{
                display: inline-flex;
                align-items: center;
                justify-content: center;
                border: 0;
                background: var(--c-primary);
                color: #fff;
                font-family: inherit;
                font-size: var(--fs-lg);
                font-weight: 700;
                line-height: 1;
                cursor: pointer;
                box-sizing: border-box;
                transition: background 0.15s;
            }}
            .rankings-load-btn:hover {{ background: var(--c-primary-hover); }}
            .btn-flag-icon {{ height: 14px; width: auto; vertical-align: middle; }}

            /* Dark-mode toggle icons - both SVGs live in the button;
               CSS shows the one opposite to the current theme. */
            .dm-icon {{ width: 16px; height: 16px; display: inline-block; vertical-align: middle; flex-shrink: 0; }}
            .home-dark-btn .dm-icon {{ width: 18px; height: 18px; }}
            .dm-icon-sun {{ display: none; }}
            [data-theme="dark"] .dm-icon-moon {{ display: none; }}
            [data-theme="dark"] .dm-icon-sun {{ display: inline-block; }}

            /* Keyboard focus ring - only fires on :focus-visible, so
               mouse clicks don't get an outline. Applies globally;
               components with custom focus styling can opt out. */
            *:focus-visible {{ outline: 2px solid var(--c-focus-ring); outline-offset: 2px; }}

            /* Skeleton loading - shimmer bars for async table renders. */
            @keyframes skeleton-shimmer {{
                0%   {{ background-position: -200px 0; }}
                100% {{ background-position: calc(200px + 100%) 0; }}
            }}
            .skeleton-bar {{
                display: inline-block;
                height: 10px;
                width: 100%;
                border-radius: 4px;
                background-color: var(--c-surface-sunk);
                background-image: linear-gradient(90deg, transparent 0%, var(--c-surface-alt) 50%, transparent 100%);
                background-size: 200px 100%;
                background-repeat: no-repeat;
                animation: skeleton-shimmer 1.2s ease-in-out infinite;
                vertical-align: middle;
            }}
            #view-rankings .skeleton-row td {{ padding: 8px 10px; }}
            @media (prefers-reduced-motion: reduce) {{
                .skeleton-bar {{ animation: none; }}
            }}

            html {{ -webkit-text-size-adjust: 100%; text-size-adjust: 100%; overflow-x: hidden; max-width: 100vw; }}
            [hidden] {{ display: none !important; }}
            .mobile-only {{ display: none; }}
            .desktop-only {{ display: inline; }}
            body {{ font-family: 'Montserrat', sans-serif; background: var(--c-bg); color: var(--c-text); margin: 0; display: flex; min-height: 100vh; overflow-y: auto; overflow-x: auto; -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }}
            .app-container {{ display: flex; width: 100%; min-height: 100vh; }}
            .sidebar {{ width: 180px; background: var(--grad-chrome); color: white; display: flex; flex-direction: column; flex-shrink: 0; min-height: 100vh; }}
            .sidebar-header {{ padding: 18px 15px; font-size: 15px; font-weight: 800; color: var(--c-primary); border-bottom: 1px solid var(--c-chrome-border-accent); display: flex; align-items: center; justify-content: space-between; gap: 8px; }}
            .sidebar-logo {{ display: block; width: 82px; height: auto; flex-shrink: 0; }}
            .menu-item {{ width: 100%; padding: 15px 20px; cursor: pointer; color: var(--c-chrome-text); font-family: inherit; font-size: 14px; text-align: left; background: transparent; border: 0; border-bottom: 1px solid var(--c-chrome-border); transition: 0.2s; text-decoration: none; display: block; }}
            .menu-item:hover {{ background: var(--c-chrome-hover); color: white; border-left: 3px solid rgba(117,170,219,0.45); padding-left: 17px; }}
            .menu-item.active {{ background: rgba(255,255,255,0.08); color: white; font-weight: bold; border-left: 3px solid var(--c-primary-accent); padding-left: 17px; }}
            .main-content {{ flex: 1; overflow-y: visible; background: var(--c-bg); padding: 20px; display: flex; flex-direction: column; }}
            .single-layout {{ width: 100%; min-width: 0; display: flex; flex-direction: column; }}
            #view-upcoming {{ max-width: 1200px; margin: 0 auto; }}
            #view-entrylists {{ width: 100%; max-width: 1100px; margin: 0; }}
            #view-rankings {{ max-width: 700px; margin: 0 auto; }}
            #view-fedbcup {{ max-width: 1400px; margin: 0 auto; }}
            #view-tstrength {{ width: 100%; margin: 0 auto; }}
            #view-roadtogs {{ max-width: 1100px; margin: 0 auto; }}
            #view-draws {{ width: 100%; max-width: 100%; margin: 0; }}
            .draws-layout {{ display: flex; flex-direction: column; width: 100%; }}
            .draws-toolbar {{ display: flex; align-items: center; gap: 10px; padding: 6px 12px; flex-wrap: wrap; position: relative; }}
            #draws-tournament-select {{ padding: 6px 24px 6px 8px; border: 2px solid #cbd5e1; border-radius: 8px; font-size: 12px; font-family: inherit; background: white; min-width: 200px; appearance: none; -webkit-appearance: none; background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%2364748b'/%3E%3C/svg%3E"); background-repeat: no-repeat; background-position: right 8px center; cursor: pointer; color: #1e293b; font-weight: 600; text-align: center; text-align-last: center; }}
            #draws-tournament-select optgroup {{ font-size: 10px; font-weight: bold; background: #e2e8f0; color: #475569; padding: 4px 0; }}
            #draws-tournament-select option {{ font-size: 11px; font-weight: normal; background: white; padding: 4px 8px; }}
            .draws-type-btns {{ display: flex; gap: 0; }}
            .draw-type-btn {{ padding: 4px 10px; border: 1px solid #cbd5e1; background: white; font-family: inherit; font-size: 10px; font-weight: 600; color: #64748b; cursor: pointer; }}
            .draw-type-btn:first-child {{ border-radius: 8px 0 0 8px; }}
            .draw-type-btn:last-child {{ border-radius: 0 8px 8px 0; border-left: none; }}
            .draw-type-btn.active {{ background: #1e293b; color: white; border-color: #1e293b; }}
            .draw-bracket-wrapper {{ overflow-x: auto; overflow-y: auto; max-height: calc(100vh - 110px); padding-bottom: 12px; }}
            .draw-bracket {{ display: flex; gap: 0; padding: 6px; min-width: max-content; position: relative; }}
            .draw-round {{ display: flex; flex-direction: column; min-width: 175px; padding: 0 10px; transition: min-width 0.2s, padding 0.2s, opacity 0.2s; }}
            .draw-round.hidden-round {{ display: none; }}
            .draw-round-header {{ text-align: center; font-weight: bold; font-size: 9px; color: #64748b; padding: 3px 0 6px; text-transform: uppercase; letter-spacing: 0.5px; position: sticky; top: 0; z-index: 2; cursor: pointer; }}
            .draw-round-header:hover {{ color: #1e40af; text-decoration: underline; }}
            .draw-round-header.active-filter {{ color: #1e40af; }}
            .draw-filter-reset {{ display: none; font-size: 10px; color: #64748b; cursor: pointer; padding: 4px 10px; border: 1px solid #cbd5e1; border-radius: 8px; background: white; font-family: inherit; }}
            .draw-filter-reset:hover {{ background: #f1f5f9; color: #1e293b; }}
            .draw-filter-reset.visible {{ display: inline-block; }}
            .draw-match-wrapper {{ flex: 1; display: flex; align-items: center; padding: 2px 0; }}
            .draw-match {{ display: flex; flex-direction: column; width: 100%; }}
            .draw-match .draw-player {{ display: flex; align-items: center; padding: 1px 3px; font-size: 10px; border: 1px solid #e2e8f0; background: white; min-height: 18px; gap: 1px; cursor: default; }}
            .draw-match .draw-player:first-child {{ border-bottom: none; }}
            .draw-match .draw-player.winner {{ font-weight: bold; background: #f0fdf4; }}
            .draw-player .seed-entry {{ display: flex; gap: 0; width: 30px; flex-shrink: 0; justify-content: center; overflow: hidden; }}
            .draw-player .seed {{ color: #6b7280; font-size: 9px; min-width: 10px; text-align: center; }}
            .draw-player .entry {{ color: #9333ea; font-size: 9px; text-align: center; }}
            .draw-player .name {{ flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
            .draw-player .country {{ flex-shrink: 0; width: 16px; min-width: 16px; display: inline-block; text-align: center; }}
            .draw-player .sets {{ display: flex; gap: 0; margin-left: 3px; flex-shrink: 0; }}
            .draw-player .set-score {{ font-size: 9px; width: 16px; text-align: center; position: relative; }}
            .draw-player .set-score.wo {{ text-align: left; padding-left: 0; transform: translateX(-8px); }}
            .draw-player .set-score sup {{ font-size: 6px; position: absolute; top: -2px; }}
            .draw-player .set-score.won {{ color: #059669; }}
            .draw-player .set-score.lost {{ color: #dc2626; }}
            .draw-no-draws {{ text-align: center; color: #94a3b8; padding: 40px; font-size: 12px; }}
            .home-hero {{ width: 100%; display: flex; flex-direction: column; align-items: center; gap: 18px; }}
            .home-title {{ order: 0; }}
            .home-note {{ order: 1; }}
            .home-grid {{ order: 1; }}
            .home-title {{ margin: 0; text-align: center; line-height: 0; }}
            .home-logo {{ display: block; width: min(240px, 58vw); height: auto; }}
            .home-note {{ margin: -2px 0 4px; font-size: 14px; color: #334155; text-align: center; }}
            .home-note strong {{ color: #75AADB; }}
            .home-grid {{ order: 2; }}
            .home-grid {{ width: 100%; max-width: 1200px; display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 14px; margin: 0 auto; justify-items: center; }}
            .home-btn {{ padding: 18px 12px; border: 2px solid #75AADB; border-radius: 12px; background: #eaf3fb; font-family: inherit; font-size: 14px; font-weight: bold; color: #1e293b; cursor: pointer; min-height: 92px; display: flex; align-items: center; justify-content: flex-start; gap: 10px; white-space: normal; line-height: 1.2; overflow: hidden; width: 100%; transition: background 0.15s, transform 0.15s, box-shadow 0.15s; }}
            .home-icon {{ width: 28px; height: 28px; stroke: #1e293b; fill: none; stroke-width: 1.6; stroke-linecap: round; stroke-linejoin: round; flex-shrink: 0; }}
            .home-icon-img {{ width: 30px; height: 30px; object-fit: contain; }}
            .home-btn .home-icon {{ margin-left: 6px; }}
            .home-btn .home-icon-img {{ margin-left: 6px; }}
            .home-label {{ flex: 1; text-align: center; padding-right: 28px; word-break: break-word; }}
            .home-icon-fill {{ fill: #1e293b; stroke: none; }}
            .home-btn:hover {{ background: #d9ecf8; transform: none; box-shadow: 0 6px 16px rgba(0,0,0,0.1); }}
            .home-btn:active {{ background: #b8d9f0; transform: scale(0.97); box-shadow: none; }}
            .home-dark-btn {{ margin-top: 10px; padding: 10px 24px; border-radius: 20px; border: 2px solid #75AADB; background: #eaf3fb; font-family: inherit; font-size: 14px; font-weight: bold; color: #1e293b; cursor: pointer; display: flex; align-items: center; gap: 8px; transition: background 0.15s; }}
            .home-dark-btn:hover {{ background: #d9ecf8; }}
            .home-hero {{ min-height: 80vh; justify-content: center; }}
            @media (max-width: 900px) {{
                body.home-mode {{ overflow: hidden; }}
                .home-mode .main-content {{ padding: 8px 6px 6px; }}
                .home-hero {{ min-height: 0; padding: 8px 0 2px; gap: 6px; }}
                .home-title {{ margin: 4px 0 2px; }}
                .home-logo {{ width: min(190px, 52vw); }}
                .home-note {{ margin: 0 0 2px; font-size: 13px; }}
                .home-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; width: calc(100vw - 20px); max-width: 420px; padding: 0; }}
                .home-btn {{ min-height: 78px; font-size: 13px; padding: 10px 8px; width: 100%; }}
                .home-label {{ padding-right: 0; }}
                .home-btn.last {{ grid-column: auto; }}
            }}
            .home-mode #sidebar {{ display: none; }}
            body.home-mode {{ background: var(--c-chrome-bg); }}
            .home-mode .main-content {{ width: 100%; margin-left: 0; background: var(--c-chrome-bg); }}
            .calendar-mode .main-content {{ overflow-x: hidden; }}
            @media (min-width: 769px) {{
                .calendar-mode .main-content {{ padding-top: 8px; padding-bottom: 8px; }}
                #history-match-page .history-summary-container {{ transform: translateX(5px); }}
                #history-match-page .history-header-actions-player {{ width: 330px; }}
            }}
            .roadtogs-controls {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }}
            #roadtogs-points-total {{ color: var(--c-text-primary); }}
            #roadtogs-table {{ width: 100%; table-layout: fixed; }}
            #view-roadtogs .content-card {{ max-width: 900px; margin: 0 auto; }}
            #roadtogs-table {{ max-width: 900px; margin: 0 auto; }}
            #roadtogs-table th, #roadtogs-table td {{ padding: 8px 12px; text-align: left; overflow: hidden; text-overflow: ellipsis; }}
            #roadtogs-table th:nth-child(1), #roadtogs-table td:nth-child(1) {{ width: 95px; white-space: nowrap; text-align: center; }}
            #roadtogs-table th:nth-child(2) {{ text-align: center; }}
            #roadtogs-table td:nth-child(2) {{ white-space: normal; word-break: break-word; }}
            #roadtogs-table th:nth-child(3), #roadtogs-table td:nth-child(3) {{ width: 85px; white-space: nowrap; text-align: center; }}
            #roadtogs-table th:nth-child(4), #roadtogs-table td:nth-child(4) {{ width: 40px; white-space: nowrap; text-align: center; }}
            #roadtogs-table th:nth-child(5), #roadtogs-table td:nth-child(5) {{ width: 95px; white-space: nowrap; text-align: center; }}
            .roadtogs-separator td {{ background: var(--c-chrome-bg) !important; color: white; text-align: center !important; font-weight: 400; font-size: 11px; line-height: 1.1; letter-spacing: 0; padding: 5px 12px !important; }}
            .roadtogs-category-separator td {{ background: #6b7280 !important; color: white; text-align: center !important; font-weight: 400; font-size: 11px; line-height: 1.1; letter-spacing: 0; padding: 5px 12px !important; }}
            tr.roadtogs-category-separator:hover td {{ background: #6b7280 !important; }}
            .rtgs-lock {{ margin-left: 4px; font-size: 11px; line-height: 1; vertical-align: baseline; }}
            .roadtogs-cutoffs {{ margin-bottom: 8px; display: flex; flex-wrap: nowrap; gap: 10px; align-items: flex-start; }}
            .roadtogs-info {{ width: 100%; margin-bottom: 12px; }}
            .roadtogs-info-summary {{
                min-height: 28px;
                position: relative;
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 10px;
                padding: 3px 30px 3px 9px;
                border: 1px solid var(--c-border);
                border-radius: 7px;
                background: var(--c-surface-alt);
                color: var(--c-text-secondary);
                font-size: 11px;
                font-weight: 700;
                cursor: pointer;
                list-style: none;
                user-select: none;
            }}
            .roadtogs-info-summary::-webkit-details-marker {{ display: none; }}
            .roadtogs-info-summary:hover {{ border-color: var(--c-primary); background: var(--c-primary-soft); }}
            .roadtogs-info-summary:focus-visible {{ outline: 2px solid var(--c-primary); outline-offset: 2px; }}
            .roadtogs-info-summary-label {{ display: inline-flex; align-items: center; gap: 6px; }}
            .roadtogs-info-icon {{
                width: 16px;
                height: 16px;
                display: inline-grid;
                place-items: center;
                flex: 0 0 16px;
                border-radius: 50%;
                background: var(--c-primary);
                color: #fff;
                font-family: Georgia, serif;
                font-size: 11px;
                font-weight: 700;
                line-height: 1;
            }}
            .roadtogs-info-summary::after {{
                content: "";
                position: absolute;
                top: 50%;
                right: 10px;
                width: 7px;
                height: 7px;
                box-sizing: border-box;
                border-right: 2px solid currentColor;
                border-bottom: 2px solid currentColor;
                transform: translateY(-50%) rotate(45deg);
                transform-origin: center;
                transition: transform 0.15s ease;
            }}
            .roadtogs-info[open] .roadtogs-info-summary::after {{ transform: translateY(-50%) rotate(225deg); }}
            .roadtogs-info-panel {{
                margin-top: 6px;
                display: grid;
                grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr);
                gap: 24px;
                padding: 11px 13px;
                border: 1px solid var(--c-border);
                border-top: 3px solid var(--c-primary);
                border-radius: 7px;
                background: var(--c-surface);
                color: var(--c-text-muted);
                box-shadow: var(--shadow-sm);
            }}
            .roadtogs-info-col {{ min-width: 0; font-size: 11px; line-height: 1.5; }}
            .roadtogs-info-line {{ display: grid; grid-template-columns: 76px minmax(0, 1fr); gap: 8px; }}
            .roadtogs-info-line + .roadtogs-info-line {{ margin-top: 4px; }}
            .roadtogs-info-term {{ color: var(--c-text-primary); font-weight: 700; }}

            /* Shared utility classes â€” replace inline style="..." attributes that appeared
               many times across rendered tables. Colours match the previous inline values
               byte-for-byte to keep rendering identical; centralising them means future
               theme tweaks can be made in one place. */
            .text-muted {{ color: #64748b; }}
            .cell-state-info {{ padding: 20px; color: #64748b; }}
            .cell-state-error {{ padding: 20px; }}
            .res-win {{ color: #166534; font-weight: bold; }}
            .res-loss {{ color: #991b1b; font-weight: bold; }}
            .rtgs-warn-14d {{ color: #cc0000; font-weight: bold; }}
            .rtgs-warn-28d {{ color: #cc5500; font-weight: bold; }}
            .gs-cutoff-table {{ border-collapse: collapse; font-size: 10px; width: 100%; min-width: 0; table-layout: fixed; }}
            .gs-cutoff-table th, .gs-cutoff-table td {{ border: 1px solid var(--c-border); padding: 2px 6px; text-align: center; white-space: nowrap; }}
            .gs-cutoff-table tbody td:nth-child(4) {{ font-weight: 400 !important; }}
            .gs-cutoff-table col.gs-col-d {{ width: 14%; }}
            .gs-cutoff-table col.gs-col-cutoff {{ width: 30%; }}
            .gs-cutoff-table col.gs-col-acc, .gs-cutoff-table col.gs-col-est {{ width: 28%; }}
            .gs-cutoff-table thead tr:last-child th {{ background: var(--c-surface-alt) !important; font-weight: bold; color: var(--c-text-subtle) !important; }}
            .header-row {{ width: 100%; margin-bottom: 20px; display: flex; flex-direction: column; align-items: center; position: relative; gap: 10px; }}
            #view-roadtogs .header-row {{ margin-bottom: 8px; }}
            h1 {{ margin: 0; font-size: var(--fs-4xl); color: var(--c-text-primary); }}
            .search-container {{ position: absolute; left: 0; top: 50%; transform: translateY(-50%); }}
            .rankings-filter-container {{ position: absolute; right: 0; top: 50%; transform: translateY(-50%); }}
            .rankings-filter-container {{ display: flex; align-items: center; }}
            .rankings-date-picker {{ display: flex; align-items: stretch; border: 2px solid var(--c-border-strong); border-radius: var(--radius-md); overflow: hidden; background: var(--c-surface); }}
            .rankings-date-select {{ width: auto; font-size: var(--fs-md); font-weight: 700; padding: var(--sp-2) 22px var(--sp-2) var(--sp-2); border: none !important; border-radius: 0 !important; background-color: transparent !important; color: var(--c-text-primary); }}
            #rankings-year-select {{ min-width: 82px; }}
            #rankings-month-select {{ min-width: 74px; border-left: 1px solid var(--c-border) !important; }}
            #rankings-day-select {{ min-width: 62px; border-left: 1px solid var(--c-border) !important; }}
            .rankings-load-btn {{ border-left: 2px solid var(--c-border-strong); border-radius: 0; padding: 0 10px; }}
            .rankings-controls {{ display: flex; align-items: center; width: 100%; gap: 8px; }}
            .rankings-controls .search-container {{ position: static; transform: none; flex: 1; display: flex; justify-content: flex-start; }}
            .rankings-controls .rankings-filter-container {{ position: static; transform: none; flex: 0 0 auto; }}
            .rankings-btn-end {{ flex: 1; display: flex; justify-content: flex-end; }}
            #rankings-search {{ width: 190px; }}
            input, select {{ padding: var(--sp-2) var(--sp-3); border-radius: var(--radius-md); border: 2px solid var(--c-border-strong); background-color: var(--c-surface); color: var(--c-text-primary); font-family: inherit; font-size: var(--fs-lg); width: 250px; box-sizing: border-box; }}
            input:focus, select:focus {{ outline: none; border-color: var(--c-primary); box-shadow: 0 0 0 3px var(--c-focus-ring); }}
            select {{ font-weight: 700; cursor: pointer; appearance: none; background-image: url("data:image/svg+xml;charset=UTF-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%23475569' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'%3E%3C/polyline%3E%3C/svg%3E"); background-repeat: no-repeat; background-position: right 10px center; }}
            .content-card {{ background: var(--c-surface); box-shadow: var(--shadow-md); width: 100%; border: 1px solid var(--c-border); }}
            .table-wrapper {{ overflow-x: auto; width: 100%; }}
            table {{ border-collapse: separate; border-spacing: 0; width: 100%; table-layout: fixed; border: 1px solid var(--c-border); }}
            #view-upcoming table {{ width: max-content; min-width: 100%; }}
            th {{ position: sticky; top: 0; background: var(--c-chrome-bg) !important; color: white; padding: 10px 15px; font-size: 11px; font-weight: bold; border-bottom: 2px solid rgba(0,0,0,0.25); border-right: 1px solid rgba(255,255,255,0.25); z-index: 10; text-transform: uppercase; text-align: center; }}
            td {{ padding: 8px 12px; border-bottom: 1px solid var(--c-border); text-align: center; font-size: 13px; border-right: 1px solid var(--c-border); }}
            #view-entrylists td {{ font-size: 12px; padding: 6px 10px; }}
            #view-entrylists table {{ table-layout: auto; }}
            #view-entrylists .entry-content {{ align-items: flex-start; }}
            #view-entrylists .content-card {{ width: 100%; max-width: 760px; margin: 0; }}

            /* Entry Lists layout */
            .entry-layout {{ display: flex; flex-direction: row; gap: 25px; width: 100%; }}
            #view-entrylists.entry-menu-collapsed .entry-menu {{ display: none; }}
            #view-entrylists.entry-menu-collapsed .entry-menu-toggle-wrap {{ display: block; }}
            .entry-menu-toggle-wrap {{ display: none; width: 100%; margin-bottom: 8px; }}
            .entry-menu-toggle-btn {{ width: 100%; justify-content: center; }}
            #view-entrylists .entry-menu-toggle-btn {{
                border-color: var(--c-primary);
                background: var(--c-primary-softer);
                color: var(--c-text-primary);
                box-shadow: 0 3px 10px rgba(117, 173, 219, 0.18);
            }}
            #view-entrylists .entry-menu-toggle-btn:hover {{ background: var(--c-primary-soft); }}
            #view-entrylists .entry-menu-toggle-btn:active {{ background: var(--c-primary-soft); box-shadow: none; transform: translateY(1px); }}
            .entry-menu {{ width: 480px; flex-shrink: 0; display: flex; flex-wrap: wrap; align-items: stretch; background: var(--c-surface); border: 1px solid var(--c-border); align-self: flex-start; }}
            .entry-menu-header {{ width: 100%; background: var(--c-chrome-bg); color: white; font-size: var(--fs-xl); font-weight: bold; text-align: center; padding: var(--sp-3); }}
            .entry-menu-legend {{ width: 100%; background: var(--c-surface); color: var(--c-text-muted); font-size: var(--fs-xs); line-height: 1.35; text-align: center; padding: var(--sp-2) 10px; border-bottom: 1px solid var(--c-border); box-sizing: border-box; }}
            .entry-menu-gm-sample {{ display: inline-block; font-size: 8px; font-weight: 700; padding: 0px 3px; border-radius: 2px; background: var(--c-arg-tint); color: #1a1a1a; line-height: 13px; margin: 0 2px; vertical-align: middle; }}
            .entry-menu-week {{ width: 100%; background: var(--c-surface-sunk); font-size: var(--fs-sm); font-weight: bold; text-align: center; padding: var(--sp-2); color: var(--c-text-subtle); border-bottom: 1px solid var(--c-border); }}
            .entry-menu-item {{ flex: 1 1 calc(33.333% - 1px); min-width: 0; padding: 8px 6px; font-size: var(--fs-xs); cursor: pointer; border-bottom: 1px solid var(--c-border-soft); border-right: 1px solid var(--c-border-soft); color: var(--c-text-secondary); transition: background 0.15s; text-align: center; box-sizing: border-box; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 1px; }}
            .entry-menu-item:hover {{ background: var(--c-surface-alt); }}
            .entry-menu-item.active {{ background: var(--c-primary-soft); color: var(--c-primary-deep); font-weight: bold; }}
            .entry-menu-top {{ display: flex; align-items: center; justify-content: center; gap: 1px; font-size: 10px; line-height: 1.05; width: 100%; text-align: center; transform: translateY(-1px); }}
            .entry-menu-level {{ display: none; }}
            .entry-menu-dot {{ order: 1; }}
            .entry-menu-flag {{ order: 2; display: inline-flex; align-items: center; }}
            .entry-menu-gm {{ order: 3; white-space: nowrap; margin-left: 4px; }}
            .entry-menu-flag img {{ width: 13px !important; height: 9px !important; vertical-align: middle; margin-right: 0 !important; }}
            .entry-menu-gm {{ font-weight: 700; color: var(--c-text-muted); }}
            .entry-menu-gm-value {{ display: inline-block; font-size: 8px; font-weight: 700; padding: 0px 3px; margin-left: 0; border-radius: 2px; background: var(--c-arg-tint); color: #1a1a1a; line-height: 13px; letter-spacing: -0.1px; }}
            .entry-menu-name {{ width: 100%; font-size: 10px; line-height: 1.08; text-align: center; color: var(--c-text); margin-top: 1px; transform: translateY(3px); }}
            .entry-content {{ flex: 1; display: flex; flex-direction: column; min-width: 0; }}
            #view-entrylists .entry-header-row {{ position: relative; min-height: 40px; }}
            #view-entrylists .entry-strength {{ position: absolute; left: 0; top: 50%; transform: translateY(-50%); font-size: 12px; font-weight: 700; line-height: 1.1; max-width: calc(50% - 14px); }}
            #view-entrylists .entry-gm-value {{ display: inline-flex; align-items: center; justify-content: center; min-height: 28px; padding: 5px 12px; border-radius: var(--radius-pill); font-size: 13px; line-height: 1; letter-spacing: 0; box-sizing: border-box; }}
            #view-rankings table {{ table-layout: auto; }}
            #view-rankings td {{ font-size: 12px; padding: 6px 10px; }}
            .sticky-col {{ position: sticky; background: var(--c-surface) !important; z-index: 2; }}
            td.col-week {{ width: 170px; font-size: 11px; line-height: 1.2; overflow: hidden; text-overflow: ellipsis; }}
            .tournament-surface-dot {{ display: inline-block; width: 7px; height: 7px; border-radius: 50%; margin-right: 4px; vertical-align: middle; flex-shrink: 0; }}
            th.sticky-col {{ z-index: 11; background: var(--c-chrome-bg) !important; color: white; }}
            .col-rank {{ left: 0; width: 32px; min-width: 45px; max-width: 45px; }}
            .col-name {{ left: 45px; width: 112px; min-width: 112px; max-width: 112px; text-align: left; font-weight: bold; }}
            #view-upcoming .col-rank {{
                width: 78px;
                min-width: 78px;
                max-width: 78px;
                box-sizing: border-box;
                white-space: nowrap;
            }}
            #view-upcoming .col-name {{ left: 78px; }}
            .col-week {{ width: 150px; font-size: 11px; font-weight: bold; line-height: 1.2; overflow: hidden; text-overflow: ellipsis; }}
            .divider-row td {{ background: var(--c-surface-sunk); font-weight: bold; text-align: center; padding: 5px 15px; font-size: 11px; border-right: none; }}
            tr.hidden {{ display: none; }}
            table:not(.calendar-table) tbody tr:nth-child(even):not(.divider-row):not(.roadtogs-separator):not(.roadtogs-category-separator):not(.cal-group-first):not(.cal-group-last) td {{ background: var(--c-surface-alt); }}
            table:not(.calendar-table) tbody tr:nth-child(even):not(.divider-row):not(.roadtogs-separator):not(.roadtogs-category-separator):not(.cal-group-first):not(.cal-group-last) td.sticky-col {{ background: var(--c-surface-alt) !important; }}
            .dropdown-header {{ background-color: #e2e8f0 !important; font-weight: bold !important; text-align: center !important; padding: 12px 0 !important; font-size: 11px; display: block; }}
            .dropdown-item {{ padding: 8px 15px; text-align: left; background-color: #ffffff; }}

            .select2-container--default .select2-selection--single {{
                border: 2px solid #94a3b8;
                border-radius: 8px;
                height: 38px;
                padding: 4px 12px;
                font-family: inherit;
                font-size: 13px;
            }}
            .select2-container--default .select2-selection--single .select2-selection__rendered {{
                color: #1e293b;
                line-height: 28px;
                padding-left: 0;
            }}
            .select2-container--default .select2-selection--single .select2-selection__arrow {{
                height: 36px;
            }}
            .select2-container--default.select2-container--open .select2-selection--single {{
                border-color: #75AADB;
            }}
            .select2-dropdown {{
                border: 2px solid #94a3b8;
                border-radius: 8px;
                font-family: inherit;
            }}
            .select2-search--dropdown .select2-search__field {{
                border: 1px solid #94a3b8;
                border-radius: 4px;
                padding: 4px 8px;
                font-family: inherit;
            }}
            .select2-results__option {{
                padding: 8px 12px;
                font-size: 13px;
            }}
            .select2-results__option--highlighted {{
                background-color: #75AADB !important;
                color: white !important;
            }}
            .select2-container {{
                width: 250px !important;
            }}

            #view-entrylists .content-card {{ overflow-y: visible; max-height: none; }}

            /* Fed/BJK Cup toggle buttons */
            .fedbcup-header-controls {{
                display: flex;
                align-items: center;
                margin-bottom: 12px;
            }}
            .fedbcup-filter-left {{
                flex: 1;
                display: flex;
                align-items: center;
            }}
            .fedbcup-toggle-row {{
                flex: 0 0 auto;
                display: flex;
                gap: 0;
            }}
            .fedbcup-record-right {{
                flex: 1;
                display: flex;
                align-items: center;
                justify-content: flex-end;
            }}
            .fedbcup-btn {{
                flex: 0 0 120px;
                text-align: center;
                padding: 7px 8px;
                margin: 0;
                cursor: pointer;
                border: none;
                border-radius: 0;
                font-size: 12px;
                font-weight: 600;
                background: #e2e8f0;
                color: #334155;
                transition: background 0.2s, color 0.2s;
            }}
            .fedbcup-btn:first-child {{ border-radius: 8px 0 0 8px; }}
            .fedbcup-btn:last-child {{ border-radius: 0 8px 8px 0; }}
            .fedbcup-btn.active {{ background: #75AADB; color: #fff; }}
            .fedbcup-btn:hover:not(.active) {{ background: #cbd5e1; }}

            /* Player Debuts table: size every column from its longest rendered value. */
            #fedbcup-view-players {{ width: fit-content; max-width: 100%; margin: 0 auto; }}
            #fedbcup-view-players .table-wrapper {{ width: fit-content; max-width: 100%; overflow-x: auto; }}
            #national-table {{ table-layout: auto; width: max-content; min-width: 0; margin: 0; }}
            #national-table th, #national-table td {{
                font-size: 11px;
                padding: 5px 6px;
                white-space: nowrap;
                overflow-wrap: normal;
                line-height: 1.2;
            }}
            #national-table th:nth-child(2), #national-table td:nth-child(2) {{
                text-align: center;
            }}
            #national-table th:nth-child(4), #national-table td:nth-child(4) {{
                white-space: nowrap;
            }}
            #national-table th:nth-child(5), #national-table td:nth-child(5) {{
                white-space: nowrap;
            }}
            #national-table th:nth-child(6), #national-table td:nth-child(6) {{
                text-align: center;
                white-space: nowrap;
            }}
            #national-table th:nth-child(7), #national-table td:nth-child(7) {{
                white-space: nowrap;
            }}
            #national-table .national-opponent-content {{
                display: flex;
                align-items: center;
                gap: 3px;
                width: 100%;
            }}
            #national-table .national-opponent-flag {{
                flex: 0 0 auto;
                display: flex;
                align-items: center;
                padding-left: 1px;
            }}
            #national-table .national-opponent-flag img {{ margin-right: 0 !important; }}
            #national-table .national-opponent-name {{
                flex: 1 1 auto;
                min-width: 0;
                text-align: center;
                white-space: nowrap;
            }}
            #national-table .national-opponent-player {{
                display: block;
                white-space: nowrap;
            }}
            #national-table .national-opponent-player + .national-opponent-player {{ padding-top: 2px; }}

            /* Captain Debuts table: compact width */
            #fedbcup-view-captains {{ width: fit-content; max-width: 100%; margin: 0 auto; }}
            #fedbcup-view-captains .table-wrapper {{ width: fit-content; max-width: 100%; overflow-x: auto; }}
            #captains-table {{ width: max-content; min-width: 0; table-layout: auto; margin: 0; }}
            #captains-table th, #captains-table td {{
                font-size: 11px;
                padding: 5px 6px;
                white-space: nowrap;
                line-height: 1.2;
            }}
            #captains-table th:nth-child(1), #captains-table td:nth-child(1) {{ width: 42px; }}
            #captains-table th:nth-child(2), #captains-table td:nth-child(2) {{ width: auto; }}
            #captains-table th:nth-child(3), #captains-table td:nth-child(3) {{ width: 64px; }}

            /* T-Strength table */
            #view-tstrength {{ display: flex; flex-direction: column; align-items: center; }}
            .tstrength-wrapper {{ overflow-x: auto; overflow-y: auto; max-height: calc(100vh - 160px); }}
            .ts-controls {{ display: flex; gap: 6px; align-items: center; flex-wrap: wrap; margin-bottom: 8px; justify-content: center; }}
            .ts-controls button {{ padding: 4px 8px; font-size: 11px; border: 1px solid #cbd5e1; border-radius: 6px; cursor: pointer; background: #75AADB; color: #fff; border-color: #75AADB; font-family: inherit; min-width: 0; }}
            .ts-controls button:hover {{ opacity: 0.85; }}
            .ts-controls select {{ padding: 3px 18px 3px 4px; font-size: 11px; border: 1px solid #cbd5e1; border-radius: 6px; font-family: inherit; width: 100px; text-align-last: center; }}
            #ts-filter-year {{ width: 60px; }}
            .ts-explanation {{ max-width: 700px; margin: 0 auto 8px auto; font-size: 11px; color: #64748b; line-height: 1.4; }}
            .ts-explanation p {{ margin: 2px 0; }}
            #tstrength-table {{ border-collapse: collapse; font-size: 12px; white-space: nowrap; width: auto; margin: 0 auto; }}
            #tstrength-table th, #tstrength-table td {{ padding: 3px 6px; border: 1px solid #e2e8f0; text-align: center; }}
            #tstrength-table th {{ background: var(--c-chrome-bg); color: #fff; font-size: 11px; position: sticky; top: 0; z-index: 2; }}
            #tstrength-table td.ts-rank-num {{ font-weight: 700; color: #64748b; }}
            #tstrength-table td.ts-name {{ font-weight: 600; }}
            #tstrength-table td.ts-gm, #tstrength-table td.ts-hm {{ font-weight: 700; }}
            [data-theme="dark"] #tstrength-table th,
            [data-theme="dark"] #tstrength-table td {{ border-color: #334155; }}
            [data-theme="dark"] #tstrength-table td[style*="background"] {{ color: #0f172a; }}
            [data-theme="dark"] #tstrength-table td.ts-rank-num {{ color: #94a3b8; }}

            .ts-row1, .ts-row2 {{ display: contents; }}

            @media (max-width: 768px) {{
                .ts-controls {{
                    flex-direction: column;
                    align-items: center;
                    gap: 4px;
                }}
                .ts-row1, .ts-row2 {{
                    display: flex;
                    gap: 6px;
                    justify-content: center;
                }}
                .ts-controls select, .ts-controls button {{ font-size: 10px; }}
                .ts-explanation {{ font-size: 8px; padding: 0 8px; }}
                .tstrength-wrapper {{ width: 100%; overflow-x: hidden; }}
                #tstrength-table {{ width: 100% !important; min-width: 0 !important; table-layout: fixed !important; font-size: 7px; white-space: normal !important; }}
                #tstrength-table th, #tstrength-table td {{ font-size: 7px; padding: 3px 0px !important; white-space: normal !important; word-break: break-word; line-height: 1.1; overflow: hidden; }}
                #tstrength-table th:nth-child(1), #tstrength-table td:nth-child(1) {{ width: 5%; }}
                #tstrength-table th:nth-child(2), #tstrength-table td:nth-child(2) {{ width: 8%; }}
                #tstrength-table th:nth-child(3), #tstrength-table td:nth-child(3) {{ width: 8%; }}
                #tstrength-table th:nth-child(4), #tstrength-table td:nth-child(4) {{ width: 11%; }}
                #tstrength-table th:nth-child(5), #tstrength-table td:nth-child(5) {{ width: 27%; }}
                #tstrength-table th:nth-child(6), #tstrength-table td:nth-child(6) {{ width: 9%; }}
                #tstrength-table th:nth-child(7), #tstrength-table td:nth-child(7) {{ width: 12%; }}
                #tstrength-table th:nth-child(8), #tstrength-table td:nth-child(8) {{ width: 9%; }}
                #tstrength-table th:nth-child(9), #tstrength-table td:nth-child(9) {{ width: 8%; }}
            }}

            /* Series view */
            #fedbcup-view-series {{ width: 100%; }}
            .fedbcup-series-toolbar {{ display: flex; align-items: center; gap: 10px; }}
            #fedbcup-player-filter {{ padding: 6px 10px; border: 1px solid #cbd5e1; border-radius: 8px; font-family: inherit; font-size: 12px; background: white; min-width: 180px; }}
            .fedbcup-record-text {{ font-size: 13px; font-weight: bold; color: #475569; white-space: nowrap; }}
            .bjkc-series-block {{ margin-bottom: 13px; }}
            .bjkc-series-header {{
                min-height: 39px;
                display: grid;
                grid-template-columns: minmax(92px, auto) minmax(0, 1fr) auto 18px;
                align-items: center;
                gap: 10px;
                background: #334155;
                color: #fff;
                font-weight: 700;
                font-size: 12px;
                padding: 7px 10px;
                border-radius: 8px;
                cursor: pointer;
                list-style: none;
                user-select: none;
            }}
            .bjkc-series-header::-webkit-details-marker {{ display: none; }}
            .bjkc-series-header:focus-visible {{ outline: 2px solid var(--c-primary); outline-offset: 2px; }}
            .bjkc-series-block[open] .bjkc-series-header {{ border-radius: 8px 8px 0 0; }}
            .bjkc-series-block > .content-card {{ margin: 0; border-top: 0; border-radius: 0 0 12px 12px; }}
            .bjkc-header-title {{ min-width: 0; overflow: hidden; text-overflow: ellipsis; text-align: center; white-space: nowrap; }}
            .bjkc-header-date {{ text-align: left; white-space: nowrap; font-size: 11px; opacity: 0.85; }}
            .bjkc-header-side {{ text-align: right; }}
            .bjkc-tie-score {{ display: inline-block; min-width: 47px; font-size: 15px; font-weight: 900; padding: 2px 10px; border-radius: 4px; letter-spacing: 1px; text-align: center; }}
            .bjkc-header-arrow {{ position: relative; width: 18px; height: 18px; }}
            .bjkc-header-arrow::after {{
                content: '';
                position: absolute;
                left: 50%;
                top: 50%;
                width: 7px;
                height: 7px;
                box-sizing: border-box;
                border-right: 2px solid currentColor;
                border-bottom: 2px solid currentColor;
                transform: translate(-50%, -70%) rotate(45deg);
                transform-origin: center;
                transition: transform 0.15s ease;
            }}
            .bjkc-series-block[open] .bjkc-header-arrow::after {{ transform: translate(-50%, -30%) rotate(225deg); }}
            .bjkc-series-table {{ table-layout: auto !important; width: max-content !important; min-width: 100%; }}
            .bjkc-series-table th:nth-child(2), .bjkc-series-table td:nth-child(2) {{ width: 44px; text-align: center; }}
            .doubles-br {{ display: none; }}

            #history-table th {{ background: var(--c-chrome-bg) !important; position: sticky; top: 0; z-index: 10; }}
            #history-table {{ table-layout: fixed; width: 100%; }}
            #history-table th:nth-child(1) {{ width: 80px; }} /* DATE */
            #history-table th:nth-child(2) {{ width: auto; }} /* TOURNAMENT */
            #history-table th:nth-child(3) {{ width: 70px; }} /* SURFACE */
            #history-table th:nth-child(4) {{ width: 55px; }} /* RND */
            #history-table th:nth-child(5) {{ width: auto; }} /* PLAYER */
            #history-table th:nth-child(6) {{ width: 110px; }} /* SCORE */
            #history-table th:nth-child(7) {{ width: auto; min-width: 260px; }} /* OPPONENT */
            #history-table td {{ font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
            #history-table td:nth-child(2) {{ white-space: normal; overflow: visible; text-overflow: clip; }} /* Allow TOURNAMENT to wrap */
            #history-table td:nth-child(5),
            #history-table td:nth-child(7) {{ white-space: normal; overflow: visible; text-overflow: clip; }}
            #history-table .history-player-cell {{
                display: flex;
                align-items: center;
                gap: 3px;
                width: 100%;
            }}
            #history-table .history-player-flag {{
                flex: 0 0 auto;
                display: flex;
                align-items: center;
            }}
            @media (min-width: 769px) {{
                #history-table .history-player-flag {{ transform: translateX(2px); }}
            }}
            #history-table .history-player-flag img {{ margin-right: 0 !important; }}
            #history-table .history-player-rank {{
                display: flex;
                align-items: center;
                justify-content: center;
                flex: 0 0 38px;
                width: 38px;
                text-align: center;
                white-space: nowrap;
            }}
            #history-table .history-player-name {{
                flex: 1 1 auto;
                min-width: 0;
                overflow-wrap: anywhere;
            }}
            #history-table td.score-win,
            #history-table td.score-loss,
            #national-table td.score-win,
            #national-table td.score-loss {{
                text-align: center;
                font-weight: 800;
                padding-left: 4px;
                padding-right: 4px;
            }}
            #history-table td.score-win,
            #national-table td.score-win {{ background: #166534; }}
            #history-table td.score-loss,
            #national-table td.score-loss {{ background: #b91c1c; }}
            #history-table .score-badge,
            #national-table .score-badge {{
                display: inline-block;
                color: #fff;
                padding: 0 4px;
                line-height: 1.2;
                white-space: nowrap;
                background: transparent;
                box-sizing: border-box;
            }}

            /* Filter Panel Styles */
            .history-layout {{ display: flex; gap: 20px; width: 100%; align-items: flex-start; }}
            .filter-panel {{ width: 250px; padding: 15px; flex-shrink: 0; border: 2px solid #cbd5e1; background: white; align-self: flex-start; }}
            .history-filter-sheet-header {{ display: contents; }}
            .history-filter-sheet-close,
            .history-filter-backdrop,
            .history-mobile-filter-bar {{ display: none; }}
            .filter-panel h3 {{ margin: -15px -15px 15px -15px; font-size: 16px; color: white; text-align: center; font-weight: 400; background: var(--c-chrome-bg); border: none; padding: 12px; border-radius: 0; }}
            .filter-group {{ margin-bottom: 20px; text-align: left; }}
            .filter-group-title {{ width: 100%; padding: 0; font-family: inherit; font-size: 13px; font-weight: bold; color: #475569; background: transparent; border: 0; margin-bottom: 8px; cursor: pointer; user-select: none; display: flex; justify-content: center; align-items: center; text-align: center; position: relative; }}
            .filter-group-title:hover {{ color: #75AADB; }}
            .filter-options {{ border: 1px solid #e2e8f0; border-radius: 6px; padding: 8px; background: #f8fafc; text-align: left; }}
            .filter-options.scrollable {{ max-height: 180px; overflow-y: auto; }}
            .filter-option {{ width: 100%; padding: 6px 10px; margin-bottom: 4px; font-family: inherit; font-size: 12px; text-align: left; color: inherit; background: transparent; border: 0; cursor: pointer; user-select: none; border-radius: 3px; transition: background 0.15s; }}
            .filter-option:hover {{ background: #e2e8f0; }}
            .filter-option.selected {{ font-weight: bold; background: #dbeafe; color: #1e40af; }}
            .rank-filter-row {{ display: flex; gap: 8px; align-items: center; }}
            .rank-filter-input {{ width: 72px; padding: 6px 8px; border: 1px solid #cbd5e1; border-radius: 8px; font-size: 12px; }}
            .rank-filter-mode {{ flex: 1; padding: 6px 8px; border: 1px solid #cbd5e1; border-radius: 8px; font-size: 12px; }}
            .filter-actions {{ margin-top: 20px; display: flex; justify-content: space-between; align-items: center; gap: 10px; }}
            .filter-instructions {{ font-size: 10px; color: #64748b; flex: 1; line-height: 1.3; padding-left: 15px; }}
            .filter-instructions-mobile {{ display: none; }}
            .filter-btn {{ display: inline-flex; align-items: center; justify-content: center; min-height: var(--btn-h); padding: 0 var(--sp-4); border: none; border-radius: var(--radius-pill); cursor: pointer; font-family: inherit; font-size: var(--fs-md); font-weight: 700; white-space: nowrap; box-sizing: border-box; transition: background 0.15s, color 0.15s; }}
            .filter-btn-clear {{ background: var(--c-surface-sunk); color: var(--c-text-subtle); }}
            .filter-btn-clear:hover {{ background: var(--c-border); }}
            #filter-opponent-select,
            #filter-tournament-select {{ font-size: 11px; }}
            .history-content {{ flex: 1; display: flex; flex-direction: column; min-width: 0; }}
            .collapse-icon {{ font-size: 14px; position: absolute; right: 0; }}
            .filter-group.collapsed .filter-options {{ display: none; }}
            .filter-group.collapsed .opponent-select-container {{ display: none; }}
            .filter-group.collapsed .collapse-icon::before {{ content: '\u25bc'; }}
            .filter-group:not(.collapsed) .collapse-icon::before {{ content: '\u25b2'; }}
            .filter-search {{ width: 100%; padding: 6px 8px; border: 1px solid #cbd5e1; border-radius: 6px; font-family: inherit; font-size: 11px; margin-bottom: 8px; box-sizing: border-box; }}
            .filter-search:focus {{ outline: none; border-color: #75AADB; }}
            .history-subpage {{ width: 100%; display: flex; flex-direction: column; gap: 15px; }}
            .history-subpage[style*="display: none"] {{ display: none !important; }}
            .table-header-section {{ margin-bottom: 15px; display: flex; align-items: center; justify-content: space-between; gap: 12px; }}
            .table-title {{ margin: 0; font-size: 22px; color: #1e293b; flex: 1; text-align: center; }}
            .history-summary-container {{ width: 250px; text-align: left; }}
            .history-header-actions {{ width: 320px; display: flex; align-items: center; justify-content: flex-end; gap: 8px; }}
            .history-header-actions .player-select-container {{ width: auto; flex: 1 1 0; min-width: 0; }}
            .history-header-actions .player-select-container .select2-container {{ width: 100% !important; }}
            .history-header-actions .history-nav-btn {{ flex: 0 0 auto; min-width: 118px; }}
            .history-wl-counter {{ font-size: 14px; font-weight: 700; color: #1e293b; white-space: nowrap; }}
            .history-nav-btn {{ height: var(--btn-h-lg); font-size: var(--fs-lg); }}
            #history-milestones-page .content-card {{
                display: flex;
                flex-direction: row;
                align-items: flex-start;
                justify-content: center;
                gap: 20px;
                padding: 0;
                background: transparent;
                border: none;
                box-shadow: none;
            }}
            #history-milestones-page .milestones-filter-panel {{
                width: 220px;
                flex: 0 0 220px;
                align-self: flex-start;
                border: 2px solid #cbd5e1;
                background: white;
            }}
            #history-milestones-page .milestones-main-column {{
                flex: 0 1 380px;
                max-width: 380px;
                min-width: 0;
                display: flex;
                flex-direction: column;
                gap: 8px;
            }}
            #history-milestones-page .table-wrapper {{
                width: 100%;
                max-width: 360px;
                margin-left: auto;
                margin-right: auto;
                box-sizing: border-box;
            }}
            #history-milestones-page .table-header-section {{
                position: relative;
                width: 100%;
                min-height: 38px;
            }}
            #history-milestones-page .table-title {{
                position: absolute;
                left: 50%;
                top: 50%;
                transform: translate(-50%, -50%);
                flex: 0 0 auto;
                width: max-content;
                text-align: center;
            }}
            #history-milestones-page .milestones-header-actions {{
                position: absolute;
                left: calc(50% + 200px);
                top: 50%;
                transform: translate(-50%, -50%);
                display: flex;
                justify-content: center;
                width: auto;
            }}
            #history-milestones-page .milestones-header-actions .history-nav-btn {{
                min-width: 118px;
            }}
            .milestones-filter-panel {{ border: 2px solid var(--c-border); background: var(--c-surface); align-self: stretch; }}
            .milestones-filter-panel h3 {{ margin: 0; font-size: var(--fs-2xl); color: white; text-align: center; font-weight: bold; background: var(--grad-primary); border: none; padding: var(--sp-3); border-radius: 0; }}
            .milestones-filter-body {{ padding: 12px; display: flex; flex-direction: column; gap: 8px; align-items: stretch; }}
            .milestones-filter-chip {{ display: inline-flex; align-items: center; justify-content: flex-start; gap: 6px; width: 100%; border: 1px solid #cbd5e1; border-radius: 12px; padding: 8px 10px; background: #f8fafc; color: #334155; font-size: 12px; font-weight: 700; cursor: pointer; user-select: none; box-sizing: border-box; }}
            .milestones-filter-chip input {{ margin: 0; width: 14px; height: 14px; accent-color: #75AADB; }}
            .milestones-filter-chip:hover {{ background: #e2e8f0; }}
            #milestones-table {{ table-layout: fixed; width: 100%; }}
            #milestones-table th {{ background: var(--c-chrome-bg) !important; position: sticky; top: 0; z-index: 10; }}
            #milestones-table th:nth-child(1) {{ width: auto; text-align: center; }}
            #milestones-table th:nth-child(2) {{ width: 100px; text-align: center; }}
            #milestones-table td {{ font-size: 12px; }}
            #milestones-table td:nth-child(1) {{ white-space: normal; overflow: visible; text-overflow: clip; }}
            #milestones-table td:nth-child(2) {{ text-align: center; font-weight: 800; }}

            /* Calendar Styles */
            #view-calendar {{ width: 100%; min-height: 0; }}
            .calendar-container {{ width: 100%; min-width: 100%; min-height: 0; margin-bottom: 0; display: block; box-sizing: border-box; }}
            .calendar-toolbar {{ display: flex; flex-wrap: wrap; gap: 10px; justify-content: flex-start; align-items: center; margin: 0 0 10px; position: sticky; top: 0; z-index: 50; background: var(--c-bg); border-bottom: 1px solid var(--c-border); padding: 10px 8px; box-sizing: border-box; }}
            .cal-dd {{ position: relative; }}
            .cal-dd-btn {{ justify-content: flex-start; padding: 0 32px 0 var(--sp-3); font-size: var(--fs-lg); user-select: none; background-color: var(--c-surface); background-image: url("data:image/svg+xml;charset=UTF-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%23475569' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'%3E%3C/polyline%3E%3C/svg%3E"); background-repeat: no-repeat; background-position: right 10px center; }}
            .calendar-gm-toggle {{ min-width: 96px; padding: 0 12px; }}
            .calendar-gm-toggle.active {{ background: var(--c-chrome-bg); border-color: var(--c-chrome-bg); color: #fff; }}
            .cal-dd-panel {{ position: absolute; top: calc(100% + 6px); left: 0; width: max-content; min-width: 170px; max-width: min(320px, calc(100vw - 20px)); max-height: 280px; overflow: auto; background: var(--c-surface); border: 2px solid var(--c-border-strong); border-radius: 8px; padding: 6px; box-shadow: var(--shadow-lg); display: none; z-index: 60; }}
            .cal-dd.open .cal-dd-panel {{ display: block; }}
            .cal-dd-item {{ display: flex; align-items: center; gap: 8px; padding: 6px 8px; border-radius: 6px; cursor: pointer; user-select: none; }}
            .cal-dd-item:hover {{ background: transparent; }}
            .cal-dd-item input {{ width: 14px; height: 14px; margin: 0; }}
            .cal-dd-item span {{ font-size: 12px; font-weight: 700; color: var(--c-text-primary); }}
            .calendar-container .table-wrapper {{ display: block; overflow-x: auto; overflow-y: hidden; -webkit-overflow-scrolling: touch; width: 100%; max-width: 100%; border-right: 1px solid var(--c-text-subtle); box-sizing: border-box; cursor: grab; overscroll-behavior-x: contain; }}
            .calendar-container .table-wrapper.dragging {{ cursor: grabbing; }}
            .calendar-table {{ border-collapse: separate; border-spacing: 0; width: max-content; min-width: max-content; table-layout: auto; border: 1px solid var(--c-border); overflow: visible !important; }}
            .calendar-table th {{ padding: 4px 4px; vertical-align: top; border-bottom: 2px solid rgba(0,0,0,0.25); border-right: 1px solid rgba(255,255,255,0.25); }}
            .calendar-table td {{ padding: 4px 4px; vertical-align: top; border-bottom: 1px solid var(--c-border); border-right: 1px solid var(--c-border); }}
            .cal-week-header {{ background: var(--c-chrome-bg); color: white; font-size: 10px; font-weight: bold; text-align: center; white-space: nowrap; padding: 6px 6px; position: sticky; top: 0; z-index: 10; min-width: 90px; }}
            .cal-cat-header {{ background: var(--c-chrome-bg); color: white; position: sticky; top: 0; left: 0; z-index: 15; width: 24px; min-width: 24px; max-width: 24px; box-sizing: border-box; }}
            .cal-cont-header {{ background: var(--c-chrome-bg); color: white; position: sticky; top: 0; left: 24px; z-index: 15; min-width: 58px; }}
            .cal-cat-label {{ background: var(--c-chrome-bg); color: white; font-size: 11px; font-weight: bold; text-align: center; vertical-align: middle !important; text-transform: uppercase; writing-mode: vertical-lr; text-orientation: mixed; transform: rotate(180deg); padding: 0; width: 24px; min-width: 24px; max-width: 24px; position: sticky; left: 0; z-index: 14; border-top: 1px solid var(--c-text-subtle); border-bottom: 1px solid var(--c-text-subtle); border-right: 1px solid var(--c-border); box-shadow: inset 0 0 0 50px var(--c-chrome-bg); box-sizing: border-box; flex: 0 0 24px; }}
            .cal-cont-label {{ background: var(--c-surface-alt); font-size: 10px; font-weight: 600; color: var(--c-text-subtle); text-align: center; vertical-align: middle !important; white-space: nowrap; position: sticky; left: 24px; z-index: 14; min-width: 58px; border-left: 1px solid var(--c-border); }}
            .cal-cont-label-mobile {{ display: none; }}
            .cal-cell {{ font-size: 10px; min-height: 24px; vertical-align: middle !important; }}
            .cal-group-first td {{ border-top: 1px solid var(--c-text-subtle); }}
            .cal-group-last td {{ border-bottom: 1px solid var(--c-text-subtle); }}
            .calendar-tournament {{ display: block; font-size: 10px; padding: 2px 6px; border-radius: 3px; line-height: 1.3; font-weight: 600; white-space: nowrap; margin: 1px 0; }}
            .calendar-tournament img {{ width: 12px; height: 8px; margin-right: 3px; vertical-align: middle; outline: 0.3px solid #000; }}
            .cal-gm-badge {{ display: inline-block; font-size: 8px; font-weight: 700; padding: 0px 3px; border-radius: 2px; margin-left: 4px; vertical-align: middle; color: #1a1a1a; line-height: 13px; }}
            .cal-gm-na {{ background: #94a3b8; color: #fff; }}
            .cal-cutoff-box {{ display: block; font-size: 8px; font-weight: 600; padding: 1px 4px; border-radius: 2px; margin: 1px 0; background: #94a3b8; color: #fff; white-space: normal; line-height: 1.3; }}
            .cal-gm-legend {{ font-size: 11px; color: #64748b; padding: 2px 4px; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; flex: 1; }}
            .cal-gm-legend-badge {{ display: inline-block; font-size: 8px; font-weight: 700; padding: 0px 3px; border-radius: 2px; background: #e0f2fe; color: #1a1a1a; line-height: 13px; margin: 0 2px; vertical-align: middle; }}
            .cal-clay {{ background: #e8a882; color: #5c2e0e; }}
            .cal-hard {{ background: #88b4e8; color: #1a3a5c; }}
            .cal-grass {{ background: #7cc89a; color: #1a4a2e; }}
            .cal-carpet {{ background: #d8b4fe; color: #5b21b6; }}

            /* Mobile Menu Toggle */
            .mobile-menu-toggle {{ display: none; position: fixed; top: 15px; left: 15px; z-index: 1000; background: var(--c-chrome-bg); color: white; border: none; padding: 10px 15px; border-radius: 8px; cursor: pointer; font-size: 18px; }}
            .sidebar.mobile-hidden {{ transform: translateX(-100%); }}

            /* Responsive Styles */
            @media (max-width: 1024px) {{
                /* Tablet adjustments */
                input, select {{ width: 200px; }}
                .select2-container {{ width: 200px !important; }}
            }}

            @media (max-width: 768px) {{
                /* Mobile styles */
                body {{ overflow-x: hidden; max-width: 100vw; }}
                .mobile-only {{ display: inline; }}
                .desktop-only {{ display: none; }}
                .mobile-menu-toggle {{ display: none; }}

                .app-container {{ flex-direction: column; }}

                .sidebar {{
                    position: fixed;
                    top: 0;
                    left: 0;
                    right: 0;
                    width: 100% !important;
                    max-width: 100% !important;
                    padding-left: env(safe-area-inset-left);
                    padding-right: env(safe-area-inset-right);
                    box-sizing: border-box;
                    height: auto;
                    min-height: 0;
                    z-index: 999;
                    box-shadow: 0 2px 10px rgba(0,0,0,0.2);
                    flex-direction: row;
                    overflow-x: hidden;
                    overflow-y: hidden;
                    white-space: normal;
                }}
                .sidebar.mobile-hidden {{ transform: none; }}
                .sidebar-header {{ display: none; }}

                .main-content {{
                    padding: 56px 5px 8px 5px;
                    width: 100%;
                    box-sizing: border-box;
                }}

                .menu-item {{
                    flex: 1 1 0;
                    border-bottom: none;
                    border-right: 1px solid var(--c-chrome-border);
                    border-left: none !important;
                    white-space: normal;
                    min-height: 40px;
                    padding: 4px 3px !important;
                    font-size: 9px;
                    line-height: 1.1;
                    text-align: center;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                }}
                .menu-item:last-child {{ border-right: none; }}
                .menu-item.active {{ border-left: none; border-bottom: 3px solid #3B82F6; padding: 4px 3px !important; }}
                .menu-item:hover {{ border-left: none; }}

                #view-upcoming, #view-rankings, #view-national, #view-captains, #view-roadtogs {{ max-width: 100%; }}

                .entry-layout {{ flex-direction: column; gap: 15px; }}
                #view-entrylists.entry-menu-collapsed .entry-menu {{ display: none; }}
                .entry-menu-toggle-wrap {{ display: block; }}
                .entry-menu {{
                    width: 100%;
                    display: flex;
                    flex-wrap: wrap;
                    align-items: stretch;
                    border: 1px solid #cbd5e1;
                }}
                .entry-menu-header {{
                    width: 100%;
                    font-size: 11px;
                    padding: 8px;
                }}
                .entry-menu-legend {{
                    width: 100%;
                    font-size: 9px;
                    line-height: 1.3;
                    padding: 6px 8px;
                }}
                .entry-menu-week {{
                    width: 100%;
                    font-size: 9px;
                    padding: 5px 6px;
                }}
                .entry-menu-item {{
                    width: auto;
                    flex: 1 1 calc(33.333% - 1px);
                    min-width: 0;
                    border-bottom: 1px solid #cbd5e1;
                    border-right: 1px solid #cbd5e1;
                    padding: 5px 6px;
                    font-size: 9px;
                    line-height: 1.1;
                    text-align: center;
                    box-sizing: border-box;
                    gap: 2px;
                }}
                #view-entrylists .entry-menu-top {{
                    font-size: 9px;
                    gap: 3px;
                    transform: translateY(-2px);
                }}
                #view-entrylists .entry-menu-name {{
                    font-size: 10px;
                    transform: translateY(1px);
                }}
                #view-entrylists .entry-strength {{
                    font-size: 10px;
                    left: 12px;
                    max-width: 40vw;
                    overflow: hidden;
                    text-overflow: ellipsis;
                }}

                /* Adjust header rows */
                .header-row {{
                    flex-direction: column;
                    gap: 10px;
                    align-items: stretch;
                }}

                .search-container {{
                    position: static;
                    transform: none;
                    width: 100%;
                    order: 2;
                }}
                .rankings-filter-container {{
                    position: static;
                    transform: none;
                    width: 100%;
                    order: 3;
                    display: flex;
                    justify-content: center;
                }}

                /* Rankings mobile: two-row controls layout */
                #view-rankings .rankings-controls {{
                    flex-wrap: wrap;
                    gap: 6px;
                    align-items: center;
                    order: 2;
                }}
                #view-rankings .search-container {{
                    flex: 0 0 100% !important;
                    width: 100% !important;
                    position: static;
                    transform: none;
                    order: 1;
                }}
                #view-rankings #rankings-search {{
                    width: 100% !important;
                    height: 32px;
                    padding: 4px 10px;
                    font-size: 11px;
                    box-sizing: border-box;
                    margin: 0;
                }}
                #view-rankings #rankings-search::placeholder {{ font-size: 10px; }}
                #view-rankings .rankings-filter-container {{
                    flex: 1 1 auto !important;
                    width: auto !important;
                    position: static;
                    transform: none;
                    order: 2;
                    justify-content: flex-start;
                }}
                #view-rankings .rankings-btn-end {{
                    flex: 0 0 auto !important;
                    order: 3;
                }}
                #view-rankings .rankings-toggle-btn {{
                    height: 32px;
                    padding: 0 10px;
                    font-size: 11px;
                    line-height: 1;
                    box-sizing: border-box;
                    margin: 0;
                    white-space: nowrap;
                }}
                #view-rankings .rankings-date-picker {{
                    height: 32px;
                }}
                #view-rankings .rankings-date-select {{
                    height: 32px;
                    padding: 0 20px 0 6px;
                    font-size: 10px;
                    width: auto !important;
                    box-sizing: border-box;
                    margin: 0;
                }}
                #view-rankings #rankings-year-select {{ min-width: 70px !important; }}
                #view-rankings #rankings-month-select {{ min-width: 62px !important; }}
                #view-rankings #rankings-day-select {{ min-width: 52px !important; }}
                #view-rankings .rankings-load-btn {{
                    height: 32px;
                    padding: 0 9px;
                    font-size: 12px;
                    box-sizing: border-box;
                    margin: 0;
                }}
                #view-entrylists .rankings-filter-container {{
                    width: auto !important;
                    display: flex;
                    justify-content: flex-end;
                    align-items: stretch;
                    margin: 0;
                }}
                #view-entrylists #btn-prio1 {{
                    height: 28px;
                    padding: 0 10px;
                    font-size: 10px;
                    line-height: 1;
                    box-sizing: border-box;
                    margin: 0;
                }}

                h1 {{
                    font-size: 18px;
                    text-align: center;
                    order: 1;
                }}

                input, select {{
                    width: 100%;
                    max-width: 100%;
                }}

                .select2-container {{
                    width: 100% !important;
                }}

                /* Table adjustments */
                .table-wrapper {{
                    overflow-x: auto;
                    -webkit-overflow-scrolling: touch;
                }}

                table {{
                    font-size: 10px;
                    min-width: 560px;
                }}

                th, td {{
                    padding: 4px 6px;
                    font-size: 9px;
                }}

                /* Upcoming: mobile layout */
                #view-upcoming table {{ width: 100%; min-width: 100%; table-layout: fixed; }}
                #view-upcoming th, #view-upcoming td {{ font-size: 7px; padding: 2px 2px; }}
                #view-upcoming th {{ font-size: 6px; }}
                #view-upcoming th.col-week {{ font-size: 6px !important; }}
                #view-upcoming td.col-week, #view-upcoming .col-week {{ font-size: 5px; line-height: 1.6; }}
                #view-upcoming .tournament-surface-dot {{ width: 5px; height: 5px; margin-right: 2px; }}
                #view-upcoming .col-rank {{
                    width: 36px !important;
                    min-width: 36px !important;
                    max-width: 36px !important;
                    left: 0;
                }}
                #view-upcoming th.col-rank, #view-upcoming td.col-rank {{
                    white-space: nowrap;
                    overflow-wrap: normal;
                    word-break: normal;
                    line-height: 1.05;
                }}
                #view-upcoming .col-name {{
                    width: 62px !important;
                    min-width: 62px !important;
                    max-width: 62px !important;
                }}
                #view-upcoming .col-name {{ left: 36px; }}
                #view-upcoming th.col-name, #view-upcoming td.col-name {{
                    white-space: normal;
                    overflow-wrap: anywhere;
                    word-break: break-word;
                    text-overflow: clip;
                    text-align: center;
                }}
                #view-upcoming .col-week, #view-upcoming td.col-week {{ width: auto !important; min-width: 0 !important; max-width: none !important; }}

                /* Entry Lists: compact mode */
                #view-entrylists table {{ min-width: 0; table-layout: auto; }}
                #view-entrylists th {{ font-size: 10px; padding: 3px 4px; }}
                #view-entrylists td {{ font-size: 10px; padding: 4px 4px; }}
                #view-entrylists .entry-content .header-row {{
                    margin-bottom: 8px;
                    flex-direction: row !important;
                    align-items: center !important;
                    position: relative;
                }}
                #view-entrylists #entry-title {{ font-size: 14px; margin: 0; text-align: center; width: 100%; }}
                #view-entrylists .rankings-filter-container {{ position: absolute; right: 0; top: 50%; transform: translateY(-50%); flex-shrink: 0; }}

                /* Rankings table: compact mode */
                #view-rankings .content-card {{
                    width: 100%;
                    margin: 0 auto;
                }}
                #view-rankings .content-card .table-wrapper {{
                    width: 100%;
                    overflow-x: auto;
                }}
                #view-rankings table {{ min-width: 100%; width: 100%; margin: 0; table-layout: fixed; }}
                #view-rankings th, #view-rankings td {{
                    font-size: 7px;
                    padding: 2px 2px;
                    white-space: nowrap;
                    overflow: hidden;
                    text-overflow: ellipsis;
                }}
                /* Entry-list style: fixed side columns, wide PLAYER */
                #view-rankings th:nth-child(1), #view-rankings td:nth-child(1) {{ width: 20px !important; }}
                #view-rankings th:nth-child(2), #view-rankings td:nth-child(2) {{
                    width: 120px !important;
                    max-width: 120px !important;
                    text-align: left;
                    white-space: nowrap;
                    overflow: hidden;
                    text-overflow: ellipsis;
                }}
                #view-rankings th:nth-child(3), #view-rankings td:nth-child(3) {{ width: 26px !important; }}
                #view-rankings th:nth-child(4), #view-rankings td:nth-child(4) {{ width: 42px !important; }}
                #view-rankings th:nth-child(5), #view-rankings td:nth-child(5) {{ width: 34px !important; }}
                #view-rankings th:nth-child(6), #view-rankings td:nth-child(6) {{ width: 58px !important; }}

                .col-name {{
                    min-width: 98px;
                    max-width: 98px;
                }}

                .col-week {{
                    font-size: 9px;
                }}

                /* History layout - stack vertically */
                .history-layout {{
                    flex-direction: column;
                    gap: 12px;
                }}

                /* Mobile-only history flow: compact trigger + bottom sheet. */
                #view-history {{
                    width: 100%;
                    max-width: 100%;
                }}

                .history-content {{
                    width: 100%;
                    display: flex;
                    flex-direction: column;
                    gap: 12px;
                }}

                .history-mobile-filter-bar {{
                    display: flex;
                    width: min(100%, 420px);
                    margin: 0 auto;
                    box-sizing: border-box;
                }}

                .history-mobile-filter-btn {{
                    width: 100%;
                    min-height: 36px;
                    padding: 7px 14px;
                    border: 2px solid var(--c-border-strong);
                    border-radius: var(--radius-md);
                    background: var(--c-surface);
                    color: var(--c-text-primary);
                    font-family: inherit;
                    font-size: 11px;
                    font-weight: 800;
                    cursor: pointer;
                }}

                .history-mobile-filter-btn.has-active-filters {{
                    border-color: var(--c-primary);
                    background: var(--c-primary-softer);
                    color: var(--c-primary-deep);
                }}

                .history-filter-backdrop {{
                    display: block;
                    position: fixed;
                    inset: 0;
                    z-index: 1199;
                    width: 100%;
                    height: 100%;
                    padding: 0;
                    border: 0;
                    border-radius: 0;
                    background: rgba(15, 23, 42, 0.52);
                    opacity: 0;
                    visibility: hidden;
                    pointer-events: none;
                    transition: opacity 0.18s ease, visibility 0.18s ease;
                }}

                #history-filter-panel {{
                    position: fixed;
                    left: 50%;
                    right: auto;
                    bottom: 0;
                    z-index: 1200;
                    width: 100%;
                    max-width: 520px;
                    max-height: min(82dvh, 700px);
                    margin: 0;
                    padding: 0 8px calc(10px + env(safe-area-inset-bottom));
                    border: 1px solid var(--c-border-strong);
                    border-bottom: 0;
                    border-radius: 18px 18px 0 0;
                    display: flex;
                    flex-wrap: wrap;
                    gap: 4px;
                    align-items: flex-start;
                    box-sizing: border-box;
                    overflow-y: auto;
                    overscroll-behavior: contain;
                    -webkit-overflow-scrolling: touch;
                    box-shadow: 0 -12px 36px rgba(15, 23, 42, 0.24);
                    transform: translate(-50%, calc(100% + 24px));
                    visibility: hidden;
                    pointer-events: none;
                    transition: transform 0.22s ease, visibility 0.22s ease;
                }}

                body.history-filters-open {{ overflow: hidden; }}
                body.history-filters-open .history-filter-backdrop {{
                    opacity: 1;
                    visibility: visible;
                    pointer-events: auto;
                }}
                body.history-filters-open #history-filter-panel {{
                    transform: translate(-50%, 0);
                    visibility: visible;
                    pointer-events: auto;
                }}

                .history-filter-sheet-header {{
                    position: sticky;
                    top: 0;
                    z-index: 3;
                    order: -100;
                    display: flex;
                    align-items: center;
                    width: calc(100% + 16px);
                    margin: 0 -8px 6px;
                    background: var(--c-chrome-bg);
                    border-radius: 17px 17px 0 0;
                }}

                #history-filter-panel h3 {{
                    flex: 1;
                    width: auto;
                    margin: 0;
                    padding: 12px 48px;
                    border-radius: 17px 17px 0 0;
                    font-size: 14px;
                    line-height: 1.2;
                }}

                .history-filter-sheet-close {{
                    position: absolute;
                    top: 50%;
                    right: 10px;
                    display: inline-flex;
                    align-items: center;
                    justify-content: center;
                    width: 30px;
                    height: 30px;
                    padding: 0;
                    transform: translateY(-50%);
                    border: 1px solid rgba(255, 255, 255, 0.48);
                    border-radius: 50%;
                    background: rgba(255, 255, 255, 0.12);
                    color: #fff;
                    font-family: inherit;
                    font-size: 21px;
                    line-height: 1;
                    cursor: pointer;
                }}

                body.history-filters-open .select2-container--open {{ z-index: 1300; }}

                .filter-group {{
                    margin-bottom: 0;
                    flex: 1 1 100px;
                    min-width: 95px;
                    border: 1px solid #d1d5db;
                    border-radius: 6px;
                    padding: 2px;
                    background: #f8fafc;
                }}
                .filter-group-title {{ font-size: 8px; margin-bottom: 2px; }}
                .filter-option {{ font-size: 7px; padding: 2px 3px; margin-bottom: 1px; }}
                .filter-options.scrollable {{ max-height: 120px; }}

                /* Rank filters: force last row with 2 half-width boxes */
                .rank-filter-last-row {{
                    width: 100%;
                    display: flex;
                    gap: 4px;
                    order: 98;
                    align-items: flex-start; /* prevent the collapsed box from stretching to open box height */
                }}
                .rank-filter-last-row .filter-group {{
                    flex: 1 1 0;
                    min-width: 0;
                    margin-bottom: 0;
                    align-self: flex-start;
                }}
                .rank-filter-last-row .rank-filter-row {{
                    gap: 4px;
                }}
                .rank-filter-last-row .rank-filter-input {{
                    width: 42px;
                    min-width: 42px;
                    padding: 2px 3px;
                    border-radius: 6px;
                    font-size: 9px;
                }}
                .rank-filter-last-row .rank-filter-mode {{
                    padding: 2px 3px;
                    border-radius: 6px;
                    font-size: 9px;
                    min-width: 0;
                }}

                .table-header-section {{
                    flex-direction: column;
                    gap: 10px;
                    margin-bottom: 0;
                    align-items: stretch;
                }}

                .table-title {{ font-size: 14px; text-align: center; order: 1; }}

                .history-header-actions {{
                    width: min(100%, 420px);
                    box-sizing: border-box;
                    margin-left: auto;
                    margin-right: auto;
                    order: 2;
                }}

                .history-header-actions-player {{
                    display: flex;
                    gap: 8px;
                    align-items: center;
                    justify-content: space-between;
                }}

                #history-match-page .history-header-actions-player {{
                    width: min(100%, 420px);
                }}

                .history-header-actions-player .player-select-container {{
                    width: auto;
                    max-width: none;
                    margin: 0;
                    flex: 7 1 0;
                    box-sizing: border-box;
                }}

                .history-header-actions-player .history-nav-btn {{
                    flex: 3 1 0;
                    min-width: 0;
                    height: 35px;
                }}

                .history-header-actions-player .select2-container--default .select2-selection--single {{
                    height: 35px;
                    min-height: 35px;
                    padding-top: 0;
                    padding-bottom: 0;
                    display: flex;
                    align-items: center;
                    position: relative;
                }}
                .history-header-actions-player .select2-container--default .select2-selection--single .select2-selection__rendered {{
                    line-height: 1;
                    font-size: 9px;
                    padding-left: 6px;
                    flex: 1;
                    min-width: 0;
                    overflow: hidden;
                    text-overflow: ellipsis;
                    white-space: nowrap;
                }}
                .history-header-actions-player .select2-container--default .select2-selection--single .select2-selection__arrow {{
                    height: 100%;
                    position: absolute;
                    top: 0;
                    right: 1px;
                }}

                .history-header-actions-back {{
                    display: flex;
                    justify-content: flex-end;
                }}

                .history-summary-container {{
                    width: 100%;
                    display: flex;
                    justify-content: center;
                    text-align: center;
                    order: 3;
                }}

                .history-wl-counter {{
                    font-size: 12px;
                }}

                .filter-actions {{
                    width: 100%;
                    margin-top: 4px;
                    justify-content: space-between;
                    order: 99;
                }}

                .filter-instructions {{
                    padding-left: 8px;
                    font-size: 9px;
                }}
                .filter-instructions-desktop {{ display: none; }}
                .filter-instructions-mobile {{ display: inline; }}
                .filter-btn-clear {{ margin-right: 8px; }}

                .milestones-filter-panel {{
                    width: min(100%, 420px);
                    margin-left: auto;
                    margin-right: auto;
                    box-sizing: border-box;
                }}

                #history-milestones-page .content-card {{
                    flex-direction: column;
                    gap: 12px;
                }}

                #history-milestones-page .milestones-filter-panel,
                #history-milestones-page .milestones-main-column {{
                    width: 100%;
                    max-width: 100%;
                    min-width: 0;
                    flex: 1 1 auto;
                }}

                #history-milestones-page .milestones-main-column {{
                    gap: 6px;
                }}

                #history-milestones-page .table-wrapper {{
                    width: 100%;
                    overflow-x: hidden;
                }}

                #history-milestones-page .table-header-section {{
                    margin-bottom: 0;
                    min-height: 30px;
                    position: relative;
                    justify-content: flex-start;
                    align-items: center;
                }}

                #history-milestones-page .table-title {{
                    position: absolute;
                    left: 50%;
                    top: 50%;
                    transform: translate(-50%, -50%);
                    width: max-content;
                    margin-top: 0;
                    font-size: 14px;
                    text-align: center;
                }}

                #history-milestones-page .milestones-header-actions {{
                    position: absolute;
                    right: 0;
                    left: auto;
                    top: 50%;
                    transform: translateY(-50%);
                    width: auto;
                    margin-left: 0;
                    justify-content: flex-end;
                }}

                #history-milestones-page .milestones-header-actions .history-nav-btn {{
                    min-width: 0;
                    height: 35px;
                    padding: 0 10px;
                    font-size: 11px;
                }}

                .milestones-filter-panel h3 {{
                    font-size: 9px;
                    padding: 5px;
                }}

                .milestones-filter-body {{
                    display: grid;
                    grid-template-columns: repeat(3, minmax(0, 1fr));
                    padding: 8px;
                    gap: 4px;
                }}

                .milestones-filter-chip {{
                    font-size: 8px;
                    padding: 4px 8px;
                    min-width: 0;
                }}

                #history-milestones-page .table-wrapper {{
                    max-width: 100%;
                    width: 100%;
                    min-width: 0;
                }}

                #milestones-table {{
                    width: 100%;
                    max-width: 100%;
                    min-width: 0;
                    table-layout: fixed;
                }}

                #milestones-table th,
                #milestones-table td {{
                    font-size: 9px;
                    padding: 3px 4px;
                    box-sizing: border-box;
                }}

                #milestones-table th:nth-child(1),
                #milestones-table td:nth-child(1) {{
                    width: 70%;
                    overflow: hidden;
                    text-overflow: ellipsis;
                    white-space: normal;
                    overflow-wrap: anywhere;
                    word-break: break-word;
                }}

                #milestones-table th:nth-child(2),
                #milestones-table td:nth-child(2) {{
                    width: 30%;
                }}

                #milestones-table th:nth-child(1) {{
                    text-align: center;
                }}

                /* Opponent search (Select2): shorter height and smaller text */
                .opponent-select-container .select2-container--default .select2-selection--single {{
                    height: 24px;
                    min-height: 24px;
                    padding: 0 6px;
                }}
                .opponent-select-container .select2-container--default .select2-selection--single .select2-selection__rendered {{
                    line-height: 22px;
                    font-size: 8px;
                }}
                .opponent-select-container .select2-container--default .select2-selection--single .select2-selection__arrow {{
                    height: 22px;
                }}
                #select2-filter-opponent-select-results .select2-results__option {{
                    font-size: 8px;
                }}

                .content-card {{
                    width: 100%;
                }}

                .history-content .content-card {{
                    width: 100%;
                }}

                /* History table */
                #view-history .table-wrapper {{
                    overflow-x: auto;
                    -webkit-overflow-scrolling: touch;
                }}
                #history-table {{
                    width: 100%;
                    min-width: 0;
                    table-layout: fixed;
                }}
                #history-table th,
                #history-table td {{
                    font-size: 6px;
                    padding: 2px 3px;
                    white-space: normal;
                    overflow-wrap: anywhere;
                    line-height: 1.15;
                }}

                #history-table th:nth-child(1), #history-table td:nth-child(1) {{ width: 11%; }}
                #history-table th:nth-child(2), #history-table td:nth-child(2) {{ width: 17%; }}
                #history-table th:nth-child(3), #history-table td:nth-child(3) {{ width: 8%; }}
                #history-table th:nth-child(4), #history-table td:nth-child(4) {{ width: 6%; }}
                #history-table th:nth-child(5), #history-table td:nth-child(5) {{ width: 22%; }}
                #history-table th:nth-child(6), #history-table td:nth-child(6) {{ width: 14%; }}
                #history-table th:nth-child(7), #history-table td:nth-child(7) {{ width: 22%; }}
                #history-table td:nth-child(5), #history-table td:nth-child(7) {{ padding-left: 1px; padding-right: 1px; }}
                #history-table .history-player-rank {{ flex-basis: 18px; width: 18px; }}
                #history-table td:nth-child(6) {{ padding-left: 0; padding-right: 0; }}
                #history-table .score-badge {{ padding: 0 1px; }}
                #history-table th:nth-child(3),
                #history-table th:nth-child(4) {{ font-size: 0; }}
                #history-table th:nth-child(3)::after,
                #history-table th:nth-child(4)::after {{ font-size: 6px; }}
                #history-table th:nth-child(3)::after {{ content: 'SRF'; }}
                #history-table th:nth-child(4)::after {{ content: 'RD'; }}

                /* Fed/BJK Cup toggle buttons mobile */
                .fedbcup-header-controls {{ flex-direction: row; flex-wrap: wrap; gap: 6px; align-items: center; }}
                .fedbcup-filter-left {{ flex: 1; min-width: 0; order: 1; }}
                .fedbcup-filter-left select {{ font-size: 11px; padding: 4px 4px; max-width: 120px; }}
                .fedbcup-record-right {{ flex: 1; min-width: 0; order: 1; justify-content: flex-end; }}
                #view-fedbcup:not(.fedbcup-series-active) .fedbcup-filter-left,
                #view-fedbcup:not(.fedbcup-series-active) .fedbcup-record-right {{ display: none; }}
                .fedbcup-toggle-row {{ flex: 0 0 100%; order: 2; }}
                .fedbcup-btn {{ flex: 1; font-size: 12px; padding: 8px 0; }}

                /* Player Debuts table mobile */
                #fedbcup-view-players {{ width: 100%; max-width: 100%; }}
                #fedbcup-view-players .table-wrapper {{
                    width: 100%;
                    max-width: 100%;
                    overflow-x: auto;
                    -webkit-overflow-scrolling: touch;
                }}
                #national-table {{
                    width: 100%;
                    min-width: max-content;
                    table-layout: auto;
                }}
                #national-table th,
                #national-table td {{
                    font-size: 7px;
                    padding: 1px 1px;
                    white-space: nowrap;
                    word-break: normal;
                    line-height: 1.1;
                    overflow: visible;
                }}
                #national-table th:not(:nth-child(6)),
                #national-table td:not(:nth-child(6)) {{ width: 1%; }}
                #national-table th:nth-child(6),
                #national-table td:nth-child(6) {{ width: 100%; }}
                #national-table .national-opponent-cell img {{ width: 12px !important; height: 8px !important; }}
                #national-table .score-badge {{ padding: 0 1px; white-space: nowrap; }}

                /* Captain Debuts table mobile */
                #fedbcup-view-captains {{ max-width: 100%; }}
                #fedbcup-view-captains .table-wrapper {{ overflow-x: hidden; }}
                #captains-table {{
                    width: 100%;
                    min-width: 0;
                    table-layout: fixed;
                }}
                #captains-table th,
                #captains-table td {{
                    font-size: 9px;
                    padding: 3px 4px;
                    white-space: normal;
                    overflow-wrap: anywhere;
                    word-break: break-word;
                    line-height: 1.15;
                }}
                #captains-table th:nth-child(1), #captains-table td:nth-child(1) {{ width: 12%; }}
                #captains-table th:nth-child(2), #captains-table td:nth-child(2) {{ width: 58%; }}
                #captains-table th:nth-child(3), #captains-table td:nth-child(3) {{ width: 30%; }}

                /* Series mobile */
                .bjkc-series-block {{ margin-bottom: 9px; }}
                .bjkc-series-header {{
                    min-height: 36px;
                    grid-template-columns: 72px minmax(0, 1fr) auto 16px;
                    gap: 5px;
                    padding: 6px 7px;
                    border-radius: 6px;
                    font-size: 8px;
                }}
                .bjkc-series-block[open] .bjkc-series-header {{ border-radius: 6px 6px 0 0; }}
                .bjkc-series-block > .content-card {{ border-radius: 0 0 8px 8px; }}
                .bjkc-header-date {{ font-size: 8px; }}
                .bjkc-tie-score {{ min-width: 40px; padding: 3px 6px; font-size: 12px; }}
                .bjkc-header-arrow {{ width: 16px; height: 16px; }}
                .bjkc-series-table {{ width: 100% !important; min-width: unset !important; }}
                .bjkc-series-table th {{ font-size: 8px !important; padding: 3px 4px !important; }}
                .bjkc-series-table td {{ font-size: 9px !important; padding: 3px 4px !important; white-space: normal !important; }}
                .bjkc-series-table td:nth-child(2) {{ white-space: nowrap !important; }}
                .bjkc-series-table td:nth-child(3) {{ white-space: nowrap !important; }}
                .doubles-br {{ display: inline; }}
                .doubles-slash {{ display: none; }}

                /* Calendar mobile */
                .calendar-container .table-wrapper {{ overflow-x: auto; -webkit-overflow-scrolling: touch; }}
                .calendar-toolbar {{ gap: 6px; margin-bottom: 8px; top: 0; }}
                .cal-gm-legend {{ font-size: 10px; padding: 3px 4px; gap: 4px; flex: none; width: 100%; }}
                .cal-cutoff-box {{ font-size: 7px; }}
                .cal-week-header {{ position: static; }}
                .cal-cat-header {{ top: unset; }}
                .cal-cont-header {{ top: unset; }}
                .cal-dd-btn {{ padding: 6px 24px 6px 8px; font-size: 11px; background-position: right 7px center; }}
                .calendar-gm-toggle {{ min-width: 82px; padding: 6px 7px; font-size: 10px; }}
                .cal-week-header {{ font-size: 7px; padding: 3px 3px; min-width: 80px; }}
                .cal-cat-header, .cal-cont-header {{ font-size: 7px; }}
                .calendar-tournament {{ font-size: 8px; padding: 2px 4px; }}
                #view-calendar .content-card {{
                    border: none;
                    box-shadow: none;
                    background: transparent;
                }}
                .cal-cat-header {{ position: sticky !important; position: -webkit-sticky !important; left: 0; z-index: 14; background: linear-gradient(180deg, #75AADB 0%, #4d89c3 100%); }}
                .cal-cont-header {{ position: sticky !important; position: -webkit-sticky !important; left: 24px; z-index: 14; background: linear-gradient(180deg, #75AADB 0%, #4d89c3 100%); }}
                .cal-cat-label {{ position: sticky !important; position: -webkit-sticky !important; left: 0; z-index: 14; }}
                .cal-cont-header,
                .cal-cont-label {{
                    width: 36px;
                    min-width: 36px;
                    max-width: 36px;
                    box-sizing: border-box;
                }}
                .cal-cont-label {{ position: sticky !important; position: -webkit-sticky !important; left: 24px; z-index: 14; background: #f1f5f9; padding-left: 2px; padding-right: 2px; }}
                .cal-cont-label-desktop {{ display: none; }}
                .cal-cont-label-mobile {{ display: inline; }}
                .calendar-container .table-wrapper {{ position: relative; }}

                /* Points Breakdown mobile */
                #view-roadtogs .roadtogs-controls {{
                    flex-wrap: nowrap;
                    align-items: center;
                    justify-content: flex-start;
                    gap: 8px;
                }}
                #view-roadtogs .player-select-container {{
                    width: 55%;
                    max-width: none;
                    margin-left: 0;
                    margin-right: 0;
                }}
                #view-roadtogs .player-select-container .select2-container--default .select2-selection--single {{
                    height: 26px;
                    min-height: 26px;
                    padding-top: 0;
                    padding-bottom: 0;
                    display: flex;
                    align-items: center;
                    position: relative;
                }}
                #view-roadtogs .player-select-container .select2-container--default .select2-selection--single .select2-selection__rendered {{
                    line-height: 1;
                    font-size: 9px;
                    padding-left: 6px;
                    flex: 1;
                    min-width: 0;
                    overflow: hidden;
                    text-overflow: ellipsis;
                    white-space: nowrap;
                }}
                #view-roadtogs .player-select-container .select2-container--default .select2-selection--single .select2-selection__arrow {{
                    height: 100%;
                    position: absolute;
                    top: 0;
                    right: 1px;
                }}
                #select2-roadtogsPlayerSelect-results .select2-results__option {{
                    font-size: 9px;
                    padding: 3px 6px;
                }}
                #roadtogs-points-total {{
                    font-size: 11px !important;
                    white-space: nowrap;
                    padding-right: 0 !important;
                    padding-left: 0 !important;
                    margin-left: auto;
                    margin-right: 20px;
                }}
                #view-roadtogs .content-card {{
                    width: 100%;
                }}
                #view-roadtogs .table-wrapper {{
                    overflow-x: hidden;
                }}
                #roadtogs-table {{
                    width: 100%;
                    min-width: 0;
                    table-layout: fixed;
                }}
                #roadtogs-table th,
                #roadtogs-table td {{
                    font-size: 8px;
                    padding: 3px 3px;
                    white-space: nowrap;
                    overflow: hidden;
                    text-overflow: ellipsis;
                }}
                #roadtogs-table th:nth-child(1), #roadtogs-table td:nth-child(1) {{ width: 18% !important; }}
                #roadtogs-table th:nth-child(2) {{ text-align: center !important; }}
                #roadtogs-table th:nth-child(3), #roadtogs-table td:nth-child(3) {{ width: 18% !important; }}
                #roadtogs-table th:nth-child(4), #roadtogs-table td:nth-child(4) {{ width: 9% !important; }}
                #roadtogs-table th:nth-child(5), #roadtogs-table td:nth-child(5) {{ width: 18% !important; }}
                .roadtogs-separator td, .roadtogs-category-separator td {{ font-size: 7px !important; padding: 2px 3px !important; line-height: 1.15 !important; }}
                .rtgs-lock {{
                    font-size: 0 !important;
                    position: relative;
                    display: inline-block;
                    width: 5px;
                    height: 5px;
                    margin-left: 2px;
                    vertical-align: middle;
                }}
                .rtgs-lock::before {{
                    content: '';
                    position: absolute;
                    left: 1px;
                    top: 0;
                    width: 3px;
                    height: 2px;
                    border: 1px solid #111;
                    border-bottom: 0;
                    border-radius: 2px 2px 0 0;
                    box-sizing: border-box;
                }}
                .rtgs-lock::after {{
                    content: '';
                    position: absolute;
                    left: 0;
                    top: 2px;
                    width: 5px;
                    height: 3px;
                    background: #111;
                    border-radius: 1px;
                }}
                .roadtogs-cutoffs {{ display: grid !important; grid-template-columns: 1fr 1fr; gap: 6px; }}
                .gs-cutoff-table {{ width: 100% !important; min-width: 0 !important; table-layout: fixed !important; font-size: 8px !important; }}
                .gs-cutoff-table th, .gs-cutoff-table td {{ padding: 2px 3px !important; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
                /* Give Est. Need more room on narrow screens by borrowing from D. */
                .gs-cutoff-table col.gs-col-d {{ width: 13% !important; }}
                .gs-cutoff-table col.gs-col-cutoff {{ width: 30% !important; }}
                .gs-cutoff-table col.gs-col-acc {{ width: 25% !important; }}
                .gs-cutoff-table col.gs-col-est {{ width: 32% !important; }}
                .roadtogs-info-summary {{ min-height: 28px; padding: 3px 7px; font-size: 9px; }}
                .roadtogs-info-panel {{ display: block; margin-top: 5px; padding: 9px; }}
                .roadtogs-info-col {{ font-size: 8px !important; line-height: 1.45; }}
                .roadtogs-info-cutoffs {{ margin-top: 7px; padding-top: 6px; border-top: 1px solid var(--c-border-soft); }}
                .roadtogs-info-line {{ grid-template-columns: 64px minmax(0, 1fr); gap: 5px; white-space: normal; }}

            }}

            @media (max-width: 480px) {{
                /* Extra small mobile */
                h1 {{
                    font-size: 16px;
                }}

                .sidebar-header {{
                    font-size: 14px;
                    padding: 20px 10px;
                }}

                .menu-item {{
                    font-size: 7px;
                    min-height: 36px;
                    padding: 3px 2px;
                }}

                th, td {{
                    padding: 2px 3px;
                    font-size: 7px;
                }}

                #view-upcoming .col-name {{
                    width: 56px !important;
                    min-width: 56px !important;
                    max-width: 56px !important;
                }}

                .filter-panel h3 {{
                    font-size: 11px;
                }}

                .filter-group-title {{
                    font-size: 10px;
                }}

                .filter-option {{
                    font-size: 9px;
                }}

                #history-table th, #history-table td {{
                    font-size: 6px;
                    padding: 2px 2px;
                }}

                #view-national .table-wrapper {{
                    overflow-x: auto;
                    -webkit-overflow-scrolling: touch;
                }}
                #national-table {{
                    width: 100%;
                    min-width: max-content;
                    table-layout: auto;
                }}
                #national-table th,
                #national-table td {{
                    font-size: 6px;
                    padding: 1px 0;
                    white-space: nowrap;
                    word-break: normal;
                    line-height: 1.05;
                    overflow: visible;
                }}
                #national-table th:not(:nth-child(6)),
                #national-table td:not(:nth-child(6)) {{ width: 1%; }}
                #national-table th:nth-child(6),
                #national-table td:nth-child(6) {{ width: 100%; }}

                #view-captains .table-wrapper {{
                    overflow-x: hidden;
                }}
                #captains-table {{
                    width: 100%;
                    min-width: 0;
                    table-layout: fixed;
                }}
                #captains-table th,
                #captains-table td {{
                    font-size: 8px;
                    padding: 2px 3px;
                    white-space: normal;
                    overflow-wrap: anywhere;
                    word-break: break-word;
                    line-height: 1.15;
                }}
                #captains-table th:nth-child(1), #captains-table td:nth-child(1) {{ width: 12%; }}
                #captains-table th:nth-child(2), #captains-table td:nth-child(2) {{ width: 58%; }}
                #captains-table th:nth-child(3), #captains-table td:nth-child(3) {{ width: 30%; }}

                .calendar-tournament {{ font-size: 8px; padding: 2px 4px; }}

                /* Draws mobile: fade right edge to hint at horizontal scroll */
                .draw-bracket-wrapper {{
                    -webkit-mask-image: linear-gradient(to right, black 95%, transparent 100%);
                    mask-image: linear-gradient(to right, black 95%, transparent 100%);
                }}

                /* Draws mobile */
                .draws-toolbar {{ padding: 4px 8px; gap: 6px; flex-wrap: wrap; justify-content: center; }}
                #draws-tournament-select {{ font-size: 10px; min-width: 0; width: 100%; padding: 5px 22px 5px 7px; }}
                .draws-toolbar > span[style*="font-size"] {{ display: none !important; }}
                .draw-type-btn {{ padding: 2px 7px; font-size: 8px; }}
                .draw-filter-reset {{ font-size: 8px; padding: 2px 7px; }}
                .draw-bracket-wrapper {{ max-height: calc(100vh - 85px); }}
                .draw-bracket {{ padding: 3px; }}
                .draw-round {{ min-width: 130px; padding: 0 5px; }}
                .draw-round-header {{ font-size: 7px; padding: 2px 0 3px; }}
                .draw-match .draw-player {{ font-size: 7px; min-height: 14px; padding: 1px 2px; }}
                .draw-player .seed {{ font-size: 6px; }}
                .draw-player .entry {{ font-size: 6px; }}
                .draw-player .seed-entry {{ width: 24px; }}
                .draw-player .country {{ font-size: 7px; width: 13px; min-width: 13px; }}
                .draw-player .set-score {{ font-size: 6px; width: 8px; }}
                .draw-player .set-score.wo {{ transform: translateX(-6px); }}
                .draw-player .set-score sup {{ font-size: 4px; }}
                .draw-no-draws {{ font-size: 9px; padding: 20px; }}

                /* Points Breakdown extra-small */
                #roadtogs-table th,
                #roadtogs-table td {{
                    font-size: 7px;
                    padding: 2px 2px;
                }}
                .roadtogs-separator td,
                .roadtogs-category-separator td {{
                    font-size: 6px !important;
                    padding: 1px 2px !important;
                    line-height: normal !important;
                    letter-spacing: 0 !important;
                }}
                #roadtogs-points-total {{
                    font-size: 11px !important;
                }}

                /* Schedule: remove excess gap between title and table */
                #view-upcoming .header-row {{ margin-bottom: 6px; }}

                /* Reset Filters button: smaller on mobile */
                .filter-btn-clear {{ font-size: 9px; padding: 4px 10px; }}

                /* Rankings mobile layout:
                   Row 1 - search input (flexible) + Show ARG button
                   Row 2 - date filter centered */
                #view-rankings .search-container {{ flex: 1 1 auto !important; width: auto !important; order: 1 !important; }}
                #view-rankings #rankings-search {{ width: 100% !important; }}
                #view-rankings .rankings-btn-end {{ order: 1 !important; flex: 0 0 auto !important; }}
                #view-rankings .rankings-filter-container {{ flex: 0 0 100% !important; order: 2 !important; justify-content: center; }}
                /* Consistent heights */
                #view-rankings .rankings-date-picker {{ height: 30px; box-sizing: border-box; }}
                #view-rankings .rankings-date-select {{ height: 30px; line-height: 30px; box-sizing: border-box; }}
                #view-rankings .rankings-load-btn {{ height: 30px; padding: 0 9px; display: flex; align-items: center; justify-content: center; box-sizing: border-box; }}
                #view-rankings .rankings-toggle-btn {{ height: 30px; box-sizing: border-box; }}
            }}

            /* iOS WebKit-specific: keep Upcoming columns tight and consistent with desktop emulation */
            @supports (-webkit-touch-callout: none) {{
                @media (max-width: 768px) {{
                    #view-upcoming th,
                    #view-upcoming td {{
                        -webkit-text-size-adjust: 100%;
                        text-size-adjust: 100%;
                    }}
                }}
            }}
            /* ============================
               DARK MODE TOGGLE BUTTON
               ============================ */
            .dark-mode-btn {{
                background: rgba(255,255,255,0.1);
                border: 1px solid rgba(255,255,255,0.25);
                color: #cbd5e1;
                border-radius: 16px;
                padding: 3px 9px;
                font-size: 13px;
                font-family: inherit;
                cursor: pointer;
                transition: background 0.2s;
                line-height: 1;
                flex-shrink: 0;
            }}
            .dark-mode-btn:hover {{ background: rgba(255,255,255,0.22); }}
            [data-theme="dark"] .dark-mode-btn {{
                background: rgba(255,255,255,0.07);
                border-color: rgba(255,255,255,0.12);
                color: #fbbf24;
            }}
            [data-theme="dark"] .dark-mode-btn:hover {{ background: rgba(255,255,255,0.15); }}
            .dark-mode-btn-mobile {{ display: none; }}
            @media (max-width: 768px) {{
                .dark-mode-btn-mobile {{
                    flex: 1 1 0;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    padding: 4px 3px;
                    cursor: pointer;
                    color: #fbbf24;
                    font-size: 14px;
                    font-family: inherit;
                    border: none;
                    border-right: 1px solid var(--c-chrome-border);
                    background: transparent;
                    white-space: nowrap;
                    min-height: 40px;
                }}
            }}

            /* ============================
               DARK MODE OVERRIDES
               ============================ */
            [data-theme="dark"] body {{ background: #0f172a; color: #e2e8f0; }}
            [data-theme="dark"] .main-content {{ background: #111827; }}
            [data-theme="dark"] body.home-mode,
            [data-theme="dark"] .home-mode .main-content {{ background: var(--c-chrome-bg); }}
            [data-theme="dark"] h1 {{ color: #e2e8f0; }}
            [data-theme="dark"] .content-card {{ background: #1e293b; border-color: #334155; }}
            [data-theme="dark"] table {{ border-color: #334155; }}
            [data-theme="dark"] td {{ background: #1e293b; border-bottom-color: #334155; border-right-color: #334155; color: #e2e8f0; }}
            [data-theme="dark"] td.sticky-col {{ background: #1e293b !important; }}
            [data-theme="dark"] .divider-row td {{ background: #1a2638; color: #94a3b8; }}
            [data-theme="dark"] .roadtogs-separator td {{ background: #1e3a5c !important; color: #93c5fd; }}
            [data-theme="dark"] .roadtogs-category-separator td {{ background: #6b7280 !important; color: #f8fafc; }}
            [data-theme="dark"] tr.roadtogs-category-separator:hover td {{ background: #6b7280 !important; }}

            [data-theme="dark"] input,
            [data-theme="dark"] select {{ background: #1e293b; background-color: #1e293b; color: #e2e8f0; border-color: #475569; }}
            [data-theme="dark"] input:focus,
            [data-theme="dark"] select:focus {{ border-color: #75AADB; }}
            [data-theme="dark"] input::placeholder {{ color: #64748b; }}

            [data-theme="dark"] .home-btn {{ background: #1a3350; border-color: #3b7ec4; color: #e2e8f0; }}
            [data-theme="dark"] .home-btn:hover {{ background: #1e3a5c; box-shadow: 0 6px 16px rgba(0,0,0,0.4); }}
            [data-theme="dark"] .home-btn:active {{ background: #162e4a; }}
            [data-theme="dark"] .home-note {{ color: #94a3b8; }}
            [data-theme="dark"] .home-dark-btn {{ background: #1a3350; border-color: #3b7ec4; color: #e2e8f0; }}
            [data-theme="dark"] .home-dark-btn:hover {{ background: #1e3a5c; }}
            [data-theme="dark"] .home-icon-img:not(.no-invert) {{ filter: brightness(0) invert(1); }}

            [data-theme="dark"] .filter-panel {{ background: #1e293b; border-color: #334155; }}
            [data-theme="dark"] .filter-panel h3 {{ background: var(--c-chrome-bg); }}
            [data-theme="dark"] .filter-options {{ background: transparent; border: none; padding: 0; }}
            [data-theme="dark"] .filter-option {{ color: #e2e8f0; }}
            [data-theme="dark"] .filter-option:hover {{ background: #273548; }}
            [data-theme="dark"] .filter-option.selected {{ background: #1e3a5c; color: #93c5fd; }}
            [data-theme="dark"] .filter-group {{ border: none; background: transparent; border-bottom: 1px solid #334155; padding-bottom: 10px; }}
            [data-theme="dark"] .filter-group-title {{ color: #94a3b8; }}
            [data-theme="dark"] .filter-search {{ background: #1e293b; border-color: #475569; color: #e2e8f0; }}
            [data-theme="dark"] .rank-filter-input,
            [data-theme="dark"] .rank-filter-mode {{ background: #1e293b; border-color: #475569; color: #e2e8f0; }}

            [data-theme="dark"] .entry-menu {{ background: #1e293b; border-color: #334155; }}
            [data-theme="dark"] .entry-menu-week {{ background: #162032; color: #94a3b8; border-color: #334155; }}
            [data-theme="dark"] .entry-menu-item {{ color: #cbd5e1; border-bottom-color: #334155; border-right-color: #334155; }}
            [data-theme="dark"] .entry-menu-item:hover {{ background: #273548; }}
            [data-theme="dark"] .entry-menu-item.active {{ background: #1e3a5c; color: #93c5fd; }}
            [data-theme="dark"] .entry-menu-name {{ color: inherit; }}
            [data-theme="dark"] .entry-menu-legend {{ background: #162032; color: #94a3b8; border-color: #334155; }}
            [data-theme="dark"] .entry-menu-gm-sample {{ color: #bfdbfe; }}
            [data-theme="dark"] .entry-menu-gm-value {{ background: #1e3a5c; color: #bfdbfe; border-color: rgba(191,219,254,0.18); }}

            [data-theme="dark"] .draw-match .draw-player {{ background: #1e293b; border-color: #334155; color: #e2e8f0; }}
            [data-theme="dark"] .draw-match .draw-player:first-child {{ border-bottom-color: #334155; }}
            [data-theme="dark"] .draw-match .draw-player.winner {{ background: #0f2d1a; }}
            [data-theme="dark"] .draw-round-header {{ color: #94a3b8; }}
            [data-theme="dark"] .draw-round-header:hover {{ color: #93c5fd; }}
            [data-theme="dark"] #draws-tournament-select optgroup {{ background: var(--c-surface-alt); color: var(--c-text-subtle); }}
            [data-theme="dark"] #roadtogs-points-total {{ color: var(--c-text) !important; }}
            [data-theme="dark"] .calendar-toolbar {{ background: var(--c-bg); border-bottom-color: var(--c-border); }}
            [data-theme="dark"] .cal-dd-btn {{
                background-image: url("data:image/svg+xml;charset=UTF-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%23cbd5e1' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'%3E%3C/polyline%3E%3C/svg%3E");
            }}
            [data-theme="dark"] .cal-dd-panel {{ background: var(--c-surface); border-color: var(--c-border-strong); box-shadow: var(--shadow-lg); }}
            [data-theme="dark"] .cal-dd-item span {{ color: var(--c-text); }}

            /* Calendar - cal-cat-label keeps chrome tone, but cal-cont-label
               flips to surface in dark */
            [data-theme="dark"] .cal-cont-label {{ background: var(--c-surface); color: var(--c-text-subtle); }}
            [data-theme="dark"] .cal-group-first td {{ border-top-color: var(--c-border-strong); }}
            [data-theme="dark"] .cal-group-last td {{ border-bottom-color: var(--c-border-strong); }}
            [data-theme="dark"] .cal-cat-label {{ border-top-color: var(--c-border-strong); border-bottom-color: var(--c-border-strong); }}

            /* FedBCup / BJK - specific accents */
            [data-theme="dark"] .bjkc-series-header {{ background: var(--c-arg-tint); }}
            [data-theme="dark"] .fedbcup-record-text {{ color: #cbd5e1; }}
            [data-theme="dark"] td[style*="#166534"] {{ color: #4ade80 !important; }}
            [data-theme="dark"] td[style*="#991b1b"] {{ color: #f87171 !important; }}
            [data-theme="dark"] .roadtogs-cutoffs {{ gap: 12px; }}
            [data-theme="dark"] .gs-cutoff-table {{
                border-collapse: separate;
                border-spacing: 0;
                border: 1px solid var(--c-border-strong);
                border-radius: 10px;
                overflow: hidden;
                background: var(--c-surface-sunk);
            }}
            [data-theme="dark"] .gs-cutoff-table th,
            [data-theme="dark"] .gs-cutoff-table td {{ border-color: var(--c-border); }}
            [data-theme="dark"] .gs-cutoff-table tbody td {{ color: var(--c-text-secondary); }}
            [data-theme="dark"] .gs-cutoff-table thead tr:last-child th {{ background: var(--c-surface-alt) !important; color: var(--c-text-subtle) !important; }}

            /* Native select option lists - browsers paint transparent
               selects white by default, so force the surface color explicitly. */
            [data-theme="dark"] .rankings-date-select {{ background-color: var(--c-surface); color: var(--c-text); }}
            [data-theme="dark"] .rankings-date-select option {{ background-color: var(--c-surface); color: var(--c-text); }}
            [data-theme="dark"] #fedbcup-player-filter {{
                background-color: var(--c-surface-sunk);
                color: var(--c-text-secondary);
                border-color: var(--c-border);
                appearance: none;
                -webkit-appearance: none;
                padding-right: 26px;
                background-image: url("data:image/svg+xml;charset=UTF-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%2394a3b8' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'%3E%3C/polyline%3E%3C/svg%3E");
                background-repeat: no-repeat;
                background-position: right 8px center;
                background-size: 12px 12px;
            }}
            [data-theme="dark"] #fedbcup-player-filter option {{ background-color: var(--c-surface-sunk); color: var(--c-text); }}

            /* Select2 - third-party markup we don't control */
            [data-theme="dark"] .select2-container--default .select2-selection--single {{ background-color: var(--c-surface-sunk); border-color: var(--c-border); }}
            [data-theme="dark"] .select2-container--default .select2-selection--single .select2-selection__rendered {{ color: var(--c-text); }}
            [data-theme="dark"] .select2-dropdown {{ background-color: var(--c-surface-sunk); border-color: var(--c-border); }}
            [data-theme="dark"] .select2-search--dropdown .select2-search__field {{ background-color: var(--c-surface); border-color: var(--c-border); color: var(--c-text); }}
            [data-theme="dark"] .select2-results__option {{ color: var(--c-text); background-color: var(--c-surface-sunk); }}
            [data-theme="dark"] .select2-results__option--highlighted {{ background-color: var(--c-arg-tint) !important; color: var(--c-text) !important; }}
            [data-theme="dark"] .select2-container--default .select2-results__option[aria-selected=true] {{ background-color: var(--c-surface-alt); }}
            [data-theme="dark"] .dropdown-header {{ background-color: var(--c-surface-alt) !important; color: var(--c-text-subtle) !important; }}
            [data-theme="dark"] .dropdown-item {{ background-color: var(--c-surface); color: var(--c-text); }}

            /* =========================================================
               WTARG SPORTS APP SHELL
               Late overrides keep the data/rendering code untouched.
               ========================================================= */
            :root {{
                --app-rail-width: 224px;
                --app-mobile-header: 58px;
                --app-mobile-nav: 70px;
                --c-sun: #f6c453;
                --shadow-card: 0 10px 30px rgba(10, 51, 102, 0.08);
            }}

            body {{
                overflow-x: hidden;
                background:
                    radial-gradient(circle at 90% 0%, rgba(117,170,219,0.16), transparent 34%),
                    linear-gradient(180deg, #f2f8fc 0%, var(--c-bg) 48%, #f8fbfd 100%);
            }}
            [data-theme="dark"] body {{
                background:
                    radial-gradient(circle at 90% 0%, rgba(117,170,219,0.10), transparent 34%),
                    var(--c-bg);
            }}
            .app-container {{ min-width: 0; }}
            .mobile-app-header,
            .nav-more-toggle,
            .nav-sheet-backdrop {{ display: none; }}

            .sidebar {{
                position: sticky;
                top: 0;
                width: var(--app-rail-width);
                height: 100vh;
                min-height: 100vh;
                padding: 0 12px 18px;
                box-sizing: border-box;
                overflow-y: auto;
                overflow-x: hidden;
                gap: 4px;
                background: linear-gradient(165deg, #0a3b70 0%, #062a55 64%, #041f42 100%);
                box-shadow: 8px 0 28px rgba(4,31,66,0.16);
                z-index: 100;
            }}
            .sidebar-header {{
                min-height: 76px;
                padding: 14px 4px 12px;
                border-bottom: 1px solid rgba(117,170,219,0.24);
                margin-bottom: 8px;
            }}
            .sidebar-logo {{ width: 48px; max-height: 48px; object-fit: contain; }}
            .sidebar-brand-copy {{
                display: flex;
                flex: 1;
                min-width: 0;
                flex-direction: column;
                gap: 2px;
                color: #fff;
            }}
            .sidebar-brand-copy strong {{
                font-family: 'Montserrat', sans-serif;
                font-size: 16px;
                letter-spacing: 0.08em;
            }}
            .sidebar-brand-copy small {{
                color: #a9cfee;
                font-size: 8px;
                line-height: 1.25;
                white-space: normal;
            }}
            .dark-mode-btn {{
                width: 32px;
                height: 32px;
                border-radius: 10px;
                border: 1px solid rgba(255,255,255,0.18);
                background: rgba(255,255,255,0.08);
                color: #fff;
            }}
            .nav-section {{
                display: flex;
                flex-direction: column;
                gap: 4px;
                width: 100%;
            }}
            .nav-secondary {{
                margin-top: 14px;
                padding-top: 12px;
                border-top: 1px solid rgba(117,170,219,0.18);
            }}
            .nav-section-title {{
                display: block;
                padding: 4px 10px 6px;
                color: #7fb2dc;
                font-size: 9px;
                font-weight: 800;
                letter-spacing: 0.13em;
                text-transform: uppercase;
            }}
            .sidebar .menu-item {{
                min-height: 42px;
                padding: 8px 10px !important;
                border: 1px solid transparent;
                border-radius: 11px;
                color: #d7e8f7;
                font-size: 11px;
                font-weight: 700;
                line-height: 1.15;
                display: flex;
                align-items: center;
                gap: 10px;
                text-align: left;
                transition: background .16s ease, border-color .16s ease, color .16s ease, transform .16s ease;
            }}
            .sidebar .menu-item:hover {{
                padding-left: 10px;
                border-left: 1px solid rgba(117,170,219,0.38);
                border-color: rgba(117,170,219,0.28);
                background: rgba(117,170,219,0.13);
                color: #fff;
                transform: translateX(2px);
            }}
            .sidebar .menu-item.active {{
                padding-left: 10px;
                border-left: 1px solid rgba(255,255,255,0.34);
                border-color: rgba(255,255,255,0.22);
                background: linear-gradient(135deg, rgba(117,170,219,0.34), rgba(59,130,246,0.20));
                color: #fff;
                box-shadow: inset 3px 0 0 var(--c-sun);
            }}
            .nav-icon {{
                width: 20px;
                height: 20px;
                flex: 0 0 20px;
                object-fit: contain;
                filter: brightness(0) invert(1);
                opacity: .86;
            }}
            .nav-icon.no-invert {{ filter: none; }}
            .menu-item.active .nav-icon {{ opacity: 1; }}
            .nav-label {{
                min-width: 0;
                overflow: hidden;
                text-overflow: ellipsis;
                white-space: normal;
            }}

            .main-content {{
                min-width: 0;
                padding: 24px clamp(18px, 2.4vw, 38px) 36px;
                background: transparent;
            }}
            .main-content > .single-layout {{
                width: min(100%, 1440px);
                margin-left: auto;
                margin-right: auto;
            }}
            .home-mode #sidebar {{ display: flex; }}
            .home-mode .main-content {{
                width: auto;
                margin-left: 0;
                background: transparent;
            }}
            .header-row {{
                min-height: 44px;
                margin-bottom: 14px;
                align-items: flex-start;
                justify-content: center;
                gap: 8px;
            }}
            .header-row h1,
            .table-title {{
                font-family: 'Montserrat', sans-serif;
                font-size: clamp(20px, 2vw, 28px);
                font-weight: 800;
                letter-spacing: -0.025em;
                color: var(--c-chrome-bg);
            }}
            [data-theme="dark"] .header-row h1,
            [data-theme="dark"] .table-title {{ color: #dceeff; }}

            .content-card {{
                border: 1px solid rgba(77,137,195,0.22);
                border-radius: 15px;
                overflow: hidden;
                box-shadow: var(--shadow-card);
            }}
            .table-wrapper {{
                max-width: 100%;
                overflow-x: hidden;
                scrollbar-width: thin;
                scrollbar-color: var(--c-primary) var(--c-surface-alt);
            }}
            table {{
                max-width: 100%;
                border: 0;
                border-radius: 14px;
                overflow: hidden;
            }}
            th {{
                height: 36px;
                padding: 7px 9px;
                background: var(--c-chrome-bg) !important;
                border-right-color: rgba(255,255,255,.22);
                font-size: 10px;
                letter-spacing: .035em;
            }}
            td {{
                min-width: 0;
                padding: 7px 9px;
                border-color: var(--c-border-soft);
                color: var(--c-text-primary);
            }}
            table:not(.calendar-table) tbody tr:nth-child(even):not(.roadtogs-separator):not(.roadtogs-category-separator) td {{
                background: rgba(117,170,219,0.055);
            }}
            input,
            select,
            .select2-selection,
            .rankings-date-select {{
                max-width: 100%;
                border-radius: 10px !important;
                border-color: var(--c-border) !important;
                background-color: var(--c-surface) !important;
                color: var(--c-text-primary) !important;
                box-sizing: border-box;
            }}
            button,
            .btn,
            .rankings-toggle-btn,
            .history-nav-btn,
            .cal-dd-btn,
            .calendar-gm-toggle {{
                max-width: 100%;
                overflow: hidden;
                text-overflow: ellipsis;
                border-radius: 10px;
            }}
            .rankings-toggle-btn.active,
            .history-nav-btn.active,
            .calendar-gm-toggle.active,
            .draw-type-btn.active {{
                background: var(--c-chrome-bg);
                border-color: var(--c-chrome-bg);
                color: #fff;
            }}

            .home-hero {{
                min-height: calc(100vh - 84px);
                justify-content: center;
                gap: 10px;
                padding: 20px 12px 32px;
            }}
            .home-logo {{ width: min(210px, 46vw); }}
            .home-kicker {{
                margin: 2px 0 0;
                color: var(--c-chrome-bg);
                font-size: 16px;
                font-weight: 800;
                letter-spacing: -.01em;
                text-align: center;
            }}
            .home-subtitle {{
                margin: 0 0 14px;
                color: var(--c-text-muted);
                font-size: 11px;
                line-height: 1.45;
                text-align: center;
            }}
            .home-grid {{
                max-width: 1050px;
                display: flex;
                flex-wrap: wrap;
                justify-content: center;
                gap: 12px;
            }}
            .home-btn {{
                flex: 0 1 calc((100% - 48px) / 5);
                min-height: 92px;
                padding: 14px 12px;
                border: 1px solid rgba(77,137,195,.24);
                border-radius: 16px;
                background: rgba(255,255,255,.88);
                box-shadow: 0 8px 24px rgba(10,51,102,.07);
            }}
            .home-btn:hover {{
                border-color: var(--c-primary);
                background: #fff;
                transform: none;
                box-shadow: 0 14px 30px rgba(10,51,102,.14);
            }}
            .home-icon-img {{ width: 28px; height: 28px; }}
            .home-label {{
                padding-right: 18px;
                color: var(--c-chrome-bg);
                font-size: 12px;
            }}
            .home-dark-btn {{
                min-height: 36px;
                border-width: 1px;
                box-shadow: var(--shadow-sm);
            }}

            #view-entrylists {{ max-width: 1240px; }}
            #view-entrylists .content-card {{ max-width: 900px; }}
            #view-rankings {{ max-width: 860px; }}
            #view-roadtogs {{ max-width: 1180px; }}
            #view-history,
            #view-fedbcup {{ max-width: 1380px; }}
            @media (min-width: 769px) {{
                #view-entrylists .entry-header-row {{
                    display: grid;
                    grid-template-columns: minmax(100px, 1fr) auto minmax(100px, 1fr);
                    align-items: center;
                }}
                #view-entrylists .entry-strength {{
                    position: static;
                    max-width: 100%;
                    transform: none;
                    justify-self: start;
                }}
                #view-entrylists #entry-title {{ grid-column: 2; }}
                #view-entrylists .entry-header-row .rankings-filter-container {{
                    grid-column: 3;
                    justify-self: end;
                }}
                .calendar-container .table-wrapper {{
                    overflow-x: hidden;
                    cursor: default;
                }}
                .calendar-table {{
                    width: 100%;
                    min-width: 0;
                    table-layout: fixed;
                }}
                .cal-week-header {{
                    min-width: 0;
                    white-space: normal;
                    overflow-wrap: normal;
                    word-break: normal;
                    font-size: 7px;
                }}
                .calendar-tournament {{
                    white-space: normal;
                    overflow: hidden;
                    overflow-wrap: normal;
                    word-break: normal;
                    text-overflow: ellipsis;
                    font-size: 7px;
                    padding-left: 3px;
                    padding-right: 3px;
                }}
                .draw-bracket-wrapper {{
                    overflow-x: hidden;
                    max-width: 100%;
                }}
                .draw-bracket {{
                    width: 100%;
                    min-width: 0;
                    box-sizing: border-box;
                }}
                .draw-round {{
                    min-width: 0;
                    flex: 1 1 0;
                    padding-left: 4px;
                    padding-right: 4px;
                }}
                .draw-match .draw-player {{
                    min-width: 0;
                    font-size: 9px;
                }}
                .draw-player .seed-entry {{ width: 22px; }}
                .draw-player .country {{ width: 14px; min-width: 14px; }}
                .draw-player .set-score {{ width: 13px; font-size: 8px; }}
            }}

            @media (max-width: 768px) {{
                :root {{
                    --app-mobile-header: 56px;
                    --app-mobile-nav: 68px;
                }}
                html,
                body {{
                    width: 100%;
                    max-width: 100vw;
                    overflow-x: hidden;
                    overscroll-behavior-x: none;
                }}
                body {{
                    background: var(--c-bg);
                    -webkit-tap-highlight-color: transparent;
                }}
                .app-container {{ min-height: 100dvh; }}
                .mobile-menu-toggle {{ display: none !important; }}

                .main-content {{
                    width: 100%;
                    min-width: 0;
                    padding:
                        calc(var(--app-mobile-header) + env(safe-area-inset-top) + 8px)
                        6px
                        calc(var(--app-mobile-nav) + env(safe-area-inset-bottom) + 12px);
                    box-sizing: border-box;
                }}
                .mobile-app-header {{
                    position: fixed;
                    top: 0;
                    left: 0;
                    right: 0;
                    height: calc(var(--app-mobile-header) + env(safe-area-inset-top));
                    padding: env(safe-area-inset-top) 12px 0;
                    box-sizing: border-box;
                    display: flex;
                    align-items: center;
                    gap: 9px;
                    color: #fff;
                    background: linear-gradient(135deg, #0a3b70, #0b4f88);
                    border-bottom: 1px solid rgba(255,255,255,.16);
                    box-shadow: 0 4px 16px rgba(4,31,66,.20);
                    z-index: 1001;
                }}
                .mobile-app-logo {{
                    width: 34px;
                    height: 34px;
                    object-fit: contain;
                    flex: 0 0 34px;
                }}
                .mobile-app-heading {{
                    min-width: 0;
                    display: flex;
                    flex: 1;
                    flex-direction: column;
                    line-height: 1.1;
                }}
                .mobile-app-heading small {{
                    color: #a9cfee;
                    font-size: 7px;
                    letter-spacing: .07em;
                    text-transform: uppercase;
                }}
                .mobile-app-heading strong {{
                    overflow: hidden;
                    color: #fff;
                    font-size: 14px;
                    text-overflow: ellipsis;
                    white-space: nowrap;
                }}
                .mobile-header-theme {{
                    width: 34px;
                    height: 34px;
                    padding: 0;
                    border: 1px solid rgba(255,255,255,.2);
                    border-radius: 11px;
                    background: rgba(255,255,255,.09);
                    color: #fff;
                    display: grid;
                    place-items: center;
                }}

                .sidebar {{
                    position: fixed;
                    top: auto;
                    bottom: 0;
                    left: 0;
                    right: 0;
                    width: 100% !important;
                    height: calc(var(--app-mobile-nav) + env(safe-area-inset-bottom));
                    min-height: 0;
                    padding: 0 env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left);
                    overflow: visible;
                    background: rgba(5,39,76,.97);
                    border-top: 1px solid rgba(117,170,219,.35);
                    box-shadow: 0 -8px 24px rgba(4,31,66,.20);
                    z-index: 1002;
                    transform: none !important;
                }}
                .sidebar-header {{ display: none; }}
                .nav-section-title {{ display: none; }}
                .nav-primary {{
                    width: 100%;
                    height: var(--app-mobile-nav);
                    display: grid;
                    grid-template-columns: repeat(5, minmax(0,1fr));
                    gap: 0;
                }}
                .sidebar .nav-primary .menu-item,
                .nav-more-toggle {{
                    min-width: 0;
                    min-height: var(--app-mobile-nav);
                    height: var(--app-mobile-nav);
                    padding: 7px 2px 5px !important;
                    border: 0 !important;
                    border-radius: 0;
                    background: transparent;
                    box-shadow: none;
                    color: #bbd5ea;
                    display: flex;
                    flex-direction: column;
                    align-items: center;
                    justify-content: center;
                    gap: 4px;
                    font-family: inherit;
                    font-size: 8px;
                    font-weight: 700;
                    line-height: 1;
                    text-align: center;
                    transform: none;
                }}
                .sidebar .nav-primary .menu-item:hover,
                .sidebar .nav-primary .menu-item.active,
                .nav-more-toggle.active {{
                    padding: 7px 2px 5px !important;
                    border: 0 !important;
                    background: transparent;
                    color: #fff;
                    box-shadow: inset 0 3px 0 var(--c-sun);
                    transform: none;
                }}
                .nav-primary .nav-icon {{
                    width: 22px;
                    height: 22px;
                    flex-basis: 22px;
                    opacity: .82;
                }}
                .nav-primary .menu-item.active .nav-icon {{ opacity: 1; }}
                .nav-more-toggle {{ cursor: pointer; }}
                .nav-more-icon {{
                    height: 22px;
                    color: var(--c-primary);
                    font-size: 18px;
                    font-weight: 900;
                    letter-spacing: 1px;
                    line-height: 14px;
                }}

                .nav-secondary {{
                    position: fixed;
                    left: 8px;
                    right: 8px;
                    width: auto;
                    max-width: calc(100% - 16px);
                    box-sizing: border-box;
                    bottom: calc(var(--app-mobile-nav) + env(safe-area-inset-bottom) + 8px);
                    max-height: min(70vh, 430px);
                    margin: 0;
                    padding: 12px;
                    display: grid;
                    grid-template-columns: repeat(2, minmax(0,1fr));
                    gap: 8px;
                    overflow-y: auto;
                    border: 1px solid var(--c-border);
                    border-radius: 18px;
                    background: var(--c-surface);
                    box-shadow: 0 22px 60px rgba(4,31,66,.34);
                    opacity: 0;
                    visibility: hidden;
                    transform: translateY(18px) scale(.98);
                    transform-origin: bottom center;
                    transition: opacity .18s ease, transform .18s ease, visibility .18s;
                    z-index: 1004;
                }}
                .sidebar.more-open .nav-secondary {{
                    opacity: 1;
                    visibility: visible;
                    transform: translateY(0) scale(1);
                }}
                .sidebar.more-open .nav-more-toggle {{
                    color: #fff;
                    box-shadow: inset 0 3px 0 var(--c-sun);
                }}
                .sidebar .nav-secondary .menu-item,
                .dark-mode-btn-mobile {{
                    min-width: 0;
                    min-height: 58px;
                    padding: 8px 10px !important;
                    border: 1px solid var(--c-border-soft) !important;
                    border-radius: 12px;
                    background: var(--c-surface-alt);
                    color: var(--c-text-primary);
                    box-shadow: none;
                    display: flex;
                    flex-direction: row;
                    justify-content: flex-start;
                    gap: 8px;
                    font-family: inherit;
                    font-size: 9px;
                    font-weight: 700;
                    text-align: left;
                    transform: none;
                }}
                .sidebar .nav-secondary .menu-item.active {{
                    padding: 8px 10px !important;
                    border-color: var(--c-primary) !important;
                    background: var(--c-primary-soft);
                    color: var(--c-primary-deep);
                    box-shadow: inset 3px 0 0 var(--c-primary);
                }}
                .nav-secondary .nav-icon {{
                    filter: none;
                    opacity: .86;
                }}
                .dark-mode-btn-mobile {{
                    width: auto;
                    color: var(--c-text-primary);
                }}
                .nav-sheet-backdrop {{
                    position: fixed;
                    inset: 0;
                    width: 100%;
                    height: 100%;
                    padding: 0;
                    border: 0;
                    background: rgba(2,20,39,.48);
                    backdrop-filter: blur(2px);
                    opacity: 0;
                    visibility: hidden;
                    transition: opacity .18s ease, visibility .18s;
                    z-index: 1000;
                }}
                body.mobile-more-open .nav-sheet-backdrop {{
                    display: block;
                    opacity: 1;
                    visibility: visible;
                }}

                .main-content > .single-layout {{ width: 100%; }}
                .single-layout > .header-row > h1 {{ display: none; }}
                .header-row {{
                    min-height: 0;
                    margin-bottom: 8px;
                }}
                .content-card {{
                    border-radius: 12px;
                    box-shadow: 0 5px 18px rgba(10,51,102,.08);
                }}
                .table-wrapper {{
                    width: 100%;
                    max-width: 100%;
                    overflow-x: hidden !important;
                    overscroll-behavior-x: none;
                }}
                #view-calendar .table-wrapper {{
                    overflow-x: auto !important;
                    -webkit-overflow-scrolling: touch;
                    overscroll-behavior-x: contain;
                }}
                #view-draws .draw-bracket-wrapper {{
                    overflow-x: auto !important;
                    -webkit-overflow-scrolling: touch;
                    overscroll-behavior-x: contain;
                }}
                table:not(.calendar-table) {{
                    width: 100% !important;
                    max-width: 100% !important;
                    min-width: 0 !important;
                    table-layout: fixed;
                }}
                table:not(.calendar-table) th,
                table:not(.calendar-table) td {{
                    min-width: 0 !important;
                    max-width: 100%;
                    overflow: hidden;
                    overflow-wrap: anywhere;
                    text-overflow: ellipsis;
                }}
                button,
                .btn,
                input,
                select,
                .select2-container {{
                    min-width: 0;
                    max-width: 100%;
                    box-sizing: border-box;
                }}

                .home-hero {{
                    min-height: 0;
                    padding: 10px 4px 18px;
                    gap: 6px;
                }}
                .home-title {{ display: none; }}
                .home-kicker {{
                    margin-top: 6px;
                    font-size: 15px;
                }}
                .home-subtitle {{
                    max-width: 330px;
                    margin-bottom: 8px;
                    font-size: 9px;
                }}
                .home-grid {{
                    width: 100%;
                    max-width: 460px;
                    gap: 8px;
                }}
                .home-btn {{
                    flex-basis: calc((100% - 8px) / 2);
                    min-height: 70px;
                    padding: 9px 8px;
                    border-radius: 13px;
                    gap: 7px;
                }}
                .home-icon-img {{
                    width: 24px;
                    height: 24px;
                    margin-left: 2px;
                }}
                .home-label {{
                    padding-right: 0;
                    font-size: 10px;
                }}
                .home-dark-btn {{ display: none; }}

                .calendar-toolbar {{
                    position: relative;
                    padding: 6px 4px;
                    border-radius: 12px;
                    background: var(--c-surface);
                }}
                .draws-toolbar {{
                    padding: 6px 4px;
                    gap: 6px;
                }}
                #draws-tournament-select {{
                    width: min(100%, 260px);
                    min-width: 0;
                }}
            }}

            @media (max-width: 380px) {{
                .sidebar .nav-primary .menu-item,
                .nav-more-toggle {{ font-size: 7px; }}
                .nav-primary .nav-icon {{ width: 20px; height: 20px; }}
                .main-content {{ padding-left: 4px; padding-right: 4px; }}
            }}

            [data-theme="dark"] .home-btn {{
                background: var(--c-surface);
                border-color: var(--c-border);
            }}
            @media (max-width: 768px) {{
                [data-theme="dark"] .nav-secondary {{
                    background: #172235;
                    border-color: #334155;
                }}
                [data-theme="dark"] .sidebar .nav-secondary .menu-item,
                [data-theme="dark"] .dark-mode-btn-mobile {{
                    background: #1f2d42;
                    border-color: #334155 !important;
                    color: #dce8f4;
                }}
            }}
            [data-theme="dark"] .history-wl-counter {{ color: #e2e8f0; }}

            /* Desktop refinements requested after the app-shell redesign. */
            #rankings-body tr.rankings-visible-odd td {{
                background: var(--c-surface) !important;
            }}
            #rankings-body tr.rankings-visible-even td {{
                background: var(--c-surface-alt) !important;
            }}
            .header-row h1,
            .table-title,
            .mobile-app-heading strong {{
                font-weight: 400 !important;
            }}
            #view-roadtogs .gs-cutoff-table {{
                border: 1px solid var(--c-border) !important;
                border-radius: 0 !important;
                border-collapse: collapse !important;
                overflow: visible !important;
                box-sizing: border-box;
            }}
            #roadtogs-table {{
                table-layout: fixed !important;
            }}
            #roadtogs-table col.rtgs-col-date {{ width: 102px; }}
            #roadtogs-table col.rtgs-col-round {{ width: 102px; }}
            #roadtogs-table col.rtgs-col-points {{ width: 47px; }}
            #roadtogs-table col.rtgs-col-drop-date {{ width: 103px; }}
            #roadtogs-table th:nth-child(1),
            #roadtogs-table td:nth-child(1),
            #roadtogs-table th:nth-child(3),
            #roadtogs-table td:nth-child(3),
            #roadtogs-table th:nth-child(4),
            #roadtogs-table td:nth-child(4),
            #roadtogs-table th:nth-child(5),
            #roadtogs-table td:nth-child(5) {{
                width: auto !important;
                white-space: nowrap;
            }}
            #roadtogs-table th:nth-child(2),
            #roadtogs-table td:nth-child(2) {{
                width: auto !important;
            }}
            #roadtogs-table thead th {{
                background: #0a3366 !important;
                color: #f8fafc !important;
                border-right: 1px solid rgba(255, 255, 255, 0.14);
                border-bottom: 0;
                box-shadow: none;
                font-weight: 600;
                letter-spacing: 0.06em;
                text-shadow: none;
            }}
            #roadtogs-table thead th:last-child {{
                border-right: 0;
            }}

            @media (max-width: 768px) {{
                #roadtogs-table col.rtgs-col-date {{ width: 54px; }}
                #roadtogs-table col.rtgs-col-round {{ width: 55px; }}
                #roadtogs-table col.rtgs-col-points {{ width: 24px; }}
                #roadtogs-table col.rtgs-col-drop-date {{ width: 61px; }}
            }}

            @media (min-width: 769px) {{
                .sidebar {{
                    position: fixed;
                    inset: 0 auto 0 0;
                    height: 100dvh;
                    min-height: 100dvh;
                    background: #093366;
                }}
                .main-content {{
                    flex: 0 0 auto;
                    width: calc(100% - var(--app-rail-width));
                    margin-left: var(--app-rail-width);
                    box-sizing: border-box;
                }}
                .sidebar-header {{
                    min-height: 74px;
                    padding: 12px 6px 10px;
                    margin-bottom: 4px;
                    border-bottom: 0;
                    background: #093366;
                }}
                .sidebar-logo {{
                    width: 54px;
                    max-height: 54px;
                    filter: none;
                }}
                .sidebar-brand-copy,
                .nav-section-title {{ display: none !important; }}
                .nav-secondary {{
                    margin-top: 0;
                    padding-top: 0;
                    border-top: 0;
                }}
                .dark-mode-btn {{
                    width: 38px;
                    height: 38px;
                    flex: 0 0 38px;
                    padding: 0;
                    overflow: visible;
                    text-overflow: clip;
                }}
                .dark-mode-btn .dm-icon {{
                    width: 19px;
                    height: 19px;
                }}

                body.home-mode,
                .home-mode .main-content {{
                    width: calc(100% - var(--app-rail-width));
                    margin-left: var(--app-rail-width);
                    background: #093366;
                }}
                .home-mode .home-logo {{
                    filter: none;
                }}
                .home-kicker,
                .home-subtitle {{ display: none !important; }}
                html:not([data-theme="dark"]) .home-btn:hover {{
                    border-color: #4d89c3;
                    background: #b9ddf4;
                    background-image: none;
                    transform: none;
                    box-shadow: 0 8px 20px rgba(10,51,102,.12);
                }}

                #view-entrylists *,
                #view-upcoming *,
                #view-tstrength * {{
                    font-weight: 400 !important;
                }}
                #view-upcoming #schedule-table thead th {{
                    font-size: 10px !important;
                }}
                #view-upcoming .header-row h1 {{
                    width: 100%;
                    text-align: center;
                }}

                #view-roadtogs .roadtogs-controls {{
                    display: grid;
                    grid-template-columns: minmax(150px, 1fr) auto minmax(70px, 1fr);
                    align-items: center;
                    gap: 8px;
                    min-height: 44px;
                }}
                #view-roadtogs .player-select-container {{
                    width: min(250px, 100%);
                    min-width: 0;
                    justify-self: start;
                }}
                #view-roadtogs .player-select-container .select2-container {{
                    width: 100% !important;
                }}
                #view-roadtogs .gs-cutoff-table th,
                #view-roadtogs .gs-cutoff-table td {{
                    overflow: hidden;
                    text-overflow: ellipsis;
                    white-space: nowrap;
                    padding-left: 3px !important;
                    padding-right: 3px !important;
                }}
                #view-roadtogs .gs-cutoff-table th {{
                    padding-top: 0 !important;
                    padding-bottom: 0 !important;
                    font-size: 13px !important;
                }}
                #view-roadtogs .gs-cutoff-table thead tr:first-child th {{
                    height: 24px !important;
                    padding: 2px 3px !important;
                    box-sizing: border-box;
                    font-size: 10px !important;
                    line-height: 1.1;
                }}
                #view-roadtogs .gs-cutoff-table thead tr:last-child th {{
                    height: 28px !important;
                    padding: 0 3px !important;
                    box-sizing: border-box;
                    line-height: 1.1;
                    font-weight: 600 !important;
                    letter-spacing: 0;
                    text-transform: none;
                }}
                #view-roadtogs .gs-cutoff-table col.gs-col-d {{ width: 14%; }}
                #view-roadtogs .gs-cutoff-table col.gs-col-cutoff {{ width: 27%; }}
                #view-roadtogs .gs-cutoff-table col.gs-col-acc {{ width: 28%; }}
                #view-roadtogs .gs-cutoff-table col.gs-col-est {{ width: 31%; }}
                #roadtogs-table thead th {{
                    height: 28px !important;
                    padding: 2px 3px !important;
                    box-sizing: border-box;
                    line-height: 1.1;
                }}
                #view-roadtogs .roadtogs-title-row {{
                    width: auto;
                    min-height: 0;
                    margin: 0;
                    grid-column: 2;
                    align-items: center;
                }}
                #view-roadtogs .roadtogs-title-row h1 {{
                    width: 100%;
                    text-align: center;
                    white-space: nowrap;
                }}
                #roadtogs-points-total {{
                    grid-column: 3;
                    justify-self: end;
                }}

                #view-calendar .calendar-container .table-wrapper {{
                    overflow-x: auto;
                    cursor: grab;
                }}
                #view-calendar .calendar-table {{
                    width: max-content;
                    min-width: max-content;
                    table-layout: auto;
                }}
                #view-calendar .cal-week-header {{
                    min-width: 90px;
                    white-space: nowrap;
                    font-size: 10px;
                }}
                #view-calendar .calendar-tournament {{
                    white-space: nowrap;
                    overflow: visible;
                    text-overflow: clip;
                    font-size: 10px;
                    padding: 2px 6px;
                }}
                #view-calendar .calendar-table thead th {{
                    height: 24px;
                    padding-top: 2px;
                    padding-bottom: 2px;
                    vertical-align: middle;
                }}
                #view-calendar .cal-cutoff-box {{
                    font-size: 10px;
                }}
                #view-calendar .cal-gm-badge {{
                    font-weight: 400;
                }}

                body[data-active-tab="draws"] .main-content {{
                    padding-left: 12px;
                    padding-right: 12px;
                }}
                #view-draws .draw-bracket-wrapper {{
                    overflow-x: auto;
                    overflow-y: auto;
                    max-height: calc(100vh - 110px);
                    padding-bottom: 12px;
                }}
                #view-draws .draw-bracket {{
                    width: auto;
                    min-width: max-content;
                    justify-content: normal;
                    padding: 6px;
                }}
                #view-draws .draw-round {{
                    min-width: 175px;
                    max-width: none;
                    flex: 0 1 auto;
                    padding-left: 10px;
                    padding-right: 10px;
                }}
                #view-draws .draw-match .draw-player {{
                    min-width: auto;
                    padding: 1px 3px;
                    min-height: 18px;
                    gap: 1px;
                    font-size: 10px;
                    font-weight: 400;
                }}
                #view-draws .draw-match .draw-player.winner {{
                    font-weight: 400;
                }}
                #view-draws .draw-player .seed-entry {{
                    width: 30px;
                }}
                #view-draws .draw-player .country {{
                    width: 16px;
                    min-width: 16px;
                }}
                #view-draws .draw-player .set-score {{
                    width: 16px;
                    font-size: 9px;
                }}
                #view-draws .draw-round-header {{
                    padding: 3px 0 6px;
                    font-size: 9px;
                    font-weight: 700;
                    letter-spacing: .5px;
                }}
                #view-draws #draws-tournament-select {{
                    min-width: 200px;
                    padding: 6px 24px 6px 8px;
                    border-radius: 8px !important;
                    font-size: 12px;
                    font-weight: 600;
                }}
                #view-draws .draw-type-btn {{
                    padding: 4px 10px;
                    font-size: 10px;
                    font-weight: 600;
                }}

                #view-rankings .header-row h1,
                #view-tstrength .header-row h1,
                #view-fedbcup .header-row h1 {{
                    width: 100%;
                    text-align: center;
                }}
                #view-rankings .rankings-date-picker,
                #view-rankings .rankings-date-select,
                #view-rankings .rankings-load-btn {{
                    border-radius: 0 !important;
                }}
                #view-rankings #rankings-toggle-btn {{
                    min-width: 88px;
                    padding-left: 12px;
                    padding-right: 12px;
                    gap: 7px;
                }}

                #view-fedbcup .bjkc-series-table td:nth-child(1),
                #view-fedbcup .bjkc-series-table th:nth-child(2),
                #view-fedbcup .bjkc-series-table td:nth-child(2),
                #view-fedbcup #national-table td:nth-child(2),
                #view-fedbcup #captains-table td:nth-child(2) {{
                    font-weight: 400 !important;
                }}
            }}

            @media (min-width: 769px) and (max-width: 1280px) {{
                #view-roadtogs .roadtogs-cutoffs {{
                    display: grid !important;
                    grid-template-columns: repeat(2, minmax(0, 1fr));
                    gap: 10px;
                }}
            }}

            @media (max-width: 768px) {{
                .mobile-app-header {{
                    background: #093366;
                }}
                .mobile-app-heading {{
                    align-items: center;
                    justify-content: center;
                    text-align: center;
                }}
                .mobile-app-heading small,
                #dark-mode-btn-mobile-nav {{
                    display: none !important;
                }}
                .mobile-app-heading strong {{
                    width: 100%;
                    line-height: 1.1;
                    text-align: center;
                }}
                .sidebar {{
                    position: fixed !important;
                    top: auto !important;
                    bottom: 0 !important;
                    display: block !important;
                    visibility: visible !important;
                    opacity: 1 !important;
                    transform: none !important;
                    z-index: 1100;
                }}
                .nav-primary {{
                    display: grid !important;
                }}

                #view-entrylists * {{
                    font-weight: 400 !important;
                }}
                #view-entrylists.entry-menu-collapsed .entry-menu {{
                    display: none !important;
                }}
                #view-entrylists .entry-menu-toggle-btn {{
                    width: 100%;
                    min-height: 36px;
                }}

                #view-roadtogs .roadtogs-controls > .roadtogs-title-row {{
                    display: none !important;
                }}
                #view-roadtogs .roadtogs-cutoffs {{
                    grid-template-columns: repeat(2, minmax(0, 1fr)) !important;
                    gap: 8px;
                    align-items: stretch;
                }}
                #view-roadtogs .gs-cutoff-table {{
                    font-size: 8px !important;
                }}
                #view-roadtogs .gs-cutoff-table th,
                #view-roadtogs .gs-cutoff-table td {{
                    height: auto !important;
                    min-height: 0 !important;
                    padding: 2px !important;
                    box-sizing: border-box;
                    font-size: 8px !important;
                    line-height: 1.15;
                    letter-spacing: 0;
                    text-transform: none;
                }}
                #view-roadtogs .gs-cutoff-table thead tr:first-child th {{
                    height: 20px !important;
                    padding: 1px 2px !important;
                    font-size: 8px !important;
                    white-space: nowrap;
                }}
                #view-roadtogs .gs-cutoff-table thead tr:last-child th {{
                    height: 22px !important;
                    font-size: 8px !important;
                    font-weight: 600 !important;
                    text-overflow: clip;
                }}
                #view-roadtogs .gs-cutoff-table col.gs-col-d {{ width: 12% !important; }}
                #view-roadtogs .gs-cutoff-table col.gs-col-cutoff {{ width: 28% !important; }}
                #view-roadtogs .gs-cutoff-table col.gs-col-acc {{ width: 28% !important; }}
                #view-roadtogs .gs-cutoff-table col.gs-col-est {{ width: 32% !important; }}
                #roadtogs-table thead th {{
                    height: 20px !important;
                    padding: 2px 3px !important;
                    box-sizing: border-box;
                    line-height: 1.1;
                }}

                #view-calendar .calendar-table thead th {{
                    height: 24px !important;
                    min-height: 0 !important;
                    padding: 2px 3px !important;
                    box-sizing: border-box;
                    line-height: 1;
                    vertical-align: middle;
                }}
                #view-calendar .cal-cutoff-box {{
                    font-size: 8px;
                }}
                #view-calendar .cal-gm-badge {{
                    font-weight: 400 !important;
                }}

                #view-upcoming * {{
                    font-weight: 400 !important;
                }}

                #history-table thead th {{
                    height: 24px !important;
                    min-height: 0 !important;
                    padding: 2px !important;
                    box-sizing: border-box;
                    line-height: 1.05;
                }}

                #view-draws .draw-match .draw-player.winner {{
                    font-weight: 400 !important;
                }}

                #view-rankings .search-container {{
                    flex: 1 1 0 !important;
                    width: auto !important;
                    min-width: 0;
                }}
                #view-rankings .rankings-btn-end {{
                    flex: 0 0 94px !important;
                    width: 94px;
                    min-width: 94px;
                }}
                #view-rankings #rankings-toggle-btn {{
                    width: 100%;
                    min-width: 94px;
                    padding-left: 9px;
                    padding-right: 9px;
                    gap: 6px;
                }}
                #view-rankings .rankings-date-picker,
                #view-rankings .rankings-date-select,
                #view-rankings .rankings-load-btn {{
                    border-radius: 0 !important;
                }}
                #view-rankings table thead th {{
                    height: 24px !important;
                    min-height: 0 !important;
                    padding: 2px !important;
                    box-sizing: border-box;
                    line-height: 1.05;
                }}
                #view-rankings tbody td {{
                    font-size: 8px !important;
                }}

                #view-tstrength #tstrength-table td.ts-gm,
                #view-tstrength #tstrength-table td.ts-hm {{
                    font-weight: 400 !important;
                }}

                #view-fedbcup .bjkc-series-table td:nth-child(1),
                #view-fedbcup .bjkc-series-table th:nth-child(2),
                #view-fedbcup .bjkc-series-table td:nth-child(2),
                #view-fedbcup #national-table td:nth-child(2),
                #view-fedbcup #captains-table td:nth-child(2) {{
                    font-weight: 400 !important;
                }}
            }}

            @media (max-width: 480px) {{
                #view-fedbcup #national-table th,
                #view-fedbcup #national-table td {{
                    padding-left: 0 !important;
                    padding-right: 0 !important;
                }}
            }}

                </style>
    </head>
    <body class="home-mode" onload="renderHistoryTable(); renderMilestonesTable();">
        <button class="mobile-menu-toggle" onclick="toggleMobileMenu()">\\u2630</button>
        <div class="app-container">
            <nav class="sidebar" id="sidebar" aria-label="Primary">
                <div class="sidebar-header">
                    <img class="sidebar-logo" src="assets/wtarg-app-icon.png" alt="WTARG">
                    <button class="dark-mode-btn" onclick="toggleDarkMode()" title="Toggle dark mode" aria-label="Toggle dark mode"><svg class="dm-icon dm-icon-moon" viewBox="0 0 24 24" aria-hidden="true"><path d="M21 12.79A9 9 0 1 1 11.21 3a7 7 0 0 0 9.79 9.79z" fill="currentColor"/></svg><svg class="dm-icon dm-icon-sun" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4" fill="currentColor"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg></button>
                </div>
                <button type="button" class="menu-item" id="btn-upcoming" onclick="switchTab('upcoming')">Schedule</button>
                <button type="button" class="menu-item" id="btn-entrylists" onclick="switchTab('entrylists')">Entry Lists</button>
                <button type="button" class="menu-item" id="btn-draws" onclick="switchTab('draws')">Draws</button>
                <button type="button" class="menu-item" id="btn-calendar" onclick="switchTab('calendar')">Calendar</button>
                <button type="button" class="menu-item" id="btn-rankings" onclick="switchTab('rankings')"><span class="desktop-only">WTA Rankings</span><span class="mobile-only">WTA Ranks</span></button>
                <button type="button" class="menu-item" id="btn-roadtogs" onclick="switchTab('roadtogs')"><span class="desktop-only">Points Breakdown</span><span class="mobile-only">Points Breakd.</span></button>
                <button type="button" class="menu-item" id="btn-history" onclick="switchTab('history')">Match History</button>
                <button type="button" class="menu-item" id="btn-fedbcup" onclick="switchTab('fedbcup')">Fed/BJK Cup</button>
                <button type="button" class="menu-item" id="btn-tstrength" onclick="switchTab('tstrength')">WTA TRN STR</button>
            </nav>

            <div class="main-content">
                <div id="view-home" class="single-layout">
                    <div class="home-hero">
                        <h1 class="home-title"><img class="home-logo" src="assets/wtarg-app-icon.png" alt="Women's Tennis Argentina"></h1>
                        <div class="home-grid">
                            <button class="home-btn" onclick="switchTab('upcoming')">
                                <img class="home-icon-img" src="assets/trophy.png" alt="Trophy icon" />
                                <span class="home-label">Schedule</span>
                            </button>
                            <button class="home-btn" onclick="switchTab('entrylists')">
                                <img class="home-icon-img" src="assets/files.png" alt="Files icon" />
                                <span class="home-label">Entry Lists</span>
                            </button>
                            <button class="home-btn" onclick="switchTab('draws')">
                                <img class="home-icon-img" src="assets/tournament.png" alt="Tournament icon" />
                                <span class="home-label">Draws</span>
                            </button>
                            <button class="home-btn" onclick="switchTab('calendar')">
                                <img class="home-icon-img" src="assets/calendar.png" alt="Calendar icon" />
                                <span class="home-label">Calendar</span>
                            </button>
                            <button class="home-btn" onclick="switchTab('rankings')">
                                <img class="home-icon-img" src="assets/list.png" alt="List icon" />
                                <span class="home-label">WTA Rankings</span>
                            </button>
                            <button class="home-btn" onclick="switchTab('roadtogs')">
                                <img class="home-icon-img" src="assets/data.png" alt="Data icon" />
                                <span class="home-label">Points Breakdown</span>
                            </button>
                            <button class="home-btn" onclick="switchTab('history')">
                                <img class="home-icon-img" src="assets/tennis-player.png" alt="Tennis player icon" />
                                <span class="home-label">Match History</span>
                            </button>
                            <button class="home-btn" onclick="switchTab('fedbcup')">
                                <img class="home-icon-img no-invert" src="assets/argentina.png" alt="Argentina flag icon" />
                                <span class="home-label">Fed/BJK Cup</span>
                            </button>
                            <button class="home-btn" onclick="switchTab('tstrength')">
                                <img class="home-icon-img" src="assets/score-board.png" alt="Analytics icon" />
                                <span class="home-label">WTA Tournament Strength</span>
                            </button>
                        </div>
                        <button class="home-dark-btn" id="home-dark-btn" onclick="toggleDarkMode()"><svg class="dm-icon dm-icon-moon" viewBox="0 0 24 24" aria-hidden="true"><path d="M21 12.79A9 9 0 1 1 11.21 3a7 7 0 0 0 9.79 9.79z" fill="currentColor"/></svg><svg class="dm-icon dm-icon-sun" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4" fill="currentColor"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg><span id="home-dark-label">Dark Mode</span></button>
                    </div>
                </div>

                <div id="view-upcoming" class="single-layout" style="display: none;">
                    <div class="header-row">
                        <h1>Schedule</h1>
                    </div>
                    <div class="content-card">
                        <div class="table-wrapper">
                            <table id="schedule-table">
                                <thead>
                                    <tr>
                                        <th class="sticky-col col-rank">Rank</th>
                                        <th class="sticky-col col-name">Player</th>
                                        {schedule_week_headers}
                                    </tr>
                                </thead>
                                <tbody id="tb">{table_rows}</tbody>
                            </table>
                        </div>
                    </div>
                </div>

                <div id="view-entrylists" style="display: none;">
                    <div class="entry-layout">
                        <div class="entry-menu">
                            <div class="entry-menu-header">Tournaments</div>
                            {entry_menu_html}
                        </div>
                        <div class="entry-content">
                            <div class="entry-menu-toggle-wrap">
                                <button type="button" id="btn-open-entry-menu" class="rankings-toggle-btn entry-menu-toggle-btn" hidden onclick="openEntryTournamentList()">Open tournament list</button>
                            </div>
                            <div class="header-row entry-header-row">
                                <div id="entry-strength" class="entry-strength"><span class="entry-gm-value">-</span></div>
                                <h1 id="entry-title">Entry List</h1>
                                <div class="rankings-filter-container">
                                    <button id="btn-prio1" class="rankings-toggle-btn" hidden onclick="togglePrio1()">Show Prio 1</button>
                                </div>
                            </div>
                            <div class="content-card">
                                <table id="entrylists-table">
                                    <thead>
                                        <tr>
                                            <th style="width:35px;min-width:35px">#</th>
                                            <th>PLAYER</th>
                                            <th id="entry-seed-header" style="width:35px;display:none">SEED</th>
                                            <th style="width:70px">E-Rank</th>
                                            <th id="entry-prio-header" style="width:35px;display:none">PRIO</th>
                                        </tr>
                                    </thead>
                                    <tbody id="entry-body"></tbody>
                                </table>
                            </div>
                        </div>
                    </div>
                </div>

                <div id="view-rankings" class="single-layout rankings-show-all" style="display: none;">
                    <div class="header-row">
                        <h1>WTA Rankings</h1>
                        <div class="rankings-controls">
                            <div class="search-container">
                                <input type="text" id="rankings-search" placeholder="Search player..." oninput="filterRankings()">
                            </div>
                            <div class="rankings-filter-container">
                                <div class="rankings-date-picker">
                                    <select id="rankings-year-select" class="rankings-date-select" onchange="onRankingYearChange(this.value)">{rankings_year_options}</select>
                                    <select id="rankings-month-select" class="rankings-date-select" onchange="onRankingMonthChange()"></select>
                                    <select id="rankings-day-select" class="rankings-date-select"></select>
                                    <button id="rankings-load-btn" class="rankings-load-btn" onclick="applyRankingSelection()">&#8594;</button>
                                </div>
                            </div>
                            <div class="rankings-btn-end">
                                <button id="rankings-toggle-btn" class="rankings-toggle-btn" onclick="toggleRankingsScope()">Show <img class="btn-flag-icon" src="assets/argentina.png" alt="ARG"></button>
                            </div>
                        </div>
                    </div>
                    <div class="content-card">
                        <div class="table-wrapper">
                            <table id="rankings-table">
                                <thead>
                                    <tr>
                                        <th style="width:55px">RANK</th>
                                        <th>PLAYER</th>
                                        <th style="width:70px">POINTS</th>
                                        <th style="width:100px">DOB</th>
                                    </tr>
                                </thead>
                                <tbody id="rankings-body">{rankings_rows}</tbody>
                            </table>
                        </div>
                    </div>
                </div>

                <div id="view-history" class="single-layout" style="display: none;">
                    <div class="history-layout">
                        <button type="button" class="history-filter-backdrop" aria-label="Close filters" tabindex="-1" onclick="closeHistoryFilters()"></button>
                        <div class="filter-panel" id="history-filter-panel">
                            <div class="history-filter-sheet-header">
                                <h3 id="history-filter-sheet-title">Filters</h3>
                                <button type="button" class="history-filter-sheet-close" aria-label="Close filters" onclick="closeHistoryFilters()">&times;</button>
                            </div>

                            <div class="filter-group collapsed">
                                <button type="button" class="filter-group-title" aria-expanded="false" aria-controls="filter-surface" onclick="toggleFilterGroup(this)">
                                    Surface <span class="collapse-icon" aria-hidden="true"></span>
                                </button>
                                <div class="filter-options" id="filter-surface"></div>
                            </div>

                            <div class="filter-group collapsed">
                                <button type="button" class="filter-group-title" aria-expanded="false" aria-controls="filter-round" onclick="toggleFilterGroup(this)">
                                    Round <span class="collapse-icon" aria-hidden="true"></span>
                                </button>
                                <div class="filter-options" id="filter-round"></div>
                            </div>

                            <div class="filter-group collapsed">
                                <button type="button" class="filter-group-title" aria-expanded="false" aria-controls="filter-result" onclick="toggleFilterGroup(this)">
                                    Result <span class="collapse-icon" aria-hidden="true"></span>
                                </button>
                                <div class="filter-options" id="filter-result"></div>
                            </div>

                            <div class="filter-group collapsed">
                                <button type="button" class="filter-group-title" aria-expanded="false" aria-controls="filter-year" onclick="toggleFilterGroup(this)">
                                    Year <span class="collapse-icon" aria-hidden="true"></span>
                                </button>
                                <div class="filter-options" id="filter-year"></div>
                            </div>

                            <div class="filter-group collapsed">
                                <button type="button" class="filter-group-title" aria-expanded="false" aria-controls="filter-tournament-container" onclick="toggleFilterGroup(this)">
                                    Tournament <span class="collapse-icon" aria-hidden="true"></span>
                                </button>
                                <div class="opponent-select-container" id="filter-tournament-container" style="padding: 8px; overflow: visible;">
                                    <select id="filter-tournament-select" style="width: 100%;">
                                        <option value="">All Tournaments</option>
                                    </select>
                                </div>
                            </div>

                            <div class="filter-group collapsed">
                                <button type="button" class="filter-group-title" aria-expanded="false" aria-controls="filter-category" onclick="toggleFilterGroup(this)">
                                    Category <span class="collapse-icon" aria-hidden="true"></span>
                                </button>
                                <div class="filter-options scrollable" id="filter-category"></div>
                            </div>

                            <div class="filter-group collapsed">
                                <button type="button" class="filter-group-title" aria-expanded="false" aria-controls="filter-opponent-container" onclick="toggleFilterGroup(this)">
                                    Opponent <span class="collapse-icon" aria-hidden="true"></span>
                                </button>
                                <div class="opponent-select-container" id="filter-opponent-container" style="padding: 8px; overflow: visible;">
                                    <select id="filter-opponent-select" style="width: 100%;">
                                        <option value="">All Opponents</option>
                                    </select>
                                </div>
                            </div>

                            <div class="filter-group collapsed">
                                <button type="button" class="filter-group-title" aria-expanded="false" aria-controls="filter-opponent-country" onclick="toggleFilterGroup(this)">
                                    Opp. Country <span class="collapse-icon" aria-hidden="true"></span>
                                </button>
                                <div class="filter-options" id="filter-opponent-country"></div>
                            </div>

                            <div class="filter-group collapsed">
                                <button type="button" class="filter-group-title" aria-expanded="false" aria-controls="filter-player-entry" onclick="toggleFilterGroup(this)">
                                    Player Entry <span class="collapse-icon" aria-hidden="true"></span>
                                </button>
                                <div class="filter-options" id="filter-player-entry"></div>
                            </div>

                            <div class="filter-group collapsed">
                                <button type="button" class="filter-group-title" aria-expanded="false" aria-controls="filter-seed" onclick="toggleFilterGroup(this)">
                                    Seed <span class="collapse-icon" aria-hidden="true"></span>
                                </button>
                                <div class="filter-options" id="filter-seed"></div>
                            </div>

                            <div class="filter-group collapsed">
                                <button type="button" class="filter-group-title" aria-expanded="false" aria-controls="filter-match-type" onclick="toggleFilterGroup(this)">
                                    Match Type <span class="collapse-icon" aria-hidden="true"></span>
                                </button>
                                <div class="filter-options" id="filter-match-type"></div>
                            </div>

                                <div class="rank-filter-last-row">
                                    <div class="filter-group collapsed">
                                    <button type="button" class="filter-group-title" aria-expanded="false" aria-controls="filter-as-rank-panel" onclick="toggleRankFilterGroup(this)">
                                        As Rank <span class="collapse-icon" aria-hidden="true"></span>
                                    </button>
                                    <div class="filter-options" id="filter-as-rank-panel" style="padding: 8px;">
                                        <div class="rank-filter-row">
                                            <input id="filter-as-rank" class="rank-filter-input" inputmode="numeric" placeholder="#" value="" oninput="this.value=this.value.replace(/\\D/g,''); applyHistoryFilters();">
                                            <select id="filter-as-rank-mode" class="rank-filter-mode" onchange="applyHistoryFilters();">
                                                <option value="higher">or Higher</option>
                                                <option value="lower">or Lower</option>
                                            </select>
                                        </div>
                                    </div>
                                </div>

                                <div class="filter-group collapsed">
                                    <button type="button" class="filter-group-title" aria-expanded="false" aria-controls="filter-vs-rank-panel" onclick="toggleRankFilterGroup(this)">
                                        VS Rank <span class="collapse-icon" aria-hidden="true"></span>
                                    </button>
                                    <div class="filter-options" id="filter-vs-rank-panel" style="padding: 8px;">
                                        <div class="rank-filter-row">
                                            <input id="filter-vs-rank" class="rank-filter-input" inputmode="numeric" placeholder="#" value="" oninput="this.value=this.value.replace(/\\D/g,''); applyHistoryFilters();">
                                            <select id="filter-vs-rank-mode" class="rank-filter-mode" onchange="applyHistoryFilters();">
                                                <option value="higher">or Higher</option>
                                                <option value="lower">or Lower</option>
                                            </select>
                                        </div>
                                    </div>
                                </div>
                            </div>

                            <div class="filter-actions">
                                <div class="filter-instructions">
                                    <span class="filter-instructions-desktop">Ctrl+Click to select multiple options.</span>
                                    <span class="filter-instructions-mobile">Tap to add or remove filter options.</span>
                                </div>
                                <button class="filter-btn filter-btn-clear" onclick="clearHistoryFilters()">Reset Filters</button>
                            </div>
                        </div>

                        <div class="history-content">
                            <div id="history-match-page" class="history-subpage">
                                <div class="table-header-section">
                                    <div class="history-summary-container">
                                        <span id="history-wl-counter" class="history-wl-counter">Matches: 0 (0-0)</span>
                                    </div>
                                    <h1 class="table-title">Match History</h1>
                                    <div class="history-header-actions history-header-actions-player">
                                        <div class="player-select-container">
                                            <select id="playerHistorySelect">
                                                <option value="">Select Player...</option>
                                                <option value="__ALL__">ALL PLAYERS</option>
                                                {"".join([f'<option value="{name}">{name}</option>' for name in history_players_sorted])}
                                            </select>
                                        </div>
                                        <button class="history-nav-btn" onclick="setHistorySubpage('milestones')">Milestones</button>
                                    </div>
                                </div>

                                <div class="history-mobile-filter-bar">
                                    <button type="button" class="history-mobile-filter-btn" id="history-mobile-filter-btn" aria-controls="history-filter-panel" aria-expanded="false" onclick="openHistoryFilters()">
                                        <span id="history-mobile-filter-label">Filters</span>
                                    </button>
                                </div>

                                <div class="content-card">
                                    <div class="table-wrapper">
                                        <table id="history-table">
                                            <thead id="history-head"></thead>
                                            <tbody id="history-body">
                                                <tr><td colspan="100%" class="cell-state-info">Select a player to view their matches</td></tr>
                                            </tbody>
                                        </table>
                                    </div>
                                    <div id="history-pagination" style="display:none; justify-content:center; align-items:center; gap:12px; padding:12px; font-size:0.85rem;"></div>
                                </div>
                            </div>

                            <div id="history-milestones-page" class="history-subpage" style="display: none;">
                                <div class="table-header-section">
                                    <h1 class="table-title">Milestones</h1>
                                    <div class="milestones-header-actions">
                                        <button class="history-nav-btn" onclick="setHistorySubpage('match')">Match History</button>
                                    </div>
                                </div>

                                <div class="content-card">
                                    <div class="milestones-filter-panel">
                                        <h3>Filters</h3>
                                        <div class="milestones-filter-body" id="milestones-filter-body"></div>
                                    </div>

                                    <div class="milestones-main-column">
                                        <div class="table-wrapper">
                                            <table id="milestones-table">
                                                <thead>
                                                    <tr>
                                                        <th>PLAYER</th>
                                                        <th>WINS</th>
                                                    </tr>
                                                </thead>
                                                <tbody id="milestones-body">
                                                    <tr><td colspan="2" class="cell-state-info">Open Milestones to view the leaderboard</td></tr>
                                                </tbody>
                                            </table>
                                        </div>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>

                <div id="view-fedbcup" class="single-layout fedbcup-series-active" style="display: none;">
                    <div class="header-row">
                        <h1>Fed/BJK Cup</h1>
                    </div>
                    <div class="fedbcup-header-controls">
                        <div class="fedbcup-filter-left" id="fedbcup-filter-left">
                            <select id="fedbcup-player-filter" onchange="filterFedBjkPlayer()">
                                <option value="">All Players</option>
                            </select>
                        </div>
                        <div class="fedbcup-toggle-row">
                            <button class="fedbcup-btn active" id="fedbcup-btn-series" onclick="switchFedBjkTab('series')">Series</button>
                            <button class="fedbcup-btn" id="fedbcup-btn-players" onclick="switchFedBjkTab('players')">Player Debuts</button>
                            <button class="fedbcup-btn" id="fedbcup-btn-captains" onclick="switchFedBjkTab('captains')">Captain Debuts</button>
                        </div>
                        <div class="fedbcup-record-right" id="fedbcup-record-right">
                            <span id="fedbcup-record" class="fedbcup-record-text"></span>
                        </div>
                    </div>
                    <div id="fedbcup-view-players" class="content-card" style="display: none;">
                        <div class="table-wrapper">
                            <table id="national-table">
                                <thead>
                                    <tr>
                                        {national_header_html}
                                    </tr>
                                </thead>
                                <tbody id="national-body">{national_rows}</tbody>
                            </table>
                        </div>
                    </div>
                    <div id="fedbcup-view-captains" class="content-card" style="display: none;">
                        <div class="table-wrapper">
                            <table id="captains-table">
                                <thead>
                                    <tr>
                                        {captains_header_html}
                                    </tr>
                                </thead>
                                <tbody id="captains-body">{captains_rows}</tbody>
                            </table>
                        </div>
                    </div>
                    <div id="fedbcup-view-series">
                        {bjkc_series_html}
                    </div>
                </div>

                <div id="view-tstrength" class="single-layout" style="display: none;">
                    <div class="header-row">
                        <h1>WTA Tournament Strength</h1>
                    </div>
                    <div class="ts-controls">
                        <div class="ts-row1"><button id="ts-sort-toggle" onclick="tsToggleSort()">Order by Strength</button>
                        <button id="ts-view-toggle" onclick="tsToggleView()">View Qualy</button>
                        <select id="ts-filter-year" onchange="tsRender()"><option value="2026">2026</option><option value="2025">2025</option></select></div>
                        <div class="ts-row2"><select id="ts-filter-level" onchange="tsRender()"><option value="">All Levels</option><option value="WTA 1000">WTA 1000</option><option value="WTA 500">WTA 500</option><option value="WTA 250">WTA 250</option><option value="WTA 125">WTA 125</option></select>
                        <select id="ts-filter-surface" onchange="tsRender()"><option value="">All Surfaces</option><option value="Hard">Hard</option><option value="Clay">Clay</option><option value="Grass">Grass</option></select>
                        <select id="ts-filter-region" onchange="tsRender()"><option value="">All Regions</option><option value="Europe">Europe</option><option value="North America">North America</option><option value="South America">South America</option><option value="Asia">Asia</option><option value="Middle East">Middle East</option><option value="Oceania">Oceania</option><option value="Africa">Africa</option></select></div>
                    </div>
                    <div class="ts-explanation">
                        <p><strong>GM</strong> (Geometric Mean): Balanced measure of overall draw quality across all players.</p>
                        <p><strong>HM</strong> (Harmonic Mean): Weighted toward top-ranked players. Reflects star power in the draw.</p>
                    </div>
                    <div class="tstrength-wrapper">
                        <table id="tstrength-table">
                            <thead><tr><th>#</th><th>GM</th><th>HM</th><th>Date</th><th>Tournament</th><th>Level</th><th>Surface</th><th>Region</th><th>Draw</th></tr></thead>
                            <tbody id="tstrength-tbody"></tbody>
                        </table>
                    </div>
                    <script>
                    (function() {{
                        var tsData = {tstrength_json_str};
                        var tsSort = 'date';
                        var tsView = 'MD'; // 'MD' or 'Q'
                        window.__wtargTStrengthSort = tsSort;
                        window.__wtargTStrengthView = tsView;
                        var levelColors = {{"WTA 1000":"#d946ef55","WTA 500":"#aa00ff88","WTA 250":"#0055ff88","WTA 125":"#ffaa0088"}};
                        var surfaceColors = {{"Hard":"#0055ff88","Clay":"#ff550088","Grass":"#00bb3388","Carpet":"#aa00ff88"}};
                        var regionColors = {{"Europe":"#0055ff88","North America":"#ff111188","South America":"#00bb3388","Asia":"#ffaa0088","Oceania":"#aa00ff88","Middle East":"#ff660088","Africa":"#ff330088"}};

                        function tsGradient(val, minV, maxV) {{
                            if (maxV <= minV) return '#f1f5f9';
                            var t = (val - minV) / (maxV - minV);
                            t = Math.max(0, Math.min(1, t));
                            var r, g, b;
                            if (t < 0.5) {{
                                var p = t * 2;
                                r = Math.round(0 + p * (255 - 0));
                                g = Math.round(200 + p * (220 - 200));
                                b = Math.round(0 + p * (0 - 0));
                            }} else {{
                                var p = (t - 0.5) * 2;
                                r = Math.round(255 + p * (220 - 255));
                                g = Math.round(220 + p * (0 - 220));
                                b = Math.round(0 + p * (0 - 0));
                            }}
                            return 'rgba(' + r + ',' + g + ',' + b + ',0.50)';
                        }}

                        window.tsToggleSort = function() {{
                            tsSort = tsSort === 'strength' ? 'date' : 'strength';
                            window.__wtargTStrengthSort = tsSort;
                            document.getElementById('ts-sort-toggle').textContent = tsSort === 'strength' ? 'Order by Date' : 'Order by Strength';
                            tsRender();
                        }};

                        function tsUpdateViewToggle() {{
                            var btn = document.getElementById('ts-view-toggle');
                            if (!btn) return;
                            btn.textContent = (tsView === 'MD') ? 'View Qualy' : 'View MD';
                        }}

                        window.tsToggleView = function() {{
                            tsView = (tsView === 'MD') ? 'Q' : 'MD';
                            window.__wtargTStrengthView = tsView;
                            tsUpdateViewToggle();
                            tsRender();
                        }};

                        window.__restoreTStrengthState = function(params) {{
                            function setSelectFromParam(id, paramName) {{
                                var el = document.getElementById(id);
                                if (!el || !params.has(paramName)) return;
                                if (window.setSelectValueFromSlug) {{
                                    setSelectValueFromSlug(el, params.get(paramName));
                                    return;
                                }}
                                el.value = params.get(paramName) || el.value;
                            }}
                            setSelectFromParam('ts-filter-year', 'year');
                            setSelectFromParam('ts-filter-level', 'level');
                            setSelectFromParam('ts-filter-surface', 'surface');
                            setSelectFromParam('ts-filter-region', 'region');
                            var draw = (params.get('draw') || '').toUpperCase();
                            if (draw === 'Q' || draw === 'QUALY') tsView = 'Q';
                            else if (draw === 'MD' || draw === 'M') tsView = 'MD';
                            var sort = (params.get('sort') || '').toLowerCase();
                            if (sort === 'strength' || sort === 'date') tsSort = sort;
                            window.__wtargTStrengthSort = tsSort;
                            window.__wtargTStrengthView = tsView;
                            document.getElementById('ts-sort-toggle').textContent = tsSort === 'strength' ? 'Order by Date' : 'Order by Strength';
                            tsUpdateViewToggle();
                            tsRender();
                        }};

                        window.tsRender = function() {{
                            var fy = document.getElementById('ts-filter-year').value;
                            var fl = document.getElementById('ts-filter-level').value;
                            var fs = document.getElementById('ts-filter-surface').value;
                            var fr = document.getElementById('ts-filter-region').value;
                            var filtered = tsData.filter(function(t) {{
                                if ((t.year || '2025') !== fy) return false;
                                if (fl && t.level !== fl) return false;
                                if (fs && t.surface !== fs) return false;
                                if (fr && t.region !== fr) return false;
                                var d = (t.draw || 'MD');
                                if (tsView === 'Q') return d === 'Q' || d === 'QUALY';
                                return d === 'MD' || d === 'M' || d === 'MAIN';
                            }});
                            if (tsSort === 'strength') {{
                                filtered.sort(function(a, b) {{ return a.gm - b.gm; }});
                            }} else {{
                                filtered.sort(function(a, b) {{ return a.startDate < b.startDate ? -1 : a.startDate > b.startDate ? 1 : 0; }});
                            }}
                            var gmVals = filtered.map(function(t) {{ return t.gm; }});
                            var hmVals = filtered.map(function(t) {{ return t.hm; }});
                            var gmMin = Math.min.apply(null, gmVals), gmMax = Math.max.apply(null, gmVals);
                            var hmMin = Math.min.apply(null, hmVals), hmMax = Math.max.apply(null, hmVals);
                            var tbody = document.getElementById('tstrength-tbody');
                            var html = '';
                            var isMobile = window.innerWidth <= 768;
                            var regionShort = {{"North America":"NA","South America":"SA","Central America":"CA","Caribbean":"Carib","Middle East":"ME","Europe":"EU","Asia":"AS","Oceania":"OC","Africa":"AF"}};
                            var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
                            function ordinal(d) {{ var s = ['th','st','nd','rd']; var v = d % 100; return d + (s[(v-20)%10] || s[v] || s[0]); }}
                            function fmtDate(ds) {{
                                var p = ds.split('-'); var m = parseInt(p[1],10)-1; var d = parseInt(p[2],10);
                                return months[m] + ' ' + ordinal(d);
                            }}
                            function cleanName(n) {{
                                var cleaned = n.replace(/\\s*\\d{{3}}\\s*/g, ' ').replace(/\\s+/g,' ').trim();
                                var hashMatch = n.match(/#\\d+/);
                                if (hashMatch && cleaned.indexOf(hashMatch[0]) === -1) cleaned += ' ' + hashMatch[0];
                                return cleaned;
                            }}
                            for (var i = 0; i < filtered.length; i++) {{
                                var t = filtered[i];
                                var lc = levelColors[t.level] || '';
                                var sc = surfaceColors[t.surface] || '';
                                var rc = regionColors[t.region] || '';
                                var gmBg = tsGradient(t.gm, gmMin, gmMax);
                                var hmBg = tsGradient(t.hm, hmMin, hmMax);
                                var dateStr = fmtDate(t.startDate);
                                var levelStr = isMobile ? t.level.replace('WTA ','') : t.level;
                                var regionStr = isMobile ? (regionShort[t.region] || t.region || '') : (t.region || '');
                                var nameStr = cleanName(t.name);
                                html += '<tr>';
                                html += '<td class="ts-rank-num">' + (i + 1) + '</td>';
                                html += '<td class="ts-gm" style="background:' + gmBg + '">' + t.gm + '</td>';
                                html += '<td class="ts-hm" style="background:' + hmBg + '">' + t.hm + '</td>';
                                html += '<td>' + dateStr + '</td>';
                                html += '<td class="ts-name">' + nameStr + '</td>';
                                html += '<td style="background:' + lc + '">' + levelStr + '</td>';
                                html += '<td style="background:' + sc + '">' + t.surface + '</td>';
                                html += '<td style="background:' + rc + '">' + regionStr + '</td>';
                                html += '<td>' + t.playerCount + '</td>';
                                html += '</tr>';
                            }}
                            tbody.innerHTML = html;
                            window.__wtargTStrengthSort = tsSort;
                            window.__wtargTStrengthView = tsView;
                            if (window.syncUrlStateForTab) window.syncUrlStateForTab('tstrength');
                        }};
                        tsUpdateViewToggle();
                        tsRender();
                    }})();
                    </script>
                </div>

                <div id="view-calendar" class="single-layout" style="display: none;">
                    <div class="calendar-toolbar" id="calendar-toolbar">
                        <div class="cal-dd" data-cal-dd="categories">
                            <button type="button" class="cal-dd-btn" data-cal-dd-btn aria-expanded="false">
                                Categories
                            </button>
                        <div class="cal-dd-panel" role="menu">
                            <label class="cal-dd-item"><input type="checkbox" data-cal-filter-toggle="gs" checked><span>GS</span></label>
                            <label class="cal-dd-item"><input type="checkbox" data-cal-filter-toggle="wta_tour" checked><span>WTA TOUR</span></label>
                            <label class="cal-dd-item"><input type="checkbox" data-cal-filter-toggle="wta125" checked><span>WTA 125</span></label>
                                <label class="cal-dd-item"><input type="checkbox" data-cal-filter-toggle="w100" checked><span>W100</span></label>
                                <label class="cal-dd-item"><input type="checkbox" data-cal-filter-toggle="w75" checked><span>W75</span></label>
                                <label class="cal-dd-item"><input type="checkbox" data-cal-filter-toggle="w50" checked><span>W50</span></label>
                                <label class="cal-dd-item"><input type="checkbox" data-cal-filter-toggle="w35" checked><span>W35</span></label>
                                <label class="cal-dd-item"><input type="checkbox" data-cal-filter-toggle="w15" checked><span>W15</span></label>
                            </div>
                        </div>

                        <div class="cal-dd" data-cal-dd="region">
                            <button type="button" class="cal-dd-btn" data-cal-dd-btn aria-expanded="false">
                                Region
                            </button>
                            <div class="cal-dd-panel" role="menu">
                                <label class="cal-dd-item"><input type="checkbox" data-cal-continent-toggle="south_america" checked><span>S America</span></label>
                                <label class="cal-dd-item"><input type="checkbox" data-cal-continent-toggle="north_central_america" checked><span>N/C America</span></label>
                                <label class="cal-dd-item"><input type="checkbox" data-cal-continent-toggle="europe" checked><span>Europe</span></label>
                                <label class="cal-dd-item"><input type="checkbox" data-cal-continent-toggle="africa" checked><span>Africa</span></label>
                                <label class="cal-dd-item"><input type="checkbox" data-cal-continent-toggle="asia" checked><span>Asia</span></label>
                                <label class="cal-dd-item"><input type="checkbox" data-cal-continent-toggle="oceania" checked><span>Oceania</span></label>
                            </div>
                        </div>

                        <div class="cal-dd" data-cal-dd="surface">
                            <button type="button" class="cal-dd-btn" data-cal-dd-btn aria-expanded="false">
                                Surface
                            </button>
                            <div class="cal-dd-panel" role="menu">
                                <label class="cal-dd-item"><input type="checkbox" data-cal-surface-toggle="hard" checked><span>Hard</span></label>
                                <label class="cal-dd-item"><input type="checkbox" data-cal-surface-toggle="clay" checked><span>Clay</span></label>
                                <label class="cal-dd-item"><input type="checkbox" data-cal-surface-toggle="grass" checked><span>Grass</span></label>
                                <label class="cal-dd-item"><input type="checkbox" data-cal-surface-toggle="carpet" checked><span>Carpet</span></label>
                            </div>
                        </div>

                        <button type="button" class="calendar-gm-toggle active" id="calendar-gm-toggle"
                                aria-pressed="true" aria-label="Hide draw quality" title="Hide draw quality">
                            Hide Quality
                        </button>

                        <div class="cal-gm-legend">
                            <span class="cal-gm-legend-badge">99.9</span> Draw quality from last year's edition of the tournament.
                        </div>
                    </div>

                    <div class="content-card calendar-container">
                        <div class="table-wrapper" tabindex="0" aria-label="Calendar table">
                            {calendar_html}
                        </div>
                    </div>
                </div>

                <div id="view-roadtogs" class="single-layout" style="display: none;">
                    <div class="header-row">
                        <h1>Points Breakdown</h1>
                    </div>
                    <div class="roadtogs-controls">
                        <div class="player-select-container">
                            <select id="roadtogsPlayerSelect">
                                <option value="">Select Player...</option>
                                {"".join([f'<option value="{escape(name, quote=True)}">{escape(name)}</option>' for name in roadtogs_players_sorted])}
                            </select>
                        </div>
                        <div id="roadtogs-points-total" style="font-size: 16px; font-weight: bold; padding-right: 12px;">Points: 0</div>
                    </div>
                    <div class="roadtogs-cutoffs">
                        {gs_tables_html}
                    </div>
                    <details class="roadtogs-info">
                        <summary class="roadtogs-info-summary">
                            <span class="roadtogs-info-summary-label"><span class="roadtogs-info-icon" aria-hidden="true">i</span>Grand Slams information</span>
                        </summary>
                        <div class="roadtogs-info-panel">
                            <div class="roadtogs-info-col roadtogs-info-legend">
                                <div class="roadtogs-info-line"><span class="roadtogs-info-term">ACC. PTS</span><span>Points accumulated that count towards the ranking as of the cutoff date.</span></div>
                                <div class="roadtogs-info-line"><span class="roadtogs-info-term">EST. NEED</span><span>Estimated points needed to qualify for the Grand Slam: {GS_THRESHOLD_Q} for Q, {GS_THRESHOLD_MD} for MD.</span></div>
                            </div>
                            <div class="roadtogs-info-col roadtogs-info-cutoffs">
                                <div>Cutoffs 2025 (Q/MD) = <span style="color:#0066B3;font-weight:700;">AO 316/756</span> - <span style="color:#C8602A;font-weight:700;">RG 326/742</span> - <span style="color:#3D7A3D;font-weight:700;">WB 315/727</span> - <span style="color:#003087;font-weight:700;">US 323/730</span></div>
                                <div>Cutoffs 2026 (Q/MD) = <span style="color:#0066B3;font-weight:700;">AO 308/754</span> - <span style="color:#C8602A;font-weight:700;">RG 283/786</span> - <span style="color:#3D7A3D;font-weight:700;">WB 315/736</span></div>
                            </div>
                        </div>
                    </details>
                    <div class="content-card">
                        <div class="table-wrapper">
                            <table id="roadtogs-table">
                                <colgroup>
                                    <col class="rtgs-col-date">
                                    <col class="rtgs-col-tournament">
                                    <col class="rtgs-col-round">
                                    <col class="rtgs-col-points">
                                    <col class="rtgs-col-drop-date">
                                </colgroup>
                                <thead>
                                    <tr>
                                        <th>Date</th>
                                        <th>Tournament</th>
                                        <th>Round</th>
                                        <th>PTS</th>
                                        <th>Drop Date</th>
                                    </tr>
                                </thead>
                                <tbody id="roadtogs-body">
                                    <tr><td colspan="5" class="cell-state-info">Select a player to view their results</td></tr>
                                </tbody>
                            </table>
                        </div>
                    </div>
                </div>

                <div id="view-draws" class="single-layout" style="display: none;">
                    <div class="draws-layout">
                        <div class="draws-toolbar">
                            <select id="draws-tournament-select" onchange="onDrawTournamentChange(this.value)">
                                {draws_dropdown_html}
                            </select>
                            <div class="draws-type-btns" id="draws-type-btns"></div>
                            <span style="font-size:9px;color:#94a3b8;">Click a round header to filter</span>
                            <span style="flex:1;"></span>
                            <button class="draw-filter-reset" id="draw-filter-reset" onclick="resetDrawFilter()">Show Full Draw</button>
                        </div>
                        <div class="draw-bracket-wrapper" id="draw-bracket-wrapper">
                            <div class="draw-bracket" id="draw-bracket"></div>
                        </div>
                    </div>
                </div>

            </div>
        </div>
        <script src="data/player_aliases_wta_itf_bundle.js"></script>
        <script>
            const tournamentData = {_script_safe_json(tournament_store)};
            const _localScriptPromises = {{}};
            function _loadLocalScriptOnce(src) {{
                if (_localScriptPromises[src]) return _localScriptPromises[src];
                _localScriptPromises[src] = new Promise((resolve, reject) => {{
                    if (!src) {{
                        reject(new Error('Missing script src'));
                        return;
                    }}
                    const script = document.createElement('script');
                    script.src = src;
                    script.async = true;
                    script.dataset.localBundle = src;
                    script.onload = () => {{
                        resolve();
                    }};
                    script.onerror = () => {{
                        _localScriptPromises[src] = null;
                        reject(new Error('Failed to load ' + src));
                    }};
                    document.head.appendChild(script);
                }});
                return _localScriptPromises[src];
            }}

            const URL_STATE_TABS = new Set([
                'home', 'upcoming', 'entrylists', 'draws', 'calendar', 'rankings',
                'roadtogs', 'history', 'fedbcup', 'tstrength'
            ]);
            let _currentTabName = 'home';
            let _urlStateApplying = false;
            let _urlStateSwitching = false;
            let _urlStateRestoreSeq = 0;
            let _urlStateLastTrackRequest = '';

            function normalizeUrlPath(path) {{
                let out = (path || '/').toString().replace(/\\/+$/g, '/');
                out = out.startsWith('/') ? out : ('/' + out);
                out = out.replace(/\\/+/g, '/');
                return out;
            }}

            function getUrlBasePath() {{
                if (window.SITE_BASE_PATH) return window.SITE_BASE_PATH;
                const baseEl = document.querySelector('base');
                if (!baseEl) return '/';
                try {{
                    const u = new URL(baseEl.href, location.origin);
                    let p = normalizeUrlPath(u.pathname || '/');
                    if (!p.endsWith('/')) p += '/';
                    return p;
                }} catch (e) {{
                    return '/';
                }}
            }}

            function tabPathForUrlState(tabName) {{
                const base = getUrlBasePath();
                const tab = (tabName || 'home').toString().trim().toLowerCase();
                if (!URL_STATE_TABS.has(tab) || tab === 'home') return base;
                return normalizeUrlPath(base + tab + '/');
            }}

            function trackedFullUrl() {{
                return location.pathname + location.search + (location.hash || '');
            }}

            function slugStateValue(value) {{
                const raw = (value == null ? '' : String(value)).trim();
                if (!raw) return '';
                return raw
                    .normalize('NFD')
                    .replace(/[\\u0300-\\u036f]/g, '')
                    .toLowerCase()
                    .replace(/&/g, ' and ')
                    .replace(/[^a-z0-9]+/g, '-')
                    .replace(/^-+|-+$/g, '')
                    .replace(/-+/g, '-');
            }}

            function deslugSearchValue(value) {{
                return (value || '').toString().trim().replace(/-/g, ' ');
            }}

            function stateSlugMatches(value, slug) {{
                const wanted = slugStateValue(slug);
                if (!wanted) return false;
                const raw = (value == null ? '' : String(value)).trim().toLowerCase();
                return raw === wanted || slugStateValue(value) === wanted;
            }}

            function entryTournamentStateSlugFromKey(key) {{
                const raw = (key == null ? '' : String(key)).trim();
                if (!raw) return '';
                const wta = raw.match(/\\/tournaments\\/([^\\/?#]+)\\/([^\\/?#]+)\\/([^\\/?#]+)\\/player-list/i);
                if (wta) return slugStateValue(wta[1] + '-' + wta[2] + '-' + wta[3]);
                return slugStateValue(raw);
            }}

            function readUrlParams() {{
                try {{
                    return new URLSearchParams(location.search || '');
                }} catch (e) {{
                    return new URLSearchParams();
                }}
            }}

            function splitUrlStateList(value) {{
                const raw = (value || '').toString().trim();
                if (!raw) return [];
                if (raw === 'none') return [];
                return raw.split(',').map(part => slugStateValue(part)).filter(Boolean);
            }}

            function setStateParam(params, key, value, options = {{}}) {{
                if (value == null) return;
                if (Array.isArray(value)) {{
                    const items = value.map(v => slugStateValue(v)).filter(Boolean);
                    if (!items.length) return;
                    params.set(key, items.join(','));
                    return;
                }}
                const raw = String(value).trim();
                if (!raw) return;
                params.set(key, options.raw ? raw : slugStateValue(raw));
            }}

            function writeUrlStateForTab(tabName, state, options = {{}}) {{
                const tab = (tabName || _currentTabName || 'home').toString().trim().toLowerCase();
                if (!URL_STATE_TABS.has(tab)) return;
                const params = new URLSearchParams();
                Object.keys(state || {{}}).forEach(key => {{
                    const value = state[key];
                    const raw = key === 'q' || key === 'date' || key === 'asrank' || key === 'vsrank' || key === 'type' || key === 'draw';
                    setStateParam(params, key, value, {{ raw }});
                }});
                const query = params.toString().replace(/%2C/g, ',');
                const querySuffix = query ? ('?' + query) : '';
                const isLocalFile = location.protocol === 'file:';
                // A file:// page may only replace history with the same physical
                // file. Pointing it at /rankings/ raises a SecurityError after the
                // rankings have rendered, which previously looked like a data-load
                // failure. Keep local state on app.html and use the tab hash.
                const target = isLocalFile
                    ? location.href.split(/[?#]/)[0] + querySuffix + '#' + tab
                    : tabPathForUrlState(tab) + querySuffix;
                const current = isLocalFile
                    ? location.href
                    : location.pathname + location.search;
                if (current !== target) {{
                    try {{
                        history.replaceState(null, '', target);
                    }} catch (err) {{
                        // URL synchronization is optional and must never break data rendering.
                        console.warn('URL state update skipped:', err);
                    }}
                }}
                if (options.track !== false) trackCurrentUrlState();
            }}

            function trackCurrentUrlState() {{
                if (_urlStateApplying) return;
                const full = trackedFullUrl();
                if (_urlStateLastTrackRequest === full) return;
                _urlStateLastTrackRequest = full;
                if (window.trackVisit) window.trackVisit(full);
            }}

            function syncUrlStateForTab(tabName, options = {{}}) {{
                const tab = (tabName || _currentTabName || 'home').toString().trim().toLowerCase();
                if (_urlStateApplying || _urlStateSwitching) return;
                if (tab !== _currentTabName) return;
                writeUrlStateForTab(tab, collectUrlStateForTab(tab), options);
            }}
            window.syncUrlStateForTab = syncUrlStateForTab;

            function restoreAndSyncUrlStateForTab(tabName) {{
                const tab = (tabName || _currentTabName || 'home').toString().trim().toLowerCase();
                const seq = ++_urlStateRestoreSeq;
                Promise.resolve(restoreUrlStateForTab(tab))
                    .catch(err => console.warn('URL filter restore skipped:', err))
                    .finally(() => {{
                        if (seq === _urlStateRestoreSeq) syncUrlStateForTab(tab, {{ track: true }});
                    }});
            }}

            function findSelectValueBySlug(select, slug) {{
                if (!select || !slug) return '';
                const wanted = slugStateValue(slug);
                const options = Array.from(select.options || []);
                const exact = options.find(opt => (opt.value || '').toString().trim().toLowerCase() === wanted);
                if (exact) return exact.value;
                const byValue = options.find(opt => stateSlugMatches(opt.value, wanted));
                if (byValue) return byValue.value;
                const byText = options.find(opt => stateSlugMatches(opt.textContent, wanted));
                return byText ? byText.value : '';
            }}

            function setSelectValueFromSlug(select, slug) {{
                const value = findSelectValueBySlug(select, slug);
                if (!value && slugStateValue(slug)) return false;
                select.value = value;
                if (window.jQuery && $(select).data('select2')) {{
                    $(select).val(value).trigger('change.select2');
                }}
                return true;
            }}
            window.setSelectValueFromSlug = setSelectValueFromSlug;

            function getSelectedCheckboxValues(selector, attrName) {{
                return Array.from(document.querySelectorAll(selector))
                    .filter(cb => cb.checked)
                    .map(cb => cb.getAttribute(attrName) || '')
                    .filter(Boolean);
            }}

            function collectCheckboxGroupState(selector, attrName) {{
                const toggles = Array.from(document.querySelectorAll(selector));
                if (!toggles.length) return null;
                const selected = toggles
                    .filter(cb => cb.checked)
                    .map(cb => cb.getAttribute(attrName) || '')
                    .filter(Boolean);
                if (selected.length === toggles.length) return null;
                return selected.length ? selected : 'none';
            }}

            function restoreCheckboxGroupState(params, key, selector, attrName) {{
                if (!params.has(key)) return;
                const raw = params.get(key) || '';
                const selected = new Set(splitUrlStateList(raw));
                document.querySelectorAll(selector).forEach(cb => {{
                    const value = cb.getAttribute(attrName) || '';
                    cb.checked = raw === 'none' ? false : selected.has(slugStateValue(value));
                }});
            }}

            function collectUpcomingUrlState() {{
                const input = document.getElementById('s');
                return input && input.value.trim() ? {{ q: slugStateValue(input.value) }} : {{}};
            }}

            function restoreUpcomingUrlState(params) {{
                if (!params.has('q')) return;
                const input = document.getElementById('s');
                if (!input) return;
                input.value = deslugSearchValue(params.get('q'));
                filter();
            }}

            function collectEntryListUrlState() {{
                const active = document.querySelector('#view-entrylists .entry-menu-item.active');
                const state = {{}};
                if (active) state.t = entryTournamentStateSlugFromKey(active.getAttribute('data-key')) || entryMenuNameForItem(active);
                if (_prioFilterActive) state.prio = '1';
                return state;
            }}

            function restoreEntryListUrlState(params) {{
                const tSlug = params.get('t');
                if (tSlug) {{
                    const item = Array.from(document.querySelectorAll('#view-entrylists .entry-menu-item')).find(el => {{
                        const key = el.getAttribute('data-key') || '';
                        return stateSlugMatches(entryTournamentStateSlugFromKey(key), tSlug)
                            || stateSlugMatches(key, tSlug)
                            || stateSlugMatches(entryMenuNameForItem(el), tSlug);
                    }});
                    if (item) selectEntryTournament(item);
                }}
                if (params.has('prio')) {{
                    _prioFilterActive = ['1', 'true', 'yes'].includes((params.get('prio') || '').toLowerCase());
                    updateEntryList();
                }}
            }}

            function collectCalendarUrlState() {{
                const state = {{}};
                const levels = collectCheckboxGroupState('[data-cal-filter-toggle]', 'data-cal-filter-toggle');
                const continents = collectCheckboxGroupState('[data-cal-continent-toggle]', 'data-cal-continent-toggle');
                const surfaces = collectCheckboxGroupState('[data-cal-surface-toggle]', 'data-cal-surface-toggle');
                const gmToggle = document.getElementById('calendar-gm-toggle');
                if (levels !== null) state.level = levels;
                if (continents !== null) state.continent = continents;
                if (surfaces !== null) state.surface = surfaces;
                if (gmToggle && gmToggle.getAttribute('aria-pressed') === 'false') state.gm = '0';
                return state;
            }}

            function restoreCalendarUrlState(params) {{
                restoreCheckboxGroupState(params, 'level', '[data-cal-filter-toggle]', 'data-cal-filter-toggle');
                restoreCheckboxGroupState(params, 'continent', '[data-cal-continent-toggle]', 'data-cal-continent-toggle');
                restoreCheckboxGroupState(params, 'surface', '[data-cal-surface-toggle]', 'data-cal-surface-toggle');
                const gmToggle = document.getElementById('calendar-gm-toggle');
                if (gmToggle && params.has('gm')) {{
                    const showGm = !['0', 'false', 'no', 'off'].includes((params.get('gm') || '').toLowerCase());
                    gmToggle.setAttribute('aria-pressed', showGm ? 'true' : 'false');
                }}
                applyCalendarFilters();
            }}

            function collectRankingsUrlState() {{
                const state = {{}};
                const search = document.getElementById('rankings-search');
                if (search && search.value.trim()) state.q = slugStateValue(search.value);
                if (showArgOnly) state.scope = 'arg';
                const year = document.getElementById('rankings-year-select');
                const month = document.getElementById('rankings-month-select');
                const day = document.getElementById('rankings-day-select');
                if (year && month && day && year.value && month.value && day.value) {{
                    state.date = year.value + '-' + String(month.value).padStart(2, '0') + '-' + String(day.value).padStart(2, '0');
                }}
                return state;
            }}

            function restoreRankingsUrlState(params) {{
                const search = document.getElementById('rankings-search');
                if (search && params.has('q')) search.value = deslugSearchValue(params.get('q'));
                if (params.has('scope')) {{
                    showArgOnly = (params.get('scope') || '').toLowerCase() === 'arg';
                    const btn = document.getElementById('rankings-toggle-btn');
                    const view = document.getElementById('view-rankings');
                    if (btn) btn.innerHTML = showArgOnly ? 'Show ALL' : 'Show <img class="btn-flag-icon" src="assets/argentina.png" alt="ARG">';
                    if (view) view.classList.toggle('rankings-show-all', !showArgOnly);
                }}
                const date = params.get('date') || '';
                if (date.length === 10) {{
                    const parts = date.split('-');
                    const year = parts[0], month = parseInt(parts[1], 10), day = parseInt(parts[2], 10);
                    if (_rankingsDatesIndex[year] && _rankingsDatesIndex[year][String(month)] && _rankingsDatesIndex[year][String(month)].includes(day)) {{
                        const ySel = document.getElementById('rankings-year-select');
                        if (ySel) ySel.value = year;
                        _populateRankingMonths(year, month, day);
                        return switchRankingWeek(date);
                    }}
                }}
                filterRankings();
            }}

            function collectRoadToGSUrlState() {{
                const selectedPlayer = getNormalizedPlayerSelection('roadtogsPlayerSelect');
                return selectedPlayer ? {{ player: selectedPlayer }} : {{}};
            }}

            function restoreRoadToGSUrlState(params) {{
                if (!params.has('player')) return;
                const select = document.getElementById('roadtogsPlayerSelect');
                if (!select || !setSelectValueFromSlug(select, params.get('player'))) return;
                return renderRoadToGS();
            }}

            function collectFedBjkUrlState() {{
                const state = {{}};
                const activeBtn = document.querySelector('#view-fedbcup .fedbcup-btn.active');
                if (activeBtn && activeBtn.id) state.view = activeBtn.id.replace('fedbcup-btn-', '');
                const select = document.getElementById('fedbcup-player-filter');
                if (select && select.value) state.player = select.value;
                return state;
            }}

            function restoreFedBjkUrlState(params) {{
                const view = slugStateValue(params.get('view') || '');
                if (['series', 'players', 'captains'].includes(view)) switchFedBjkTab(view);
                const select = document.getElementById('fedbcup-player-filter');
                if (select && params.has('player') && setSelectValueFromSlug(select, params.get('player'))) {{
                    filterFedBjkPlayer();
                }}
            }}

            const HISTORY_URL_MULTI_FILTERS = [
                ['surface', 'filter-surface'],
                ['round', 'filter-round'],
                ['result', 'filter-result'],
                ['year', 'filter-year'],
                ['category', 'filter-category'],
                ['oppcountry', 'filter-opponent-country'],
                ['entry', 'filter-player-entry'],
                ['seed', 'filter-seed'],
                ['type', 'filter-match-type']
            ];

            function applyFilterOptionSlugs(filterId, slugs) {{
                const container = document.getElementById(filterId);
                if (!container || !slugs.length) return false;
                const selected = new Set(slugs.map(slugStateValue).filter(Boolean));
                let matched = false;
                container.querySelectorAll('.filter-option').forEach(option => {{
                    const value = option.getAttribute('data-value') || option.textContent || '';
                    const isSelected = selected.has(slugStateValue(value));
                    option.classList.toggle('selected', isSelected);
                    option.setAttribute('aria-pressed', isSelected ? 'true' : 'false');
                    if (isSelected) matched = true;
                }});
                return matched;
            }}

            function collectHistoryUrlState() {{
                const state = {{}};
                const page = typeof historySubpage === 'string' ? historySubpage : 'match';
                if (page && page !== 'match') state.page = page;
                if (page === 'milestones') {{
                    if (typeof getMilestonesFilterState === 'function' && typeof getMilestonesCategoryDefs === 'function') {{
                        const defs = getMilestonesCategoryDefs();
                        const selected = getMilestonesFilterState();
                        if (defs.length && selected.categories.length !== defs.length) state.mcat = selected.categories;
                        if (!selected.includeQualy) state.qualy = '0';
                    }}
                    return state;
                }}
                const player = getNormalizedPlayerSelection('playerHistorySelect');
                if (player) state.player = player === '__ALL__' ? 'all' : player;
                if (!player) return state;
                const filters = getHistoryFilterSelectionState();
                if (filters.surfaces.length) state.surface = filters.surfaces;
                if (filters.rounds.length) state.round = filters.rounds;
                if (filters.results.length) state.result = filters.results;
                if (filters.years.length) state.year = filters.years;
                if (filters.tournament) state.t = filters.tournament;
                if (filters.categories.length) state.category = filters.categories;
                if (filters.opponent) state.opp = filters.opponent;
                if (filters.opponentCountries.length) state.oppcountry = filters.opponentCountries;
                if (filters.playerEntries.length) state.entry = filters.playerEntries;
                if (filters.seeds.length) state.seed = filters.seeds;
                if (filters.matchTypes.length) state.type = filters.matchTypes;
                if (filters.asRankVal !== null) {{
                    state.asrank = String(filters.asRankVal);
                    if (filters.asRankMode !== 'higher') state.asmode = filters.asRankMode;
                }}
                if (filters.vsRankVal !== null) {{
                    state.vsrank = String(filters.vsRankVal);
                    if (filters.vsRankMode !== 'higher') state.vsmode = filters.vsRankMode;
                }}
                return state;
            }}

            async function restoreHistoryUrlState(params) {{
                const page = slugStateValue(params.get('page') || '');
                if (page === 'milestones') {{
                    setHistorySubpage('milestones');
                    await renderMilestonesPage();
                    restoreMilestonesUrlState(params);
                    await renderMilestonesPage();
                    return;
                }}

                setHistorySubpage('match');
                const playerSlug = params.get('player');
                const select = document.getElementById('playerHistorySelect');
                if (playerSlug && select) {{
                    let value = playerSlug.toLowerCase() === 'all' ? '__ALL__' : findSelectValueBySlug(select, playerSlug);
                    if (value) {{
                        select.value = value;
                        if (window.jQuery && $(select).data('select2')) $(select).val(value).trigger('change.select2');
                        await filterHistoryByPlayer();
                    }}
                }}
                if (!getNormalizedPlayerSelection('playerHistorySelect')) return;

                HISTORY_URL_MULTI_FILTERS.forEach(([paramName, filterId]) => {{
                    applyFilterOptionSlugs(filterId, splitUrlStateList(params.get(paramName)));
                }});

                const tournSelect = document.getElementById('filter-tournament-select');
                const tournSlug = params.get('t') || params.get('tournament');
                if (tournSelect && tournSlug) setSelectValueFromSlug(tournSelect, tournSlug);

                const oppSelect = document.getElementById('filter-opponent-select');
                if (oppSelect && params.has('opp')) setSelectValueFromSlug(oppSelect, params.get('opp'));

                const asRankInput = document.getElementById('filter-as-rank');
                const asRankMode = document.getElementById('filter-as-rank-mode');
                const vsRankInput = document.getElementById('filter-vs-rank');
                const vsRankMode = document.getElementById('filter-vs-rank-mode');
                if (asRankInput && params.has('asrank')) asRankInput.value = (params.get('asrank') || '').replace(/\\D/g, '');
                if (vsRankInput && params.has('vsrank')) vsRankInput.value = (params.get('vsrank') || '').replace(/\\D/g, '');
                if (asRankMode && ['higher', 'lower'].includes(params.get('asmode'))) asRankMode.value = params.get('asmode');
                if (vsRankMode && ['higher', 'lower'].includes(params.get('vsmode'))) vsRankMode.value = params.get('vsmode');
                applyHistoryFilters();
            }}

            function restoreMilestonesUrlState(params) {{
                const selectedCats = splitUrlStateList(params.get('mcat'));
                if (selectedCats.length) {{
                    getMilestonesCategoryDefs().forEach(def => {{
                        const el = document.getElementById(def.id);
                        if (el) el.checked = selectedCats.includes(slugStateValue(def.key));
                    }});
                }}
                const qualy = document.getElementById('milestones-filter-qualy');
                if (qualy && params.has('qualy')) qualy.checked = params.get('qualy') !== '0';
            }}

            function collectDrawsUrlState() {{
                const state = {{}};
                const select = document.getElementById('draws-tournament-select');
                const key = currentDrawTKey || (select ? select.value : '');
                if (key) state.t = entryTournamentStateSlugFromKey(key) || key;
                if (currentDrawType) state.type = currentDrawType;
                if (currentDrawFilterRound > 0) state.round = String(currentDrawFilterRound + 1);
                return state;
            }}

            function restoreDrawsUrlState(params) {{
                const select = document.getElementById('draws-tournament-select');
                let key = params.has('t') && select ? findSelectValueBySlug(select, params.get('t')) : '';
                if (!key && select && select.value) key = select.value;
                const requestedType = (params.get('type') || '').toUpperCase();
                if (requestedType) currentDrawType = requestedType;
                if (key) {{
                    if (select) select.value = key;
                    onDrawTournamentChange(key);
                }}
                const round = parseInt(params.get('round') || '', 10);
                if (Number.isInteger(round) && round > 1) filterDrawFromRound(round - 1);
            }}

            function collectTStrengthUrlState() {{
                const state = {{}};
                const year = document.getElementById('ts-filter-year');
                const level = document.getElementById('ts-filter-level');
                const surface = document.getElementById('ts-filter-surface');
                const region = document.getElementById('ts-filter-region');
                if (year && year.value) state.year = year.value;
                if (level && level.value) state.level = level.value;
                if (surface && surface.value) state.surface = surface.value;
                if (region && region.value) state.region = region.value;
                if (window.__wtargTStrengthView && window.__wtargTStrengthView !== 'MD') state.draw = window.__wtargTStrengthView;
                if (window.__wtargTStrengthSort && window.__wtargTStrengthSort !== 'date') state.sort = window.__wtargTStrengthSort;
                return state;
            }}

            function restoreTStrengthUrlState(params) {{
                if (window.__restoreTStrengthState) window.__restoreTStrengthState(params);
            }}

            function collectUrlStateForTab(tabName) {{
                switch (tabName) {{
                    case 'upcoming': return collectUpcomingUrlState();
                    case 'entrylists': return collectEntryListUrlState();
                    case 'draws': return collectDrawsUrlState();
                    case 'calendar': return collectCalendarUrlState();
                    case 'rankings': return collectRankingsUrlState();
                    case 'roadtogs': return collectRoadToGSUrlState();
                    case 'history': return collectHistoryUrlState();
                    case 'fedbcup': return collectFedBjkUrlState();
                    case 'tstrength': return collectTStrengthUrlState();
                    default: return {{}};
                }}
            }}

            async function restoreUrlStateForTab(tabName) {{
                const params = readUrlParams();
                _urlStateApplying = true;
                try {{
                    switch (tabName) {{
                        case 'upcoming': restoreUpcomingUrlState(params); break;
                        case 'entrylists': restoreEntryListUrlState(params); break;
                        case 'draws': restoreDrawsUrlState(params); break;
                        case 'calendar': restoreCalendarUrlState(params); break;
                        case 'rankings': await restoreRankingsUrlState(params); break;
                        case 'roadtogs': await restoreRoadToGSUrlState(params); break;
                        case 'history': await restoreHistoryUrlState(params); break;
                        case 'fedbcup': restoreFedBjkUrlState(params); break;
                        case 'tstrength': restoreTStrengthUrlState(params); break;
                    }}
                }} finally {{
                    _urlStateApplying = false;
                }}
            }}
            const playerMapping = window.__WTA_PLAYER_MAPPING__ || {{}};

            function normalizeHistoryPlayerName(rawName, source, playerId) {{
                const name = (rawName || '').toString().trim();
                if (!name) return name;
                if (name.includes('/')) {{
                    return name.split('/').map(part => {{
                        const trimmed = part.trim();
                        return trimmed ? normalizeHistoryPlayerName(trimmed, source, '') : trimmed;
                    }}).join(' / ');
                }}
                const mapped = getDisplayNameForIdentity(source, playerId, name);
                return (mapped || name).toString();
            }}

            function normalizeHistoryRow(row) {{
                if (!row || typeof row !== 'object' || Array.isArray(row)) return row;
                const normalized = {{ ...row }};
                const source = historyIdentitySource(normalized);
                [
                    '_winnerName', '_loserName', 'winnerName', 'loserName',
                    'winner_name', 'loser_name', 'PLAYER', 'OPPONENT', 'RIVAL',
                    'player', 'opponent', 'rival'
                ].forEach(field => {{
                    const value = normalized[field];
                    if (typeof value === 'string' && value.trim() && !value.includes('/')) {{
                        const fieldLower = field.toLowerCase();
                        const side = fieldLower.includes('winner')
                            ? 'winner'
                            : fieldLower.includes('loser') ? 'loser' : '';
                        const playerId = side ? normalized[`_${{side}}Id`] : '';
                        normalized[field] = normalizeHistoryPlayerName(value, source, playerId);
                    }}
                }});
                return normalized;
            }}

            function expandHistoryData(rows) {{
                if (!Array.isArray(rows) || !rows.length) return rows;
                const first = rows[0];
                if (!first || typeof first !== 'object' || Array.isArray(first) || !Array.isArray(first.rows)) return rows;
                const expanded = [];
                rows.forEach(group => {{
                    if (!group || typeof group !== 'object' || Array.isArray(group) || !Array.isArray(group.rows)) return;
                    const shared = {{}};
                    Object.keys(group).forEach(key => {{
                        if (key !== 'rows') shared[key] = group[key];
                    }});
                    group.rows.forEach(row => {{
                        if (!row || typeof row !== 'object' || Array.isArray(row)) return;
                        const merged = {{ ...shared, ...row }};
                        [
                            'PLAYER', 'ENTRY', 'SEED', 'RESULT',
                            'RIVAL_ENTRY', 'RIVAL_SEED', 'RIVAL', 'RIVAL_COUNTRY'
                        ].forEach(field => {{
                            if (!(field in merged)) merged[field] = '';
                        }});
                        expanded.push(merged);
                    }});
                }});
                return expanded;
            }}

            function normalizeHistoryData(rows) {{
                const expanded = expandHistoryData(rows);
                return Array.isArray(expanded) ? expanded.map(normalizeHistoryRow) : expanded;
            }}

            let historyData = window.__WTA_HISTORY_DATA__ || null;
            let _historyDataNormalized = false;
            let _historyDataPromise = null;
            function ensureHistoryDataLoaded() {{
                if (Array.isArray(historyData)) {{
                    if (!_historyDataNormalized) {{
                        historyData = normalizeHistoryData(historyData);
                        window.__WTA_HISTORY_DATA__ = historyData;
                        _historyDataNormalized = true;
                    }}
                    return Promise.resolve(historyData);
                }}
                if (window.__WTA_HISTORY_DATA__ && Array.isArray(window.__WTA_HISTORY_DATA__)) {{
                    historyData = normalizeHistoryData(window.__WTA_HISTORY_DATA__);
                    window.__WTA_HISTORY_DATA__ = historyData;
                    _historyDataNormalized = true;
                    return Promise.resolve(historyData);
                }}
                if (_historyDataPromise) return _historyDataPromise;
                _historyDataPromise = _loadLocalScriptOnce('data/history_data_bundle.js')
                    .then(() => {{
                        const d = window.__WTA_HISTORY_DATA__;
                        if (!Array.isArray(d)) throw new Error('History bundle did not initialize');
                        historyData = normalizeHistoryData(d);
                        window.__WTA_HISTORY_DATA__ = historyData;
                        _historyDataNormalized = true;
                        _historyDataPromise = null;
                        return historyData;
                    }})
                    .catch(err => {{
                        _historyDataPromise = null;
                        throw err;
                    }});
                return _historyDataPromise;
            }}
            const pointsDistribution = {_script_safe_json(points_distribution)};
            const itfDrawSizes = {_script_safe_json(itf_draw_sizes)};
            const wtaDrawSizes = {_script_safe_json(wta_draw_sizes)};
            const historicalDrawSlots = {_script_safe_json(historical_draw_slots)};
            const gsCutoffs = {gs_cutoffs_json};
            const drawsData = {_script_safe_json(draws_js_data)};
            const drawsTournamentInfo = {_script_safe_json(draws_tournament_info)};
            const _iocToIso2 = {{ALB:'al',ALG:'dz',AND:'ad',ANG:'ao',ARG:'ar',ARM:'am',ASA:'as',AUS:'au',AUT:'at',AZE:'az',BAH:'bs',BAR:'bb',BDI:'bi',BEL:'be',BEN:'bj',BIH:'ba',BLR:'by',BOL:'bo',BOT:'bw',BRA:'br',BUL:'bg',CAL:'nc',CAM:'kh',CAN:'ca',CHI:'cl',CHL:'cl',CHN:'cn',CIV:'ci',CMR:'cm',COD:'cd',COL:'co',CRC:'cr',CRO:'hr',CUB:'cu',CUW:'cw',CYP:'cy',CZE:'cz',CZS:'cz',DEN:'dk',DOM:'do',DZA:'dz',ECU:'ec',EGY:'eg',ESA:'sv',ESP:'es',EST:'ee',FIJ:'fj',FIN:'fi',FRA:'fr',FRG:'de',GAB:'ga',GBR:'gb',GEO:'ge',GER:'de',GHA:'gh',GLP:'gp',GRB:'gb',GRE:'gr',GRC:'gr',GUA:'gt',HAI:'ht',HKG:'hk',HRV:'hr',HUN:'hu',INA:'id',IND:'in',IRI:'ir',IRL:'ie',IRN:'ir',ISR:'il',ITA:'it',JAM:'jm',JOR:'jo',JPN:'jp',KAZ:'kz',KEN:'ke',KGZ:'kg',KHM:'kh',KOR:'kr',KOS:'xk',KSA:'sa',LAO:'la',LAT:'lv',LIE:'li',LTU:'lt',LUX:'lu',MAD:'mg',MAR:'ma',MAS:'my',MDA:'md',MEX:'mx',MKD:'mk',MLI:'ml',MLT:'mt',MNE:'me',MON:'mc',MRI:'mu',MOZ:'mz',NAM:'na',NCA:'ni',NCD:'nc',NED:'nl',NEP:'np',NET:'nl',NGA:'ng',NGR:'ng',NOR:'no',NZL:'nz',OMA:'om',OMN:'om',PAK:'pk',PAN:'pa',PAR:'py',PER:'pe',PHI:'ph',PLE:'ps',PNG:'pg',POL:'pl',POR:'pt',PUR:'pr',QAT:'qa',ROC:'ru',ROM:'ro',ROU:'ro',RSA:'za',RUS:'ru',SAF:'za',SAM:'ws',SEN:'sn',SGP:'sg',SIN:'sg',SLO:'si',SMR:'sm',SRB:'rs',SRI:'lk',SUI:'ch',SVK:'sk',SWE:'se',SYR:'sy',TCH:'cz',THA:'th',TKM:'tm',TOG:'tg',TPE:'tw',TRI:'tt',TTO:'tt',TUN:'tn',TUR:'tr',UAE:'ae',UKR:'ua',URU:'uy',USA:'us',UZB:'uz',VEN:'ve',VIE:'vn',XKX:'xk',ZAM:'zm',ZIM:'zw'}};
            const _localFlags = new Set(['AHO','YUG','SCG','CIS','URS']);
            function countryFlag(code, showCode) {{
                if (!code || code === '-') return code || '';
                const upper = code.toUpperCase();
                if (_localFlags.has(upper)) {{
                    const img = `<img src="data/flags/${{upper.toLowerCase()}}.svg" alt="${{code}}" title="${{code}}" style="vertical-align:middle;margin-right:3px;width:16px;height:11px;outline:0.3px solid #000">`;
                    return showCode === false ? img : img + code;
                }}
                const iso = _iocToIso2[upper];
                if (!iso) return code;
                const img = `<img src="https://purecatamphetamine.github.io/country-flag-icons/3x2/${{iso.toUpperCase()}}.svg" alt="${{code}}" title="${{code}}" style="vertical-align:middle;margin-right:3px;width:16px;height:11px;outline:0.3px solid #000">`;
                return showCode === false ? img : img + code;
            }}
            function countryFlagHistory(code, showCode) {{
                const html = countryFlag(code, showCode);
                if (window.innerWidth > 768) return html;
                return String(html).replace('width:16px;height:11px', 'width:12px;height:8px');
            }}
            // Icon swapping is CSS-driven via [data-theme="dark"]; JS only
            // manages the data-theme attribute, localStorage, and the label.
            function _syncHomeDarkBtn(isDark) {{
                const lbl = document.getElementById('home-dark-label');
                if (lbl) lbl.textContent = isDark ? 'Light Mode' : 'Dark Mode';
            }}
            function toggleDarkMode() {{
                const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
                if (isDark) {{
                    document.documentElement.removeAttribute('data-theme');
                    localStorage.setItem('theme', 'light');
                }} else {{
                    document.documentElement.setAttribute('data-theme', 'dark');
                    localStorage.setItem('theme', 'dark');
                }}
                _syncHomeDarkBtn(!isDark);
            }}
            _syncHomeDarkBtn(document.documentElement.getAttribute('data-theme') === 'dark');

            function toggleMobileMenu() {{
                const sidebar = document.getElementById('sidebar');
                sidebar.classList.toggle('mobile-hidden');
            }}

            // Close mobile menu when clicking outside
            document.addEventListener('click', function(event) {{
                const sidebar = document.getElementById('sidebar');
                const menuToggle = document.querySelector('.mobile-menu-toggle');

                if (window.innerWidth <= 768) {{
                    if (!sidebar.contains(event.target) && !menuToggle.contains(event.target)) {{
                        sidebar.classList.add('mobile-hidden');
                    }}
                }}
            }});

            // Close mobile menu when tab is clicked
            let homeLocked = false;
            let calendarFiltersInitialized = false;
            function closeAllCalendarDropdowns() {{
                const toolbar = document.getElementById('calendar-toolbar');
                if (!toolbar) return;
                toolbar.querySelectorAll('.cal-dd.open').forEach(dd => {{
                    dd.classList.remove('open');
                    const btn = dd.querySelector('[data-cal-dd-btn]');
                    if (btn) btn.setAttribute('aria-expanded', 'false');
                }});
            }}
            function initCalendarDropdowns() {{
                const toolbar = document.getElementById('calendar-toolbar');
                if (!toolbar) return;
                if (toolbar.dataset.calDdInit === '1') return;
                toolbar.dataset.calDdInit = '1';

                toolbar.addEventListener('click', function(e) {{
                    const btn = e.target.closest('[data-cal-dd-btn]');
                    if (!btn) return;
                    const dd = btn.closest('.cal-dd');
                    if (!dd) return;
                    const wasOpen = dd.classList.contains('open');
                    closeAllCalendarDropdowns();
                    if (!wasOpen) {{
                        dd.classList.add('open');
                        btn.setAttribute('aria-expanded', 'true');
                    }}
                    e.preventDefault();
                }});

                if (!window.__calendarDdDocInit) {{
                    window.__calendarDdDocInit = true;
                    document.addEventListener('click', function(e) {{
                        const tb = document.getElementById('calendar-toolbar');
                        if (!tb) return;
                        if (!tb.contains(e.target)) closeAllCalendarDropdowns();
                    }});
                    document.addEventListener('keydown', function(e) {{
                        if (e.key === 'Escape') closeAllCalendarDropdowns();
                    }});
                }}
            }}
            function initCalendarHorizontalScroll() {{
                const view = document.getElementById('view-calendar');
                if (!view) return;
                if (view.dataset.calHScrollInit === '1') return;
                const wrapper = view.querySelector('.table-wrapper');
                if (!wrapper) return;
                view.dataset.calHScrollInit = '1';

                function hasHorizontalOverflow() {{
                    return (wrapper.scrollWidth - wrapper.clientWidth) > 2;
                }}

                view.addEventListener('wheel', function(e) {{
                    if (e.ctrlKey) return;
                    if (e.target && e.target.closest && e.target.closest('.cal-dd-panel')) return;
                    let delta = 0;
                    if (e.deltaX && Math.abs(e.deltaX) > 0) delta = e.deltaX;
                    else if (e.shiftKey && e.deltaY && Math.abs(e.deltaY) > 0) delta = e.deltaY;
                    if (!delta) return;
                    if (!hasHorizontalOverflow()) return;
                    wrapper.scrollLeft += delta;
                    e.preventDefault();
                }}, {{ passive: false }});

                wrapper.addEventListener('wheel', function(e) {{
                    if (e.ctrlKey) return;
                    if (e.shiftKey) return;
                    if (e.target && e.target.closest && e.target.closest('.cal-dd-panel')) return;
                    if (!hasHorizontalOverflow()) return;
                    if (!e.deltaY || Math.abs(e.deltaY) < 1) return;
                    if (e.deltaX && Math.abs(e.deltaX) > Math.abs(e.deltaY)) return;
                    const before = wrapper.scrollLeft;
                    wrapper.scrollLeft += e.deltaY;
                    if (wrapper.scrollLeft !== before) e.preventDefault();
                }}, {{ passive: false }});

                let dragging = false;
                let dragStartX = 0;
                let dragStartScrollLeft = 0;
                wrapper.addEventListener('mousedown', function(e) {{
                    if (e.button !== 0) return;
                    if (!hasHorizontalOverflow()) return;
                    dragging = true;
                    wrapper.classList.add('dragging');
                    dragStartX = e.pageX;
                    dragStartScrollLeft = wrapper.scrollLeft;
                }});
                window.addEventListener('mouseup', function() {{
                    dragging = false;
                    wrapper.classList.remove('dragging');
                }});
                window.addEventListener('mousemove', function(e) {{
                    if (!dragging) return;
                    const dx = e.pageX - dragStartX;
                    wrapper.scrollLeft = dragStartScrollLeft - dx;
                }});

            }}
            function syncCalendarRowspans() {{
                const table = document.querySelector('#view-calendar .calendar-table');
                if (!table) return;
                const rows = Array.from(table.querySelectorAll('tbody tr'));
                if (!rows.length) return;

                const groupFirstRows = Array.from(table.querySelectorAll('tbody tr.cal-group-first'));
                if (!groupFirstRows.length) return;

                for (let gi = 0; gi < groupFirstRows.length; gi++) {{
                    const startRow = groupFirstRows[gi];
                    const startIdx = rows.indexOf(startRow);
                    if (startIdx === -1) continue;
                    const nextStartRow = (gi + 1 < groupFirstRows.length) ? groupFirstRows[gi + 1] : null;
                    const endIdx = nextStartRow ? rows.indexOf(nextStartRow) : rows.length;
                    if (endIdx === -1) continue;

                    const groupRows = rows.slice(startIdx, endIdx);
                    if (!groupRows.length) continue;

                    const catCell = groupRows.map(r => r.querySelector('.cal-cat-label')).find(Boolean);
                    if (!catCell) continue;

                    if (catCell.parentElement) catCell.parentElement.removeChild(catCell);
                    groupRows.forEach(r => {{
                        r.querySelectorAll('.cal-cat-label').forEach(c => c.remove());
                    }});

                    const visibleRows = groupRows.filter(r => r.style.display !== 'none');
                    const targetRow = visibleRows.length ? visibleRows[0] : groupRows[0];
                    targetRow.insertBefore(catCell, targetRow.firstChild);
                    catCell.rowSpan = visibleRows.length ? visibleRows.length : groupRows.length;
                }}
            }}
            function applyCalendarFilters() {{
                const levelToggles = document.querySelectorAll('[data-cal-filter-toggle]');
                const continentToggles = document.querySelectorAll('[data-cal-continent-toggle]');
                const surfaceToggles = document.querySelectorAll('[data-cal-surface-toggle]');
                const gmToggle = document.getElementById('calendar-gm-toggle');
                if (!levelToggles.length && !continentToggles.length && !surfaceToggles.length && !gmToggle) return;

                const activeLevels = new Set();
                levelToggles.forEach(cb => {{ if (cb.checked) activeLevels.add(cb.dataset.calFilterToggle); }});

                const activeContinents = new Set();
                continentToggles.forEach(cb => {{ if (cb.checked) activeContinents.add(cb.dataset.calContinentToggle); }});

                const activeSurfaces = new Set();
                surfaceToggles.forEach(cb => {{ if (cb.checked) activeSurfaces.add(cb.dataset.calSurfaceToggle); }});

                document.querySelectorAll('#view-calendar tr[data-cal-row-continent]').forEach(row => {{
                    const rowCont = row.dataset.calRowContinent || '';
                    let show = true;
                    if (continentToggles.length && rowCont && !activeContinents.has(rowCont)) show = false;
                    row.style.display = show ? '' : 'none';
                }});
                syncCalendarRowspans();

                document.querySelectorAll('#view-calendar [data-cal-filter]').forEach(el => {{
                    const levelKey = el.dataset.calFilter || '';
                    const contKey = el.dataset.calContinent || '';
                    const surfKey = el.dataset.calSurface || '';

                    let visible = true;
                    if (levelToggles.length && levelKey && !activeLevels.has(levelKey)) visible = false;
                    if (continentToggles.length && contKey && !activeContinents.has(contKey)) visible = false;
                    if (surfaceToggles.length && surfKey && !activeSurfaces.has(surfKey)) visible = false;

                    el.style.display = visible ? '' : 'none';
                }});

                const showGm = !gmToggle || gmToggle.getAttribute('aria-pressed') !== 'false';
                if (gmToggle) {{
                    const gmAction = showGm ? 'Hide Quality' : 'Show Quality';
                    gmToggle.classList.toggle('active', showGm);
                    gmToggle.textContent = gmAction;
                    gmToggle.setAttribute('aria-label', gmAction + ' values');
                    gmToggle.title = gmAction + ' values';
                }}
                document.querySelectorAll('#view-calendar .cal-gm-badge').forEach(badge => {{
                    badge.style.display = showGm ? '' : 'none';
                }});
                const gmLegend = document.querySelector('#view-calendar .cal-gm-legend');
                if (gmLegend) gmLegend.style.display = showGm ? '' : 'none';
                syncUrlStateForTab('calendar');
            }}
            function initCalendarFilters() {{
                if (calendarFiltersInitialized) {{
                    applyCalendarFilters();
                    return;
                }}
                initCalendarDropdowns();
                initCalendarHorizontalScroll();
                const toggles = document.querySelectorAll('[data-cal-filter-toggle], [data-cal-continent-toggle], [data-cal-surface-toggle]');
                const gmToggle = document.getElementById('calendar-gm-toggle');
                if (!toggles.length && !gmToggle) return;
                toggles.forEach(cb => cb.addEventListener('change', applyCalendarFilters));
                if (gmToggle) {{
                    gmToggle.addEventListener('click', function() {{
                        const showGm = gmToggle.getAttribute('aria-pressed') !== 'true';
                        gmToggle.setAttribute('aria-pressed', showGm ? 'true' : 'false');
                        applyCalendarFilters();
                    }});
                }}
                calendarFiltersInitialized = true;
                applyCalendarFilters();
            }}
            function switchTab(tabName) {{
                if (tabName === 'home' && homeLocked) return;
                if (tabName !== 'history') closeHistoryFilters(false);
                _currentTabName = (tabName || 'home').toString().trim().toLowerCase() || 'home';
                _urlStateSwitching = true;
                document.querySelectorAll('.menu-item').forEach(el => {{
                    el.classList.remove('active');
                    el.removeAttribute('aria-current');
                }});
                const btn = document.getElementById('btn-' + tabName);
                if (btn) {{
                    btn.classList.add('active');
                    btn.setAttribute('aria-current', 'page');
                }}

                if (tabName !== 'home') {{
                    homeLocked = true;
                    const homeView = document.getElementById('view-home');
                    if (homeView) homeView.style.display = 'none';
                    document.body.classList.remove('home-mode');
                }} else {{
                    document.body.classList.add('home-mode');
                }}

                if (tabName === 'calendar') {{
                    document.body.classList.add('calendar-mode');
                }} else {{
                    document.body.classList.remove('calendar-mode');
                }}

                document.getElementById('view-upcoming').style.display = (tabName === 'upcoming') ? 'flex' : 'none';
                document.getElementById('view-entrylists').style.display = (tabName === 'entrylists') ? 'flex' : 'none';

                document.getElementById('view-rankings').style.display = (tabName === 'rankings') ? 'flex' : 'none';
                document.getElementById('view-history').style.display = (tabName === 'history') ? 'flex' : 'none';
                document.getElementById('view-fedbcup').style.display = (tabName === 'fedbcup') ? 'flex' : 'none';
                document.getElementById('view-calendar').style.display = (tabName === 'calendar') ? 'flex' : 'none';
                document.getElementById('view-roadtogs').style.display = (tabName === 'roadtogs') ? 'flex' : 'none';
                document.getElementById('view-draws').style.display = (tabName === 'draws') ? 'block' : 'none';
                document.getElementById('view-tstrength').style.display = (tabName === 'tstrength') ? 'flex' : 'none';
                if (tabName === 'history') setHistorySubpage(HISTORY_SUBPAGE_MATCH);

                if (tabName === 'entrylists') {{
                    setEntryMenuCollapsed(false);
                    updateEntryMenuLabels();
                    updateEntryList();
                }}
                if (tabName === 'draws') updateDraw();
                if (tabName === 'calendar') initCalendarFilters();
                if (tabName === 'rankings') initRankingsIfEmpty();
                if (tabName === 'roadtogs') initRoadToGS();

                applyMobileHistoryLayout();
                syncEntryMenuToggle();

                if (tabName !== 'home') {{
                    try {{ localStorage.setItem('lastTab', tabName); }} catch(e) {{}}
                }}

                _urlStateSwitching = false;
                restoreAndSyncUrlStateForTab(tabName);

                // Close mobile menu after selecting
                if (window.innerWidth <= 768) {{
                    document.getElementById('sidebar').classList.add('mobile-hidden');
                }}
            }}

            document.body.classList.add('home-mode');
            document.addEventListener('DOMContentLoaded', initCalendarFilters);

            const BJKC_PLAYERS = {bjkc_players_json};

            (function() {{
                const sel = document.getElementById('fedbcup-player-filter');
                if (sel) {{
                    BJKC_PLAYERS.forEach(function(p) {{
                        const o = document.createElement('option');
                        o.value = p;
                        o.textContent = p;
                        sel.appendChild(o);
                    }});
                }}
                updateFedBjkRecord('');
            }})();

            function switchFedBjkTab(subTab) {{
                document.getElementById('view-fedbcup').classList.toggle('fedbcup-series-active', subTab === 'series');
                document.getElementById('fedbcup-view-players').style.display = (subTab === 'players') ? '' : 'none';
                document.getElementById('fedbcup-view-captains').style.display = (subTab === 'captains') ? '' : 'none';
                document.getElementById('fedbcup-view-series').style.display = (subTab === 'series') ? '' : 'none';
                document.getElementById('fedbcup-btn-players').classList.toggle('active', subTab === 'players');
                document.getElementById('fedbcup-btn-captains').classList.toggle('active', subTab === 'captains');
                document.getElementById('fedbcup-btn-series').classList.toggle('active', subTab === 'series');
                const filterLeft = document.getElementById('fedbcup-filter-left');
                const recordRight = document.getElementById('fedbcup-record-right');
                const vis = (subTab === 'series') ? 'visible' : 'hidden';
                if (filterLeft) filterLeft.style.visibility = vis;
                if (recordRight) recordRight.style.visibility = vis;
                syncUrlStateForTab('fedbcup');
            }}

            function filterFedBjkPlayer() {{
                const sel = document.getElementById('fedbcup-player-filter');
                const player = sel ? sel.value : '';
                const visibleBlocks = [];
                document.querySelectorAll('.bjkc-series-table tbody tr').forEach(function(tr) {{
                    if (!player) {{ tr.style.display = ''; return; }}
                    const players = (tr.getAttribute('data-player') || '').split('|');
                    tr.style.display = players.includes(player) ? '' : 'none';
                }});
                document.querySelectorAll('.bjkc-series-block').forEach(function(block) {{
                    if (!player) {{
                        block.style.display = '';
                        visibleBlocks.push(block);
                        return;
                    }}
                    const rows = block.querySelectorAll('.bjkc-series-table tbody tr');
                    const visible = Array.from(rows).some(function(r) {{ return r.style.display !== 'none'; }});
                    block.style.display = visible ? '' : 'none';
                    if (visible) visibleBlocks.push(block);
                }});
                visibleBlocks.forEach(function(block, index) {{ block.open = index === 0; }});
                updateFedBjkRecord(player);
                syncUrlStateForTab('fedbcup');
            }}

            function updateFedBjkRecord(player) {{
                let sw = 0, sl = 0, dw = 0, dl = 0;
                document.querySelectorAll('.bjkc-series-table tbody tr').forEach(function(tr) {{
                    const result = tr.getAttribute('data-result');
                    if (!result) return;
                    const type = tr.getAttribute('data-type');
                    const players = (tr.getAttribute('data-player') || '').split('|');
                    if (player && !players.includes(player)) return;
                    if (type === 'S') {{ if (result === 'W') sw++; else sl++; }}
                    else {{ if (result === 'W') dw++; else dl++; }}
                }});
                const rec = document.getElementById('fedbcup-record');
                if (rec) {{
                    let text = 'S: ' + sw + '-' + sl;
                    if (dw + dl > 0) text += ' | D: ' + dw + '-' + dl;
                    rec.textContent = text;
                }}
            }}

            function applyMobileHistoryLayout() {{
                const historyLayout = document.querySelector('#view-history .history-layout');
                if (!historyLayout) return;

                const filterPanel = historyLayout.querySelector('.filter-panel');
                const historyContent = historyLayout.querySelector('.history-content');
                if (!filterPanel || !historyContent) return;

                // The panel stays in the desktop rail in the DOM; mobile CSS presents it as a sheet.
                if (historyContent.contains(filterPanel)) {{
                    historyLayout.insertBefore(filterPanel, historyContent);
                }}
                syncHistoryFilterSheetMode();
            }}

            let _historyFilterReturnFocus = null;

            function getHistoryActiveFilterCount(filterState) {{
                const state = filterState || getHistoryFilterSelectionState();
                const multiValueKeys = [
                    'surfaces', 'rounds', 'results', 'years', 'categories',
                    'opponentCountries', 'playerEntries', 'seeds', 'matchTypes'
                ];
                let count = multiValueKeys.reduce((total, key) => total + state[key].length, 0);
                if (state.tournament) count += 1;
                if (state.opponent) count += 1;
                if (state.asRankVal !== null) count += 1;
                if (state.vsRankVal !== null) count += 1;
                return count;
            }}

            function updateHistoryMobileFilterButton(filterState) {{
                const button = document.getElementById('history-mobile-filter-btn');
                const label = document.getElementById('history-mobile-filter-label');
                if (!button || !label) return;
                const count = getHistoryActiveFilterCount(filterState);
                label.textContent = count ? `Filters · ${{count}}` : 'Filters';
                button.classList.toggle('has-active-filters', count > 0);
                button.setAttribute('aria-label', count ? `Filters, ${{count}} active` : 'Filters, no active filters');
            }}

            function syncHistoryFilterSheetMode() {{
                const panel = document.getElementById('history-filter-panel');
                const button = document.getElementById('history-mobile-filter-btn');
                if (!panel || !button) return;
                const mobile = window.innerWidth <= 768;
                const available = mobile && historySubpage === HISTORY_SUBPAGE_MATCH;
                if (!available) document.body.classList.remove('history-filters-open');
                const isOpen = available && document.body.classList.contains('history-filters-open');
                button.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
                if (mobile) {{
                    panel.setAttribute('role', 'dialog');
                    panel.setAttribute('aria-modal', 'true');
                    panel.setAttribute('aria-labelledby', 'history-filter-sheet-title');
                    panel.setAttribute('aria-hidden', isOpen ? 'false' : 'true');
                }} else {{
                    panel.removeAttribute('role');
                    panel.removeAttribute('aria-modal');
                    panel.removeAttribute('aria-labelledby');
                    panel.removeAttribute('aria-hidden');
                }}
            }}

            function openHistoryFilters() {{
                if (window.innerWidth > 768 || historySubpage !== HISTORY_SUBPAGE_MATCH) return;
                _historyFilterReturnFocus = document.activeElement;
                document.body.classList.add('history-filters-open');
                syncHistoryFilterSheetMode();
                const closeButton = document.querySelector('.history-filter-sheet-close');
                if (closeButton) requestAnimationFrame(() => closeButton.focus({{ preventScroll: true }}));
            }}

            function closeHistoryFilters(restoreFocus = true) {{
                const wasOpen = document.body.classList.contains('history-filters-open');
                document.body.classList.remove('history-filters-open');
                syncHistoryFilterSheetMode();
                if (restoreFocus && wasOpen && _historyFilterReturnFocus && document.contains(_historyFilterReturnFocus)) {{
                    _historyFilterReturnFocus.focus({{ preventScroll: true }});
                }}
                _historyFilterReturnFocus = null;
            }}

            document.addEventListener('keydown', function(event) {{
                if (event.key === 'Escape' && document.body.classList.contains('history-filters-open')) {{
                    closeHistoryFilters();
                }}
            }});

            function reverseScore(score) {{
                if (!score) return '';
                return score.split(' ').map(set => {{
                    const m = set.match(/^(\\d+)-(\\d+)(.*)$/);
                    if (!m) return set;
                    return m[2] + '-' + m[1] + m[3];
                }}).join(' ');
            }}

            function formatSeed(seed) {{
                if (seed === null || seed === undefined) return '';
                const text = String(seed).trim();
                if (!text) return '';
                const num = Number(text);
                if (!Number.isNaN(num) && Number.isInteger(num)) {{
                    return String(num);
                }}
                return text;
            }}

            function buildPrefix(seed, entry) {{
                const parts = [];
                const formattedSeed = formatSeed(seed);
                if (formattedSeed) parts.push(formattedSeed);
                if (entry) parts.push(entry);
                if (parts.length === 0) return '';
                return '(' + parts.join('/') + ') ';
            }}

            function buildHistoryPlayerCell(rank, country, seed, entry, name) {{
                const rankText = String(rank || '').trim();
                const rankHtml = rankText && rankText !== '-' ? `#${{rankText}}` : '';
                const countryText = String(country || '').trim();
                const flagHtml = countryText && countryText !== '-' ? countryFlagHistory(countryText, false) : '';
                return `<span class="history-player-cell"><span class="history-player-rank">${{rankHtml}}</span>${{
                    flagHtml ? `<span class="history-player-flag">${{flagHtml}}</span>` : ''
                }}<span class="history-player-name">${{buildPrefix(seed, entry) + name}}</span></span>`;
            }}

            const _drItfDrawLookup = {{}};
            itfDrawSizes.forEach(t => {{
                const key = (t.tournamentName || '') + '|' + (t.date || '');
                _drItfDrawLookup[key] = t.mainDrawSize;
                const weekMatch = (t.tournamentName || '').match(/^(.+?)\\s*\\(Week \\d+\\)$/);
                if (weekMatch) {{
                    _drItfDrawLookup[weekMatch[1].trim() + '|' + (t.date || '')] = t.mainDrawSize;
                }}
            }});
            const _drWtaDrawLookup = {{}};
            wtaDrawSizes.forEach(t => {{
                if (!t.tournamentId) return;
                const normId = String(parseInt(t.tournamentId) || t.tournamentId);
                _drWtaDrawLookup[normId] = t.mainDrawSize;
            }});
            const _drCategoryDrawSize = {{
                'GS': 128, 'WTA 1000': 96,
                'WTA 500': 32, 'WTA 250': 32, 'WTA 125': 32,
                '125K': 32, '125K Series': 32,
                'W100': 32, 'W75': 32, 'W50': 32, 'W35': 32, 'W15': 32
            }};
            function _drHistoricalKeys(tournamentId, date, tournamentName) {{
                const year = String(date || '').slice(0, 4);
                if (!year) return [];
                const keys = [];
                if (tournamentId) {{
                    const normId = String(parseInt(tournamentId) || tournamentId).trim();
                    if (normId) keys.push('id|' + normId + '|' + year);
                }}
                const normName = String(tournamentName || '').trim().toUpperCase();
                if (normName) keys.push('name|' + normName + '|' + year);
                return keys;
            }}
            function _drHistoricalDrawSize(tournamentId, date, tournamentName) {{
                const keys = _drHistoricalKeys(tournamentId, date, tournamentName);
                for (const key of keys) {{
                    const slotSize = historicalDrawSlots[key];
                    if (slotSize) return slotSize;
                }}
                return null;
            }}
            function _drResolveDrawSize(tournamentId, date, tournamentName, category, matchType) {{
                if ((matchType === 'WTA' || matchType === 'GS') && tournamentId) {{
                    const normId = String(parseInt(tournamentId) || tournamentId);
                    const sz = _drWtaDrawLookup[normId];
                    if (sz) return sz;
                }}
                if (matchType === 'WTA' || matchType === 'GS') {{
                    const sz = _drHistoricalDrawSize(tournamentId, date, tournamentName);
                    if (sz) return sz;
                }}
                if (matchType === 'ITF') {{
                    const sz = _drItfDrawLookup[(tournamentName || '') + '|' + (date || '')];
                    if (sz) return sz;
                    const historicalSz = _drHistoricalDrawSize(tournamentId, date, tournamentName);
                    if (historicalSz) return historicalSz;
                }}
                return _drCategoryDrawSize[category] || 32;
            }}

            function displayRound(round, tournamentId, date, tournamentName, category, matchType, draw) {{
                if (!round) return '';
                const roundText = (round || '').toString().trim();
                const roundUpper = roundText.toUpperCase();
                if (roundUpper.startsWith('ROUND ROBIN')) return 'RR';
                // Qualifying draw: convert ordinal names to Q1/Q2/Q3
                if (draw === 'Q') {{
                    const _qMap = {{'1st Round':'Q1','2nd Round':'Q2','3rd Round':'Q3','4th Round':'Q4'}};
                    return _qMap[roundText] || roundText;
                }}
                // Team/non-individual draws: normalize round names
                if (draw !== 'M') {{
                    const _tMap = {{'Round Robin':'RR','Last 32':'R32','Last 16':'R16','Last 8':'QF','Quarter Finals':'QF','Semi Finals':'SF','Final':'F'}};
                    return _tMap[roundText] || roundText;
                }}
                if (roundText === 'Final') return 'F';
                if (roundText === 'Semi-finals' || roundText === 'Semi Finals') return 'SF';
                if (roundText === 'Quarter-finals' || roundText === 'Quarter Finals') return 'QF';
                const drawSize = _drResolveDrawSize(tournamentId, date, tournamentName, category, matchType);
                const _ordinalNum = {{'1st Round':1,'2nd Round':2,'3rd Round':3,'4th Round':4,'5th Round':5}}[roundText];
                if (_ordinalNum !== undefined) {{
                    const nextPow2 = Math.pow(2, Math.ceil(Math.log2(drawSize)));
                    const n = nextPow2 / Math.pow(2, _ordinalNum - 1);
                    if (n <= 2) return 'F';
                    if (n <= 4) return 'SF';
                    if (n <= 8) return 'QF';
                    return 'R' + n;
                }}
                return roundText;
            }}

            // Format date string to yyyy-MM-dd
            function formatDate(dateStr) {{
                if (!dateStr) return '';
                const parts = dateStr.split('/');
                if (parts.length === 3) {{
                    return parts[2] + '-' + parts[1].padStart(2, '0') + '-' + parts[0].padStart(2, '0');
                }}
                const d = new Date(dateStr);
                if (isNaN(d)) return dateStr;
                return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
            }}

            // Helper function to get display name from player mapping
            // Build reverse lookup cache for O(1) name resolution
            const _displayNameCache = {{}};
            const _displayNameBySourceId = {{wta: {{}}, itf: {{}}, bjkc: {{}}}};
            const _ambiguousNameKeys = new Set();
            (function() {{
                function registerSourceId(source, playerId, canonicalName) {{
                    const sourceKey = (source || '').toString().trim().toLowerCase();
                    const id = (playerId || '').toString().trim();
                    const display = (canonicalName || '').toString().trim();
                    if (!sourceKey || !id || !display || !_displayNameBySourceId[sourceKey]) return;
                    _displayNameBySourceId[sourceKey][id] = display;
                }}

                function registerName(canonicalName, rawName) {{
                    const canonical = (canonicalName || '').toString().trim();
                    const raw = (rawName || '').toString().trim();
                    if (!canonical && !raw) return;
                    const display = canonical || raw;
                    _displayNameCache[display.toUpperCase()] = display;
                    if (raw) {{
                        const rawKey = raw.toUpperCase();
                        if (_ambiguousNameKeys.has(rawKey)) return;
                        const previous = _displayNameCache[rawKey];
                        if (previous && previous !== display) {{
                            delete _displayNameCache[rawKey];
                            _ambiguousNameKeys.add(rawKey);
                        }} else {{
                            _displayNameCache[rawKey] = display;
                        }}
                    }}
                }}

                if (Array.isArray(playerMapping)) {{
                    for (const item of playerMapping) {{
                        if (!item || typeof item !== 'object') continue;
                        const canonical = item.presentation_name || item.display_name || item.wta_name || item.itf_name || item.bjkc_name || '';
                        if (!canonical) continue;
                        registerSourceId('wta', item.wta_id, canonical);
                        registerSourceId('itf', item.itf_id, canonical);
                        registerSourceId('bjkc', item.bjkc_id, canonical);
                        (item.additional_wta_ids || []).forEach(id => registerSourceId('wta', id, canonical));
                        (item.additional_itf_ids || []).forEach(id => registerSourceId('itf', id, canonical));
                        (item.additional_bjkc_ids || []).forEach(id => registerSourceId('bjkc', id, canonical));
                        registerName(canonical, canonical);
                        registerName(canonical, item.wta_name);
                        registerName(canonical, item.itf_name);
                        registerName(canonical, item.bjkc_name);
                        if (Array.isArray(item.aliases)) {{
                            for (const alias of item.aliases) {{
                                registerName(canonical, alias);
                            }}
                        }}
                    }}
                }} else {{
                    for (const [displayName, aliases] of Object.entries(playerMapping)) {{
                        if (!displayName) continue;
                        registerName(displayName, displayName);
                        if (!Array.isArray(aliases)) continue;
                        for (const alias of aliases) {{
                            registerName(displayName, alias);
                        }}
                    }}
                }}
            }})();

            function getDisplayNameForIdentity(source, playerId, rawName) {{
                const sourceKey = (source || '').toString().trim().toLowerCase();
                const id = (playerId || '').toString().trim();
                const byId = _displayNameBySourceId[sourceKey];
                if (byId && id && byId[id]) return byId[id];
                return getDisplayName((rawName || '').toString().toUpperCase());
            }}

            function historyIdentitySource(row) {{
                const value = ((row && row.MATCH_TYPE) || '').toString().trim().toUpperCase();
                if (value === 'ITF') return 'itf';
                if (['WTA', 'GS', 'OG', 'UNITED CUP'].includes(value)) return 'wta';
                if (value.includes('BJK') || value.includes('FED CUP')) return 'bjkc';
                return value.toLowerCase();
            }}

            function getDisplayName(upperCaseName) {{
                const normalizedKey = (upperCaseName || '').toString().trim().toUpperCase();
                const cached = _displayNameCache[normalizedKey];
                if (cached) return cached;
                // If not found, convert to title case (handling hyphens)
                const result = normalizedKey.split(' ').map(word => {{
                    if (word.includes('-')) {{
                        return word.split('-').map(part =>
                            part.charAt(0).toUpperCase() + part.slice(1).toLowerCase()
                        ).join('-');
                    }}
                    return word.charAt(0).toUpperCase() + word.slice(1).toLowerCase();
                }}).join(' ');
                _displayNameCache[normalizedKey] = result;
                return result;
            }}

            function normalizeHistoryPlayerSelect() {{
                const select = document.getElementById('playerHistorySelect');
                if (!select) return;

                const currentValue = (select.value || '').toString();
                const currentUpper = currentValue.toUpperCase();
                const seen = new Set();
                const fragment = document.createDocumentFragment();

                function appendOption(value, text) {{
                    const option = document.createElement('option');
                    option.value = value;
                    option.textContent = text;
                    fragment.appendChild(option);
                }}

                appendOption('', 'Select Player...');
                appendOption('__ALL__', 'ALL PLAYERS');

                Array.from(select.options).forEach(option => {{
                    const value = (option.value || '').toString().trim();
                    if (!value || value === '__ALL__' || value === 'Select Player...') return;
                    const canonical = getDisplayName(value.toUpperCase());
                    const canonicalUpper = canonical.toUpperCase();
                    if (seen.has(canonicalUpper)) return;
                    seen.add(canonicalUpper);
                    appendOption(canonical, canonical);
                }});

                select.innerHTML = '';
                select.appendChild(fragment);

                if (currentUpper === '__ALL__') {{
                    select.value = '__ALL__';
                }} else if (currentUpper) {{
                    select.value = getDisplayName(currentUpper);
                }}

                _historyPlayerUniverse = null;
                _historyPlayerUniverseUpper = null;
            }}

            function getNormalizedPlayerSelection(selectId) {{
                const select = document.getElementById(selectId);
                const value = select ? (select.value || '').toString().trim() : '';
                if (!value || value === '__ALL__') return value;
                return getDisplayName(value.toUpperCase()).toUpperCase();
            }}

            $(document).ready(function() {{
                // Initialize sidebar state for mobile
                if (window.innerWidth <= 768) {{
                    document.getElementById('sidebar').classList.add('mobile-hidden');
                }}

                normalizeHistoryPlayerSelect();

                $('#playerHistorySelect').select2({{
                    placeholder: 'Select a player...',
                    allowClear: true,
                    width: '100%'
                }});

                $('#playerHistorySelect').on('change', function() {{
                    filterHistoryByPlayer();
                }});

                renderHistoryTable();
                renderMilestonesTable();
                setHistorySubpage(historySubpage);
                applyMobileHistoryLayout();
                updateHistoryMobileFilterButton();

                // Handle window resize
                window.addEventListener('resize', function() {{
                    if (window.innerWidth > 768) {{
                        document.getElementById('sidebar').classList.remove('mobile-hidden');
                    }} else {{
                        document.getElementById('sidebar').classList.add('mobile-hidden');
                    }}
                    applyMobileHistoryLayout();
                }});
            }});

            function filter() {{
                const q = document.getElementById('s').value.toLowerCase();
                document.querySelectorAll('#tb tr').forEach(row => {{
                    const matches = row.getAttribute('data-name').includes(q);
                    row.classList.toggle('hidden', !matches);
                }});
                syncUrlStateForTab('upcoming');
            }}
            let showArgOnly = false;
            function toggleRankingsScope() {{
                showArgOnly = !showArgOnly;
                const btn = document.getElementById('rankings-toggle-btn');
                const view = document.getElementById('view-rankings');
                if (btn) btn.innerHTML = showArgOnly ? 'Show ALL' : 'Show <img class="btn-flag-icon" src="assets/argentina.png" alt="ARG">';
                if (view) view.classList.toggle('rankings-show-all', !showArgOnly);
                filterRankings();
            }}
            function updateRankingsRowParity() {{
                let visibleIndex = 0;
                document.querySelectorAll('#rankings-body tr').forEach(row => {{
                    row.classList.remove('rankings-visible-odd', 'rankings-visible-even');
                    if (row.classList.contains('hidden') || row.classList.contains('rankings-system-row')) return;
                    row.classList.add(visibleIndex % 2 === 0 ? 'rankings-visible-odd' : 'rankings-visible-even');
                    visibleIndex += 1;
                }});
            }}
            function filterRankings() {{
                const q = document.getElementById('rankings-search').value.toLowerCase();
                document.querySelectorAll('#rankings-body tr').forEach(row => {{
                    if (row.classList.contains('rankings-system-row')) {{
                        row.classList.remove('hidden');
                        return;
                    }}
                    const text = row.textContent.toLowerCase();
                    const nat = row.getAttribute('data-country') || (row.children[2] ? row.children[2].textContent.trim().toUpperCase() : '');
                    const matchesSearch = text.includes(q);
                    const matchesCountry = !showArgOnly || nat === 'ARG';
                    row.classList.toggle('hidden', !(matchesSearch && matchesCountry));
                }});
                updateRankingsRowParity();
                syncUrlStateForTab('rankings');
            }}
            const _rankingBundleCaches = {{}};
            const _rankingBundlePromises = {{}};
            const _rankingsLatestDate = {rankings_latest_date_json};
            function _rankingBundleForDate(dateStr) {{
                if (dateStr === _rankingsLatestDate) {{
                    return {{ file: 'data/wta_rankings_latest_bundle.js', globalName: '__WTA_RANKINGS_LATEST__' }};
                }}
                const year = parseInt(String(dateStr || '').split('-')[0]);
                return {{
                    file: `data/wta_rankings_${{year}}_bundle.js`,
                    globalName: `__WTA_RANKINGS_${{year}}__`
                }};
            }}
            function _loadRankingDataForDate(dateStr) {{
                const info = _rankingBundleForDate(dateStr);
                const file = info.file;
                if (_rankingBundleCaches[file]) return Promise.resolve(_rankingBundleCaches[file]);
                if (_rankingBundlePromises[file]) return _rankingBundlePromises[file];
                const existing = window[info.globalName];
                if (existing && typeof existing === 'object') {{
                    _rankingBundleCaches[file] = existing;
                    return Promise.resolve(existing);
                }}
                _rankingBundlePromises[file] = _loadLocalScriptOnce(file)
                    .then(() => {{
                        const data = window[info.globalName];
                        if (!data || typeof data !== 'object') {{
                            throw new Error('Ranking bundle did not initialize: ' + file);
                        }}
                        _rankingBundleCaches[file] = data;
                        _rankingBundlePromises[file] = null;
                        return data;
                    }})
                    .catch(err => {{
                        console.error('Failed to load ' + file + ':', err);
                        _rankingBundlePromises[file] = null;
                        throw err;
                    }});
                return _rankingBundlePromises[file];
            }}
            function _renderRankingRows(players) {{
                const tbody = document.getElementById('rankings-body');
                let html = '';
                players.forEach(p => {{
                    const dob = (p.d || '').split('T')[0];
                    const name = (p.n || '').toLowerCase().replace(/(^|\\s)(\\S)/g, (_, b, c) => b + c.toUpperCase());
                    html += `<tr data-country="${{(p.c||'').toUpperCase()}}"><td>${{p.r || ''}}</td><td style="text-align:left;font-weight:bold;">${{countryFlag(p.c || '', false)}} ${{name}}</td><td>${{p.pts || ''}}</td><td>${{dob}}</td></tr>`;
                }});
                tbody.innerHTML = html;
                filterRankings();
            }}
            const _rankingsDatesIndex = {rankings_dates_index_json};
            const _rankingMonthNames = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
            function _populateRankingMonths(year, selectMonth, selectDay) {{
                const sel = document.getElementById('rankings-month-select');
                const months = Object.keys(_rankingsDatesIndex[year] || {{}}).map(Number).sort((a,b)=>a-b);
                const chosenM = (selectMonth != null && months.includes(+selectMonth)) ? +selectMonth : months[months.length-1];
                sel.innerHTML = months.map(m => {{
                    const isSel = (m === chosenM) ? ' selected' : '';
                    return `<option value="${{m}}"${{isSel}}>${{_rankingMonthNames[m-1]}}</option>`;
                }}).join('');
                _populateRankingDays(year, chosenM, selectDay);
            }}
            function _populateRankingDays(year, monthNum, selectDay) {{
                const sel = document.getElementById('rankings-day-select');
                const days = ((_rankingsDatesIndex[year] || {{}})[String(monthNum)] || []).slice().sort((a,b)=>a-b);
                const chosenD = (selectDay != null && days.includes(+selectDay)) ? +selectDay : days[days.length-1];
                sel.innerHTML = days.map(d => {{
                    const isSel = (d === chosenD) ? ' selected' : '';
                    return `<option value="${{d}}"${{isSel}}>${{d}}</option>`;
                }}).join('');
            }}
            function onRankingYearChange(year) {{
                _populateRankingMonths(year, null, null);
            }}
            function onRankingMonthChange() {{
                const year = document.getElementById('rankings-year-select').value;
                const month = +document.getElementById('rankings-month-select').value;
                _populateRankingDays(year, month, null);
            }}
            function applyRankingSelection() {{
                const year = document.getElementById('rankings-year-select').value;
                const month = document.getElementById('rankings-month-select').value;
                const day = document.getElementById('rankings-day-select').value;
                if (!year || !month || !day) return;
                const mm = month.toString().padStart(2,'0');
                const dd = day.toString().padStart(2,'0');
                switchRankingWeek(`${{year}}-${{mm}}-${{dd}}`);
            }}
            function _renderRankingSkeleton(rowCount) {{
                const tbody = document.getElementById('rankings-body');
                if (!tbody) return;
                const widths = ['40%', '80%', '60%', '65%', '55%', '75%', '50%', '70%', '45%', '60%'];
                let html = '';
                for (let i = 0; i < rowCount; i++) {{
                    const w = widths[i % widths.length];
                    html += '<tr class="skeleton-row">'
                          + '<td><span class="skeleton-bar" style="width:24px"></span></td>'
                          + '<td><span class="skeleton-bar" style="width:' + w + '"></span></td>'
                          + '<td><span class="skeleton-bar" style="width:40px"></span></td>'
                          + '<td><span class="skeleton-bar" style="width:60px"></span></td>'
                          + '</tr>';
                }}
                tbody.innerHTML = html;
            }}
            function switchRankingWeek(dateStr) {{
                const controls = ['rankings-year-select','rankings-month-select','rankings-day-select','rankings-load-btn'].map(id => document.getElementById(id));
                controls.forEach(el => {{ if(el) {{ el.disabled = true; el.style.opacity = '0.5'; }} }});
                _renderRankingSkeleton(15);
                return _loadRankingDataForDate(dateStr)
                    .then(data => {{
                        const players = data[dateStr];
                        if (players) _renderRankingRows(players);
                        else document.getElementById('rankings-body').innerHTML = '<tr class="rankings-system-row"><td colspan="4" class="cell-state-info rankings-system-row">No rankings found for the selected date.</td></tr>';
                    }})
                    .catch(err => {{
                        console.error('Failed to load rankings data:', err);
                        document.getElementById('rankings-body').innerHTML = '<tr class="rankings-system-row"><td colspan="4" class="cell-state-error rankings-system-row">Failed to load local rankings data. Please regenerate the site and reopen it.</td></tr>';
                    }})
                    .finally(() => {{
                        controls.forEach(el => {{ if(el) {{ el.disabled = false; el.style.opacity = '1'; }} }});
                        syncUrlStateForTab('rankings');
                    }});
            }}
            let _rankingsInitialized = false;
            function initRankingsIfEmpty() {{
                if (_rankingsInitialized) return;
                _rankingsInitialized = true;
                applyRankingSelection();
            }}
            _populateRankingMonths('{rankings_latest_year_str}', {rankings_latest_month}, {rankings_latest_day});
            function filterNational() {{
                const q = document.getElementById('national-search').value.toLowerCase();
                document.querySelectorAll('#national-body tr').forEach(row => {{
                    const text = row.textContent.toLowerCase();
                    row.classList.toggle('hidden', !text.includes(q));
                }});
            }}
            function entryMenuNameForItem(el) {{
                if (!el) return '';
                const nameEl = el.querySelector('.entry-menu-name');
                return nameEl ? nameEl.textContent.trim() : el.textContent.trim();
            }}

            function entryByPos(a, b) {{
                return (Number(a.pos_num ?? 999) - Number(b.pos_num ?? 999))
                    || String(a.name || '').localeCompare(String(b.name || ''));
            }}

            function entrySortEL(list) {{
                const byRank = (a, b) => {{
                    const rankScore = p => {{
                        const r = String(p.rank || '');
                        const m = r.match(/\\d+(\\.\\d+)?/);
                        const n = m ? parseFloat(m[0]) : 9999;
                        if (r.startsWith('WTN')) return [2, n];
                        if (r.startsWith('ITF')) return [1, n];
                        return [0, n];
                    }};
                    const [ta, na] = rankScore(a);
                    const [tb, nb] = rankScore(b);
                    return (ta - tb) || (na - nb) || String(a.name || '').localeCompare(String(b.name || ''));
                }};
                const mdo = list.filter(p => p.pos === 'MDO').sort(byRank);
                const numbered = list.filter(p => p.pos !== 'MDO').sort(entryByPos);
                if (mdo.length === 0) return numbered;
                const used = new Set(numbered.map(p => p.pos_num));
                const maxPos = numbered.length > 0 ? Math.max(...numbered.map(p => p.pos_num)) : 0;
                const gaps = [];
                for (let i = 1; i <= maxPos; i++) {{ if (!used.has(i)) gaps.push(i); }}
                const result = [];
                let mi = 0, gi = 0, overflow = 1;
                for (const p of numbered) {{
                    while (mi < mdo.length && byRank(mdo[mi], p) < 0) {{
                        const pos_num = gi < gaps.length ? gaps[gi++] : maxPos + overflow++;
                        result.push({{...mdo[mi++], pos_num}});
                    }}
                    result.push(p);
                }}
                while (mi < mdo.length) {{
                    const pos_num = gi < gaps.length ? gaps[gi++] : maxPos + overflow++;
                    result.push({{...mdo[mi++], pos_num}});
                }}
                return result;
            }}

            function entryGetDisplayMain(players, isITF) {{
                const safe = (players || []).filter(Boolean);
                const main = safe.filter(p => p.type === 'MAIN').sort(entryByPos);
                if (!_prioFilterActive || !isITF) return main;
                const qual = entrySortEL(safe.filter(p => p.type === 'QUAL'));
                const alt = entrySortEL(safe.filter(p => p.type === 'ALT'));
                const mainJRPrio1 = main.filter(p => (p.entry === 'JR' || p.entry === 'JA') && p.priority === '1');
                const mainRegular = main.filter(p => p.entry !== 'JR' && p.entry !== 'JA');
                const regularSpots = main.length - mainJRPrio1.length;
                const pool = [
                    ...mainRegular.filter(p => p.priority === '1'),
                    ...qual.filter(p => p.priority === '1'),
                    ...alt.filter(p => p.priority === '1'),
                ];
                return [
                    ...pool.slice(0, regularSpots),
                    ...mainJRPrio1,
                ];
            }}

            const _itfMainPlaceholderNames = new Set(['(Available Slot)', '(Special Exempt)']);

            function entryFillITFMainPlaceholders(mainPlayers, qualPlayers) {{
                const main = (mainPlayers || []).filter(Boolean);
                const quals = (qualPlayers || []).filter(Boolean);
                if (!main.length || !quals.length) return main;

                let qualIndex = 0;
                let replaced = false;
                const filled = main.map(p => {{
                    if (_itfMainPlaceholderNames.has(String(p.name || '')) && qualIndex < quals.length) {{
                        replaced = true;
                        return quals[qualIndex++];
                    }}
                    return p;
                }});

                return replaced ? filled : main;
            }}

            function entryRankToNumber(rank) {{
                if (Number.isFinite(rank)) return rank;
                if (rank === null || rank === undefined) return 2000;
                const text = String(rank).trim();
                if (!text || text === '-') return 2000;
                // Only a plain numeric value is a WTA ranking. Values such as
                // "ITF 285", "WTN 17.08", and "JE" use incompatible scales.
                if (!/^\\d+(?:\\.\\d+)?$/.test(text)) return 2000;
                const value = parseFloat(text);
                return Number.isFinite(value) && value > 0 ? value : 2000;
            }}

            function entryDrawStrengthGM(players) {{
                const ranks = (players || []).map(p => entryRankToNumber(p.rank)).filter(n => Number.isFinite(n) && n > 0);
                if (!ranks.length) return null;
                const logSum = ranks.reduce((acc, v) => acc + Math.log(v), 0);
                return Math.exp(logSum / ranks.length);
            }}

            let _entryMenuGmMin = 0;
            let _entryMenuGmMax = 0;

            function styleEntryStrengthBadge(el, gm) {{
                const gmEl = el ? el.querySelector('.entry-gm-value') : null;
                if (!gmEl) return;
                gmEl.style.background = Number.isFinite(gm) ? entryMenuGmBadgeColor(gm, _entryMenuGmMin, _entryMenuGmMax) : '#94a3b8';
                gmEl.style.color = '#1a1a1a';
            }}

            function setEntryDrawStrength(players, key = '', allPlayers = null, byPosFn = null) {{
                const el = document.getElementById('entry-strength');
                if (!el) return;
                const isQual = String(key || '').includes('#qual');
                const isITF = !!key && !String(key).startsWith('http');
                let gmPlayers = players || [];
                if (isQual && (!gmPlayers || gmPlayers.length === 0)) {{
                    const sorter = byPosFn || ((a, b) => (Number((a && a.pos_num) ?? 999) - Number((b && b.pos_num) ?? 999)) || String((a && a.name) || '').localeCompare(String((b && b.name) || '')));
                    gmPlayers = (allPlayers || []).filter(p => p && p.type === 'QUAL').sort(sorter);
                }} else if (isITF) {{
                    const sorter = byPosFn || ((a, b) => (Number((a && a.pos_num) ?? 999) - Number((b && b.pos_num) ?? 999)) || String((a && a.name) || '').localeCompare(String((b && b.name) || '')));
                    const qualPlayers = (allPlayers || []).filter(p => p && p.type === 'QUAL').sort(sorter);
                    gmPlayers = entryFillITFMainPlaceholders(gmPlayers, qualPlayers);
                }}
                const gm = entryDrawStrengthGM(gmPlayers);
                el.innerHTML = '<span class="entry-gm-value">' + (gm ? gm.toFixed(1) : '-') + '</span>';
                styleEntryStrengthBadge(el, gm);
            }}

            function entryMenuGmBadgeColor(gm, min, max) {{
                if (!Number.isFinite(gm)) return '#94a3b8';
                if (!Number.isFinite(min) || !Number.isFinite(max) || max <= min) return 'rgba(148,163,184,0.65)';
                const t = Math.max(0, Math.min(1, (gm - min) / (max - min)));
                if (t < 0.25) {{
                    const p = t / 0.25;
                    const r = Math.round(34 + p * (234 - 34));
                    const g = Math.round(197 + p * (179 - 197));
                    const b = Math.round(94 + p * (8 - 94));
                    return `rgba(${{r}},${{g}},${{b}},0.72)`;
                }}
                if (t < 0.5) {{
                    const p = (t - 0.25) / 0.25;
                    const r = Math.round(234 + p * (239 - 234));
                    const g = Math.round(179 + p * (140 - 179));
                    const b = Math.round(8 + p * (16 - 8));
                    return `rgba(${{r}},${{g}},${{b}},0.72)`;
                }}
                if (t < 0.75) {{
                    const p = (t - 0.5) / 0.25;
                    const r = Math.round(239 + p * (239 - 239));
                    const g = Math.round(140 + p * (68 - 140));
                    const b = Math.round(16 + p * (68 - 16));
                    return `rgba(${{r}},${{g}},${{b}},0.72)`;
                }}
                const p = (t - 0.75) / 0.25;
                const r = Math.round(239 + p * (220 - 239));
                const g = Math.round(68 + p * (38 - 68));
                const b = Math.round(68 + p * (38 - 68));
                return `rgba(${{r}},${{g}},${{b}},0.72)`;
            }}

            function updateEntryMenuLabels() {{
                const items = Array.from(document.querySelectorAll('#view-entrylists .entry-menu-item'));
                const rows = [];
                items.forEach(item => {{
                    const key = item.getAttribute('data-key') || '';
                    const players = tournamentData[key];
                    if (!players) return;
                    const isITF = !key.startsWith('http');
                    const qualPlayers = players.filter(p => p && p.type === 'QUAL').sort(entryByPos);
                    const displayMain = key.includes('#qual') ? qualPlayers : entryGetDisplayMain(players, isITF);
                    const gmPlayers = key.includes('#qual') ? displayMain : (isITF ? entryFillITFMainPlaceholders(displayMain, qualPlayers) : displayMain);
                    const gm = entryDrawStrengthGM(gmPlayers);
                    rows.push({{ item, gm, gmEl: item.querySelector('.entry-menu-gm-value') }});
                }});
                const gmValues = rows.map(row => row.gm).filter(Number.isFinite);
                _entryMenuGmMin = gmValues.length ? Math.min(...gmValues) : 0;
                _entryMenuGmMax = gmValues.length ? Math.max(...gmValues) : 0;
                rows.forEach(({{ gm, gmEl }}) => {{
                    if (!gmEl) return;
                    gmEl.textContent = Number.isFinite(gm) ? gm.toFixed(1) : '-';
                    gmEl.style.background = Number.isFinite(gm) ? entryMenuGmBadgeColor(gm, _entryMenuGmMin, _entryMenuGmMax) : '#94a3b8';
                    gmEl.style.color = '#1a1a1a';
                }});
                const headerGm = document.querySelector('#entry-strength');
                if (headerGm) {{
                    const headerText = headerGm.querySelector('.entry-gm-value');
                    const current = headerText ? parseFloat(String(headerText.textContent || '').replace(/[^\\d.]/g, '')) : NaN;
                    styleEntryStrengthBadge(headerGm, current);
                }}
            }}

            function isMobileEntryLists() {{
                return window.matchMedia('(max-width: 900px)').matches;
            }}

            function syncEntryMenuToggle() {{
                const view = document.getElementById('view-entrylists');
                const btn = document.getElementById('btn-open-entry-menu');
                if (!view || !btn) return;
                const shouldShow = isMobileEntryLists() && view.classList.contains('entry-menu-collapsed');
                btn.hidden = !shouldShow;
            }}

            function setEntryMenuCollapsed(collapsed) {{
                const view = document.getElementById('view-entrylists');
                if (!view) return;
                view.classList.toggle('entry-menu-collapsed', !!collapsed);
                syncEntryMenuToggle();
            }}

            function openEntryTournamentList() {{
                setEntryMenuCollapsed(false);
            }}

            window.addEventListener('resize', syncEntryMenuToggle);

            function selectEntryTournament(el) {{
                document.querySelectorAll('#view-entrylists .entry-menu-item').forEach(item => item.classList.remove('active'));
                el.classList.add('active');
                if (isMobileEntryLists()) setEntryMenuCollapsed(true);
                const nameEl = el.querySelector('.entry-menu-name');
                updateEntryList(el.getAttribute('data-key'), nameEl ? nameEl.textContent : el.textContent);
            }}




            let _prioFilterActive = false;

            function togglePrio1() {{
                _prioFilterActive = !_prioFilterActive;
                document.getElementById('btn-prio1').textContent = _prioFilterActive ? 'Show All' : 'Show Prio 1';
                updateEntryList();
            }}

            function renderRows(list, isMain, isITF, renumber, showSeed, seedMap = null) {{
                const prioCell = p => isITF ? `<td>${{p.priority||''}}</td>` : '';
                const seedCell = p => {{
                    if (!showSeed) return '';
                    if (seedMap && seedMap.has(p)) return `<td>${{seedMap.get(p)}}</td>`;
                    return `<td>${{Number.isInteger(p.seed) ? p.seed : '-'}}</td>`;
                }};
                let html = '';
                list.forEach((p, i) => {{
                    const displayPos = renumber ? (i + 1) : p.pos;
                    const bold = isMain ? 'font-weight:bold;' : '';
                    const flag = (p.country && p.country !== '-') ? countryFlag(p.country, false) + ' ' : '';
                    const nameDisplay = p.name.startsWith('(') ? p.name : getDisplayName(p.name.toUpperCase());
                    html += `<tr><td>${{displayPos}}</td><td style="text-align:left;${{bold}}">${{flag}}${{nameDisplay}}</td>${{seedCell(p)}}<td>${{p.rank}}</td>${{prioCell(p)}}</tr>`;
                }});
                return html;
            }}

            function updateEntryList(key, name) {{
                if (!key) {{
                    const active = document.querySelector('.entry-menu-item.active');
                    if (!active) return;
                    key = active.getAttribute('data-key');
                    name = entryMenuNameForItem(active);
                }}
                const body = document.getElementById('entry-body');
                document.getElementById('entry-title').textContent = name || 'Entry List';
                if (!tournamentData[key]) return;
                const players = tournamentData[key];
                const isITF = !key.startsWith('http');
                document.getElementById('entry-prio-header').style.display = isITF ? '' : 'none';
                const btn = document.getElementById('btn-prio1');
                btn.hidden = !isITF;
                if (!isITF) _prioFilterActive = false;
                btn.textContent = _prioFilterActive ? 'Show All' : 'Show Prio 1';
                const showSeed = players.some(p => Number.isInteger(p.seed));
                document.getElementById('entry-seed-header').style.display = showSeed ? '' : 'none';
                let html = '';
                const rankScore = p => {{
                    const r = String(p.rank || '');
                    const m = r.match(/\\d+(\\.\\d+)?/);
                    const n = m ? parseFloat(m[0]) : 9999;
                    if (r.startsWith('WTN')) return [2, n];
                    if (r.startsWith('ITF')) return [1, n];
                    return [0, n];
                }};
                const byRank = (a, b) => {{
                    const [ta, na] = rankScore(a);
                    const [tb, nb] = rankScore(b);
                    return (ta - tb) || (na - nb) || String(a.name || '').localeCompare(String(b.name || ''));
                }};
                const byPos = (a, b) => (Number(a.pos_num ?? 999) - Number(b.pos_num ?? 999))
                    || String(a.name || '').localeCompare(String(b.name || ''));
                const sortEL = list => {{
                    const mdo = list.filter(p => p.pos === 'MDO').sort(byRank);
                    const numbered = list.filter(p => p.pos !== 'MDO').sort(byPos);
                    if (mdo.length === 0) return numbered;
                    const used = new Set(numbered.map(p => p.pos_num));
                    const maxPos = numbered.length > 0 ? Math.max(...numbered.map(p => p.pos_num)) : 0;
                    const gaps = [];
                    for (let i = 1; i <= maxPos; i++) {{ if (!used.has(i)) gaps.push(i); }}
                    const result = [];
                    let mi = 0, gi = 0, overflow = 1;
                    for (const p of numbered) {{
                        while (mi < mdo.length && byRank(mdo[mi], p) < 0) {{
                            const pos_num = gi < gaps.length ? gaps[gi++] : maxPos + overflow++;
                            result.push({{...mdo[mi++], pos_num}});
                        }}
                        result.push(p);
                    }}
                    while (mi < mdo.length) {{
                        const pos_num = gi < gaps.length ? gaps[gi++] : maxPos + overflow++;
                        result.push({{...mdo[mi++], pos_num}});
                    }}
                    return result;
                }};
                const main = players.filter(p => p.type === 'MAIN').sort(byPos);
                const qual = sortEL(players.filter(p => p.type === 'QUAL'));
                const alt = sortEL(players.filter(p => p.type === 'ALT'));
                const cols = (isITF ? 5 : 4) + (showSeed ? 1 : 0);
                let displayMain = main;
                let displayQual = qual;
                let displayAlt = alt;
                let prioSeedMap = null;

                if (_prioFilterActive) {{
                    // JR prio1 players go at the bottom; non-prio1 JR spots filled from qual/alt
                    const mainJRPrio1 = main.filter(p => (p.entry === 'JR' || p.entry === 'JA') && p.priority === '1');
                    const mainRegular = main.filter(p => p.entry !== 'JR' && p.entry !== 'JA');
                    const regularSpots = main.length - mainJRPrio1.length;
                    const pool = [
                        ...mainRegular.filter(p => p.priority === '1'),
                        ...qual.filter(p => p.priority === '1'),
                        ...alt.filter(p => p.priority === '1'),
                    ];
                    displayMain = [
                        ...pool.slice(0, regularSpots),
                        ...mainJRPrio1,
                    ];
                    const seedSlots = Math.max(0, ...main
                        .map(p => Number.isInteger(p.seed) ? p.seed : 0));
                    const seedRankValue = p => {{
                        const n = Number(p.seed_rank);
                        return Number.isFinite(n) && n > 0 ? n : null;
                    }};
                    const bySeedRank = (a, b) => {{
                        const sa = Number.isInteger(a.seed) ? a.seed : 9999;
                        const sb = Number.isInteger(b.seed) ? b.seed : 9999;
                        if (sa !== sb) return sa - sb;
                        const ra = seedRankValue(a);
                        const rb = seedRankValue(b);
                        if (ra !== null || rb !== null) {{
                            return ((ra ?? 9999) - (rb ?? 9999)) || byRank(a, b);
                        }}
                        return byRank(a, b);
                    }};
                    const prioSeedCandidates = displayMain
                        .filter(p => !(String(p.name || '').startsWith('(')))
                        .sort(bySeedRank)
                        .slice(0, seedSlots);
                    prioSeedMap = new Map(prioSeedCandidates.map((p, i) => [p, i + 1]));
                    const remainingPool = pool.slice(regularSpots);
                    displayQual = remainingPool.slice(0, qual.length);
                    displayAlt  = remainingPool.slice(qual.length);
                    html += renderRows(displayMain, true, isITF, true, showSeed, prioSeedMap);
                    if (displayQual.length > 0) {{
                        html += `<tr class="divider-row"><td colspan="${{cols}}">QUALIFYING</td></tr>`;
                        html += renderRows(displayQual, false, isITF, true, showSeed);
                    }}
                    if (displayAlt.length > 0) {{
                        html += `<tr class="divider-row"><td colspan="${{cols}}">ALTERNATES</td></tr>`;
                        html += renderRows(displayAlt, false, isITF, true, showSeed);
                    }}
                }} else {{
                    html += renderRows(displayMain, true, isITF, false, showSeed);
                    if (qual.length > 0) {{
                        html += `<tr class="divider-row"><td colspan="${{cols}}">QUALIFYING</td></tr>`;
                        html += renderRows(qual, false, isITF, false, showSeed);
                    }}
                    if (alt.length > 0) {{
                        html += `<tr class="divider-row"><td colspan="${{cols}}">ALTERNATES</td></tr>`;
                        html += renderRows(alt, false, isITF, false, showSeed);
                    }}
                }}
                body.innerHTML = html;
                setEntryDrawStrength(displayMain, key, players, byPos);
                updateEntryMenuLabels();
                syncEntryMenuToggle();
                syncUrlStateForTab('entrylists');
            }}


            function renderHistoryTable() {{
                const thead = document.getElementById('history-head');
                const tbody = document.getElementById('history-body');
                if (!thead || !tbody || _historyTableInitialized) return;

                // Define column headers (excluding hidden _ columns)
                const displayColumns = ['DATE', 'TOURNAMENT', 'SURFACE', 'RND', 'PLAYER', 'SCORE', 'OPPONENT'];
                let headHtml = '<tr>';
                displayColumns.forEach(col => {{
                    const headerText = col.replace('_', ' ');
                    headHtml += `<th>${{headerText}}</th>`;
                }});
                headHtml += '</tr>';
                thead.innerHTML = headHtml;

                // Set initial placeholder message
                tbody.innerHTML = `<tr><td colspan="${{displayColumns.length}}" class="cell-state-info">Select a player to view their matches</td></tr>`;
                _historyTableInitialized = true;
            }}

            let currentPlayerData = [];
            const HISTORY_SUBPAGE_MATCH = 'match';
            const HISTORY_SUBPAGE_MILESTONES = 'milestones';
            let historySubpage = HISTORY_SUBPAGE_MATCH;

            let _historyTableInitialized = false;
            let _milestonesTableInitialized = false;
            let _historyPlayerUniverse = null;
            let _historyPlayerUniverseUpper = null;
            let _milestonesIndex = null;
            let _milestonesIndexPromise = null;
            let _milestonesRenderSeq = 0;
            let _milestonesCategoryDefs = null;

            function renderMilestonesTable() {{
                const thead = document.querySelector('#milestones-table thead');
                const tbody = document.getElementById('milestones-body');
                if (!thead || !tbody || _milestonesTableInitialized) return;

                thead.innerHTML = '<tr><th>PLAYER</th><th>WINS</th></tr>';
                tbody.innerHTML = '<tr><td colspan="2" class="cell-state-info">Open Milestones to view the leaderboard</td></tr>';
                _milestonesTableInitialized = true;
            }}

            function _getMilestonesCategorySortRank(label) {{
                const priority = [
                    'GS',
                    'WTA 1000 / P5 / PM',
                    'WTA 500 / P700',
                    'WTA 250 / International',
                    'WTA 125 / 125K Series',
                    'ITF',
                    'BJKC/Fed Cup',
                    'Olympic Games',
                    'Tier I',
                    'Tier II',
                    'Tier III',
                    'Tier IV',
                    'Tier V',
                    'Tier 2',
                    'Tier',
                    'WTA 1000',
                    'WTA 500',
                    'WTA 250',
                    'WTA 125',
                    '125K',
                    '125K Series',
                    'Premier Mandatory',
                    'Premier 5',
                    'Premier 700',
                    'Premier',
                    'International',
                    'International Gold',
                    'WTA',
                    'World Tour',
                    'WT',
                    'WTA Tour Championships',
                    'YE Championships'
                ];
                const idx = priority.indexOf(label);
                return idx >= 0 ? idx : 1000;
            }}

            function _sortMilestonesCategoryLabels(labels) {{
                return Array.from(labels).sort((a, b) => {{
                    const rankA = _getMilestonesCategorySortRank(a);
                    const rankB = _getMilestonesCategorySortRank(b);
                    if (rankA !== rankB) return rankA - rankB;
                    return a.localeCompare(b);
                }});
            }}

            function getMilestonesCategoryDisplayLabel(label) {{
                if (label === 'GS') return 'Grand Slams';
                return label;
            }}

            function getMilestonesCategoryGroup(row) {{
                const rawCategory = (row['CATEGORY'] || row['tournamentCategory'] || '').toString().trim();
                const matchType = getRowMatchType(row).toString().trim();
                const tournament = (row['TOURNAMENT'] || row['tournamentName'] || '').toString().trim();
                const categoryUpper = rawCategory.toUpperCase();
                const matchTypeUpper = matchType.toUpperCase();
                const tournamentUpper = tournament.toUpperCase();
                // Keep short codes exact so tournament names like Oegstgeest or Bogota do not false-match.
                const isExact = (...values) => values.some(value => categoryUpper === value || matchTypeUpper === value);
                const tournamentHas = (...values) => values.some(value => tournamentUpper.includes(value));
                const grandSlamNames = ['AUSTRALIAN OPEN', 'ROLAND GARROS', 'WIMBLEDON', 'US OPEN'];

                if (isExact('FED/BJK CUP') || tournamentHas('FED CUP', 'BJK CUP', 'BILLIE JEAN KING CUP', 'BJKC')) return 'BJKC/Fed Cup';
                if (isExact('OG') || tournamentHas('OLYMPIC')) return 'Olympic Games';
                if (isExact('GS') || categoryUpper.includes('GRAND SLAM') || tournamentHas('GRAND SLAM') || grandSlamNames.includes(tournamentUpper)) return 'GS';
                if (matchTypeUpper === 'ITF' || categoryUpper === 'ITF' || /^W\\d+$/.test(categoryUpper) || tournamentUpper.includes('ITF')) return 'ITF';
                if (isExact('WTA 1000', 'PREMIER MANDATORY', 'PREMIER 5')) return 'WTA 1000 / P5 / PM';
                if (isExact('WTA 500', 'PREMIER 700', 'PREMIER')) return 'WTA 500 / P700';
                if (isExact('WTA 250', 'INTERNATIONAL', 'INTERNATIONAL GOLD')) return 'WTA 250 / International';
                if (isExact('WTA 125', '125K', '125K SERIES')) return 'WTA 125 / 125K Series';
                if (!rawCategory) {{
                    if (matchTypeUpper === 'WTA') return 'WTA';
                    if (matchTypeUpper === 'OG') return 'Olympic Games';
                    if (matchTypeUpper === 'FED/BJK CUP') return 'BJKC/Fed Cup';
                }}
                if (categoryUpper === 'TIER IIIV') return 'Tier III';
                return rawCategory;
            }}

            function getMilestonesCategoryDefs() {{
                if (!_milestonesIndex) return [];
                if (_milestonesCategoryDefs) return _milestonesCategoryDefs;
                const labels = new Set();
                _milestonesIndex.forEach(stat => {{
                    if (!stat || !stat.active || !stat.playedCategories) return;
                    stat.playedCategories.forEach(label => {{
                        if (label) labels.add(label);
                    }});
                }});
                _milestonesCategoryDefs = _sortMilestonesCategoryLabels(labels).map((label, idx) => ({{
                    id: `milestones-filter-${{idx}}`,
                    key: label,
                    label: getMilestonesCategoryDisplayLabel(label)
                }}));
                return _milestonesCategoryDefs;
            }}

            function renderMilestonesFilters() {{
                const body = document.getElementById('milestones-filter-body');
                if (!body) return;
                const defs = getMilestonesCategoryDefs();
                if (body.children.length) return;
                const filterHtml = defs.map(def => (
                    `<label class="milestones-filter-chip" for="${{def.id}}">
                        <input type="checkbox" id="${{def.id}}" checked onchange="applyMilestonesFilters()">
                        <span>${{escapeHtml(def.label)}}</span>
                    </label>`
                )).join('');
                body.innerHTML = `${{filterHtml}}<label class="milestones-filter-chip" for="milestones-filter-qualy"><input type="checkbox" id="milestones-filter-qualy" checked onchange="applyMilestonesFilters()"><span>Include Qualy</span></label>`;
            }}

            function getHistoryPlayerUniverse() {{
                if (_historyPlayerUniverse && _historyPlayerUniverseUpper) return _historyPlayerUniverse;
                const select = document.getElementById('playerHistorySelect');
                const names = [];
                const upper = new Set();
                if (select && select.options) {{
                    Array.from(select.options).forEach(option => {{
                        const value = (option.value || '').toString().trim();
                        if (!value || value === '__ALL__' || value === 'Select Player...') return;
                        const upperValue = value.toUpperCase();
                        if (upper.has(upperValue)) return;
                        upper.add(upperValue);
                        names.push(value);
                    }});
                }}
                _historyPlayerUniverse = names;
                _historyPlayerUniverseUpper = upper;
                return names;
            }}

            function isHistoryPlayerName(name) {{
                if (!name) return false;
                if (!_historyPlayerUniverseUpper) getHistoryPlayerUniverse();
                return !!_historyPlayerUniverseUpper && _historyPlayerUniverseUpper.has(name.toString().toUpperCase());
            }}

            function isWalkoverOrByeHistoryRow(row) {{
                const statusDesc = (row['_resultStatusDesc'] || '').toString().toLowerCase();
                const scoreText = (row['SCORE'] || '').toString().toLowerCase();
                return statusDesc.includes('walkover') || statusDesc.includes('bye') || scoreText.includes('w/o') || scoreText === '-';
            }}

            function isMilestonesQualifyingRow(row) {{
                const draw = (row['DRAW'] || '').toString().trim().toUpperCase();
                const round = (row['ROUND'] || '').toString().trim().toUpperCase();
                return draw === 'Q' || draw.includes('QUAL') || round === 'Q' || /^Q\\d+$/.test(round) || round.startsWith('QR');
            }}

            async function ensureMilestonesIndex() {{
                if (_milestonesIndex) return _milestonesIndex;
                if (!_milestonesIndexPromise) {{
                    _milestonesIndexPromise = (async function() {{
                        await ensureHistoryDataLoaded();
                        _milestonesIndex = buildMilestonesIndex();
                        return _milestonesIndex;
                    }})();
                }}
                return _milestonesIndexPromise;
            }}

            function buildMilestonesIndex() {{
                const universe = getHistoryPlayerUniverse();
                const playerByUpper = new Map(universe.map(name => [name.toUpperCase(), name]));
                const stats = new Map();
                const recentCutoff = new Date();
                recentCutoff.setFullYear(recentCutoff.getFullYear() - 2);
                recentCutoff.setHours(0, 0, 0, 0);

                function createStats(name) {{
                    return {{
                        name,
                        lastPlayed: null,
                        active: false,
                        wins: {{}},
                        playedCategories: new Set()
                    }};
                }}

                function getStats(name) {{
                    if (!stats.has(name)) stats.set(name, createStats(name));
                    return stats.get(name);
                }}

                function touchActive(entry, date) {{
                    const ts = date.getTime();
                    if (entry.lastPlayed === null || ts > entry.lastPlayed) entry.lastPlayed = ts;
                    if (date >= recentCutoff) entry.active = true;
                }}

                (Array.isArray(historyData) ? historyData : []).forEach(row => {{
                    if (isWalkoverOrByeHistoryRow(row)) return;
                    const rowDate = new Date(row['DATE'] || '');
                    if (isNaN(rowDate)) return;

                    const winnerName = getDisplayName((row['_winnerName'] || '').toString().toUpperCase());
                    const loserName = getDisplayName((row['_loserName'] || '').toString().toUpperCase());
                    const winnerKey = winnerName ? winnerName.toUpperCase() : '';
                    const loserKey = loserName ? loserName.toUpperCase() : '';
                    const winner = winnerKey ? playerByUpper.get(winnerKey) : '';
                    const loser = loserKey ? playerByUpper.get(loserKey) : '';

                    if (winner) touchActive(getStats(winner), rowDate);
                    if (loser) touchActive(getStats(loser), rowDate);

                    const categoryGroup = getMilestonesCategoryGroup(row);
                    if (categoryGroup) {{
                        if (winner) getStats(winner).playedCategories.add(categoryGroup);
                        if (loser) getStats(loser).playedCategories.add(categoryGroup);
                    }}
                    if (!categoryGroup || !winner) return;
                    const stat = getStats(winner);
                    if (!stat.wins[categoryGroup]) {{
                        stat.wins[categoryGroup] = {{ main: 0, qualy: 0 }};
                    }}
                    if (isMilestonesQualifyingRow(row)) {{
                        stat.wins[categoryGroup].qualy += 1;
                    }} else {{
                        stat.wins[categoryGroup].main += 1;
                    }}
                }});

                universe.forEach(name => {{
                    if (!stats.has(name)) stats.set(name, createStats(name));
                }});

                return stats;
            }}

            function getMilestonesFilterState() {{
                const defs = getMilestonesCategoryDefs();
                return {{
                    categories: defs.filter(def => {{
                        const el = document.getElementById(def.id);
                        return el ? el.checked : false;
                    }}).map(def => def.key),
                    includeQualy: !!(document.getElementById('milestones-filter-qualy') && document.getElementById('milestones-filter-qualy').checked)
                }};
            }}

            function updateMilestonesCounter(count) {{
                const counter = document.getElementById('milestones-active-counter');
                if (!counter) return;
                counter.textContent = '';
            }}

            async function renderMilestonesPage() {{
                const tbody = document.getElementById('milestones-body');
                if (!tbody) return;
                renderMilestonesTable();
                const renderSeq = ++_milestonesRenderSeq;
                tbody.innerHTML = '<tr><td colspan="2" class="cell-state-info">Loading milestones...</td></tr>';
                try {{
                    await ensureMilestonesIndex();
                    renderMilestonesFilters();
                }} catch (err) {{
                    console.error('Failed to load milestones data:', err);
                    if (renderSeq !== _milestonesRenderSeq) return;
                    tbody.innerHTML = '<tr><td colspan="2" class="cell-state-error">Failed to load milestones. Please refresh and try again.</td></tr>';
                    updateMilestonesCounter(0);
                    return;
                }}

                if (renderSeq !== _milestonesRenderSeq) return;
                renderMilestonesFilters();
                const selection = getMilestonesFilterState();
                const activePlayers = [];
                const categorySet = new Set(selection.categories);
                const stats = _milestonesIndex || new Map();

                stats.forEach(stat => {{
                    if (!stat.active) return;
                    let totalWins = 0;
                    categorySet.forEach(category => {{
                        const bucket = stat.wins[category];
                        if (!bucket) return;
                        totalWins += bucket.main + (selection.includeQualy ? bucket.qualy : 0);
                    }});
                    if (totalWins <= 0) return;
                    activePlayers.push({{
                        name: stat.name,
                        wins: totalWins,
                        lastPlayed: stat.lastPlayed || 0
                    }});
                }});

                activePlayers.sort((a, b) => {{
                    if (b.wins !== a.wins) return b.wins - a.wins;
                    if (b.lastPlayed !== a.lastPlayed) return b.lastPlayed - a.lastPlayed;
                    return a.name.localeCompare(b.name);
                }});

                updateMilestonesCounter(activePlayers.length);

                if (activePlayers.length === 0) {{
                    tbody.innerHTML = '<tr><td colspan="2" class="cell-state-error">No players found for the selected filters.</td></tr>';
                    return;
                }}

                tbody.innerHTML = activePlayers.map(player => (
                    `<tr><td>${{escapeHtml(player.name)}}</td><td>${{player.wins}}</td></tr>`
                )).join('');
            }}

            function applyMilestonesFilters() {{
                renderMilestonesPage().then(() => syncUrlStateForTab('history'));
            }}

            function syncHistorySubpageVisibility() {{
                const historyLayout = document.querySelector('#view-history .history-layout');
                const filterPanel = historyLayout ? historyLayout.querySelector('.filter-panel') : null;
                const matchPage = document.getElementById('history-match-page');
                const milestonesPage = document.getElementById('history-milestones-page');
                if (filterPanel) filterPanel.style.display = historySubpage === HISTORY_SUBPAGE_MATCH ? '' : 'none';
                if (matchPage) matchPage.style.display = historySubpage === HISTORY_SUBPAGE_MATCH ? 'flex' : 'none';
                if (milestonesPage) milestonesPage.style.display = historySubpage === HISTORY_SUBPAGE_MILESTONES ? 'flex' : 'none';
            }}

            function setHistorySubpage(page) {{
                historySubpage = page === HISTORY_SUBPAGE_MILESTONES ? HISTORY_SUBPAGE_MILESTONES : HISTORY_SUBPAGE_MATCH;
                if (historySubpage !== HISTORY_SUBPAGE_MATCH) closeHistoryFilters(false);
                syncHistorySubpageVisibility();
                if (historySubpage === HISTORY_SUBPAGE_MILESTONES) {{
                    renderMilestonesPage();
                }} else {{
                    applyMobileHistoryLayout();
                    applyHistoryFilters();
                }}
                syncUrlStateForTab('history');
            }}

            function syncFilterGroupState(group) {{
                if (!group) return;
                const title = group.querySelector('.filter-group-title');
                if (title) {{
                    title.setAttribute('aria-expanded', group.classList.contains('collapsed') ? 'false' : 'true');
                }}
            }}

            function toggleFilterGroup(element) {{
                const group = element.closest('.filter-group');
                if (!group) return;
                group.classList.toggle('collapsed');
                syncFilterGroupState(group);
            }}

            function toggleRankFilterGroup(element) {{
                const group = element.closest('.filter-group');
                if (!group) return;
                const row = group.closest('.rank-filter-last-row');
                if (row) {{
                    row.querySelectorAll('.filter-group').forEach(g => {{
                        if (g !== group) {{
                            g.classList.add('collapsed');
                            syncFilterGroupState(g);
                        }}
                    }});
                }}
                group.classList.toggle('collapsed');
                syncFilterGroupState(group);
            }}

            function getRowMatchType(row) {{
                const explicit = (row['MATCH_TYPE'] || row['matchType'] || '').toString().trim();
                if (explicit) return explicit;

                // Backward-compatible fallback for older rows without matchType.
                const tournament = (row['TOURNAMENT'] || '').toString();
                const isITF = tournament.includes('ITF') || tournament.includes('W15') || tournament.includes('W25') ||
                              tournament.includes('W35') || tournament.includes('W50') || tournament.includes('W60') ||
                              tournament.includes('W75') || tournament.includes('W100');
                return isITF ? 'ITF' : 'WTA';
            }}

            function getRowYear(row) {{
                const dateStr = (row['DATE'] || '').toString().trim();
                const match = dateStr.match(/^(\\d{{4}})/);
                return match ? match[1] : '';
            }}

            function getResultLabel(row, isWinner) {{
                const statusDesc = (row['_resultStatusDesc'] || '').toString().toLowerCase();
                const scoreText = (row['SCORE'] || '').toString().toLowerCase();
                const isRet = statusDesc.includes('retired') || statusDesc.includes('ret.') || scoreText.includes('ret.');
                const isDef = statusDesc.includes('default') || statusDesc.includes('def.') || scoreText.includes('def.');

                if (isWinner) {{
                    if (isRet) return 'Wins by RET';
                    if (isDef) return 'Wins by DEF';
                    return 'Wins';
                }}
                if (isRet) return 'Losses by RET';
                if (isDef) return 'Losses by DEF';
                return 'Losses';
            }}

            function isTeamEventRow(row) {{
                const matchType = (row['MATCH_TYPE'] || row['matchType'] || '').toString();
                const category = (row['CATEGORY'] || row['tournamentCategory'] || '').toString();
                const tournament = (row['TOURNAMENT'] || row['tournamentName'] || '').toString();
                return (
                    matchType === 'Fed/BJK Cup' ||
                    category.includes('Fed/BJK Cup') ||
                    tournament.includes('BJK') ||
                    tournament.includes('Fed Cup')
                );
            }}

            function isDoublesHistoryRow(row) {{
                const wName = (row['_winnerName'] || '').toString();
                const lName = (row['_loserName'] || '').toString();
                const playerName = (row['PLAYER'] || '').toString();
                const opponentName = (row['OPPONENT'] || '').toString();
                return (
                    wName.includes('/') ||
                    lName.includes('/') ||
                    playerName.includes('/') ||
                    opponentName.includes('/')
                );
            }}

            function getHistoryPerspective(row, selectedPlayer) {{
                const winnerNameRaw = (row['_winnerName'] || "").toString().toUpperCase();
                const loserNameRaw = (row['_loserName'] || "").toString().toUpperCase();
                const winnerNameNormalized = getDisplayName(winnerNameRaw).toUpperCase();
                const loserNameNormalized = getDisplayName(loserNameRaw).toUpperCase();
                const winnerCountry = (row['_winnerCountry'] || '').toString().trim().toUpperCase();
                const loserCountry = (row['_loserCountry'] || '').toString().trim().toUpperCase();

                // Always keep the ARG side in PLAYER when only one side is ARG.
                if (winnerCountry === 'ARG' && loserCountry !== 'ARG') {{
                    return {{
                        isWinner: true,
                        winnerNameRaw,
                        loserNameRaw,
                        winnerNameNormalized,
                        loserNameNormalized
                    }};
                }}
                if (loserCountry === 'ARG' && winnerCountry !== 'ARG') {{
                    return {{
                        isWinner: false,
                        winnerNameRaw,
                        loserNameRaw,
                        winnerNameNormalized,
                        loserNameNormalized
                    }};
                }}

                // If both are ARG (or neither), preserve selected-player perspective when possible.
                if (selectedPlayer && selectedPlayer !== '__ALL__') {{
                    if (winnerNameNormalized === selectedPlayer) {{
                        return {{
                            isWinner: true,
                            winnerNameRaw,
                            loserNameRaw,
                            winnerNameNormalized,
                            loserNameNormalized
                        }};
                    }}
                    if (loserNameNormalized === selectedPlayer) {{
                        return {{
                            isWinner: false,
                            winnerNameRaw,
                            loserNameRaw,
                            winnerNameNormalized,
                            loserNameNormalized
                        }};
                    }}
                }}

                return {{
                    isWinner: true,
                    winnerNameRaw,
                    loserNameRaw,
                    winnerNameNormalized,
                    loserNameNormalized
                }};
            }}

            function _formatTournName(name, category) {{
                if (!name) return '';
                if (name.toUpperCase().includes('MALLORCA')) return 'WTA 125 Mallorca';
                const sep = name.lastIndexOf(' - ');
                if (sep === -1) return name;
                let city = name.slice(sep + 3);
                const comma = city.indexOf(',');
                if (comma !== -1) city = city.slice(0, comma).trim();
                return category ? category + ' ' + city : city;
            }}

            function getRoundFilterLabel(row) {{
                const roundValue = (row['ROUND'] || '').toString().trim();
                if (!roundValue) return '';
                const abbr = displayRound(roundValue, row['TOURNAMENT_ID'] || '', row['DATE'] || '',
                    row['TOURNAMENT'] || '', row['CATEGORY'] || '', row['MATCH_TYPE'] || '', row['DRAW'] || '');
                return isTeamEventRow(row) ? 'Team - ' + abbr : abbr;
            }}

            function escapeHtml(value) {{
                return String(value ?? '').replace(/[&<>"']/g, ch => ({{
                    '&': '&amp;',
                    '<': '&lt;',
                    '>': '&gt;',
                    '"': '&quot;',
                    "'": '&#39;'
                }}[ch]));
            }}

            function normalizeHistoryFilterValues(values) {{
                if (Array.isArray(values)) return values.filter(Boolean);
                return values ? [values] : [];
            }}

            function mergeUniqueHistoryFilterValues(values, selectedValues) {{
                const merged = [];
                const seen = new Set();
                [...normalizeHistoryFilterValues(values), ...normalizeHistoryFilterValues(selectedValues)].forEach(value => {{
                    if (!value || seen.has(value)) return;
                    seen.add(value);
                    merged.push(value);
                }});
                return merged;
            }}

            function getHistoryFilterSelectionState() {{
                const tournSelect = document.getElementById('filter-tournament-select');
                const oppSelect = document.getElementById('filter-opponent-select');
                const asRankInput = document.getElementById('filter-as-rank');
                const asRankModeEl = document.getElementById('filter-as-rank-mode');
                const vsRankInput = document.getElementById('filter-vs-rank');
                const vsRankModeEl = document.getElementById('filter-vs-rank-mode');

                return {{
                    surfaces: getSelectedFilterValues('filter-surface'),
                    rounds: getSelectedFilterValues('filter-round'),
                    results: getSelectedFilterValues('filter-result'),
                    years: getSelectedFilterValues('filter-year'),
                    tournament: tournSelect ? tournSelect.value : '',
                    categories: getSelectedFilterValues('filter-category'),
                    opponent: oppSelect ? oppSelect.value : '',
                    opponentCountries: getSelectedFilterValues('filter-opponent-country'),
                    playerEntries: getSelectedFilterValues('filter-player-entry'),
                    seeds: getSelectedFilterValues('filter-seed'),
                    matchTypes: getSelectedFilterValues('filter-match-type'),
                    asRankVal: asRankInput && asRankInput.value ? parseInt(asRankInput.value, 10) : null,
                    asRankMode: asRankModeEl ? asRankModeEl.value : 'higher',
                    vsRankVal: vsRankInput && vsRankInput.value ? parseInt(vsRankInput.value, 10) : null,
                    vsRankMode: vsRankModeEl ? vsRankModeEl.value : 'higher'
                }};
            }}

            function rowMatchesHistoryFilters(row, filterState, selectedPlayer, excludedFilterKey = '') {{
                if (isDoublesHistoryRow(row)) return false;

                const perspective = getHistoryPerspective(row, selectedPlayer);
                const isWinner = perspective.isWinner;
                const surface = row['SURFACE'] || '';
                const roundFilterLabel = getRoundFilterLabel(row);
                const resultLabel = getResultLabel(row, isWinner);
                const rowYear = getRowYear(row);
                const tournamentDisplay = _formatTournName(row['TOURNAMENT'] || '', row['CATEGORY'] || '');
                const rowCategory = row['CATEGORY'] || '';
                const opponentName = isWinner ? (row['_loserName'] || '') : (row['_winnerName'] || '');
                const opponentDisplay = opponentName ? getDisplayName(opponentName.toUpperCase()) : '';
                const opponentCountry = isWinner ? (row['_loserCountry'] || '') : (row['_winnerCountry'] || '');
                const playerEntry = isWinner ? (row['_winnerEntry'] || '') : (row['_loserEntry'] || '');
                const playerSeed = isWinner ? (row['_winnerSeed'] || '') : (row['_loserSeed'] || '');
                const hasSeed = playerSeed ? 'Yes' : 'No';
                const matchType = getRowMatchType(row);

                if (excludedFilterKey !== 'surface' && filterState.surfaces.length > 0 && !filterState.surfaces.includes(surface)) return false;
                if (excludedFilterKey !== 'round' && filterState.rounds.length > 0 && !filterState.rounds.includes(roundFilterLabel)) return false;
                if (excludedFilterKey !== 'result' && filterState.results.length > 0 && !filterState.results.includes(resultLabel)) return false;
                if (excludedFilterKey !== 'year' && filterState.years.length > 0 && !filterState.years.includes('Career')) {{
                    const wantLast52 = filterState.years.includes('Last 52');
                    const otherYears = filterState.years.filter(y => y !== 'Last 52');
                    let pass = false;
                    if (wantLast52) {{
                        const today = new Date();
                        const dayOfWeek = today.getDay() === 0 ? 6 : today.getDay() - 1; // Mon=0
                        const weekStart = new Date(today);
                        weekStart.setDate(today.getDate() - dayOfWeek);
                        weekStart.setHours(0, 0, 0, 0);
                        const cutoff = new Date(weekStart);
                        cutoff.setDate(weekStart.getDate() - 51 * 7);
                        const rowDate = new Date(row['DATE'] || '');
                        if (!isNaN(rowDate) && rowDate >= cutoff) pass = true;
                    }}
                    if (!pass && otherYears.length > 0 && otherYears.includes(rowYear)) pass = true;
                    if (!pass) return false;
                }}
                if (excludedFilterKey !== 'tournament' && filterState.tournament && tournamentDisplay !== filterState.tournament) return false;
                if (excludedFilterKey !== 'category' && filterState.categories.length > 0 && !filterState.categories.includes(rowCategory)) return false;
                if (excludedFilterKey !== 'opponent' && filterState.opponent) {{
                    if (opponentDisplay !== filterState.opponent) return false;
                }}
                if (excludedFilterKey !== 'opponentCountry' && filterState.opponentCountries.length > 0 && !filterState.opponentCountries.includes(opponentCountry)) return false;
                if (excludedFilterKey !== 'playerEntry' && filterState.playerEntries.length > 0 && !filterState.playerEntries.includes(playerEntry)) return false;
                if (excludedFilterKey !== 'seed' && filterState.seeds.length > 0 && !filterState.seeds.includes(hasSeed)) return false;
                if (excludedFilterKey !== 'matchType' && filterState.matchTypes.length > 0 && !filterState.matchTypes.includes(matchType)) return false;
                if (excludedFilterKey !== 'asRank' && filterState.asRankVal !== null) {{
                    const pr = parseInt(isWinner ? (row['_winnerRank'] || '') : (row['_loserRank'] || ''), 10);
                    if (isNaN(pr)) return false;
                    if (filterState.asRankMode === 'higher' && pr > filterState.asRankVal) return false;
                    if (filterState.asRankMode === 'lower' && pr < filterState.asRankVal) return false;
                }}
                if (excludedFilterKey !== 'vsRank' && filterState.vsRankVal !== null) {{
                    const vr = parseInt(isWinner ? (row['_loserRank'] || '') : (row['_winnerRank'] || ''), 10);
                    if (isNaN(vr)) return false;
                    if (filterState.vsRankMode === 'higher' && vr > filterState.vsRankVal) return false;
                    if (filterState.vsRankMode === 'lower' && vr < filterState.vsRankVal) return false;
                }}

                return true;
            }}

            function collectHistoryFilterValues(sourceRows, filterState, selectedPlayer, excludedFilterKey, valueGetter) {{
                const values = new Set();
                (Array.isArray(sourceRows) ? sourceRows : []).forEach(row => {{
                    if (!rowMatchesHistoryFilters(row, filterState, selectedPlayer, excludedFilterKey)) return;
                    const value = valueGetter(row);
                    if (value) values.add(value);
                }});
                return Array.from(values);
            }}

            function populateFilters(playerMatches, selectedState = null) {{
                const selectionState = selectedState || getHistoryFilterSelectionState();
                const selectedPlayer = getNormalizedPlayerSelection('playerHistorySelect');

                const surfaces = collectHistoryFilterValues(playerMatches, selectionState, selectedPlayer, 'surface', row => row['SURFACE'] || '');
                const rounds = collectHistoryFilterValues(playerMatches, selectionState, selectedPlayer, 'round', row => getRoundFilterLabel(row));
                const resultsSet = new Set(collectHistoryFilterValues(playerMatches, selectionState, selectedPlayer, 'result', row => getResultLabel(row, getHistoryPerspective(row, selectedPlayer).isWinner)));
                const years = collectHistoryFilterValues(playerMatches, selectionState, selectedPlayer, 'year', row => getRowYear(row));
                const tournaments = collectHistoryFilterValues(playerMatches, selectionState, selectedPlayer, 'tournament', row => _formatTournName(row['TOURNAMENT'] || '', row['CATEGORY'] || ''));
                const categories = collectHistoryFilterValues(playerMatches, selectionState, selectedPlayer, 'category', row => row['CATEGORY'] || '');
                const opponents = collectHistoryFilterValues(playerMatches, selectionState, selectedPlayer, 'opponent', row => {{
                    const perspective = getHistoryPerspective(row, selectedPlayer);
                    const opponentName = perspective.isWinner ? (row['_loserName'] || '') : (row['_winnerName'] || '');
                    return opponentName ? getDisplayName(opponentName.toUpperCase()) : '';
                }});
                const opponentCountries = collectHistoryFilterValues(playerMatches, selectionState, selectedPlayer, 'opponentCountry', row => {{
                    const perspective = getHistoryPerspective(row, selectedPlayer);
                    return perspective.isWinner ? (row['_loserCountry'] || '') : (row['_winnerCountry'] || '');
                }});
                const playerEntries = collectHistoryFilterValues(playerMatches, selectionState, selectedPlayer, 'playerEntry', row => {{
                    const perspective = getHistoryPerspective(row, selectedPlayer);
                    return perspective.isWinner ? (row['_winnerEntry'] || '') : (row['_loserEntry'] || '');
                }});
                const seeds = collectHistoryFilterValues(playerMatches, selectionState, selectedPlayer, 'seed', row => {{
                    const perspective = getHistoryPerspective(row, selectedPlayer);
                    const playerSeed = perspective.isWinner ? (row['_winnerSeed'] || '') : (row['_loserSeed'] || '');
                    return playerSeed ? 'Yes' : 'No';
                }});
                const matchTypes = collectHistoryFilterValues(playerMatches, selectionState, selectedPlayer, 'matchType', row => getRowMatchType(row));

                // Populate filter options
                const orderedResults = [
                    'Wins',
                    'Losses',
                    'Wins by RET',
                    'Losses by RET',
                    'Wins by DEF',
                    'Losses by DEF'
                ].filter(r => resultsSet.has(r));
                const orderedYears = Array.from(new Set(years)).sort((a, b) => Number(b) - Number(a));
                orderedYears.unshift('Last 52');
                orderedYears.unshift('Career');

                populateFilterOptions('filter-surface', Array.from(new Set(surfaces)).sort((a, b) => a.localeCompare(b)), selectionState.surfaces);
                const roundOrderForFilter = {{
                    'Q1': 1, 'Q2': 2, 'Q3': 3, 'Q4': 4,
                    'R128': 5, 'R64': 6, 'R32': 7, 'R16': 8,
                    'QF': 9, 'SF': 10, 'F': 11,
                    'Team - RR': 12, 'Team - R32': 13, 'Team - R16': 14,
                    'Team - QF': 15, 'Team - SF': 16, 'Team - F': 17,
                }};
                const orderedRounds = Array.from(new Set(rounds)).sort((a, b) => {{
                    const oa = roundOrderForFilter[a] ?? 99;
                    const ob = roundOrderForFilter[b] ?? 99;
                    return oa !== ob ? oa - ob : a.localeCompare(b);
                }});
                populateFilterOptions('filter-round', orderedRounds, selectionState.rounds);
                populateFilterOptions('filter-result', orderedResults, selectionState.results);
                populateFilterOptions('filter-year', orderedYears, selectionState.years);
                populateTournamentSelect(Array.from(new Set(tournaments)).sort((a, b) => a.localeCompare(b)), selectionState.tournament);
                populateFilterOptions('filter-category', Array.from(new Set(categories)).sort((a, b) => a.localeCompare(b)), selectionState.categories);
                populateOpponentSelect(Array.from(new Set(opponents)).sort((a, b) => a.localeCompare(b)), selectionState.opponent);
                populateFilterOptions('filter-opponent-country', Array.from(new Set(opponentCountries)).sort((a, b) => a.localeCompare(b)), selectionState.opponentCountries);
                populateFilterOptions('filter-player-entry', Array.from(new Set(playerEntries)).sort((a, b) => a.localeCompare(b)), selectionState.playerEntries);
                populateFilterOptions('filter-seed', Array.from(new Set(seeds)), selectionState.seeds);
                populateFilterOptions('filter-match-type', Array.from(new Set(matchTypes)).sort((a, b) => a.localeCompare(b)), selectionState.matchTypes);
            }}

            function populateFilterOptions(filterId, values, selectedValues = []) {{
                const container = document.getElementById(filterId);
                if (!container) return;
                const selectedList = normalizeHistoryFilterValues(selectedValues);
                const selectedSet = new Set(selectedList);
                let html = '';
                mergeUniqueHistoryFilterValues(values, selectedList).forEach(value => {{
                    if (value) {{
                        const selectedClass = selectedSet.has(value) ? ' selected' : '';
                        const pressed = selectedSet.has(value) ? 'true' : 'false';
                        html += `<button type="button" class="filter-option${{selectedClass}}" data-value="${{escapeHtml(value)}}" aria-pressed="${{pressed}}" onclick="toggleFilterOption(event, this)">${{escapeHtml(value)}}</button>`;
                    }}
                }});
                container.innerHTML = html || '<div style="padding: 5px; color: #94a3b8; font-size: 11px;">No options</div>';
            }}

            function populateTournamentSelect(tournaments, selectedTournament = '') {{
                const select = document.getElementById('filter-tournament-select');
                if (!select) return;
                const selectedValue = selectedTournament || '';
                const optionValues = mergeUniqueHistoryFilterValues(tournaments, selectedValue).sort((a, b) => a.localeCompare(b));

                if ($(select).data('select2')) {{
                    $(select).select2('destroy');
                }}

                let html = '<option value="">All Tournaments</option>';
                optionValues.forEach(tournament => {{
                    if (tournament) {{
                        html += `<option value="${{escapeHtml(tournament)}}">${{escapeHtml(tournament)}}</option>`;
                    }}
                }});
                select.innerHTML = html;

                $(select).select2({{
                    placeholder: 'All Tournaments',
                    allowClear: true,
                    width: '100%'
                }});
                $(select).val(selectedValue).trigger('change.select2');

                $(select).off('change').on('change', function() {{
                    const selectedText = this.options[this.selectedIndex] ? this.options[this.selectedIndex].text : 'All Tournaments';
                    const rendered = this.nextElementSibling
                        ? this.nextElementSibling.querySelector('.select2-selection__rendered')
                        : null;
                    if (rendered) {{
                        rendered.textContent = selectedText;
                        rendered.title = selectedText;
                    }}
                    applyHistoryFilters();
                }});
            }}

            function populateOpponentSelect(opponents, selectedOpponent = '') {{
                const select = document.getElementById('filter-opponent-select');
                if (!select) return;
                const selectedValue = selectedOpponent || '';
                const optionValues = mergeUniqueHistoryFilterValues(opponents, selectedValue).sort((a, b) => a.localeCompare(b));

                // Destroy existing Select2 if it exists
                if ($(select).data('select2')) {{
                    $(select).select2('destroy');
                }}

                // Clear and populate options
                let html = '<option value="">All Opponents</option>';
                optionValues.forEach(opponent => {{
                    if (opponent) {{
                        html += `<option value="${{escapeHtml(opponent)}}">${{escapeHtml(opponent)}}</option>`;
                    }}
                }});
                select.innerHTML = html;

                // Initialize Select2 with search
                $(select).select2({{
                    placeholder: 'All Opponents',
                    allowClear: true,
                    width: '100%'
                }});
                $(select).val(selectedValue).trigger('change.select2');

                // Auto-apply filters when selection changes
                $(select).off('change').on('change', function() {{
                    const selectedText = this.options[this.selectedIndex] ? this.options[this.selectedIndex].text : 'All Opponents';
                    const rendered = this.nextElementSibling
                        ? this.nextElementSibling.querySelector('.select2-selection__rendered')
                        : null;
                    if (rendered) {{
                        rendered.textContent = selectedText;
                        rendered.title = selectedText;
                    }}
                    applyHistoryFilters();
                }});
            }}

            function toggleFilterOption(event, element) {{
                // Mobile taps are additive; desktop keeps Ctrl/Cmd+Click multi-select.
                const additiveSelection = event.ctrlKey || event.metaKey || window.innerWidth <= 768;
                if (!additiveSelection) {{
                    // Plain desktop click - deselect all others in this group first
                    const siblings = element.parentElement.querySelectorAll('.filter-option');
                    siblings.forEach(sib => {{
                        if (sib !== element) {{
                            sib.classList.remove('selected');
                            sib.setAttribute('aria-pressed', 'false');
                        }}
                    }});
                }}

                // Toggle this option
                element.classList.toggle('selected');
                element.setAttribute('aria-pressed', element.classList.contains('selected') ? 'true' : 'false');

                // Auto-apply filters
                applyHistoryFilters();
            }}

            function getSelectedFilterValues(filterId) {{
                const container = document.getElementById(filterId);
                const selectedOptions = container.querySelectorAll('.filter-option.selected');
                return Array.from(selectedOptions).map(option => option.getAttribute('data-value'));
            }}

            function updateHistoryCounter(matches, selectedPlayer) {{
                const counter = document.getElementById('history-wl-counter');
                if (!counter) return;

                const nonWO = (matches || []).filter(row =>
                    !isDoublesHistoryRow(row) &&
                    !['Walkover', 'Bye'].includes(row['_resultStatusDesc'] || '')
                );
                const total = nonWO.length;
                if (!selectedPlayer || total === 0) {{
                    counter.textContent = `Matches: ${{total}}`;
                    return;
                }}
                if (selectedPlayer === '__ALL__') {{
                    let wins = 0, argVsArg = 0;
                    nonWO.forEach(row => {{
                        const wc = (row['_winnerCountry'] || '').toUpperCase();
                        const lc = (row['_loserCountry'] || '').toUpperCase();
                        if (wc === 'ARG' && lc === 'ARG') {{ argVsArg++; }}
                        else if (wc === 'ARG') {{ wins++; }}
                    }});
                    const losses = total - wins - argVsArg;
                    const record = argVsArg > 0 ? `${{wins}}-${{argVsArg}}-${{losses}}` : `${{wins}}-${{losses}}`;
                    counter.textContent = `Matches: ${{total}} (${{record}})`;
                    return;
                }}

                let wins = 0;
                nonWO.forEach(row => {{
                    if (getHistoryPerspective(row, selectedPlayer).isWinner) wins += 1;
                }});
                const losses = total - wins;
                counter.textContent = `Matches: ${{total}} (${{wins}}-${{losses}})`;
            }}

            function applyHistoryFilters() {{
                const selectedPlayer = getNormalizedPlayerSelection('playerHistorySelect');
                const filterState = getHistoryFilterSelectionState();
                updateHistoryMobileFilterButton(filterState);
                if (!selectedPlayer) {{
                    syncUrlStateForTab('history');
                    return;
                }}

                // Filter the data (if nothing selected in a category, show all)
                const filtered = currentPlayerData.filter(row => rowMatchesHistoryFilters(row, filterState, selectedPlayer));

                populateFilters(currentPlayerData, filterState);
                updateHistoryCounter(filtered, selectedPlayer);
                renderFilteredMatches(filtered, selectedPlayer);
                syncUrlStateForTab('history');
            }}

            function clearHistoryFilters() {{
                // Remove selected class from all filter options
                document.querySelectorAll('.filter-option.selected').forEach(option => {{
                    option.classList.remove('selected');
                    option.setAttribute('aria-pressed', 'false');
                }});
                $('#filter-tournament-select').val('').trigger('change');
                // Reset opponent select dropdown
                $('#filter-opponent-select').val('').trigger('change');
                // Reset rank filters
                const asRankInput = document.getElementById('filter-as-rank');
                const vsRankInput = document.getElementById('filter-vs-rank');
                const asRankMode = document.getElementById('filter-as-rank-mode');
                const vsRankMode = document.getElementById('filter-vs-rank-mode');
                if (asRankInput) asRankInput.value = '';
                if (vsRankInput) vsRankInput.value = '';
                if (asRankMode) asRankMode.value = 'higher';
                if (vsRankMode) vsRankMode.value = 'higher';
                // Auto-apply filters (which will show all matches since nothing is selected)
                applyHistoryFilters();
            }}

            const HISTORY_PAGE_SIZE = 1000;
            let _historyPagedMatches = [];
            let _historyPagedPlayer = '';
            let _historyCurrentPage = 1;

            function renderFilteredMatches(matches, selectedPlayer) {{
                const tbody = document.getElementById('history-body');
                const displayColumns = ['DATE', 'TOURNAMENT', 'SURFACE', 'RND', 'PLAYER', 'SCORE', 'OPPONENT'];
                matches = (matches || []).filter(row => !isDoublesHistoryRow(row));
                updateHistoryCounter(matches, selectedPlayer);

                if (matches.length === 0) {{
                    tbody.innerHTML = `<tr><td colspan="${{displayColumns.length}}" class="cell-state-error">No matches found with the selected filters.</td></tr>`;
                    _updateHistoryPagination(0, 1, 1);
                    return;
                }}

                // Round priority (lower = higher in table)
                const roundOrder = {{
                    'Final': 1, 'Semi-finals': 2, 'Quarter-finals': 3,
                    '4th Round': 4, '3rd Round': 5, '2nd Round': 6, '1st Round': 7,
                    'QR4': 8, 'QR3': 9, 'QR2': 10, 'QR1': 11,
                    'Semi Finals': 12, 'Quarter Finals': 13,
                    'Last 16': 14, 'Last 32': 15, 'Round Robin': 16
                }};
                function getRoundOrder(round) {{
                    return roundOrder[round] || 99;
                }}

                // Sort by date descending, then by round order ascending
                matches.sort((a, b) => {{
                    const dateA = formatDate(a['DATE'] || '1900-01-01');
                    const dateB = formatDate(b['DATE'] || '1900-01-01');
                    if (dateA !== dateB) return dateB.localeCompare(dateA);
                    return getRoundOrder(a['ROUND'] || '') - getRoundOrder(b['ROUND'] || '');
                }});

                _historyPagedMatches = matches;
                _historyPagedPlayer = selectedPlayer;
                _renderHistoryPage(1);
            }}

            function _renderHistoryPage(page) {{
                const total = _historyPagedMatches.length;
                const totalPages = Math.ceil(total / HISTORY_PAGE_SIZE);
                _historyCurrentPage = Math.max(1, Math.min(page, totalPages));
                const start = (_historyCurrentPage - 1) * HISTORY_PAGE_SIZE;
                const pageMatches = _historyPagedMatches.slice(start, start + HISTORY_PAGE_SIZE);
                const selectedPlayer = _historyPagedPlayer;

                const parts = [];
                for (let i = 0; i < pageMatches.length; i++) {{
                    const row = pageMatches[i];
                    const perspective = getHistoryPerspective(row, selectedPlayer);
                    const isWinner = perspective.isWinner;
                    const playerNameRaw = isWinner ? perspective.winnerNameRaw : perspective.loserNameRaw;
                    const playerDisplayName = getDisplayName(playerNameRaw);

                    const rivalName = isWinner ? (row['_loserName'] || '') : (row['_winnerName'] || '');
                    const rivalDisplayName = rivalName ? getDisplayName(rivalName.toUpperCase()) : '';

                    const pSeed = isWinner ? (row['_winnerSeed'] || '') : (row['_loserSeed'] || '');
                    const pEntry = isWinner ? (row['_winnerEntry'] || '') : (row['_loserEntry'] || '');
                    const rSeed = isWinner ? (row['_loserSeed'] || '') : (row['_winnerSeed'] || '');
                    const rEntry = isWinner ? (row['_loserEntry'] || '') : (row['_winnerEntry'] || '');

                    const playerRank = (isWinner ? (row['_winnerRank'] || '') : (row['_loserRank'] || '')).toString();
                    const oppRank = (isWinner ? (row['_loserRank'] || '') : (row['_winnerRank'] || '')).toString();
                    const rivalCountry = isWinner ? (row['_loserCountry'] || '') : (row['_winnerCountry'] || '');
                    const playerCell = buildHistoryPlayerCell(playerRank, 'ARG', pSeed, pEntry, playerDisplayName);
                    const opponentCell = buildHistoryPlayerCell(oppRank, rivalCountry, rSeed, rEntry, rivalDisplayName);
                    const scoreText = isWinner ? (row['SCORE'] || '') : reverseScore(row['SCORE'] || '');
                    const scoreClass = isWinner ? 'score-win' : 'score-loss';

                    const displayTournament = _formatTournName(row['TOURNAMENT'] || '', row['CATEGORY'] || '');

                    parts.push('<tr><td>', formatDate(row['DATE'] || ''),
                        '</td><td>', displayTournament,
                        '</td><td>', row['SURFACE'] || '',
                        '</td><td>', displayRound(row['ROUND'] || '', row['TOURNAMENT_ID'] || '', row['DATE'] || '', row['TOURNAMENT'] || '', row['CATEGORY'] || '', row['MATCH_TYPE'] || '', row['DRAW'] || ''),
                        '</td><td>', playerCell,
                        '</td><td class="', scoreClass, '">', `<span class="score-badge">${{scoreText}}</span>`,
                        '</td><td>', opponentCell,
                        '</td></tr>');
                }}
                document.getElementById('history-body').innerHTML = parts.join('');
                _updateHistoryPagination(total, _historyCurrentPage, totalPages);
            }}

            function _updateHistoryPagination(total, currentPage, totalPages) {{
                const container = document.getElementById('history-pagination');
                if (!container) return;
                if (total <= HISTORY_PAGE_SIZE) {{
                    container.style.display = 'none';
                    return;
                }}
                const start = (currentPage - 1) * HISTORY_PAGE_SIZE + 1;
                const end = Math.min(currentPage * HISTORY_PAGE_SIZE, total);
                const prevDisabled = currentPage === 1 ? 'disabled' : '';
                const nextDisabled = currentPage === totalPages ? 'disabled' : '';
                container.style.display = 'flex';
                container.innerHTML =
                    `<button class="history-page-btn" ${{prevDisabled}} onclick="_renderHistoryPage(_historyCurrentPage - 1)">&#9664; Prev</button>` +
                    `<span>${{start}}-${{end}} of ${{total}}</span>` +
                    `<button class="history-page-btn" ${{nextDisabled}} onclick="_renderHistoryPage(_historyCurrentPage + 1)">Next &#9654;</button>`;
            }}

            async function filterHistoryByPlayer() {{
                const selectedPlayer = getNormalizedPlayerSelection('playerHistorySelect');
                const tbody = document.getElementById('history-body');
                const displayColumns = ['DATE', 'TOURNAMENT', 'SURFACE', 'RND', 'PLAYER', 'SCORE', 'OPPONENT'];

                if (selectedPlayer === '__ALL__') {{
                    tbody.innerHTML = `<tr><td colspan="${{displayColumns.length}}" class="cell-state-info">Loading match history...</td></tr>`;
                    try {{
                        await ensureHistoryDataLoaded();
                    }} catch (err) {{
                        console.error('Failed to load match history:', err);
                        tbody.innerHTML = `<tr><td colspan="${{displayColumns.length}}" class="cell-state-error">Failed to load match history. Please refresh and try again.</td></tr>`;
                        updateHistoryCounter([], '__ALL__');
                        return;
                    }}
                    const allFiltered = historyData.filter(row => !isDoublesHistoryRow(row));
                    if (allFiltered.length === 0) {{
                        tbody.innerHTML = `<tr><td colspan="${{displayColumns.length}}" class="cell-state-error">No matches found.</td></tr>`;
                        updateHistoryCounter([], '__ALL__');
                        return;
                    }}
                    currentPlayerData = allFiltered;
                    applyHistoryFilters();
                    return;
                }}

                if (!selectedPlayer) {{
                    currentPlayerData = [];
                    ['filter-surface', 'filter-round', 'filter-result', 'filter-year', 'filter-category', 'filter-opponent-country', 'filter-player-entry', 'filter-seed', 'filter-match-type']
                        .forEach(id => {{
                            const el = document.getElementById(id);
                            if (el) el.innerHTML = '';
                        }});
                    const tournSelect = document.getElementById('filter-tournament-select');
                    if (tournSelect) {{
                        tournSelect.innerHTML = '<option value="">All Tournaments</option>';
                        if ($(tournSelect).data('select2')) {{
                            $(tournSelect).select2('destroy');
                        }}
                    }}
                    const asRankInput = document.getElementById('filter-as-rank');
                    const vsRankInput = document.getElementById('filter-vs-rank');
                    const asRankMode = document.getElementById('filter-as-rank-mode');
                    const vsRankMode = document.getElementById('filter-vs-rank-mode');
                    if (asRankInput) asRankInput.value = '';
                    if (vsRankInput) vsRankInput.value = '';
                    if (asRankMode) asRankMode.value = 'higher';
                    if (vsRankMode) vsRankMode.value = 'higher';
                    const oppSelect = document.getElementById('filter-opponent-select');
                    if (oppSelect) {{
                        if ($(oppSelect).data('select2')) {{
                            $(oppSelect).select2('destroy');
                        }}
                        oppSelect.innerHTML = '<option value="">All Opponents</option>';
                    }}
                    tbody.innerHTML = `<tr><td colspan="${{displayColumns.length}}" class="cell-state-error">Select a player...</td></tr>`;
                    updateHistoryCounter([], '');
                    updateHistoryMobileFilterButton();
                    syncUrlStateForTab('history');
                    return;
                }}

                tbody.innerHTML = `<tr><td colspan="${{displayColumns.length}}" class="cell-state-info">Loading match history...</td></tr>`;
                try {{
                    await ensureHistoryDataLoaded();
                }} catch (err) {{
                    console.error('Failed to load match history:', err);
                    tbody.innerHTML = `<tr><td colspan="${{displayColumns.length}}" class="cell-state-error">Failed to load match history. Please refresh and try again.</td></tr>`;
                    updateHistoryCounter([], selectedPlayer);
                    return;
                }}

                const filtered = historyData.filter(row => {{
                    if (isDoublesHistoryRow(row)) return false;
                    // For a selected player, only keep rows where she is the rendered PLAYER side.
                    // This guarantees PLAYER remains the ARG-side view and nationality-switch rows
                    // where she appears only as OPPONENT are excluded (but still visible in ALL).
                    const perspective = getHistoryPerspective(row, selectedPlayer);
                    const playerNameNormalized = perspective.isWinner
                        ? perspective.winnerNameNormalized
                        : perspective.loserNameNormalized;
                    return playerNameNormalized === selectedPlayer;
                }});

                if (filtered.length === 0) {{
                    tbody.innerHTML = `<tr><td colspan="${{displayColumns.length}}" class="cell-state-error">No matches found for this player.</td></tr>`;
                    updateHistoryCounter([], selectedPlayer);
                    return;
                }}

                // Store current player data for filtering
                currentPlayerData = filtered;

                applyHistoryFilters();
            }}

            // Road to GS: shared lookups (initialised once, reused by renderRoadToGS + computeBest18)
            const _rtgs_roundOrder = {{'QR1':1,'QR2':2,'QR3':3,'QR4':4,'Round Robin':4.5,'1st Round':5,'2nd Round':6,'3rd Round':7,'4th Round':8,'5th Round':9,'Quarter Finals':10,'Quarter-finals':10,'Semi-finals':11,'Final':12}};
            const _rtgs_categoryToDesc = {{
                'GS':'Grand Slam','WTA 1000':'WTA 1000 (56M, 32Q)','WTA 500':'WTA 500 (30/28M, 24/16Q)',
                'WTA 250':'WTA 250 (32M, 24/16Q)','WTA 125':'WTA 125 (32M, 8Q)',
                '125K':'WTA 125 (32M, 8Q)','125K Series':'WTA 125 (32M, 8Q)',
                'W100':'W100 (32M, 32Q)','W75':'W75 (32M, 32Q)','W50':'W50 (32M, 32Q)',
                'W35':'W35 (32M, 64/48/32/24Q)','W15':'W15 (32M, 64/48/32/24Q)'
            }};
            const _rtgs_categoryDrawSize = {{'GS':128,'WTA 1000':64,'WTA 500':32,'WTA 250':32,'WTA 125':32,'125K':32,'125K Series':32,'W100':32,'W75':32,'W50':32,'W35':32,'W15':32}};
            const _rtgs_mandatory1000Names = ['Indian Wells','Miami','Madrid','Rome','Toronto','Montreal','Cincinnati','Beijing'];
            const _rtgs_optional1000Names  = ['Doha','Dubai','Wuhan'];
            // Drop-date and threshold constants â€” single source of truth for the Road-to-GS logic.
            //   2W:      GS / genuine WTA-1000 two-week events drop after 54 weeks.
            //   DEFAULT: every other tournament drops after 53 weeks.
            //   W15W35_DELAY_DAYS: ITF W15/W35 points go live one Monday AFTER the tournament starts.
            //   GS_THRESHOLD_*:    points needed to qualify (Q) / make main draw (MD) at a Grand Slam.
            const _RTGS_DROP_WEEKS_2W = 54;
            const _RTGS_DROP_WEEKS_DEFAULT = 53;
            const _RTGS_W15W35_DELAY_DAYS = 7;
            const _RTGS_GS_THRESHOLD_Q = {GS_THRESHOLD_Q};
            const _RTGS_GS_THRESHOLD_MD = {GS_THRESHOLD_MD};
            // ITF women's tier categories â€” TWO flavours, used for different decisions:
            //   ALL:         every ITF women's tier. Used to gate "is this a 2-week WTA event?"
            //                â€” an ITF tournament whose name contains e.g. "Madrid" must NOT be
            //                classified as a 2-week WTA-1000 event.
            //   WITH_POINTS: ITF tiers that have rows in the points-distribution / draw-size
            //                lookup tables. Used to choose ITF vs WTA points lookup. Tiers
            //                W40 / W10 / W80 are deliberately excluded here because no point
            //                table exists for them â€” they fall through to the WTA branch fallback.
            const _RTGS_ITF_CATS_ALL = ['W100','W75','W60','W50','W40','W35','W25','W15','W10','W80'];
            const _RTGS_ITF_CATS_WITH_POINTS = ['W100','W75','W60','W50','W35','W25','W15'];
            let _rtgs_pointsLookup = null, _rtgs_itfDrawLookup = null, _rtgs_wtaDrawLookup = null;

            function _rtgs_initLookups() {{
                if (!_rtgs_pointsLookup) {{
                    _rtgs_pointsLookup = {{}};
                    pointsDistribution.forEach(p => {{ _rtgs_pointsLookup[p.Description] = p; }});
                    _rtgs_itfDrawLookup = {{}};
                    itfDrawSizes.forEach(t => {{
                        const key = (t.tournamentName||'') + '|' + (t.date||'');
                        _rtgs_itfDrawLookup[key] = {{description:t.description, mainDrawSize:t.mainDrawSize}};
                        const wm = (t.tournamentName||'').match(/^(.+?)\\s*\\(Week \\d+\\)$/);
                        if (wm) _rtgs_itfDrawLookup[wm[1].trim()+'|'+(t.date||'')] = _rtgs_itfDrawLookup[key];
                    }});
                    _rtgs_wtaDrawLookup = {{}};
                    wtaDrawSizes.forEach(t => {{
                        if (!t.description || !t.tournamentId) return;
                        _rtgs_wtaDrawLookup[String(parseInt(t.tournamentId)||t.tournamentId)] = {{description:t.description, mainDrawSize:t.mainDrawSize}};
                    }});
                }}

                if (!_rtgs_twoWeekFreezeMondays) {{
                    const s = new Set();
                    (Array.isArray(historyData) ? historyData : []).forEach(r => {{
                        const tName = r['TOURNAMENT'] || '';
                        const draw = (r['DRAW'] || '').toUpperCase();
                        const cat = (r['CATEGORY'] || '').trim();
                        const mt = (r['MATCH_TYPE'] || '').trim();
                        const isGenuine2Week = mt === 'GS' || cat === 'WTA 1000' || cat === 'Premier Mandatory' || cat === 'Premier 5';
                        if (draw === 'M' && isGenuine2Week && _rtgs_twoWeekNames.some(n => tName.includes(n))) {{
                            const ds = r['DATE'] || '';
                            let mon = _rtgs_monday(ds);
                            // For freeze detection only: if a 2-week event's first main-draw match is
                            // on Sunday, treat it as part of the upcoming Monday week.
                            const d0 = new Date(ds);
                            if (mon && d0.getUTCDay() === 0) {{
                                const m2 = new Date(mon);
                                m2.setUTCDate(m2.getUTCDate() + 7);
                                mon = m2.toISOString().slice(0, 10);
                            }}
                            if (mon) {{
                                s.add(mon);
                                const w2 = new Date(mon);
                                w2.setUTCDate(w2.getUTCDate() + 7);
                                s.add(w2.toISOString().slice(0, 10));
                            }}
                        }}
                    }});
                    _rtgs_twoWeekFreezeMondays = s;
                }}
            }}

            function _rtgs_monday(dateStr) {{
                const d = new Date(dateStr), day = d.getUTCDay();
                const m = new Date(d);
                m.setUTCDate(d.getUTCDate() + (day===0 ? -6 : 1-day));
                return m.toISOString().slice(0,10);
            }}

            // Single source of truth for drop-date computation. Both renderRoadToGS and
            // computeBest18 used to inline this; an out-of-sync edit on one side caused the
            // 767-vs-797 ACC PTS regression that motivated extracting it.
            //
            // Caller must guarantee t.date is set (truthy YYYY-MM-DD); behaviour on a falsy
            // t.date is undefined (Invalid Date arithmetic).
            function _rtgs_computeDropDate(t) {{
                const monday = new Date(t.date + 'T00:00:00Z');
                const isW15W35 = t.category === 'W15' || t.category === 'W35';
                const effectiveMonday = new Date(monday);
                if (isW15W35) effectiveMonday.setUTCDate(monday.getUTCDate() + _RTGS_W15W35_DELAY_DAYS);
                const effectiveDateStr = effectiveMonday.toISOString().slice(0, 10);
                const is2WeekEvent = t.isGS || (!_RTGS_ITF_CATS_ALL.includes(t.category) && _rtgs_twoWeekNames.some(n => t.tournament.includes(n)));
                const isConcurrentFreeze = !is2WeekEvent && _rtgs_twoWeekFreezeMondays.has(effectiveDateStr);
                const dropDate = new Date(effectiveMonday);
                if (is2WeekEvent) {{
                    dropDate.setUTCDate(effectiveMonday.getUTCDate() + _RTGS_DROP_WEEKS_2W * 7);
                }} else if (isConcurrentFreeze) {{
                    // Share week1Mon of the concurrent two-week event so all concurrent
                    // tournaments drop on the same date as that event.
                    const prevMon = new Date(effectiveMonday);
                    prevMon.setUTCDate(effectiveMonday.getUTCDate() - 7);
                    const week1Mon = _rtgs_twoWeekFreezeMondays.has(prevMon.toISOString().slice(0, 10)) ? prevMon : effectiveMonday;
                    dropDate.setTime(week1Mon.getTime());
                    dropDate.setUTCDate(dropDate.getUTCDate() + _RTGS_DROP_WEEKS_2W * 7);
                }} else {{
                    dropDate.setUTCDate(effectiveMonday.getUTCDate() + _RTGS_DROP_WEEKS_DEFAULT * 7);
                }}
                return {{ effectiveMonday, effectiveDateStr, dropDate, is2WeekEvent, isConcurrentFreeze }};
            }}

            function _rtgs_keepLatestGrandSlamEditions(entries) {{
                if (!Array.isArray(entries) || entries.length < 2) {{
                    return Array.isArray(entries) ? entries.slice() : [];
                }}

                const latestByKey = new Map();
                entries.forEach(t => {{
                    if (!t || !t.isGS) return;
                    const key = String(t.tournament || '').trim().toUpperCase() || String(t.tournamentId || '').trim();
                    if (!key) return;
                    const prev = latestByKey.get(key);
                    const tDate = String(t.date || '');
                    const prevDate = prev ? String(prev.date || '') : '';
                    const tMainMonday = String(t.mainMonday || '');
                    const prevMainMonday = prev ? String(prev.mainMonday || '') : '';
                    if (!prev || tDate > prevDate || (tDate === prevDate && tMainMonday > prevMainMonday)) {{
                        latestByKey.set(key, t);
                    }}
                }});

                if (!latestByKey.size) {{
                    return entries.slice();
                }}

                const keep = new Set(latestByKey.values());
                return entries.filter(t => !t || !t.isGS || keep.has(t));
            }}

            // 2-week tournaments that freeze rankings for 2 consecutive weeks
            const _rtgs_twoWeekNames = ['Australian Open','Roland Garros','Wimbledon','US Open','Indian Wells','Miami','Madrid','Internazionali','Rome'];
            // Main-draw mondays of genuine 2-week tournaments (GS + WTA 1000 only).
            // Computed lazily once match history is loaded.
            let _rtgs_twoWeekFreezeMondays = null;

            function _rtgs_mdKey(round, result, drawSize) {{
                if (round==='Final') return result==='W'?'W':'F';
                if (result==='W') {{
                    const _n32 ={{'1st Round':'2nd Round','2nd Round':'Quarter-finals','Quarter-finals':'Semi-finals','Semi-finals':'Final'}};
                    const _n64 ={{'1st Round':'2nd Round','2nd Round':'3rd Round','3rd Round':'Quarter-finals','Quarter-finals':'Semi-finals','Semi-finals':'Final'}};
                    const _n128={{'1st Round':'2nd Round','2nd Round':'3rd Round','3rd Round':'4th Round','4th Round':'Quarter-finals','Quarter-finals':'Semi-finals','Semi-finals':'Final'}};
                    const _nm=drawSize>=128?_n128:(drawSize>=64?_n64:_n32);
                    const _nr=_nm[round]; if (_nr) return _rtgs_mdKey(_nr,'L',drawSize);
                }}
                if (round==='Semi-finals') return 'SF';
                if (round==='Quarter-finals') return 'QF';
                if (drawSize===128) {{ if (round==='4th Round') return 'R16'; if (round==='3rd Round') return 'R32'; if (round==='2nd Round') return 'R64'; if (round==='1st Round') return 'R128'; }}
                else if (drawSize===64) {{ if (round==='3rd Round') return 'R16'; if (round==='2nd Round') return 'R32'; if (round==='1st Round') return 'R64'; }}
                else {{ if (round==='2nd Round') return 'R16'; if (round==='1st Round') return 'R32'; }}
                return null;
            }}

            function _rtgs_qKey(round, result, hasMain, pTable) {{
                if (hasMain) return 'QLFR';
                if (result === 'W') {{
                    // Only the final qualifying round upgrades to QLFR.
                    // Earlier qualifying wins should advance to the next QR bucket.
                    const finalQR = pTable ? (pTable['QR3'] != null ? 'QR3' : (pTable['QR2'] != null ? 'QR2' : 'QR1')) : null;
                    if (round === finalQR) return 'QLFR';
                    const nextQual = {{'QR1':'QR2','QR2':'QR3'}};
                    return nextQual[round] || null;
                }}
                return round;
            }}

            function computeBest18(selectedPlayer, windowEndStr) {{
                _rtgs_initLookups();
                if (!Array.isArray(historyData)) return 0;
                const windowEnd = new Date(windowEndStr);
                const windowStart = new Date(windowEnd);
                windowStart.setDate(windowStart.getDate() - 385); // 55 weeks: wide enough for W15/W35 +7 effective date shift

                const matches = historyData.filter(row => {{
                    const mt = (row['MATCH_TYPE']||'').trim();
                    if (mt==='Fed/BJK Cup') return false;
                    const wn = getDisplayName((row['_winnerName']||'').toString().toUpperCase()).toUpperCase();
                    const ln = getDisplayName((row['_loserName']||'').toString().toUpperCase()).toUpperCase();
                    if (wn!==selectedPlayer && ln!==selectedPlayer) return false;
                    const ds = row['DATE']||''; if (!ds) return false;
                    const md = new Date(ds);
                    return md>=windowStart && md<=windowEnd;
                }});
                if (!matches.length) return 0;

                const tMap = new Map();
                matches.forEach(row => {{
                    const tName=row['TOURNAMENT']||'', ds=row['DATE']||'';
                    const mt=(row['MATCH_TYPE']||'').trim(), cat=(row['CATEGORY']||'').trim();
                    const isGS=mt==='GS', isUC=tName.toUpperCase().includes('UNITED CUP');
                    const mon=_rtgs_monday(ds), draw=(row['DRAW']||'').toUpperCase();
                    const round=row['ROUND']||'', rOrd=_rtgs_roundOrder[round]||0;
                    const wn=getDisplayName((row['_winnerName']||'').toString().toUpperCase()).toUpperCase();
                    const res=wn===selectedPlayer?'W':'L';
                    const tid=(row['TOURNAMENT_ID']||'').trim();
                    const yr=ds.slice(0,4);
                    // Group by tournamentId+year when available so annual editions stay separate
                    // (e.g. Roland Garros 2025 vs Roland Garros 2026), while still combining
                    // qualifying + main-draw weeks of the same edition.
                    const key=(tid?(tid+'|'+yr+'|'+tName):((isGS||isUC)?(mt+'|'+yr+'|'+tName):(mon+'|'+tName)));
                    if (!tMap.has(key)) tMap.set(key, {{date:mon,tournament:tName,tournamentId:tid,category:cat,isGS:isGS,isUnitedCup:isUC,bestMainRound:'',bestMainOrder:0,bestMainResult:'',bestQualRound:'',bestQualOrder:0,bestQualResult:'',qualMonday:'',mainMonday:'',ucWins:0,ucTotal:0,ucHasKnockout:false}});
                    const e=tMap.get(key);
                    if (isUC) {{ e.ucTotal++; if (res==='W') e.ucWins++; if (round!=='Round Robin'&&res==='W') e.ucHasKnockout=true; }}
                    if (draw==='Q') {{
                        if (rOrd>e.bestQualOrder) {{e.bestQualRound=round;e.bestQualOrder=rOrd;e.bestQualResult=res;}}
                        if (!e.qualMonday||mon<e.qualMonday) e.qualMonday=mon;
                    }} else {{
                        if (rOrd>e.bestMainOrder) {{e.bestMainRound=round;e.bestMainOrder=rOrd;e.bestMainResult=res;}}
                        if (!e.mainMonday||mon<e.mainMonday) e.mainMonday=mon;
                    }}
                }});

                tMap.forEach(t => {{
                    if (t.isGS) {{
                        if (t.mainMonday) {{ t.date=t.mainMonday; }}
                        else if (t.qualMonday) {{ const q=new Date(t.qualMonday); q.setUTCDate(q.getUTCDate()+7); t.date=q.toISOString().slice(0,10); }}
                    }} else if (t.isUnitedCup&&t.mainMonday) {{ t.date=t.mainMonday; }}
                    else if (t.mainMonday) {{ t.date=t.mainMonday; }} // set to main-draw week for multi-week tournaments
                }});

                // Filter: only include tournaments whose points are still live at windowEnd.
                // effectiveDateStr > windowEndStr â†’ points not yet live at cutoff.
                // dropDate > windowEnd â†’ points still on the rolling 12-month ranking.
                const ts=_rtgs_keepLatestGrandSlamEditions(Array.from(tMap.values())).filter(t => {{
                    if (!t.date) return false;
                    const {{ effectiveDateStr, dropDate }} = _rtgs_computeDropDate(t);
                    if (effectiveDateStr > windowEndStr) return false;
                    return dropDate > windowEnd;
                }});
                if (!ts.length) return 0;

                const wtaCats=['WTA 1000','WTA 500','WTA 250','WTA 125','125K','125K Series'];
                ts.forEach(t => {{
                    if (t.isUnitedCup) {{
                        const uc=_rtgs_pointsLookup['United Cup']; t.points=0;
                        if (uc) {{ const w=t.ucWins,ko=t.ucHasKnockout;
                            if(w>=5)t.points=uc['5W']; else if(w===4)t.points=uc['4W']; else if(w===3)t.points=uc['3W'];
                            else if(w===2&&ko)t.points=uc['2W_KO']; else if(w===2)t.points=uc['2W_RR'];
                            else if(w===1&&ko)t.points=uc['1W_KO']; else if(w===1)t.points=uc['1W_RR'];
                            else t.points=uc['0W']; }}
                    }} else {{
                        const qual=t.bestQualRound&&t.bestQualResult==='W';
                        const ll=t.bestQualRound&&t.bestQualResult==='L'&&!!t.bestMainRound;
                        let desc,drawSize;
                        if (_RTGS_ITF_CATS_WITH_POINTS.includes(t.category)) {{
                            const di=_rtgs_itfDrawLookup[t.tournament+'|'+t.date];
                            if(di){{desc=di.description;drawSize=di.mainDrawSize>32?64:32;}}
                            else{{desc=_rtgs_categoryToDesc[t.category]||'';drawSize=_rtgs_categoryDrawSize[t.category]||32;}}
                        }} else {{
                            const nid=t.tournamentId?String(parseInt(t.tournamentId)||t.tournamentId):'';
                            const wi=(wtaCats.includes(t.category)&&nid)?_rtgs_wtaDrawLookup[nid]:null;
                            if(wi){{desc=wi.description;drawSize=wi.mainDrawSize>64?128:wi.mainDrawSize>32?64:32;}}
                            else{{desc=_rtgs_categoryToDesc[t.category]||'';drawSize=_rtgs_categoryDrawSize[t.category]||32;}}
                        }}
                        const pt=_rtgs_pointsLookup[desc]; t.points=0;
                        if (pt) {{
                            if (t.bestMainRound) {{
                                const qfl=qual&&t.bestMainRound==='1st Round'&&t.bestMainResult==='L';
                                const lfl=ll&&t.bestMainRound==='1st Round'&&t.bestMainResult==='L';
                                if (!qfl&&!lfl) {{ const k=_rtgs_mdKey(t.bestMainRound,t.bestMainResult,drawSize); if(k&&pt[k]!=null)t.points+=pt[k]; }}
                            }}
                            if (t.bestQualRound) {{
                                if (ll) {{ if(pt[t.bestQualRound]!=null)t.points+=pt[t.bestQualRound]; }}
                                else {{ const k=_rtgs_qKey(t.bestQualRound,t.bestQualResult,!!t.bestMainRound,pt); if(k&&pt[k]!=null)t.points+=pt[k]; }}
                            }}
                        }}
                    }}
                }});

                const mGS=[],m1000=[],opt=[],rest=[];
                ts.forEach(t => {{
                    const hasMD=!!t.bestMainRound, up=t.tournament.toUpperCase();
                    if(t.isGS&&hasMD) mGS.push(t);
                    else if(t.category==='WTA 1000'&&hasMD&&_rtgs_mandatory1000Names.some(n=>up.includes(n.toUpperCase()))) m1000.push(t);
                    else if(t.category==='WTA 1000'&&hasMD&&_rtgs_optional1000Names.some(n=>up.includes(n.toUpperCase()))) opt.push(t);
                    else rest.push(t);
                }});
                m1000.sort((a,b)=>b.points-a.points); opt.sort((a,b)=>b.points-a.points); rest.sort((a,b)=>b.points-a.points);
                const c1000=m1000.slice(0,6), cOpt=opt.slice(0,1);
                const mandatory=[...mGS,...c1000,...cOpt];
                const fillPool=[...m1000.slice(6),...opt.slice(1),...rest];
                fillPool.sort((a,b)=>b.points-a.points);
                const countable=[...mandatory,...fillPool.slice(0,Math.max(0,18-mandatory.length))];
                return countable.reduce((s,t)=>s+t.points,0);
            }}

            function updateGSCutoffTables(selectedPlayer) {{
                gsCutoffs.forEach(gs => {{
                    ['q','md'].forEach(type => {{
                        const cutoff = type==='q' ? gs.qCutoff : gs.mdCutoff;
                        const accEl = document.getElementById('gs-acc-'+type+'-'+gs.id);
                        const estEl = document.getElementById('gs-est-'+type+'-'+gs.id);
                        if (!accEl||!estEl) return;
                        if (!selectedPlayer||cutoff==='N/A') {{ accEl.textContent='-'; estEl.textContent='-'; estEl.style.color=''; estEl.style.fontWeight=''; return; }}
                        const pts = computeBest18(selectedPlayer, cutoff);
                        accEl.textContent = pts;
                        const est = pts - (type==='q' ? _RTGS_GS_THRESHOLD_Q : _RTGS_GS_THRESHOLD_MD);
                        estEl.textContent = est;
                        estEl.style.fontWeight = 'bold';
                        estEl.style.color = est > 0 ? '#1a7a1a' : est >= -10 ? '#b8860b' : est >= -25 ? '#cc5500' : '#cc0000';
                    }});
                }});
            }}

            // Road to GS
            function abbrevRound(r) {{
                return r
                    .replace('WINNER', 'W')
                    .replace('Final', 'F')
                    .replace('Semi-finals', 'SF')
                    .replace('Quarter-finals', 'QF')
                    .replace('4th Round', '4th')
                    .replace('3rd Round', '3rd')
                    .replace('2nd Round', '2nd')
                    .replace('1st Round', '1st');
            }}

            function initRoadToGS() {{
                const select = document.getElementById('roadtogsPlayerSelect');
                if (!select) return;
                if (select.dataset.rtgsInit === '1') return;
                select.dataset.rtgsInit = '1';
                $(select).select2({{ placeholder: 'Select Player...', allowClear: true, width: '100%' }});
                $(select).on('change', renderRoadToGS);
            }}

            async function renderRoadToGS() {{
                const selectedPlayer = getNormalizedPlayerSelection('roadtogsPlayerSelect');
                const tbody = document.getElementById('roadtogs-body');

                if (!selectedPlayer) {{
                    tbody.innerHTML = '<tr><td colspan="5" class="cell-state-info">Select a player to view their results</td></tr>';
                    document.getElementById('roadtogs-points-total').textContent = 'Points: 0';
                    updateGSCutoffTables('');
                    syncUrlStateForTab('roadtogs');
                    return;
                }}

                tbody.innerHTML = '<tr><td colspan="5" class="cell-state-info">Loading match history...</td></tr>';
                try {{
                    await ensureHistoryDataLoaded();
                }} catch (err) {{
                    console.error('Failed to load match history:', err);
                    tbody.innerHTML = '<tr><td colspan="5" class="cell-state-error">Failed to load match history. Please refresh and try again.</td></tr>';
                    document.getElementById('roadtogs-points-total').textContent = 'Points: 0';
                    updateGSCutoffTables('');
                    syncUrlStateForTab('roadtogs');
                    return;
                }}
                _rtgs_initLookups();

                // Get current date and a wide prefilter window start.
                // We keep this wider than 52 weeks so W15/W35 tournaments (effective +7d)
                // can still be evaluated by the exact week-based cutoff and drop-date logic below.
                const now = new Date();
                const prefilterStart = new Date(now);
                prefilterStart.setDate(prefilterStart.getDate() - 385); // 55 weeks

                // Category to points distribution description mapping (use lower M draw size)
                const categoryToDesc = {{
                    'GS': 'Grand Slam',
                    'WTA 1000': 'WTA 1000 (56M, 32Q)',
                    'WTA 500': 'WTA 500 (30/28M, 24/16Q)',
                    'WTA 250': 'WTA 250 (32M, 24/16Q)',
                    'WTA 125': 'WTA 125 (32M, 8Q)',
                    '125K': 'WTA 125 (32M, 8Q)',
                    '125K Series': 'WTA 125 (32M, 8Q)',
                    'W100': 'W100 (32M, 32Q)',
                    'W75': 'W75 (32M, 32Q)',
                    'W50': 'W50 (32M, 32Q)',
                    'W35': 'W35 (32M, 64/48/32/24Q)',
                    'W15': 'W15 (32M, 64/48/32/24Q)'
                }};

                // Build points lookup: description -> {{ W, F, SF, ... }}
                const pointsLookup = {{}};
                pointsDistribution.forEach(p => {{ pointsLookup[p.Description] = p; }});

                // Build ITF draw size lookup: "name|date" -> {{ description, mainDrawSize }}
                const itfDrawLookup = {{}};
                itfDrawSizes.forEach(t => {{
                    const key = (t.tournamentName || '') + '|' + (t.date || '');
                    itfDrawLookup[key] = {{ description: t.description, mainDrawSize: t.mainDrawSize }};
                    // For multi-week entries with "(Week N)", also store with base name
                    const weekMatch = (t.tournamentName || '').match(/^(.+?)\\s*\\(Week \\d+\\)$/);
                    if (weekMatch) {{
                        const baseKey = weekMatch[1].trim() + '|' + (t.date || '');
                        itfDrawLookup[baseKey] = {{ description: t.description, mainDrawSize: t.mainDrawSize }};
                    }}
                }});

                // Build WTA draw size lookup by tournament ID (strip leading zeros)
                const wtaDrawLookup = {{}};
                wtaDrawSizes.forEach(t => {{
                    if (!t.description || !t.tournamentId) return;
                    const normId = String(parseInt(t.tournamentId) || t.tournamentId);
                    wtaDrawLookup[normId] = {{ description: t.description, mainDrawSize: t.mainDrawSize }};
                }});

                // Draw size per category for mapping round names to point keys
                // GS=128, WTA 1000 (56M)=64, everything else=32
                const categoryDrawSize = {{
                    'GS': 128, 'WTA 1000': 64,
                    'WTA 500': 32, 'WTA 250': 32, 'WTA 125': 32,
                    '125K': 32, '125K Series': 32,
                    'W100': 32, 'W75': 32, 'W50': 32, 'W35': 32, 'W15': 32
                }};

                // Map a main draw round name to a point key based on draw size
                function getMainDrawPointKey(round, result, drawSize) {{
                    if (round === 'Final') return result === 'W' ? 'W' : 'F';
                    if (result === 'W') {{
                        // Still in tournament - guaranteed next round; use next round's loss points
                        const _nxt32  = {{'1st Round':'2nd Round','2nd Round':'Quarter-finals','Quarter-finals':'Semi-finals','Semi-finals':'Final'}};
                        const _nxt64  = {{'1st Round':'2nd Round','2nd Round':'3rd Round','3rd Round':'Quarter-finals','Quarter-finals':'Semi-finals','Semi-finals':'Final'}};
                        const _nxt128 = {{'1st Round':'2nd Round','2nd Round':'3rd Round','3rd Round':'4th Round','4th Round':'Quarter-finals','Quarter-finals':'Semi-finals','Semi-finals':'Final'}};
                        const _nxtMap = drawSize>=128 ? _nxt128 : (drawSize>=64 ? _nxt64 : _nxt32);
                        const _nxt = _nxtMap[round];
                        if (_nxt) return getMainDrawPointKey(_nxt, 'L', drawSize);
                    }}
                    if (round === 'Semi-finals') return 'SF';
                    if (round === 'Quarter-finals') return 'QF';
                    // Numbered rounds depend on draw size
                    if (drawSize === 128) {{
                        if (round === '4th Round') return 'R16';
                        if (round === '3rd Round') return 'R32';
                        if (round === '2nd Round') return 'R64';
                        if (round === '1st Round') return 'R128';
                    }} else if (drawSize === 64) {{
                        if (round === '3rd Round') return 'R16';
                        if (round === '2nd Round') return 'R32';
                        if (round === '1st Round') return 'R64';
                    }} else {{
                        if (round === '2nd Round') return 'R16';
                        if (round === '1st Round') return 'R32';
                    }}
                    return null;
                }}

                // Map a qualifying round to a point key __MARKER_TEST__
                // pTable is used to determine the final qualifying round for this tournament
                function getQualPointKey(round, result, hasMainDraw, pTable) {{
                    if (hasMainDraw) return 'QLFR';
                    if (result === 'W') {{
                        // Won this round â€” check if it's the final qualifying round
                        const finalQR = pTable ? (pTable['QR3'] != null ? 'QR3' : (pTable['QR2'] != null ? 'QR2' : 'QR1')) : null;
                        if (round === finalQR) return 'QLFR';
                        // Still in qualifying: advance to next round (minimum guaranteed result)
                        const nextQual = {{'QR1':'QR2','QR2':'QR3'}};
                        return nextQual[round] || null;
                    }}
                    return round; // lost in this round
                }}

                // Prefilter matches for selected player, exclude Fed/BJK Cup.
                // Final "what is in the 52-week table" is decided later from tournament effective week.
                const playerMatches = historyData.filter(row => {{
                    const matchType = (row['MATCH_TYPE'] || '').trim();
                    if (matchType === 'Fed/BJK Cup') return false;

                    const wName = getDisplayName((row['_winnerName'] || '').toString().toUpperCase()).toUpperCase();
                    const lName = getDisplayName((row['_loserName'] || '').toString().toUpperCase()).toUpperCase();
                    if (wName !== selectedPlayer && lName !== selectedPlayer) return false;

                    const dateStr = row['DATE'] || '';
                    if (!dateStr) return false;
                    const matchDate = new Date(dateStr);
                    return matchDate >= prefilterStart && matchDate <= now;
                }});

                // Helper: compute Monday of a date's week
                function getMonday(dateStr) {{
                    const d = new Date(dateStr);
                    const day = d.getUTCDay();
                    const diff = (day === 0) ? -6 : 1 - day;
                    const monday = new Date(d);
                    monday.setUTCDate(d.getUTCDate() + diff);
                    return monday.toISOString().slice(0, 10);
                }}

                // Group by tournament + week, track best round per draw type (M/Q).
                // Use tournamentId+year when available so different annual editions
                // remain separate while still combining weeks inside the same edition.
                const tournamentMap = new Map();
                playerMatches.forEach(row => {{
                    const tName = row['TOURNAMENT'] || '';
                    const dateStr = row['DATE'] || '';
                    const matchType = (row['MATCH_TYPE'] || '').trim();
                    const category = (row['CATEGORY'] || '').trim();
                    const isGS = matchType === 'GS';
                    const isUnitedCup = tName.toUpperCase().includes('UNITED CUP');
                    const mondayStr = getMonday(dateStr);
                    const draw = (row['DRAW'] || '').toUpperCase();
                    const round = row['ROUND'] || '';
                    const rOrder = _rtgs_roundOrder[round] || 0;

                    // Determine if selected player won or lost this match
                    const wName = getDisplayName((row['_winnerName'] || '').toString().toUpperCase()).toUpperCase();
                    const playerResult = (wName === selectedPlayer) ? 'W' : 'L';

                    const tournamentId = (row['TOURNAMENT_ID'] || '').trim();
                    const yr = dateStr.slice(0, 4);
                    const key = tournamentId
                        ? (tournamentId + '|' + yr + '|' + tName)
                        : ((isGS || isUnitedCup) ? (matchType + '|' + yr + '|' + tName) : (mondayStr + '|' + tName));

                    if (!tournamentMap.has(key)) {{
                        tournamentMap.set(key, {{
                            date: mondayStr,
                            tournament: tName,
                            tournamentId: tournamentId,
                            category: category,
                            isGS: isGS,
                            isUnitedCup: isUnitedCup,
                            bestMainRound: '',
                            bestMainOrder: 0,
                            bestMainResult: '',
                            bestQualRound: '',
                            bestQualOrder: 0,
                            bestQualResult: '',
                            qualMonday: '',
                            mainMonday: '',
                            ucWins: 0,
                            ucTotal: 0,
                            ucHasKnockout: false
                        }});
                    }}
                    const entry = tournamentMap.get(key);

                    // United Cup: track win counts and knockout participation
                    if (isUnitedCup) {{
                        entry.ucTotal++;
                        if (playerResult === 'W') entry.ucWins++;
                        if (round !== 'Round Robin' && playerResult === 'W') entry.ucHasKnockout = true;
                    }}

                    if (draw === 'Q') {{
                        if (rOrder > entry.bestQualOrder) {{
                            entry.bestQualRound = round;
                            entry.bestQualOrder = rOrder;
                            entry.bestQualResult = playerResult;
                        }}
                        if (!entry.qualMonday || mondayStr < entry.qualMonday) {{
                            entry.qualMonday = mondayStr;
                        }}
                    }} else {{
                        if (rOrder > entry.bestMainOrder) {{
                            entry.bestMainRound = round;
                            entry.bestMainOrder = rOrder;
                            entry.bestMainResult = playerResult;
                        }}
                        if (!entry.mainMonday || mondayStr < entry.mainMonday) {{
                            entry.mainMonday = mondayStr;
                        }}
                    }}
                }});

                // Compute final date for each tournament
                tournamentMap.forEach(t => {{
                    if (t.isGS) {{
                        if (t.mainMonday) {{
                            t.date = t.mainMonday;
                        }} else if (t.qualMonday) {{
                            const qMon = new Date(t.qualMonday);
                            qMon.setUTCDate(qMon.getUTCDate() + 7);
                            t.date = qMon.toISOString().slice(0, 10);
                        }}
                    }} else if (t.isUnitedCup && t.mainMonday) {{
                        t.date = t.mainMonday;
                    }} else if (t.mainMonday) {{
                        t.date = t.mainMonday; // set to main-draw week for multi-week tournaments
                    }}
                }});

                // Remove entries whose tournament monday is in the same week as (or before) 52 weeks ago.
                // Default: live/current-week window (normal weekly updates).
                // Exception: if CURRENT week is week 1 of a 2-week freeze event, shift cutoff by +1 week
                // so we also remove next week's old results (no ranking update next Monday).
                const _cwMon = (() => {{ const d = new Date(now); const wd = d.getUTCDay(); d.setUTCDate(d.getUTCDate() - (wd===0?6:wd-1)); d.setUTCHours(0,0,0,0); return d; }})();
                const _nextUpdateMon = new Date(_cwMon);
                const _cwMonStr = _cwMon.toISOString().slice(0, 10);
                const _w2Mon = new Date(_cwMon);
                _w2Mon.setUTCDate(_w2Mon.getUTCDate() + 7);
                const _w2MonStr = _w2Mon.toISOString().slice(0, 10);
                const _isFreezeWeek1 = _rtgs_twoWeekFreezeMondays.has(_cwMonStr) && _rtgs_twoWeekFreezeMondays.has(_w2MonStr);
                if (_isFreezeWeek1) _nextUpdateMon.setUTCDate(_nextUpdateMon.getUTCDate() + 7);
                const _52wAgoMon = new Date(_nextUpdateMon);
                _52wAgoMon.setUTCDate(_nextUpdateMon.getUTCDate() - 364);
                tournamentMap.forEach((t, key) => {{
                    if (!t.date) return;
                    const effMon = new Date(t.date + 'T00:00:00Z');
                    if (t.category === 'W15' || t.category === 'W35') effMon.setUTCDate(effMon.getUTCDate() + _RTGS_W15W35_DELAY_DAYS);
                    if (effMon <= _52wAgoMon) tournamentMap.delete(key);
                }});

                const tournaments = _rtgs_keepLatestGrandSlamEditions(Array.from(tournamentMap.values()));

                if (tournaments.length === 0) {{
                    tbody.innerHTML = '<tr><td colspan="5" class="cell-state-info">No tournaments found in the last 52 weeks.</td></tr>';
                    document.getElementById('roadtogs-points-total').textContent = 'Points: 0';
                    syncUrlStateForTab('roadtogs');
                    return;
                }}

                // Calculate points and round display for each tournament
                tournaments.forEach(t => {{
                    // United Cup: special win-count based points
                    if (t.isUnitedCup) {{
                        const ucTable = pointsLookup['United Cup'];
                        t.roundDisplay = t.ucWins + 'W-' + (t.ucTotal - t.ucWins) + 'L';
                        t.points = 0;
                        if (ucTable) {{
                            const w = t.ucWins;
                            const ko = t.ucHasKnockout;
                            if (w >= 5) t.points = ucTable['5W'];
                            else if (w === 4) t.points = ucTable['4W'];
                            else if (w === 3) t.points = ucTable['3W'];
                            else if (w === 2 && ko) t.points = ucTable['2W_KO'];
                            else if (w === 2) t.points = ucTable['2W_RR'];
                            else if (w === 1 && ko) t.points = ucTable['1W_KO'];
                            else if (w === 1) t.points = ucTable['1W_RR'];
                            else t.points = ucTable['0W'];
                        }}
                    }} else {{

                    // Determine draw size and points table first (needed to identify final qualifying round)
                    let desc, drawSize;
                    if (_RTGS_ITF_CATS_WITH_POINTS.includes(t.category)) {{
                        const dsInfo = itfDrawLookup[t.tournament + '|' + t.date];
                        if (dsInfo) {{
                            desc = dsInfo.description;
                            drawSize = dsInfo.mainDrawSize > 32 ? 64 : 32;
                        }} else {{
                            console.debug(`[Road to GS] ITF draw size fallback: "${{t.tournament}}" (${{t.date}}) not found in itfDrawSizes, using default`);
                            desc = categoryToDesc[t.category] || '';
                            drawSize = categoryDrawSize[t.category] || 32;
                        }}
                    }} else {{
                        // For WTA tournaments, look up actual draw size description by tournament ID
                        const wtaCategories = ['WTA 1000','WTA 500','WTA 250','WTA 125','125K','125K Series'];
                        const wtaNormId = t.tournamentId ? String(parseInt(t.tournamentId) || t.tournamentId) : '';
                        const wtaInfo = (wtaCategories.includes(t.category) && wtaNormId) ? wtaDrawLookup[wtaNormId] : null;
                        if (wtaInfo) {{
                            desc = wtaInfo.description;
                            drawSize = wtaInfo.mainDrawSize > 64 ? 128 : (wtaInfo.mainDrawSize > 32 ? 64 : 32);
                        }} else {{
                            if (wtaCategories.includes(t.category)) {{
                                console.debug(`[Road to GS] WTA draw size fallback: "${{t.tournament}}" (${{t.date}}) not found in wtaDrawSizes, using default`);
                            }}
                            desc = categoryToDesc[t.category] || '';
                            drawSize = categoryDrawSize[t.category] || 32;
                        }}
                    }}
                    const pTable = pointsLookup[desc];

                    // Final qualifying round = highest QR key with non-null points (GS has QR3; WTA/ITF end at QR2 or QR1)
                    const finalQualRound = pTable ? (pTable['QR3'] != null ? 'QR3' : (pTable['QR2'] != null ? 'QR2' : 'QR1')) : null;

                    // Determine qualifier vs lucky loser status
                    // qualified = entered main draw after qualifying, OR won the final qualifying round (before main draw starts)
                    const wonFinalQualRound = !!t.bestQualRound && t.bestQualResult === 'W' && !t.bestMainRound && t.bestQualRound === finalQualRound;
                    const qualified = (!!t.bestQualRound && !!t.bestMainRound && t.bestQualResult !== 'L') || wonFinalQualRound;
                    const isLuckyLoser = t.bestQualRound && t.bestQualResult === 'L' && !!t.bestMainRound;

                    // Qualifying display
                    // Still in qualifying (won last match, not the final round): advance to next QR (minimum guaranteed)
                    const _nextQualRound = {{'QR1':'QR2','QR2':'QR3'}};
                    const _advancedQual = !!t.bestQualRound && t.bestQualResult === 'W' && !t.bestMainRound && !wonFinalQualRound;
                    const qualDisplay = qualified ? 'QLFR' : (_advancedQual ? (_nextQualRound[t.bestQualRound] || t.bestQualRound) : t.bestQualRound);

                    // Main draw display: "WINNER" if won the final
                    let mainDisplay = t.bestMainRound;
                    if (t.bestMainRound === 'Final' && t.bestMainResult === 'W') {{
                        mainDisplay = 'WINNER';
                    }}

                    // Build round display
                    if (t.bestMainRound && t.bestQualRound) {{
                        t.roundDisplay = abbrevRound(mainDisplay) + ' + ' + qualDisplay;
                    }} else {{
                        t.roundDisplay = abbrevRound(mainDisplay || qualDisplay || '');
                    }}

                    // If player won their last round (still active), advance roundDisplay to guaranteed next round
                    if (t.bestMainResult === 'W' && t.bestMainRound && t.bestMainRound !== 'Final') {{
                        const _rd32  = {{'1st Round':'2nd Round','2nd Round':'Quarter-finals','Quarter-finals':'Semi-finals','Semi-finals':'Final'}};
                        const _rd64  = {{'1st Round':'2nd Round','2nd Round':'3rd Round','3rd Round':'Quarter-finals','Quarter-finals':'Semi-finals','Semi-finals':'Final'}};
                        const _rd128 = {{'1st Round':'2nd Round','2nd Round':'3rd Round','3rd Round':'4th Round','4th Round':'Quarter-finals','Quarter-finals':'Semi-finals','Semi-finals':'Final'}};
                        const _rdMap = drawSize>=128 ? _rd128 : (drawSize>=64 ? _rd64 : _rd32);
                        const _rdNxt = _rdMap[t.bestMainRound];
                        if (_rdNxt) {{
                            const _rdAbbr = abbrevRound(_rdNxt);
                            t.roundDisplay = t.bestQualRound ? (_rdAbbr + ' + ' + qualDisplay) : _rdAbbr;
                        }}
                    }}
                    t.points = 0;
                    if (pTable) {{
                        // Main draw points
                        if (t.bestMainRound) {{
                            // Qualifier who lost 1st round: no MD points
                            const qualFirstRoundLoss = qualified && t.bestMainRound === '1st Round' && t.bestMainResult === 'L';
                            // Lucky loser who lost 1st round: no MD points
                            const llFirstRoundLoss = isLuckyLoser && t.bestMainRound === '1st Round' && t.bestMainResult === 'L';
                            if (!qualFirstRoundLoss && !llFirstRoundLoss) {{
                                const mdKey = getMainDrawPointKey(t.bestMainRound, t.bestMainResult, drawSize);
                                if (mdKey && pTable[mdKey] != null) t.points += pTable[mdKey];
                            }}
                        }}
                        // Qualifying points
                        if (t.bestQualRound) {{
                            if (isLuckyLoser) {{
                                // Lucky loser: points for best qualifying round lost, not QLFR
                                const qKey = t.bestQualRound; // QR1, QR2, QR3
                                if (pTable[qKey] != null) t.points += pTable[qKey];
                            }} else {{
                                const qKey = getQualPointKey(t.bestQualRound, t.bestQualResult, !!t.bestMainRound, pTable);
                                if (qKey && pTable[qKey] != null) t.points += pTable[qKey];
                            }}
                        }}
                    }}
                    }} // end else (non-United Cup)

                    const {{ dropDate }} = _rtgs_computeDropDate(t);
                    t.dropDate = dropDate.toISOString().slice(0, 10);
                }});

                // Classify tournaments
                const mandatoryGS = [];
                const mandatory1000 = [];
                const optional1000 = [];
                const rest = [];

                tournaments.forEach(t => {{
                    const hasMD = !!t.bestMainRound;
                    const tUpper = t.tournament.toUpperCase();

                    if (t.isGS && hasMD) {{
                        t.mandatory = true;
                        mandatoryGS.push(t);
                    }} else if (t.category === 'WTA 1000' && hasMD && _rtgs_mandatory1000Names.some(n => tUpper.includes(n.toUpperCase()))) {{
                        mandatory1000.push(t);
                    }} else if (t.category === 'WTA 1000' && hasMD && _rtgs_optional1000Names.some(n => tUpper.includes(n.toUpperCase()))) {{
                        optional1000.push(t);
                    }} else {{
                        rest.push(t);
                    }}
                }});

                // Sort each group by points descending
                mandatory1000.sort((a, b) => b.points - a.points);
                optional1000.sort((a, b) => b.points - a.points);
                rest.sort((a, b) => b.points - a.points);

                // Best 6 mandatory WTA 1000
                const counted1000 = mandatory1000.slice(0, 6);
                counted1000.forEach(t => {{ t.mandatory = true; }});
                const uncounted1000 = mandatory1000.slice(6);

                // Best 1 optional WTA 1000
                const countedOpt = optional1000.slice(0, 1);
                countedOpt.forEach(t => {{ t.mandatory = true; }});
                const uncountedOpt = optional1000.slice(1);

                // Combine mandatory countable tournaments
                const mandatoryAll = [...mandatoryGS, ...counted1000, ...countedOpt];
                const mandatoryCount = mandatoryAll.length;

                // Fill remaining spots up to 18 from rest + uncounted WTA 1000s
                const fillPool = [...uncounted1000, ...uncountedOpt, ...rest];
                fillPool.sort((a, b) => b.points - a.points);
                const fillSlots = Math.max(0, 18 - mandatoryCount);
                const filledCountable = fillPool.slice(0, fillSlots);
                const nonCountable = fillPool.slice(fillSlots);

                // Build final ordered list grouped by tier, each sorted by points desc
                mandatoryGS.sort((a, b) => b.points - a.points);
                const allMandatory1000 = [...counted1000, ...countedOpt];
                allMandatory1000.sort((a, b) => b.points - a.points);
                filledCountable.sort((a, b) => b.points - a.points);
                const countable = [...mandatoryGS, ...allMandatory1000, ...filledCountable];
                nonCountable.sort((a, b) => b.points - a.points);

                const totalPoints = countable.reduce((sum, t) => sum + t.points, 0);
                document.getElementById('roadtogs-points-total').textContent = 'Points: ' + totalPoints;
                updateGSCutoffTables(selectedPlayer);

                // Render table
                const _today = new Date(); _today.setUTCHours(0,0,0,0);
                const _in14 = new Date(_today); _in14.setUTCDate(_today.getUTCDate() + 14);
                const _in28 = new Date(_today); _in28.setUTCDate(_today.getUTCDate() + 28);
                function _dropClass(dropDateStr) {{
                    if (!dropDateStr) return '';
                    const d = new Date(dropDateStr);
                    if (d <= _in14) return ' class="rtgs-warn-14d"';
                    if (d <= _in28) return ' class="rtgs-warn-28d"';
                    return '';
                }}
                function _rtgsCategoryKey(t) {{
                    if (t.isGS || t.category === 'GS') return 'GS';
                    if (t.category === 'WTA 1000') return 'WTA 1000';
                    if (t.category === 'WTA 500') return 'WTA 500';
                    if (t.category === 'WTA 250') return 'WTA 250';
                    if (t.category === 'WTA 125' || t.category === '125K' || t.category === '125K Series') return 'WTA 125';
                    if (_RTGS_ITF_CATS_ALL.includes(t.category)) return 'ITF';
                    return 'OTHER';
                }}
                function _rtgsCategoryLabel(key) {{
                    if (key === 'GS') return 'Grand Slams';
                    if (key === 'WTA 1000') return 'WTA 1000';
                    if (key === 'WTA 500') return 'WTA 500';
                    if (key === 'WTA 250') return 'WTA 250';
                    if (key === 'WTA 125') return 'WTA 125';
                    if (key === 'ITF') return 'ITF';
                    return 'Other';
                }}
                function _rtgsIsLocked(t) {{
                    const hasMainDraw = !!t.bestMainRound;
                    return (t.isGS && hasMainDraw) || (t.category === 'WTA 1000' && hasMainDraw);
                }}
                function _rtgsTournamentLabel(t) {{
                    const name = _formatTournName(t.tournament, t.category);
                    if (!name) return '';
                    if (!_rtgsIsLocked(t)) return name;
                    return `${{name}} <span class="rtgs-lock" title="Locked tournament" aria-label="Locked tournament">&#128274;&#65038;</span>`;
                }}
                function _appendRoadToGSCategories(parts, list) {{
                    const order = ['GS', 'WTA 1000', 'WTA 500', 'WTA 250', 'WTA 125', 'ITF', 'OTHER'];
                    const groups = new Map();
                    list.forEach(t => {{
                        const key = _rtgsCategoryKey(t);
                        if (!groups.has(key)) groups.set(key, []);
                        groups.get(key).push(t);
                    }});
                    order.forEach(key => {{
                        const rows = groups.get(key) || [];
                        if (!rows.length) return;
                        parts.push(`<tr class="roadtogs-category-separator"><td colspan="5">${{_rtgsCategoryLabel(key)}}</td></tr>`);
                        rows.forEach(t => {{
                            parts.push(`<tr><td>${{t.date}}</td><td>${{_rtgsTournamentLabel(t)}}</td><td>${{t.roundDisplay}}</td><td>${{t.points}}</td><td${{_dropClass(t.dropDate)}}>${{t.dropDate}}</td></tr>`);
                        }});
                    }});
                }}
                const parts = [];
                _appendRoadToGSCategories(parts, countable);
                if (nonCountable.length > 0) {{
                    parts.push('<tr class="roadtogs-separator"><td colspan="5">NON-COUNTABLE TOURNAMENTS</td></tr>');
                    nonCountable.forEach(t => {{
                        parts.push(`<tr><td>${{t.date}}</td><td>${{_rtgsTournamentLabel(t)}}</td><td>${{t.roundDisplay}}</td><td>${{t.points}}</td><td${{_dropClass(t.dropDate)}}>${{t.dropDate}}</td></tr>`);
                    }});
                }}

                tbody.innerHTML = parts.join('');
                syncUrlStateForTab('roadtogs');
            }}

            document.addEventListener('DOMContentLoaded', initRoadToGS);

            // ===== DRAWS =====
            let currentDrawTKey = '';
            let currentDrawType = 'MDS';

            function onDrawTournamentChange(tKey) {{
                currentDrawTKey = tKey;
                const info = drawsTournamentInfo[tKey];
                if (!info) return;
                const types = info.types || [];
                if (types.length > 0 && !types.includes(currentDrawType)) {{
                    currentDrawType = types[0];
                }}
                updateDrawTypeButtons(types);
                loadDraw();
                syncUrlStateForTab('draws');
            }}

            function selectDrawType(dtype) {{
                currentDrawType = dtype;
                const btns = document.querySelectorAll('.draw-type-btn');
                btns.forEach(b => b.classList.toggle('active', b.dataset.type === dtype));
                loadDraw();
                syncUrlStateForTab('draws');
            }}

            function updateDrawTypeButtons(types) {{
                const container = document.getElementById('draws-type-btns');
                container.innerHTML = '';
                const labels = {{'MDS': 'Main Draw', 'QS': 'Qualifying'}};
                types.forEach(t => {{
                    const btn = document.createElement('button');
                    btn.className = 'draw-type-btn' + (t === currentDrawType ? ' active' : '');
                    btn.dataset.type = t;
                    btn.textContent = labels[t] || t;
                    btn.onclick = () => selectDrawType(t);
                    container.appendChild(btn);
                }});
            }}

            function loadDraw() {{
                currentDrawFilterRound = 0;
                document.getElementById('draw-filter-reset').classList.remove('visible');
                const key = currentDrawTKey + '|' + currentDrawType;
                const data = drawsData[key];
                const bracket = document.getElementById('draw-bracket');
                if (!data || !data.players || data.players.length === 0) {{
                    bracket.innerHTML = '<div class="draw-no-draws">No draw available</div>';
                    return;
                }}
                renderBracket(data, bracket);
            }}

            function updateDraw() {{
                const sel = document.getElementById('draws-tournament-select');
                if (!currentDrawTKey && sel.value) {{
                    onDrawTournamentChange(sel.value);
                }} else if (currentDrawTKey) {{
                    loadDraw();
                }}
            }}

            function formatDrawName(rawName) {{
                if (!rawName) return '';
                let name = rawName.replace(/\\.\\.\\.$/, '').trim();
                // Shorten names > 25 chars: "LASTNAME1 LASTNAME2, First" -> "LASTNAME1 L., First"
                if (name.length > 25) {{
                    const ci = name.indexOf(',');
                    if (ci > 0) {{
                        const last = name.substring(0, ci).trim();
                        const first = name.substring(ci + 1).trim();
                        const parts = last.split(/\\s+/);
                        if (parts.length >= 2) {{
                            // Keep first word of last name, abbreviate the rest
                            const shortened = parts[0] + ' ' + parts.slice(1).map(p => p.charAt(0) + '.').join(' ');
                            name = shortened + ', ' + first;
                        }}
                    }}
                }}
                return name;
            }}

            function parseScore(scoreStr) {{
                if (!scoreStr) return {{ sets: [], retired: false, walkover: false }};
                const parts = scoreStr.trim().split(/\\s+/);
                const sets = [];
                let retired = false;
                let walkover = false;
                for (const p of parts) {{
                    if (p === 'RET' || p === 'DEF') {{ retired = true; continue; }}
                    if (p === 'W/O' || p === 'WO' || p === 'W.O.') {{ walkover = true; continue; }}
                    // Accept both compact WTA-like set tokens ("64", "76(4)") and match-tiebreak tokens ("11-9").
                    // Also handle legacy compact match-tiebreak encoding like "119" (11-9) or "108" (10-8).
                    const mh = p.match(/^\\[?(\\d+)[-:\\/](\\d+)\\]?(?:\\((\\d+)\\))?$/);
                    if (mh) {{
                        sets.push({{ w: parseInt(mh[1], 10), l: parseInt(mh[2], 10), tb: mh[3] || null }});
                        continue;
                    }}
                    const mc = p.match(/^(\\d+)(?:\\((\\d+)\\))?$/);
                    if (mc) {{
                        const digits = mc[1];
                        let w = null;
                        let l = null;
                        if (digits.length === 2) {{
                            w = parseInt(digits.charAt(0), 10);
                            l = parseInt(digits.charAt(1), 10);
                        }} else if (digits.length === 3) {{
                            w = parseInt(digits.slice(0, 2), 10);
                            l = parseInt(digits.slice(2), 10);
                        }} else if (digits.length === 4) {{
                            w = parseInt(digits.slice(0, 2), 10);
                            l = parseInt(digits.slice(2), 10);
                        }} else {{
                            const mid = Math.floor(digits.length / 2);
                            w = parseInt(digits.slice(0, mid), 10);
                            l = parseInt(digits.slice(mid), 10);
                        }}
                        if (!Number.isNaN(w) && !Number.isNaN(l)) {{
                            sets.push({{ w, l, tb: mc[2] || null }});
                        }}
                    }}
                }}
                // Reject live/in-progress scores: every regular set must have one player on 6+ games.
                // Avoids displaying mid-match snapshots like '44 44' as if they were final results.
                if (!retired && !walkover) {{
                    for (const s of sets) {{
                        if (Math.max(s.w, s.l) < 6) {{
                            return {{ sets: [], retired: false, walkover: false }};
                        }}
                    }}
                }}
                return {{ sets, retired, walkover }};
            }}

            function isMatchWinner(playerName, winnerName) {{
                if (!playerName || !winnerName) return false;
                const truncated = winnerName.trim().endsWith('...');
                const pNorm = playerName.replace(/\\.\\.\\.$/, '').trim().toUpperCase();
                const wNorm = winnerName.replace(/\\.\\.\\.$/, '').trim().toUpperCase();
                if (pNorm === wNorm) return true;
                // playerName is "LASTNAME, First" format; winnerName is "F. Lastname" format
                const commaIdx = pNorm.indexOf(',');
                if (commaIdx > 0) {{
                    const playerLast = pNorm.substring(0, commaIdx).trim();
                    const wm = wNorm.match(/^(?:[A-Z]+\\.\\s+)+(.+)$/);
                    if (wm) {{
                        const winnerLast = wm[1].trim();
                        if (playerLast === winnerLast) return true;
                        // Handle truncated names like "Jimenez Kasints..." vs "JIMENEZ KASINTSEVA"
                        if (truncated && winnerLast.length >= 5 && playerLast.startsWith(winnerLast)) return true;
                    }}
                }}
                return false;
            }}

            function getWinnerPlayer(match, players) {{
                if (!match || !match.winner_name) return null;
                for (const p of players) {{
                    if (isMatchWinner(p.name, match.winner_name)) return p;
                }}
                return null;
            }}

            function renderPlayer(player, isBye, isQualifier, isWinner, isTop, scoreData, matchConcluded, showWalkover) {{
                const flag = player ? countryFlag(player.country, false) : '';
                const flagHtml = '<span class="country">' + flag + '</span>';
                let seedEntry = '<span class="seed-entry"></span>';
                if (player) {{
                    let seText = '';
                    if (player.seed && player.entry) {{
                        seText = '<span class="seed">' + player.seed + '/' + '</span><span class="entry">' + player.entry + '</span>';
                    }} else if (player.seed) {{
                        seText = '<span class="seed">' + player.seed + '</span>';
                    }} else if (player.entry) {{
                        seText = '<span class="entry">' + player.entry + '</span>';
                    }}
                    seedEntry = '<span class="seed-entry">' + seText + '</span>';
                }}
                let name = '';
                if (player) name = formatDrawName(player.name);
                else if (isBye) name = 'BYE';
                else if (isQualifier) name = 'Qualifier';
                const nameHtml = '<span class="name">' + name + '</span>';
                let setsHtml = '';
                if (scoreData && scoreData.sets && scoreData.sets.length > 0) {{
                    const ss = scoreData.sets;
                    for (let i = 0; i < ss.length; i++) {{
                        const s = ss[i];
                        const myScore = isWinner ? s.w : s.l;
                        const otherScore = isWinner ? s.l : s.w;
                        const won = myScore > otherScore;
                        const cls = won ? 'won' : 'lost';
                        const tb = (s.tb && !won) ? '<sup>' + s.tb + '</sup>' : '';
                        setsHtml += '<span class="set-score ' + cls + '">' + myScore + tb + '</span>';
                    }}
                    if (scoreData.retired) {{
                        if (!isWinner) {{
                            setsHtml += '<span class="set-score lost">R</span>';
                        }} else {{
                            setsHtml += '<span class="set-score">&nbsp;</span>';
                        }}
                    }}
                }} else if (matchConcluded && isWinner && showWalkover) {{
                    setsHtml += '<span class="set-score won wo">W.O.</span>';
                }}
                const cls = 'draw-player' + (isWinner ? ' winner' : '');
                return '<div class="' + cls + '">' + flagHtml + seedEntry + nameHtml + (setsHtml ? '<span class="sets">' + setsHtml + '</span>' : '') + '</div>';
            }}

            function renderMatch(p1, p2, isBye1, isBye2, isQ1, isQ2, match, players) {{
                const scoreText = (match && match.score) ? String(match.score).trim() : '';
                // Only treat a match as concluded if we have a non-empty score.
                // WTA PDFs often show "advanced" names in later rounds before matches are played (e.g., seeds with byes),
                // and parsing those as winners breaks early-round pairings (Miami WTA 1000 case).
                const matchConcluded = !!(match && match.winner_name && scoreText);
                const scoreData = matchConcluded ? parseScore(scoreText) : null;
                const winnerPlayer = matchConcluded ? getWinnerPlayer(match, players) : null;
                const showWalkover = !!(matchConcluded && scoreData && scoreData.walkover);
                const p1IsWinner = winnerPlayer && p1 && isMatchWinner(p1.name, match.winner_name);
                const p2IsWinner = winnerPlayer && p2 && isMatchWinner(p2.name, match.winner_name);
                return '<div class="draw-match">' +
                    renderPlayer(p1, isBye1, isQ1, p1IsWinner, true, matchConcluded ? scoreData : null, matchConcluded, showWalkover) +
                    renderPlayer(p2, isBye2, isQ2, p2IsWinner, false, matchConcluded ? scoreData : null, matchConcluded, showWalkover) +
                    '</div>';
            }}

            function renderBracket(data, container) {{
                const players = data.players || [];
                const matches = data.matches || [];
                const byes = new Set(data.byes || []);
                const drawSize = data.draw_size || players.length;
                const pdfRoundLabels = data.round_labels || [];
                const numRounds = data.num_rounds || Math.ceil(Math.log2(drawSize));
                const playersByPos = new Map(players.map(p => [p.pos, p]));
                const playerPosSet = new Set(players.map(p => p.pos));
                const matchMap = new Map(matches.map(m => [`${{m.round}}:${{m.match_num}}`, m]));
                const isQualifying = (data.draw_type || '').toUpperCase().includes('QUAL') || currentDrawType === 'QS';

                function getMatch(roundNum, matchNum) {{
                    return matchMap.get(`${{roundNum}}:${{matchNum}}`) || null;
                }}

                function formatRoundLabel(label, roundIdx) {{
                    const norm = (label || '').trim();
                    if (isQualifying) {{
                        if (/^Round of\\s+\\d+$/i.test(norm)) {{
                            const ordinals = ['1st Round', '2nd Round', '3rd Round', '4th Round', '5th Round', '6th Round'];
                            return ordinals[roundIdx] || ('Round ' + (roundIdx + 1));
                        }}
                        return label;
                    }}
                    if (/^(1st|2nd|3rd|4th)\\s+Round$/i.test(norm) || /^R\\d+$/i.test(norm)) {{
                        const roundOf = Math.round(drawSize / Math.pow(2, roundIdx));
                        if (roundOf >= 2) return 'Round of ' + roundOf;
                    }}
                    return label;
                }}

                function getAdvancer(roundNum, matchNum) {{
                    if (roundNum <= 0) return null;
                    const match = getMatch(roundNum, matchNum);
                    const scoreText = (match && match.score) ? String(match.score).trim() : '';
                    if (match && match.winner_name && scoreText) {{
                        const winner = getWinnerPlayer(match, players);
                        if (winner) return winner;
                        return null;
                    }}
                    if (roundNum === 1) {{
                        const pos1 = matchNum * 2 + 1;
                        const pos2 = matchNum * 2 + 2;
                        const p1 = playersByPos.get(pos1) || null;
                        const p2 = playersByPos.get(pos2) || null;
                        const bye1 = byes.has(pos1);
                        const bye2 = byes.has(pos2);
                        if (bye1 && !bye2) return p2;
                        if (bye2 && !bye1) return p1;
                        return null;
                    }}
                    return null;
                }}

                function hasPlayerInRange(startPos, endPos) {{
                    for (let pos = startPos; pos <= endPos; pos++) {{
                        if (playerPosSet.has(pos)) return true;
                    }}
                    return false;
                }}

                let html = '';
                for (let r = 0; r < numRounds; r++) {{
                    const rawLabel = r < pdfRoundLabels.length ? pdfRoundLabels[r] : 'R' + (r + 1);
                    const label = formatRoundLabel(rawLabel, r);
                    html += '<div class="draw-round" data-round="' + r + '"><div class="draw-round-header" role="button" tabindex="0" data-round="' + r + '" title="Show from this round">' + label + '</div>';

                    if (r === 0) {{
                        const numMatches = Math.floor(drawSize / 2);
                        const treatEmptyFirstRoundQualAsBye = isQualifying;
                        for (let m = 0; m < numMatches; m++) {{
                            const pos1 = m * 2 + 1;
                            const pos2 = m * 2 + 2;
                            const p1 = playersByPos.get(pos1) || null;
                            const p2 = playersByPos.get(pos2) || null;
                            const isBye1 = byes.has(pos1) || (treatEmptyFirstRoundQualAsBye && !p1);
                            const isBye2 = byes.has(pos2) || (treatEmptyFirstRoundQualAsBye && !p2);
                            const isQ1 = !p1 && !isBye1;
                            const isQ2 = !p2 && !isBye2;
                            const match = getMatch(1, m);
                            html += '<div class="draw-match-wrapper">' + renderMatch(p1, p2, isBye1, isBye2, isQ1, isQ2, match, players) + '</div>';
                        }}
                    }} else {{
                        const numMatches = Math.floor(drawSize / Math.pow(2, r + 1));
                        for (let m = 0; m < numMatches; m++) {{
                            const match = getMatch(r + 1, m);
                            const p1 = getAdvancer(r, m * 2);
                            const p2 = getAdvancer(r, m * 2 + 1);
                            const groupStart = m * Math.pow(2, r + 1) + 1;
                            const halfSize = Math.pow(2, r);
                            const topStart = groupStart;
                            const topEnd = groupStart + halfSize - 1;
                            const botStart = groupStart + halfSize;
                            const botEnd = groupStart + Math.pow(2, r + 1) - 1;
                            const topHasPlayer = hasPlayerInRange(topStart, topEnd);
                            const botHasPlayer = hasPlayerInRange(botStart, botEnd);
                            const isBye1 = !p1 && !!p2 && !topHasPlayer;
                            const isBye2 = !p2 && !!p1 && !botHasPlayer;
                            html += '<div class="draw-match-wrapper">' + renderMatch(p1, p2, isBye1, isBye2, false, false, match, players) + '</div>';
                        }}
                    }}
                    html += '</div>';
                }}

                container.innerHTML = html;
                drawConnectors(container);
            }}

            function getOffsetRelativeTo(el, ancestor) {{
                let x = 0, y = 0;
                let current = el;
                while (current && current !== ancestor) {{
                    x += current.offsetLeft;
                    y += current.offsetTop;
                    current = current.offsetParent;
                }}
                return {{ x, y, w: el.offsetWidth, h: el.offsetHeight }};
            }}

            function drawConnectors(container) {{
                const rounds = container.querySelectorAll('.draw-round');
                const oldSvg = container.querySelector('svg');
                if (oldSvg) oldSvg.remove();

                const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
                svg.style.cssText = 'position:absolute;top:0;left:0;pointer-events:none;';
                svg.setAttribute('width', container.scrollWidth);
                svg.setAttribute('height', container.scrollHeight);
                container.appendChild(svg);

                for (let r = 0; r < rounds.length - 1; r++) {{
                    // Skip connectors from/to hidden rounds
                    if (rounds[r].classList.contains('hidden-round') || rounds[r + 1].classList.contains('hidden-round')) continue;
                    const currMatches = rounds[r].querySelectorAll('.draw-match-wrapper');
                    const nextMatches = rounds[r + 1].querySelectorAll('.draw-match-wrapper');

                    for (let m = 0; m < nextMatches.length; m++) {{
                        const topIdx = m * 2;
                        const botIdx = m * 2 + 1;
                        if (topIdx >= currMatches.length) continue;

                        const topMatch = currMatches[topIdx];
                        const botMatch = botIdx < currMatches.length ? currMatches[botIdx] : null;
                        const nextMatch = nextMatches[m];

                        const topPos = getOffsetRelativeTo(topMatch, container);
                        const nextPos = getOffsetRelativeTo(nextMatch, container);

                        const xStart = topPos.x + topPos.w;
                        const xEnd = nextPos.x;
                        const xMid = (xStart + xEnd) / 2;

                        const yT = topPos.y + topPos.h / 2;
                        const yN = nextPos.y + nextPos.h / 2;

                        const pathT = document.createElementNS('http://www.w3.org/2000/svg', 'path');
                        pathT.setAttribute('d', `M${{xStart}},${{yT}} H${{xMid}} V${{yN}} H${{xEnd}}`);
                        pathT.setAttribute('fill', 'none');
                        pathT.setAttribute('stroke', '#cbd5e1');
                        pathT.setAttribute('stroke-width', '1');
                        svg.appendChild(pathT);

                        if (botMatch) {{
                            const botPos = getOffsetRelativeTo(botMatch, container);
                            const yB = botPos.y + botPos.h / 2;
                            const pathB = document.createElementNS('http://www.w3.org/2000/svg', 'path');
                            pathB.setAttribute('d', `M${{xStart}},${{yB}} H${{xMid}} V${{yN}} H${{xEnd}}`);
                            pathB.setAttribute('fill', 'none');
                            pathB.setAttribute('stroke', '#cbd5e1');
                            pathB.setAttribute('stroke-width', '1');
                            svg.appendChild(pathB);
                        }}
                    }}
                }}
            }}

            let currentDrawFilterRound = 0;

            function filterDrawFromRound(r) {{
                const container = document.getElementById('draw-bracket');
                const rounds = container.querySelectorAll('.draw-round');
                const resetBtn = document.getElementById('draw-filter-reset');

                if (currentDrawFilterRound === r) {{
                    resetDrawFilter();
                    return;
                }}

                currentDrawFilterRound = r;

                rounds.forEach((round, idx) => {{
                    const header = round.querySelector('.draw-round-header');
                    if (idx < r) {{
                        round.classList.add('hidden-round');
                    }} else {{
                        round.classList.remove('hidden-round');
                    }}
                    if (header) {{
                        header.classList.toggle('active-filter', idx === r);
                    }}
                }});

                resetBtn.classList.add('visible');
                // Redraw connectors after layout change
                setTimeout(() => drawConnectors(container), 50);
                syncUrlStateForTab('draws');
            }}

            function resetDrawFilter() {{
                const container = document.getElementById('draw-bracket');
                const rounds = container.querySelectorAll('.draw-round');
                const resetBtn = document.getElementById('draw-filter-reset');

                currentDrawFilterRound = 0;
                rounds.forEach(round => {{
                    round.classList.remove('hidden-round');
                    const header = round.querySelector('.draw-round-header');
                    if (header) header.classList.remove('active-filter');
                }});
                resetBtn.classList.remove('visible');
                setTimeout(() => drawConnectors(container), 50);
                syncUrlStateForTab('draws');
            }}

            (function bindDrawRoundHeaderControls() {{
                const container = document.getElementById('draw-bracket');
                if (!container || container._drawRoundHeaderBound) return;

                function activateHeader(header) {{
                    const round = Number.parseInt(header.getAttribute('data-round') || '', 10);
                    if (Number.isInteger(round)) filterDrawFromRound(round);
                }}

                container.addEventListener('click', function(event) {{
                    const header = event.target.closest('.draw-round-header');
                    if (!header || !container.contains(header)) return;
                    activateHeader(header);
                }});

                container.addEventListener('keydown', function(event) {{
                    if (event.key !== 'Enter' && event.key !== ' ') return;
                    const header = event.target.closest('.draw-round-header');
                    if (!header || !container.contains(header)) return;
                    event.preventDefault();
                    activateHeader(header);
                }});

                container._drawRoundHeaderBound = true;
            }})();

            // Constrain draw scroll: prevent scrolling left past initial position (scrollLeft=0)
            (function() {{
                const wrapper = document.getElementById('draw-bracket-wrapper');
                if (!wrapper) return;
                wrapper.addEventListener('scroll', function() {{
                    if (this.scrollLeft < 0) this.scrollLeft = 0;
                }});
                // Touch-based constraint for mobile
                let touchStartX = 0;
                let scrollStartX = 0;
                wrapper.addEventListener('touchstart', function(e) {{
                    touchStartX = e.touches[0].clientX;
                    scrollStartX = this.scrollLeft;
                }}, {{ passive: true }});
                wrapper.addEventListener('touchmove', function(e) {{
                    if (this.scrollLeft < 0) this.scrollLeft = 0;
                    // If at left edge and trying to scroll further left, prevent
                    const dx = e.touches[0].clientX - touchStartX;
                    if (scrollStartX === 0 && dx > 0) {{
                        this.scrollLeft = 0;
                    }}
                }}, {{ passive: true }});
            }})();

        </script>
        <script src="assets/anonymous-analytics.js"></script>
{router_script}
    <script src="assets/app-shell.js"></script>
    </body>
    </html>
    """
    html_template = _apply_content_security_policy(html_template).rstrip() + "\n"
    # Always write generated site files beside this module.  main.py may be
    # launched with a different working directory (for example from an IDE),
    # but build_deploy_site.py and the local file URL use the project root.
    site_root = str(RUNTIME_SITE_ROOT)
    write_text_if_changed(os.path.join(site_root, "app.html"), html_template, encoding="utf-8-sig")

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
        folder = os.path.join(RUNTIME_SITE_ROOT, tab)
        os.makedirs(folder, exist_ok=True)
        route_html = _apply_content_security_policy(route_template.format(tab=tab)).rstrip() + "\n"
        write_text_if_changed(os.path.join(folder, "index.html"), route_html, encoding="utf-8")
