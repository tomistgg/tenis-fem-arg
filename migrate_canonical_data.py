"""One-time, idempotent migration of legacy source data to canonical identities.

The source CSV layouts remain compatible with the site, while the player table
gains stable keys and the known ranking/match anomalies are repaired.  Re-run
this script safely after restoring an older snapshot.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from collections import Counter, defaultdict
from collections.abc import Callable
from pathlib import Path

from canonical_data import (
    MATCH_SOURCES,
    RANKING_FILENAMES,
    compact_text,
    normalized_identifier,
    normalized_name,
    validate_project_data,
    write_player_rows,
)


BAD_WTA_COPIES = {
    ("1020033008", "1987-04-13", "Suntory Japan Open - Tokyo"),
    ("1020033071", "1987-04-13", "Suntory Japan Open - Tokyo"),
    ("1020033087", "1987-04-13", "Suntory Japan Open - Tokyo"),
    ("1020033095", "1987-04-13", "Suntory Japan Open - Tokyo"),
    ("1020057093", "1987-04-13", "Suntory Japan Open - Tokyo"),
    ("1100271782", "2002-07-21", "Idea Prokom Open - Sopot"),
}

# These are evidence-backed corrections for identities that cannot be resolved
# mechanically from a shared source name.
DISPLAY_BY_ITF_ID = {
    "800462974": "Ana Luiza Cruz",
    "800201426": "Carolina García (ARG)",
    "800534700": "Carolina García (BRA)",
    "800343636": "Barbora Matusova (CZE)",
    "800169266": "Elizabeth Smylie",
    "800180377": "Francesca Romano",
    "800279440": "Francesca Romano (1971)",
    "800199860": "Laura Rossi",
    "800570615": "María Josefina Andrade Benedetti",
    "800636229": "Maria Lazarenko",
    "800176664": "Patricia Gómez (ARG)",
    "800209139": "Patricia Gómez (ECU)",
    "800439517": "Sofia Camila Rojas",
    "800375333": "Sofia Rojas",
    "800417244": "Yue Yuan (1998)",
}

DISPLAY_BY_WTA_ID = {
    "325609": "Barbora Matusova (SVK)",
    "319854": "Montserrat González (PAR)",
    "311072": "Montserrat González (MEX)",
    "314483": "Yue Yuan (1991)",
}

PRIMARY_ITF_BY_WTA_ID = {
    # Both IDs occur in the same player's contiguous ranking/match history.
    "180297": "800199860",
}

# Verified WTA/ITF crosswalks that were absent from the legacy aliases.  These
# use exact source names plus compatible nation/career evidence; keeping the
# crosswalk explicit avoids broad, unsafe name-based merging.
WTA_BY_ITF_ID = {
    "800536015": "332762",  # Alice Soulie
    "800221348": "130734",  # Andreea Matei
    "800513464": "331945",  # Barbora Michalkova
    "800546838": "332098",  # Elena Ruxandra Bertea
    "800785739": "336029",  # Emery Combs
    "800643210": "335673",  # Gabriella Mikaul
    "800209063": "190727",  # Ivana Sokac
    "800237052": "190727",  # Ivana Sakac (second ITF profile)
    "800331402": "319280",  # Jasmine Paolini
    "800537641": "332103",  # Kelly Keller
    "800585059": "333539",  # Kiara Nina Kucikova
    "800178321": "190019",  # Larisa Neiland / Savchenko
    "800543563": "330991",  # Lucie Pawlak
    "800447468": "327643",  # Maddalena Giordano
    "800404328": "324743",  # Marina Benito
    "800275736": "314237",  # Misaki Doi
    "800435272": "330844",  # Olga Golas
    "800279562": "314429",  # Qiang Wang
    "800572950": "331158",  # Romane Longueville
    "800412854": "326799",  # Sara Dahlstrom
    "800311509": "319000",  # Simone Pratt
    "800513617": "332130",  # Sun Min Ha
    "800319635": "327611",  # Venla Ahti
    "800693144": "337237",  # Yael Saffar
    "800525621": "334233",  # Yelyzaveta Chainykova
    "800726989": "333677",  # Yihan Qu
    "800186795": "30332",   # Young-Ja Choi
}


class DisjointSet:
    def __init__(self, size: int):
        self.parent = list(range(size))

    def find(self, item: int) -> int:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left: int, right: int) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def _ordered_values(rows: list[dict], *fields: str) -> list[str]:
    values: list[str] = []
    for row in rows:
        for field in fields:
            value = compact_text(row.get(field))
            if value and value not in values:
                values.append(value)
    return values


def _most_common(values: list[str]) -> str:
    if not values:
        return ""
    counts = Counter(values)
    return min(values, key=lambda value: (-counts[value], values.index(value)))


def _source_ids(row: dict, source: str) -> list[str]:
    values = [normalized_identifier(row.get(f"{source}_id"))]
    additional = row.get(f"additional_{source}_ids")
    if isinstance(additional, list):
        values.extend(normalized_identifier(value) for value in additional)
    return [value for value in values if value]


def _load_ranking_identity(data_dir: Path) -> dict[str, dict[str, str]]:
    identities: dict[str, dict[str, str]] = {}
    for filename in RANKING_FILENAMES:
        with (data_dir / filename).open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                player_id = normalized_identifier(row.get("id"))
                if not player_id:
                    continue
                identities[player_id] = {
                    "display_name": compact_text(row.get("player")),
                    "country": compact_text(row.get("country")).upper(),
                    "dob": compact_text(row.get("dob"))[:10],
                }
    return identities


def _load_match_identity(data_dir: Path) -> dict[str, dict[str, str]]:
    observations: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for filename, source in MATCH_SOURCES.items():
        with (data_dir / filename).open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                for side in ("winner", "loser"):
                    player_id = normalized_identifier(row.get(f"{side}Id"))
                    if source == "united_cup" and player_id == "319112319112":
                        player_id = "319112"
                    name = compact_text(row.get(f"{side}Name"))
                    if not player_id or name.casefold() in {"", "bye", "unknown"}:
                        continue
                    observations[player_id]["names"].append(name)
                    country = compact_text(row.get(f"{side}Country")).upper()
                    if country and country != "-":
                        observations[player_id]["countries"].append(country)

    return {
        player_id: {
            "display_name": _most_common(values["names"]),
            "country": _most_common(values["countries"]),
        }
        for player_id, values in observations.items()
    }


def _merge_legacy_players(data_dir: Path) -> tuple[list[dict], dict[str, int]]:
    path = data_dir / "player_aliases_wta_itf.json"
    legacy = json.loads(path.read_text(encoding="utf-8-sig"))
    rows: list[dict] = []
    dropped_placeholders = 0
    for original in legacy:
        if not isinstance(original, dict):
            continue
        row = dict(original)
        if normalized_identifier(row.get("itf_id")) == "Fabiana Gomez":
            # A player name was accidentally written into the ID column. The
            # valid ITF 800245810 identity is reconstructed from match data.
            dropped_placeholders += 1
            continue
        if (
            not normalized_identifier(row.get("wta_id"))
            and not normalized_identifier(row.get("itf_id"))
            and compact_text(row.get("itf_name")).casefold() == "bye"
        ):
            dropped_placeholders += 1
            continue
        itf_ids = _source_ids(row, "itf")
        mapped_wta_ids = {WTA_BY_ITF_ID[value] for value in itf_ids if value in WTA_BY_ITF_ID}
        if len(mapped_wta_ids) > 1:
            raise ValueError(f"conflicting WTA crosswalk for ITF IDs {itf_ids}")
        if mapped_wta_ids:
            row["wta_id"] = next(iter(mapped_wta_ids))
        elif normalized_identifier(row.get("wta_id")) == "Neiland":
            row["wta_id"] = "190019"
        rows.append(row)

    groups = DisjointSet(len(rows))
    source_owner: dict[tuple[str, str], int] = {}
    for index, row in enumerate(rows):
        for source in ("wta", "itf", "bjkc"):
            for value in _source_ids(row, source):
                key = (source, value)
                if key in source_owner:
                    groups.union(source_owner[key], index)
                else:
                    source_owner[key] = index

    components: dict[int, list[dict]] = defaultdict(list)
    for index, row in enumerate(rows):
        components[groups.find(index)].append(row)

    ranking_identity = _load_ranking_identity(data_dir)
    match_identity = _load_match_identity(data_dir)
    canonical: list[dict] = []
    for component in components.values():
        wta_ids = list(dict.fromkeys(value for row in component for value in _source_ids(row, "wta")))
        itf_ids = list(dict.fromkeys(value for row in component for value in _source_ids(row, "itf")))
        bjkc_ids = list(dict.fromkeys(value for row in component for value in _source_ids(row, "bjkc")))
        wta_id = wta_ids[0] if wta_ids else ""
        additional_wta_ids = wta_ids[1:]
        primary_itf = PRIMARY_ITF_BY_WTA_ID.get(wta_id, "")
        if not primary_itf:
            primary_itf = itf_ids[0] if itf_ids else ""
        if primary_itf and primary_itf not in itf_ids:
            raise ValueError(f"configured primary ITF ID {primary_itf} is absent")
        additional_itf_ids = [value for value in itf_ids if value != primary_itf]

        names = _ordered_values(
            component, "display_name", "wta_name", "itf_name", "bjkc_name"
        )
        explicit_display = DISPLAY_BY_ITF_ID.get(primary_itf, "") or DISPLAY_BY_WTA_ID.get(wta_id, "")
        display_name = explicit_display or (names[0] if names else "")
        if not display_name:
            raise ValueError(f"identity {wta_id or primary_itf} has no display name")
        aliases: list[str] = []
        for row in component:
            raw_aliases = row.get("aliases")
            for value in raw_aliases if isinstance(raw_aliases, list) else []:
                text = compact_text(value)
                if text and text != display_name and text not in aliases:
                    aliases.append(text)
        for name in names:
            if name != display_name and name not in aliases:
                aliases.append(name)

        metadata = ranking_identity.get(wta_id, {})
        if not metadata and primary_itf:
            metadata = match_identity.get(primary_itf, {})
        country = _most_common(_ordered_values(component, "country")) or metadata.get("country", "")
        dob = _most_common(_ordered_values(component, "dob")) or metadata.get("dob", "")
        wta_names = _ordered_values(component, "wta_name")
        itf_names = _ordered_values(component, "itf_name")
        bjkc_names = _ordered_values(component, "bjkc_name")
        canonical.append({
            "player_key": f"wta:{wta_id}" if wta_id else f"itf:{primary_itf}",
            "display_name": display_name,
            "country": country,
            "dob": dob,
            "wta_id": wta_id,
            "wta_name": wta_names[0] if wta_names else "",
            "itf_id": primary_itf,
            "itf_name": itf_names[0] if itf_names else "",
            "bjkc_id": bjkc_ids[0] if bjkc_ids else "",
            "bjkc_name": bjkc_names[0] if bjkc_names else "",
            "aliases": aliases,
            "additional_wta_ids": additional_wta_ids,
            "additional_itf_ids": additional_itf_ids,
            "additional_bjkc_ids": bjkc_ids[1:],
        })

    display_keys = {normalized_name(row["display_name"]) for row in canonical}

    def unique_display(display_name: str, source: str, player_id: str) -> str:
        if normalized_name(display_name) not in display_keys:
            display_keys.add(normalized_name(display_name))
            return display_name
        qualified = f"{display_name} ({source.upper()} {player_id})"
        display_keys.add(normalized_name(qualified))
        return qualified

    known_wta = {value for row in canonical for value in [row["wta_id"], *row["additional_wta_ids"]] if value}
    for player_id, metadata in ranking_identity.items():
        if player_id in known_wta:
            continue
        display_name = metadata["display_name"] or f"WTA player {player_id}"
        display_name = unique_display(display_name, "wta", player_id)
        canonical.append({
            "player_key": f"wta:{player_id}",
            "display_name": display_name,
            "country": metadata["country"],
            "dob": metadata["dob"],
            "wta_id": player_id,
            "wta_name": display_name,
            "itf_id": "",
            "itf_name": "",
            "bjkc_id": "",
            "bjkc_name": "",
            "aliases": [],
            "additional_wta_ids": [],
            "additional_itf_ids": [],
            "additional_bjkc_ids": [],
        })
        known_wta.add(player_id)

    for player_id, metadata in match_identity.items():
        if not player_id.isdigit() or player_id.startswith("800") or player_id in known_wta:
            continue
        source_name = metadata["display_name"] or f"WTA player {player_id}"
        display_name = unique_display(source_name, "wta", player_id)
        canonical.append({
            "player_key": f"wta:{player_id}",
            "display_name": display_name,
            "country": metadata["country"],
            "dob": "",
            "wta_id": player_id,
            "wta_name": source_name,
            "itf_id": "",
            "itf_name": "",
            "bjkc_id": "",
            "bjkc_name": "",
            "aliases": [],
            "additional_wta_ids": [],
            "additional_itf_ids": [],
            "additional_bjkc_ids": [],
        })
        known_wta.add(player_id)

    known_itf = {value for row in canonical for value in [row["itf_id"], *row["additional_itf_ids"]] if value}
    for player_id, metadata in match_identity.items():
        if not player_id.startswith("800") or player_id in known_itf:
            continue
        source_name = metadata["display_name"] or f"ITF player {player_id}"
        display_name = unique_display(source_name, "itf", player_id)
        canonical.append({
            "player_key": f"itf:{player_id}",
            "display_name": display_name,
            "country": metadata["country"],
            "dob": "",
            "wta_id": "",
            "wta_name": "",
            "itf_id": player_id,
            "itf_name": source_name,
            "bjkc_id": "",
            "bjkc_name": "",
            "aliases": [],
            "additional_wta_ids": [],
            "additional_itf_ids": [],
            "additional_bjkc_ids": [],
        })
        known_itf.add(player_id)

    canonical.sort(key=lambda row: (row["display_name"].casefold(), row["player_key"]))
    return canonical, {
        "legacy_rows": len(legacy),
        "merged_rows": len(components),
        "canonical_rows": len(canonical),
        "dropped_placeholders": dropped_placeholders,
    }


def _atomic_replace(path: Path, write: Callable[[Path], None]) -> None:
    temp_path = path.with_name(path.name + ".canonical.tmp")
    try:
        write(temp_path)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _deduplicate_rankings(path: Path) -> int:
    removed = 0

    def write(temp_path: Path) -> None:
        nonlocal removed
        with path.open("r", encoding="utf-8-sig", newline="") as source, temp_path.open(
            "w", encoding="utf-8", newline=""
        ) as target:
            reader = csv.DictReader(source)
            fieldnames = reader.fieldnames
            if fieldnames is None:
                raise ValueError(f"ranking file has no header: {path}")
            writer = csv.DictWriter(target, fieldnames=fieldnames, lineterminator="\r\n")
            writer.writeheader()
            seen: set[tuple[str, str]] = set()
            for row in reader:
                key = (compact_text(row.get("week_date")), normalized_identifier(row.get("id")))
                if key in seen:
                    removed += 1
                    continue
                seen.add(key)
                writer.writerow(row)

    _atomic_replace(path, write)
    return removed


def _clean_wta_matches(path: Path) -> int:
    removed = 0

    def write(temp_path: Path) -> None:
        nonlocal removed
        with path.open("r", encoding="utf-8-sig", newline="") as source, temp_path.open(
            "w", encoding="utf-8-sig", newline=""
        ) as target:
            reader = csv.DictReader(source)
            fieldnames = reader.fieldnames
            if fieldnames is None:
                raise ValueError(f"WTA match file has no header: {path}")
            writer = csv.DictWriter(target, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
            for row in reader:
                key = (row.get("matchId", ""), row.get("date", ""), row.get("tournamentName", ""))
                if key in BAD_WTA_COPIES:
                    removed += 1
                    continue
                writer.writerow(row)

    _atomic_replace(path, write)
    return removed


def _clean_itf_matches(path: Path) -> tuple[int, int]:
    explicit_losers = 0
    explicit_results = 0

    def write(temp_path: Path) -> None:
        nonlocal explicit_losers, explicit_results
        with path.open("r", encoding="utf-8-sig", newline="") as source, temp_path.open(
            "w", encoding="utf-8-sig", newline=""
        ) as target:
            reader = csv.DictReader(source)
            fieldnames = reader.fieldnames
            if fieldnames is None:
                raise ValueError(f"ITF match file has no header: {path}")
            writer = csv.DictWriter(target, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
            for row in reader:
                if compact_text(row.get("resultStatusDesc")).casefold() == "walkover":
                    if not compact_text(row.get("result")):
                        row["result"] = "W/O"
                        explicit_results += 1
                    if not compact_text(row.get("loserName")):
                        row["loserId"] = "Unknown"
                        row["loserName"] = "Unknown"
                        row["loserCountry"] = "-"
                        explicit_losers += 1
                writer.writerow(row)

    _atomic_replace(path, write)
    return explicit_losers, explicit_results


def _clean_united_cup(path: Path) -> int:
    corrected = 0

    def write(temp_path: Path) -> None:
        nonlocal corrected
        with path.open("r", encoding="utf-8-sig", newline="") as source, temp_path.open(
            "w", encoding="utf-8-sig", newline=""
        ) as target:
            reader = csv.DictReader(source)
            fieldnames = reader.fieldnames
            if fieldnames is None:
                raise ValueError(f"United Cup match file has no header: {path}")
            writer = csv.DictWriter(target, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
            for row in reader:
                if row.get("loserId") == "319112319112" and row.get("loserName") == "Nadia Podoroska":
                    row["loserId"] = "319112"
                    corrected += 1
                writer.writerow(row)

    _atomic_replace(path, write)
    return corrected


def migrate(data_dir: Path) -> dict[str, int]:
    players, stats = _merge_legacy_players(data_dir)
    write_player_rows(data_dir / "player_aliases_wta_itf.json", players)
    stats["ranking_duplicates_removed"] = sum(
        _deduplicate_rankings(data_dir / filename)
        for filename in ("wta_rankings_00_09.csv", "wta_rankings_10_19.csv")
    )
    stats["bad_wta_matches_removed"] = _clean_wta_matches(data_dir / "wta_matches_arg.csv")
    losers, results = _clean_itf_matches(data_dir / "itf_matches_arg.csv")
    stats["itf_walkover_losers_completed"] = losers
    stats["itf_walkover_results_completed"] = results
    stats["united_cup_ids_corrected"] = _clean_united_cup(
        data_dir / "united_cup_matches_arg.csv"
    )
    stats.update({f"validated_{key}": value for key, value in validate_project_data(data_dir).items()})
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate WTARG source files to canonical identities.")
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parent / "data")
    args = parser.parse_args()
    stats = migrate(args.data_dir)
    print("Canonical migration complete:")
    for key, value in stats.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
