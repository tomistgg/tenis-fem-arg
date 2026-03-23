import os
import json
import unicodedata

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
PLAYER_ALIASES_WTA_ITF_FILE = os.path.join(DATA_DIR, "player_aliases_wta_itf.json")

def _compact_spaces(value):
    return " ".join(str(value or "").strip().split())


def _fold_accents(value):
    if not value:
        return ""
    nfkd = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def _add_unique(target_list, value):
    v = _compact_spaces(value)
    if v and v not in target_list:
        target_list.append(v)


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
    if not os.path.exists(filename):
        print(f"Alerta: No se encontro {filename}.")
        return {}
    try:
        with open(filename, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except Exception as e:
        print(f"Alerta: error leyendo {filename}: {e}")
        return {}

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
                    _add_unique(bucket, alias)
        return mapping

    if not isinstance(raw, list):
        return {}

    for item in raw:
        if not isinstance(item, dict):
            continue

        display = _compact_spaces(
            item.get("display_name")
            or item.get("wta_name")
            or item.get("itf_name")
            or item.get("bjkc_name")
        )
        if not display:
            continue

        bucket = mapping.setdefault(display, [])
        for key in ("display_name", "wta_name", "itf_name", "bjkc_name"):
            _add_unique(bucket, item.get(key))

        extra_aliases = item.get("aliases")
        if isinstance(extra_aliases, list):
            for alias in extra_aliases:
                _add_unique(bucket, alias)

    return mapping


PLAYER_MAPPING = load_player_mapping()

NAME_LOOKUP = {}
for display_name, aliases in PLAYER_MAPPING.items():
    display_upper = _compact_spaces(display_name).upper()
    if not display_upper:
        continue
    for key in _lookup_keys(display_upper):
        NAME_LOOKUP[key] = display_upper

    for alias in aliases:
        for key in _lookup_keys(alias):
            NAME_LOOKUP[key] = display_upper

WTA_RANKINGS_CSV = os.path.join(DATA_DIR, "wta_rankings_20_29.csv")
WTA_RANKINGS_CSV_10_19 = os.path.join(DATA_DIR, "wta_rankings_10_19.csv")
WTA_RANKINGS_CSV_00_09 = os.path.join(DATA_DIR, "wta_rankings_00_09.csv")
ITF_CACHE_FILE = os.path.join(DATA_DIR, "itf_rankings_cache.json")
ENTRY_LISTS_CACHE_FILE = os.path.join(DATA_DIR, "entry_lists_cache.json")

API_URL = "https://api.wtatennis.com/tennis/players/ranked"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"}

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
    "BRA": "south_america", "ARG": "south_america", "CHI": "south_america", "COL": "south_america",
    "PER": "south_america", "ECU": "south_america", "URU": "south_america", "VEN": "south_america",
    "BOL": "south_america", "PAR": "south_america", "GUY": "south_america", "SUR": "south_america",
    # North and Central America
    "USA": "north_central_america", "US": "north_central_america", "CAN": "north_central_america", "MEX": "north_central_america",
    "CRC": "north_central_america", "DOM": "north_central_america", "PUR": "north_central_america", "GUA": "north_central_america",
    "HON": "north_central_america", "ESA": "north_central_america", "NCA": "north_central_america", "PAN": "north_central_america",
    "JAM": "north_central_america", "TTO": "north_central_america", "HAI": "north_central_america", "BAH": "north_central_america",
    "BAR": "north_central_america", "CUB": "north_central_america", "BER": "north_central_america", "AHO": "north_central_america",
    "ARU": "north_central_america",
    # Europe
    "FRA": "europe", "GBR": "europe", "ESP": "europe", "ITA": "europe", "GER": "europe", "SUI": "europe",
    "AUT": "europe", "BEL": "europe", "NED": "europe", "POR": "europe", "SWE": "europe", "NOR": "europe",
    "DEN": "europe", "FIN": "europe", "POL": "europe", "CZE": "europe", "SVK": "europe", "HUN": "europe",
    "ROU": "europe", "BUL": "europe", "CRO": "europe", "SRB": "europe", "SLO": "europe", "BIH": "europe",
    "MNE": "europe", "MKD": "europe", "ALB": "europe", "GRE": "europe", "CYP": "europe", "TUR": "europe",
    "GEO": "europe", "ARM": "europe", "UKR": "europe", "BLR": "europe", "MDA": "europe", "LAT": "europe",
    "LTU": "europe", "EST": "europe", "IRL": "europe", "LUX": "europe", "MON": "europe", "AND": "europe",
    "MLT": "europe", "ISR": "europe", "ISL": "europe", "RUS": "europe",
    # Asia
    "CHN": "asia", "JPN": "asia", "KOR": "asia",
    "IND": "asia", "THA": "asia", "MAS": "asia", "INA": "asia", "PHI": "asia",
    "SGP": "asia", "VIE": "asia", "TPE": "asia", "HKG": "asia", "MAC": "asia",
    "KAZ": "asia", "UZB": "asia", "QAT": "asia", "UAE": "asia", "KSA": "asia",
    "BRN": "asia", "KUW": "asia", "OMA": "asia", "JOR": "asia", "LBN": "asia",
    "IRQ": "asia", "IRI": "asia", "PAK": "asia", "SRI": "asia", "BAN": "asia",
    "NEP": "asia", "MGL": "asia", "MYA": "asia", "CAM": "asia", "LAO": "asia",
    # Oceania
    "AUS": "oceania", "NZL": "oceania", "FIJ": "oceania", "SAM": "oceania", "PNG": "oceania", "GUM": "oceania",
    # Africa
    "RSA": "africa", "EGY": "africa", "MAR": "africa", "TUN": "africa", "ALG": "africa", "NGR": "africa",
    "KEN": "africa", "GHA": "africa", "CIV": "africa", "SEN": "africa", "CMR": "africa", "UGA": "africa",
    "ETH": "africa", "TAN": "africa", "ZIM": "africa", "ZAM": "africa", "MOZ": "africa", "MAD": "africa",
    "BEN": "africa", "TOG": "africa", "GAB": "africa", "COD": "africa", "RWA": "africa", "BUR": "africa",
    "MLI": "africa", "NIG": "africa", "BOT": "africa", "NAM": "africa", "MRI": "africa", "LBA": "africa",
}

CONTINENT_LABELS = {
    "south_america": "S America",
    "north_central_america": "N/C America",
    "europe": "Europe",
    "africa": "Africa",
    "asia": "Asia",
    "oceania": "Oceania"
}

CONTINENT_KEYS = ["south_america", "north_central_america", "europe", "africa", "asia", "oceania"]

COUNTRY_OVERRIDES = {
    "FRANCESCA MATTIOLI": "ARG",
}
