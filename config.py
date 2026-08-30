import json
import os
import re
import subprocess
import unicodedata

from canonical_data import PlayerIdentityIndex, normalized_identifier
from runtime_logging import get_logger
from runtime_paths import DATA_DIR as RUNTIME_DATA_DIR

logger = get_logger("config")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = str(RUNTIME_DATA_DIR)
PLAYER_ALIASES_WTA_ITF_FILE = os.path.join(DATA_DIR, "player_aliases_wta_itf.json")

_MOJIBAKE_REPLACEMENTS = {
    "\u00ed\u00a1": "á",
    "\u00ed\u00a8": "è",
    "\u00ed\u00a9": "é",
    "\u00ed\u00b1": "ñ",
    "\u00ed\u00b3": "ó",
    "\u00ed\u00bc": "ü",
    "\u00ed\u02c6": "È",
}


def repair_name_text(value):
    text = str(value or "")
    if not text:
        return ""

    repaired = text

    # Handle the more common UTF-8-decoded-as-Latin-1 mojibake first.
    if any(token in repaired for token in ("Ã", "Â", "â€", "â€™", "â€œ", "â€\x9d")):
        for source_encoding in ("latin-1", "cp1252"):
            try:
                repaired = repaired.encode(source_encoding).decode("utf-8")
                break
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue

    # Then patch the OEM-codepage style sequences present in player_aliases_wta_itf.json.
    for bad, good in _MOJIBAKE_REPLACEMENTS.items():
        repaired = repaired.replace(bad, good)

    return repaired


def _compact_spaces(value):
    return " ".join(repair_name_text(value).strip().split())


_SOURCE_ID_DISPLAY_SUFFIX_RE = re.compile(r"\s+\((?:ITF|WTA)\s+\d+\)$", re.IGNORECASE)


def player_name_only(value):
    """Remove internal source-ID disambiguators from a presented player name."""
    return _SOURCE_ID_DISPLAY_SUFFIX_RE.sub("", _compact_spaces(value)).strip()


def _fold_accents(value):
    if not value:
        return ""
    nfkd = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def _add_unique(target_list, value):
    v = _compact_spaces(value)
    if v and v not in target_list:
        target_list.append(v)


def _build_player_mapping(raw):
    mapping = {}

    # Backward compatibility: if legacy dict mapping is passed, keep it usable.
    if isinstance(raw, dict):
        for display_name, aliases in raw.items():
            display = _compact_spaces(display_name)
            if not display:
                continue
            bucket = mapping.setdefault(display, [])
            _add_unique(bucket, display)
            if isinstance(aliases, list):
                for alias in aliases:
                    raw_alias = repair_name_text(alias).strip()
                    if raw_alias and raw_alias not in bucket:
                        bucket.append(raw_alias)
                    _add_unique(bucket, alias)
        return mapping

    if not isinstance(raw, list):
        return {}

    # Validation happens before compatibility mapping is built.  Sorting by the
    # persisted key makes the output identical regardless of JSON row order.
    identity_index = PlayerIdentityIndex(raw)
    for record in sorted(identity_index.records, key=lambda value: value.player_key):
        display = _compact_spaces(record.display_name)
        bucket = mapping.setdefault(display, [])
        for value in record.names():
            raw_alias = repair_name_text(value).strip()
            if raw_alias and raw_alias not in bucket:
                bucket.append(raw_alias)
            _add_unique(bucket, value)

    return mapping


def _load_player_mapping_from_git(filename, *, min_entries=100, max_commits=50):
    rel_path = os.path.relpath(filename, BASE_DIR).replace(os.sep, "/")
    try:
        rev_list = subprocess.run(
            ["git", "-C", BASE_DIR, "rev-list", "--max-count", str(max_commits), "HEAD", "--", rel_path],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}

    if rev_list.returncode != 0:
        return {}

    for commit in (line.strip() for line in rev_list.stdout.splitlines() if line.strip()):
        try:
            show = subprocess.run(
                ["git", "-C", BASE_DIR, "show", f"{commit}:{rel_path}"],
                capture_output=True,
                check=False,
            )
            if show.returncode != 0 or not show.stdout:
                continue
            raw_text = show.stdout.decode("latin-1")
            raw = json.loads(raw_text)
        except (OSError, subprocess.SubprocessError, UnicodeError, json.JSONDecodeError):
            continue

        mapping = _build_player_mapping(raw)
        if len(mapping) >= min_entries:
            return mapping

    return {}


def _lookup_keys(value):
    base = _compact_spaces(value).upper()
    if not base:
        return []
    keys = [base]

    folded = _fold_accents(base)
    if folded and folded not in keys:
        keys.append(folded)

    dehyphen = _compact_spaces(base.replace("-", " "))
    if dehyphen and dehyphen not in keys:
        keys.append(dehyphen)

    folded_dehyphen = _fold_accents(dehyphen)
    if folded_dehyphen and folded_dehyphen not in keys:
        keys.append(folded_dehyphen)

    return keys


def load_player_mapping(filename=PLAYER_ALIASES_WTA_ITF_FILE):
    raw = None
    read_error = None
    try:
        if os.path.exists(filename):
            with open(filename, encoding="utf-8-sig") as f:
                raw = json.load(f)
        else:
            read_error = FileNotFoundError(filename)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as e:
        read_error = e
    mapping = _build_player_mapping(raw)
    if len(mapping) >= 100:
        return mapping

    recovered = _load_player_mapping_from_git(filename)
    if recovered:
        return recovered

    if read_error is not None:
        if isinstance(read_error, FileNotFoundError):
            logger.warning(f"Alerta: No se encontro {filename}.")
        else:
            logger.warning(f"Alerta: error leyendo {filename}: {read_error}")

    return mapping


with open(PLAYER_ALIASES_WTA_ITF_FILE, encoding="utf-8-sig") as _players_file:
    PLAYER_IDENTITIES = json.load(_players_file)
PLAYER_IDENTITY_INDEX = PlayerIdentityIndex(PLAYER_IDENTITIES)
PLAYER_MAPPING = _build_player_mapping(PLAYER_IDENTITIES)


def _build_unambiguous_name_lookup(identity_index):
    candidates = {}
    for record in identity_index.records:
        display_upper = player_name_only(record.display_name).upper()
        for alias in record.names():
            raw_upper = repair_name_text(alias).strip().upper()
            keys = _lookup_keys(alias)
            if raw_upper and raw_upper != _compact_spaces(alias).upper():
                keys.append(raw_upper)
            for key in keys:
                candidates.setdefault(key, {})[record.player_key] = display_upper
    return {key: next(iter(records.values())) for key, records in candidates.items() if len(records) == 1}


NAME_LOOKUP = _build_unambiguous_name_lookup(PLAYER_IDENTITY_INDEX)
WTA_ID_TO_DISPLAY = {
    player_id: player_name_only(record.display_name).upper()
    for player_id, record in PLAYER_IDENTITY_INDEX.by_wta_id.items()
}


def resolve_player_display_name(source, *, player_id="", name=""):
    """Resolve a source player to its canonical display name.

    Source IDs are authoritative.  Names are only a fallback for legacy rows
    that predate IDs, and ambiguous names deliberately remain unresolved.
    ``resolve_any_id`` is used as a compatibility fallback because some older
    WTA/GS exports contain ITF IDs even though the match source is WTA/GS.
    """
    source_key = str(source or "").strip().casefold()
    identifier = normalized_identifier(player_id)
    record = None
    if identifier and source_key in {"wta", "itf", "bjkc"}:
        record = PLAYER_IDENTITY_INDEX.resolve(source_key, player_id=identifier)
    if record is None and identifier:
        record = PLAYER_IDENTITY_INDEX.resolve_any_id(identifier)
    if record is not None:
        return player_name_only(record.display_name)

    raw_name = repair_name_text(name).strip()
    for key in _lookup_keys(raw_name):
        mapped = NAME_LOOKUP.get(key)
        if mapped:
            return mapped
    return raw_name


def resolve_player_presentation_name(source, *, player_id="", name=""):
    """Resolve the public name without changing canonical matching semantics."""
    source_key = str(source or "").strip().casefold()
    identifier = normalized_identifier(player_id)
    record = None
    if identifier and source_key in {"wta", "itf", "bjkc"}:
        record = PLAYER_IDENTITY_INDEX.resolve(source_key, player_id=identifier)
    if record is None and identifier:
        record = PLAYER_IDENTITY_INDEX.resolve_any_id(identifier)
    if record is not None:
        return record.presentation_name
    return resolve_player_display_name(source, player_id=player_id, name=name)


WTA_RANKINGS_CSV = os.path.join(DATA_DIR, "wta_rankings_20_29.csv")
WTA_RANKINGS_CSV_10_19 = os.path.join(DATA_DIR, "wta_rankings_10_19.csv")
WTA_RANKINGS_CSV_00_09 = os.path.join(DATA_DIR, "wta_rankings_00_09.csv")
WTA_RANKINGS_CSV_83_99 = os.path.join(DATA_DIR, "wta_rankings_83_99.csv")
ITF_CACHE_FILE = os.path.join(DATA_DIR, "itf_rankings_cache.json")
ITF_CALENDAR_CACHE_FILE = os.path.join(DATA_DIR, "itf_calendar_cache.json")
ENTRY_LISTS_CACHE_FILE = os.path.join(DATA_DIR, "entry_lists_cache.json")
ITF_ACCEPTANCE_STATE_FILE = os.path.join(DATA_DIR, "itf_acceptance_state.json")

# WTA calendar events that have been withdrawn and must not be resurrected by
# an older API/cache response.  Keep this keyed by the stable WTA tournament ID
# rather than by display name, which can change between API responses.
EXCLUDED_WTA_CALENDAR_TOURNAMENT_IDS = {"1150"}  # Rio de Janeiro 125

# Provider calendar correction for regular one-week events whose published
# date range would otherwise make them appear in two calendar weeks.
CALENDAR_END_DATE_OVERRIDES = {
    "itf:w-itf-bra-2026-014": "2026-11-22",  # W35 Sao Paulo
}

# Published 2027 Grand Slam main-draw start dates used while the WTA calendar
# does not yet contain the next season.  These prevent the 52-week projection
# from shifting a Slam when its week moves between seasons.
GRAND_SLAM_START_DATE_OVERRIDES = {
    ("Roland Garros", 2027): "2027-05-24",
    ("Wimbledon", 2027): "2027-06-28",
    ("US Open", 2027): "2027-08-30",
}

API_URL = "https://api.wtatennis.com/tennis/players/ranked"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
    )
}

TOURNAMENT_NAME_OVERRIDES = {
    "Grand Slam Paris": "Roland Garros",
    "Grand Slam Wimbledon": "Wimbledon",
    "Grand Slam New York": "US Open",
}

CITY_CASE_FIXES = {
    "Dc": "DC",
}

COUNTRY_TO_CONTINENT = {
    # South America
    "BRA": "south_america",
    "ARG": "south_america",
    "CHI": "south_america",
    "COL": "south_america",
    "PER": "south_america",
    "ECU": "south_america",
    "URU": "south_america",
    "VEN": "south_america",
    "BOL": "south_america",
    "PAR": "south_america",
    "GUY": "south_america",
    "SUR": "south_america",
    # North and Central America
    "USA": "north_central_america",
    "US": "north_central_america",
    "CAN": "north_central_america",
    "MEX": "north_central_america",
    "CRC": "north_central_america",
    "DOM": "north_central_america",
    "PUR": "north_central_america",
    "GUA": "north_central_america",
    "HON": "north_central_america",
    "ESA": "north_central_america",
    "NCA": "north_central_america",
    "PAN": "north_central_america",
    "JAM": "north_central_america",
    "TTO": "north_central_america",
    "HAI": "north_central_america",
    "BAH": "north_central_america",
    "BAR": "north_central_america",
    "CUB": "north_central_america",
    "BER": "north_central_america",
    "AHO": "north_central_america",
    "ARU": "north_central_america",
    # Europe
    "FRA": "europe",
    "GBR": "europe",
    "ESP": "europe",
    "ITA": "europe",
    "GER": "europe",
    "SUI": "europe",
    "AUT": "europe",
    "BEL": "europe",
    "NED": "europe",
    "POR": "europe",
    "SWE": "europe",
    "NOR": "europe",
    "DEN": "europe",
    "FIN": "europe",
    "POL": "europe",
    "CZE": "europe",
    "SVK": "europe",
    "HUN": "europe",
    "ROU": "europe",
    "BUL": "europe",
    "CRO": "europe",
    "SRB": "europe",
    "SLO": "europe",
    "BIH": "europe",
    "MNE": "europe",
    "MKD": "europe",
    "ALB": "europe",
    "GRE": "europe",
    "CYP": "europe",
    "TUR": "europe",
    "GEO": "europe",
    "ARM": "europe",
    "UKR": "europe",
    "BLR": "europe",
    "MDA": "europe",
    "LAT": "europe",
    "LTU": "europe",
    "EST": "europe",
    "IRL": "europe",
    "LUX": "europe",
    "MON": "europe",
    "AND": "europe",
    "MLT": "europe",
    "ISR": "europe",
    "ISL": "europe",
    "RUS": "europe",
    # Asia
    "CHN": "asia",
    "JPN": "asia",
    "KOR": "asia",
    "IND": "asia",
    "THA": "asia",
    "MAS": "asia",
    "INA": "asia",
    "PHI": "asia",
    "SGP": "asia",
    "VIE": "asia",
    "TPE": "asia",
    "HKG": "asia",
    "MAC": "asia",
    "KAZ": "asia",
    "UZB": "asia",
    "QAT": "asia",
    "UAE": "asia",
    "KSA": "asia",
    "BRN": "asia",
    "KUW": "asia",
    "OMA": "asia",
    "JOR": "asia",
    "LBN": "asia",
    "IRQ": "asia",
    "IRI": "asia",
    "PAK": "asia",
    "SRI": "asia",
    "BAN": "asia",
    "NEP": "asia",
    "MGL": "asia",
    "MYA": "asia",
    "CAM": "asia",
    "LAO": "asia",
    # Oceania
    "AUS": "oceania",
    "NZL": "oceania",
    "FIJ": "oceania",
    "SAM": "oceania",
    "PNG": "oceania",
    "GUM": "oceania",
    # Africa
    "RSA": "africa",
    "ANG": "africa",
    "DZA": "africa",
    "EGY": "africa",
    "MAR": "africa",
    "TUN": "africa",
    "ALG": "africa",
    "NGR": "africa",
    "KEN": "africa",
    "GHA": "africa",
    "CIV": "africa",
    "SEN": "africa",
    "CMR": "africa",
    "UGA": "africa",
    "ETH": "africa",
    "TAN": "africa",
    "ZIM": "africa",
    "ZAM": "africa",
    "MOZ": "africa",
    "MAD": "africa",
    "BEN": "africa",
    "TOG": "africa",
    "GAB": "africa",
    "COD": "africa",
    "RWA": "africa",
    "BUR": "africa",
    "MLI": "africa",
    "NIG": "africa",
    "BOT": "africa",
    "NAM": "africa",
    "MRI": "africa",
    "LBA": "africa",
}

CONTINENT_LABELS = {
    "south_america": "S America",
    "north_central_america": "N/C America",
    "europe": "Europe",
    "africa": "Africa",
    "asia": "Asia",
    "oceania": "Oceania",
}

MOBILE_CONTINENT_LABELS = {
    "south_america": "SA",
    "north_central_america": "NA",
    "europe": "EUR",
    "africa": "AFR",
    "asia": "ASIA",
    "oceania": "OCE",
}

CONTINENT_KEYS = ["south_america", "north_central_america", "europe", "africa", "asia", "oceania"]
