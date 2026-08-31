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

# Evidence-backed corrections for identities that cannot be resolved
# mechanically from a shared source name. Every override stores both the
# unique canonical identity label and the public presentation name, even when
# they are identical, so a future exception cannot silently leak an internal
# qualifier into the UI.
NAME_OVERRIDE_BY_ITF_ID = {
    "800673790": ("Alexandra-Nicole Aldea", "Alexandra-Nicole Aldea"),
    "800714861": ("Amanda Uribe Alvarado", "Amanda Uribe Alvarado"),
    "800651913": ("Beatriz Melo Rodrigues", "Beatriz Melo Rodrigues"),
    "800650189": ("Cecilia Stella Ferrazzoli", "Cecilia Stella Ferrazzoli"),
    "800853287": ("Farah Taysser Mahmoud", "Farah Taysser Mahmoud"),
    "800690434": ("Giulia Maria Bonaccorso", "Giulia Maria Bonaccorso"),
    "800878279": ("Josefa Segovia", "Josefa Segovia"),
    "800555467": ("Kaniska Mallela", "Kaniska Mallela"),
    "800775870": ("Laura Beatriz Travagin", "Laura Beatriz Travagin"),
    "800571700": ("Lavinia Cacace Gismondi", "Lavinia Cacace Gismondi"),
    "800774696": ("Lucia Fernandez-Trabadelo", "Lucia Fernandez-Trabadelo"),
    "800774038": ("Malak haytham ismeil", "Malak haytham ismeil"),
    "800703288": ("Maria Pares Bergnes", "Maria Pares Bergnes"),
    "800671114": ("Marta Bieniecka Zawerthal", "Marta Bieniecka Zawerthal"),
    "800841500": ("Serena Wilson Donizete", "Serena Wilson Donizete"),
    "800567185": ("Tatiana Cantos Siemers", "Tatiana Cantos Siemers"),
    "800832820": ("Valentina Laime Carrillo", "Valentina Laime Carrillo"),
    "800635001": ("Vitoria Rodrigues Oliveira", "Vitoria Rodrigues Oliveira"),
    "800462974": ("Ana Luiza Cruz", "Ana Luiza Cruz"),
    "800201426": ("Carolina García (ARG)", "Carolina García"),
    "800534700": ("Carolina García (BRA)", "Carolina García"),
    "800343636": ("Barbora Matusova (CZE)", "Barbora Matusova"),
    "800169266": ("Elizabeth Smylie", "Elizabeth Smylie"),
    "800180377": ("Francesca Romano", "Francesca Romano"),
    "800279440": ("Francesca Romano (1971)", "Francesca Romano"),
    "800199860": ("Laura Rossi", "Laura Rossi"),
    "800570615": ("María Josefina Andrade", "María Josefina Andrade"),
    "800636229": ("Maria Lazarenko", "Maria Lazarenko"),
    "800176664": ("Patricia Gómez (ARG)", "Patricia Gómez"),
    "800209139": ("Patricia Gómez (ECU)", "Patricia Gómez"),
    "800439517": ("Sofia Camila Rojas", "Sofia Camila Rojas"),
    "800375333": ("Sofia Rojas", "Sofia Rojas"),
    "800417244": ("Yue Yuan (1998)", "Yue Yuan"),
    # User-curated public names for Argentine players with long source names.
    "800141327": ("María Emilia Manago", "María Emilia Manago"),
    "800155144": ("Paula Orlini", "Paula Orlini"),
    "800221559": ("Marcela Voyame", "Marcela Voyame"),
    "800276945": ("Maria Agostina Liccardi", "Maria Agostina Liccardi"),
    "800284064": ("María Laura Martínez", "María Laura Martínez"),
    "800297846": ("Julieta Evangelina", "Julieta Evangelina"),
    "800304065": ("Eliana Pistone", "Eliana Pistone"),
    "800308320": ("Romina Sanio", "Romina Sanio"),
    "800314514": ("Daiana Firman", "Daiana Firman"),
    "800316752": ("Mariana Fernández", "Mariana Fernández"),
    "800324883": ("Greccia Cáceres", "Greccia Cáceres"),
    "800328964": ("Macarena Millán", "Macarena Millán"),
    "800335862": ("Elizabeth Caamano", "Elizabeth Caamano"),
    "800348621": ("Florencia Mattalia", "Florencia Mattalia"),
    "800364660": ("Tatiana Delafuente", "Tatiana Delafuente"),
    "800367687": ("Ornella Spinetta", "Ornella Spinetta"),
    "800384312": ("Sabrina Varela", "Sabrina Varela"),
    "800390690": ("María Belén Vaz", "María Belén Vaz"),
    "800402410": ("María Del Rosario Paso", "María Del Rosario Paso"),
    "800402413": ("Bárbara Urrutia", "Bárbara Urrutia"),
    "800409852": ("Lola Mora García", "Lola Mora García"),
    "800432292": ("Florencia Grieco", "Florencia Grieco"),
    "800460777": ("Maia Haumuller", "Maia Haumuller"),
    "800462333": ("Marina Carreira", "Marina Carreira"),
    "800462339": ("María Victoria González", "María Victoria González"),
    "800483014": ("Federica Steinmetz", "Federica Steinmetz"),
    "800533024": ("María Sol Abraham", "María Sol Abraham"),
    "800533512": ("Victoria Marchesini", "Victoria Marchesini"),
    "800544450": ("Paula Curci Micieli", "Paula Curci Micieli"),
    "800544455": ("Dulce Rodríguez Taverna", "Dulce Rodríguez Taverna"),
    "800553125": ("Sofía Madrid", "Sofía Madrid"),
    "800570407": ("Tatiana Baracco", "Tatiana Baracco"),
    "800581158": ("Martina Roldán", "Martina Roldán"),
    "800582815": ("Malena Luna Ahumada", "Malena Luna Ahumada"),
    "800639826": ("Martina Acebedo", "Martina Acebedo"),
    "800643869": ("María Dolores Martínez", "María Dolores Martínez"),
    "800645572": ("Gabriela Serrano", "Gabriela Serrano"),
    "800678208": ("Agostina Pizarro", "Agostina Pizarro"),
    "800693948": ("Martina Piotti", "Martina Piotti"),
    "800714007": ("Natalia Barcelo", "Natalia Barcelo"),
    "800723110": ("Valentina Sánchez Briend", "Valentina Sánchez Briend"),
    "800727207": ("Trinidad Vagliengo", "Trinidad Vagliengo"),
    "800727285": ("Ginebra Domínguez", "Ginebra Domínguez"),
    "800728344": ("Agustina Di Lucente", "Agustina Di Lucente"),
    "800753197": ("Ornela Mondati", "Ornela Mondati"),
    "800812081": ("Scarlet Panijan", "Scarlet Panijan"),
    "800828603": ("María José Escobar", "María José Escobar"),
    # User-curated public names for players with long source names.
    "800103946": ("Beatriz Mejuto-Perez", "Beatriz Mejuto-Perez"),
    "800175560": ("Dianne Fromholtz", "Dianne Fromholtz"),
    "800209876": ("Lorena Arias", "Lorena Arias"),
    "800214217": ("Ana Lucía Migliarini", "Ana Lucía Migliarini"),
    "800215466": ("Marcela Rodezno", "Marcela Rodezno"),
    "800245956": ("Karen Martinez-Bernal", "Karen Martinez-Bernal"),
    "800261758": ("Paula Robles García", "Paula Robles García"),
    "800268197": ("Gabriella Barbosa-Costa", "Gabriella Barbosa-Costa"),
    "800275092": ("Ana Claudia Carbajal", "Ana Claudia Carbajal"),
    "800275105": ("Maria Jose Rodriguez", "Maria Jose Rodriguez"),
    "800284694": ("Maria Claudia Santos", "Maria Claudia Santos"),
    "800293370": ("Beatriz Martins", "Beatriz Martins"),
    "800294759": ("Tifanny Aguirre Álvarez", "Tifanny Aguirre Álvarez"),
    "800295840": ("Maria Fernanda Aguirre", "Maria Fernanda Aguirre"),
    "800304103": ("Gabrielle Zambotto", "Gabrielle Zambotto"),
    "800306638": ("Eliana López Pappalardo", "Eliana López Pappalardo"),
    "800321009": ("Andressa Vaz Osorio", "Andressa Vaz Osorio"),
    "800324632": ("Manoela Corbellini", "Manoela Corbellini"),
    "800328298": ("Paula Puentes Jimenez", "Paula Puentes Jimenez"),
    "800368775": ("Rita Bentes De Oliveira", "Rita Bentes De Oliveira"),
    "800369451": ("Evelyn Moreno Rivera", "Evelyn Moreno Rivera"),
    "800370163": ("Agustina Santamaria", "Agustina Santamaria"),
    "800383229": ("Nicole Aragones", "Nicole Aragones"),
    "800402856": ("Paola Quintana Rojas", "Paola Quintana Rojas"),
    "800402888": ("Maria Elena Medina", "Maria Elena Medina"),
    "800409957": ("Nicole De O Crispino", "Nicole De O Crispino"),
    "800421431": ("Daniela La Fuente", "Daniela La Fuente"),
    "800429195": ("Jizel Matos Sequeira", "Jizel Matos Sequeira"),
    "800465794": ("Ximena Duarte Ramirez", "Ximena Duarte Ramirez"),
    "800470621": ("Beatriz Gutierrez Cerezo", "Beatriz Gutierrez Cerezo"),
    "800480814": ("Ana Carla Dos Santos", "Ana Carla Dos Santos"),
    "800491570": ("Evelin Baziloni", "Evelin Baziloni"),
    "800507833": ("Tiantsoa Rakotomanga", "Tiantsoa Rakotomanga"),
    "800512860": ("Claudia Martinez Solis", "Claudia Martinez Solis"),
    "800514389": ("Debora Janjulio Braga", "Debora Janjulio Braga"),
    "800522675": ("Isabel Ulhoa De Faria", "Isabel Ulhoa De Faria"),
    "800533834": ("Karim Carreras", "Karim Carreras"),
    "800547421": ("Tania Andrade Sabando", "Tania Andrade Sabando"),
    "800569437": ("Anna Fischer Marcondes", "Anna Fischer Marcondes"),
    "800600531": ("Mariana Higuita Barraza", "Mariana Higuita Barraza"),
    "800603217": ("Francesca Maguina", "Francesca Maguina"),
    "800630721": ("Capucine Cedillo-Vayson", "Capucine Cedillo-Vayson"),
    "800631347": ("Aline Aveiro", "Aline Aveiro"),
    "800639278": ("Lya Fernández", "Lya Fernández"),
    "800652510": ("Fernanda Escudero", "Fernanda Escudero"),
    "800656032": ("Ariadna García-Patrón", "Ariadna García-Patrón"),
    "800661082": ("Katina Zepeda", "Katina Zepeda"),
    "800678506": ("Josefa Meneses Parada", "Josefa Meneses Parada"),
    "800678677": ("Renata Guevara", "Renata Guevara"),
    "800691611": ("Thamna Díaz Alvarenga", "Thamna Díaz Alvarenga"),
    "800694191": ("Mia Junghanns", "Mia Junghanns"),
    "800698352": ("Olivia Tognato", "Olivia Tognato"),
    "800718000": ("Adel Fernández", "Adel Fernández"),
    "800720932": ("Laura Pedrotti de Oliveira", "Laura Pedrotti de Oliveira"),
    "800730099": ("Martina Mora Da Silva", "Martina Mora Da Silva"),
    "800736596": ("Stephanie Lautenschlaeger", "Stephanie Lautenschlaeger"),
    "800737949": ("Natasha Fourouclas", "Natasha Fourouclas"),
    "800816577": ("Sophia Sakada da Costa", "Sophia Sakada da Costa"),
    "800822018": ("Regina Cervera", "Regina Cervera"),
    "800846006": ("Ticiani Vasconcelos", "Ticiani Vasconcelos"),
}

NAME_OVERRIDE_BY_WTA_ID = {
    "310974": ("Laura Vallverdu", "Laura Vallverdu"),
    "334568": ("Giovana Schincariol", "Giovana Schincariol"),
    "335329": ("Laura Portela Borges", "Laura Portela Borges"),
    "336666": ("Maria Eduarda Carbone", "Maria Eduarda Carbone"),
    "334602": ("Nauhany Leme Da Silva", "Nauhany Leme Da Silva"),
    "325609": ("Barbora Matusova (SVK)", "Barbora Matusova"),
    "319854": ("Montserrat González (PAR)", "Montserrat González"),
    "311072": ("Montserrat González (MEX)", "Montserrat González"),
    "314483": ("Yue Yuan (1991)", "Yue Yuan"),
    # User-curated public names for WTA-only players with long source names.
    "318785": ("Claudia Herrero Garica", "Claudia Herrero Garica"),
    "320083": ("Clementina Riobueno", "Clementina Riobueno"),
    "320706": ("Sarai Monarrez", "Sarai Monarrez"),
    "331596": ("Carolina Chiatti Reynoso", "Carolina Chiatti Reynoso"),
    "332008": ("Anne Christine Lutkemeyer", "Anne Christine Lutkemeyer"),
    "334494": ("Nathalie Marinovitch", "Nathalie Marinovitch"),
    "337189": ("Sara Lozano Avellaneda", "Sara Lozano Avellaneda"),
}

# Public names for every identity whose unique canonical label contains an
# internal qualifier.  This metadata is persisted on the player record, so
# runtime presentation never has to infer meaning from parentheses.  Newly
# discovered name collisions receive the same metadata automatically below.
PRESENTATION_BY_PLAYER_KEY = {
    "wta:206952": "Ekaterina Kuznetsova",
    "wta:311604": "Ekaterina Makarova",
    "wta:315172": "Fernanda Sandoval",
    "wta:20054": "Katerina Bohmova",
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
    "800178944": "10001",   # Anne Aallonen
    "800513464": "331945",  # Barbora Michalkova
    "800182267": "180081",  # Belkis Rodriguez
    "800177048": "80039",   # Chiu-Mei Ho
    "800587892": "333499",  # Daphnee Mpetshi Perricard
    "800177871": "30020",   # Dyan Castillejo
    "800546838": "332098",  # Elena Ruxandra Bertea
    "800785739": "336029",  # Emery Combs
    "800643210": "335673",  # Gabriella Mikaul
    "800575336": "336655",  # Galatea Ferro
    "800209063": "190727",  # Ivana Sokac
    "800237052": "190727",  # Ivana Sakac (second ITF profile)
    "800331402": "319280",  # Jasmine Paolini
    "800179196": "250023",  # Jennifer Young (USA)
    "800263174": "312770",  # Jennifer Young (GER)
    "800646432": "337080",  # Jordyn Hazelitt
    "800180089": "160056",  # Karin Ptaszek
    "800436364": "326630",  # Kateryna Diatlova
    "800537641": "332103",  # Kelly Keller
    "800506455": "335306",  # Kim Chiarello
    "800585059": "333539",  # Kiara Nina Kucikova
    "800178321": "190019",  # Larisa Neiland / Savchenko
    "800240123": "310974",  # Laura Vallverdu-Zafra / Vallverdu-Zaira
    "800700710": "332753",  # Lorena Schaedel
    "800543563": "330991",  # Lucie Pawlak
    "800590927": "332229",  # Maayan Laron
    "800447468": "327643",  # Maddalena Giordano
    "800586966": "334909",  # Manon Favier
    "800404328": "324743",  # Marina Benito
    "800275736": "314237",  # Misaki Doi
    "800101514": "110124",  # Natasha Khan (AUS)
    "800240168": "312278",  # Natasha Khan (GBR)
    "800737949": "316823",  # Natasha Fourouclas / Fourouclas - De Castro
    "800800767": "260014",  # Ni Zhong / Zhong Shen Ni
    "800537809": "331009",  # Olga Bienzobas Fernandez Sancho
    "800435272": "330844",  # Olga Golas
    "800561777": "333462",  # Polina Kaibekova
    "800279562": "314429",  # Qiang Wang
    "800673210": "334210",  # Rachael Smith
    "800179163": "190114",  # Reeka Szikszay
    "800572950": "331158",  # Romane Longueville
    "800180479": "70022",   # Sabrina Giusto
    "800412854": "326799",  # Sara Dahlstrom
    "800179171": "30082",   # Silvana Casaretto
    "800311509": "319000",  # Simone Pratt
    "800192053": "100096",  # Stephanie Johnson (1971)
    "800393420": "324984",  # Stephanie Johnson (younger profile)
    "800513617": "332130",  # Sun Min Ha
    "800511259": "331720",  # Talia Neilson-Gatenby
    "800319635": "327611",  # Venla Ahti
    "800693144": "337237",  # Yael Saffar
    "800525621": "334233",  # Yelyzaveta Chainykova
    "800726989": "333677",  # Yihan Qu
    "800186795": "30332",   # Young-Ja Choi
}

# Source names for externally verified ITF IDs that may not have appeared in
# the locally stored match history yet. This lets the canonical crosswalk
# survive a rebuild without inventing the ITF spelling from the WTA record.
ITF_NAME_BY_ID = {
    "800101514": "Natasha Khan",
    "800179196": "Jennifer Young",
    "800192053": "Stephanie Johnson",
    "800800767": "Zhong Shen Ni",
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

    present_itf_ids = {
        value
        for row in rows
        for value in _source_ids(row, "itf")
    }
    for itf_id, wta_id in WTA_BY_ITF_ID.items():
        if itf_id in present_itf_ids:
            continue
        owners = [row for row in rows if wta_id in _source_ids(row, "wta")]
        if len(owners) > 1:
            raise ValueError(f"multiple canonical owners for configured WTA ID {wta_id}")
        if not owners:
            continue
        owner = owners[0]
        current_itf_ids = _source_ids(owner, "itf")
        if not current_itf_ids:
            owner["itf_id"] = itf_id
        else:
            additional = owner.get("additional_itf_ids")
            additional_ids = list(additional) if isinstance(additional, list) else []
            if itf_id not in additional_ids:
                additional_ids.append(itf_id)
            owner["additional_itf_ids"] = additional_ids
        itf_name = ITF_NAME_BY_ID.get(itf_id, "")
        if itf_name and not compact_text(owner.get("itf_name")):
            owner["itf_name"] = itf_name
        present_itf_ids.add(itf_id)

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
        player_key = f"wta:{wta_id}" if wta_id else f"itf:{primary_itf}"

        names = _ordered_values(
            component, "display_name", "wta_name", "itf_name", "bjkc_name"
        )
        name_override = (
            NAME_OVERRIDE_BY_ITF_ID.get(primary_itf)
            or NAME_OVERRIDE_BY_WTA_ID.get(wta_id)
        )
        display_name = name_override[0] if name_override else (names[0] if names else "")
        if not display_name:
            raise ValueError(f"identity {wta_id or primary_itf} has no display name")
        presentation_name = (
            (name_override[1] if name_override else "")
            or PRESENTATION_BY_PLAYER_KEY.get(player_key, "")
            or _most_common(_ordered_values(component, "presentation_name"))
            or display_name
        )
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
            "player_key": player_key,
            "display_name": display_name,
            "presentation_name": presentation_name,
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

    def unique_display(display_name: str, source: str, player_id: str) -> tuple[str, str]:
        if normalized_name(display_name) not in display_keys:
            display_keys.add(normalized_name(display_name))
            return display_name, ""
        qualified = f"{display_name} ({source.upper()} {player_id})"
        display_keys.add(normalized_name(qualified))
        return qualified, display_name

    known_wta = {value for row in canonical for value in [row["wta_id"], *row["additional_wta_ids"]] if value}
    for player_id, metadata in ranking_identity.items():
        if player_id in known_wta:
            continue
        display_name = metadata["display_name"] or f"WTA player {player_id}"
        display_name, presentation_name = unique_display(display_name, "wta", player_id)
        canonical.append({
            "player_key": f"wta:{player_id}",
            "display_name": display_name,
            "presentation_name": presentation_name,
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
        display_name, presentation_name = unique_display(source_name, "wta", player_id)
        canonical.append({
            "player_key": f"wta:{player_id}",
            "display_name": display_name,
            "presentation_name": presentation_name,
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
        display_name, presentation_name = unique_display(source_name, "itf", player_id)
        canonical.append({
            "player_key": f"itf:{player_id}",
            "display_name": display_name,
            "presentation_name": presentation_name,
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
