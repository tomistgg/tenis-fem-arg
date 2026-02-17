import os
import json


def load_player_mapping(filename="player_aliases.json"):
    if not os.path.exists(filename):
        print(f"Alerta: No se encontr\u00f3 {filename}.")
        return {}
    with open(filename, "r", encoding="utf-8") as f:
        return json.load(f)


PLAYER_MAPPING = load_player_mapping()

NAME_LOOKUP = {}
for display_name, aliases in PLAYER_MAPPING.items():
    for alias in aliases:
        NAME_LOOKUP[alias.strip().upper()] = display_name.upper()

WTA_CACHE_FILE = "wta_rankings_cache.json"
ITF_CACHE_FILE = "itf_rankings_cache.json"
ENTRY_LISTS_CACHE_FILE = "entry_lists_cache.json"

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
