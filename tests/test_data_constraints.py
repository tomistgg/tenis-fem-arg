import csv
import unittest
from pathlib import Path

from canonical_data import PlayerIdentityIndex, load_player_rows, validate_project_data
from config import (
    NAME_LOOKUP,
    _build_player_mapping,
    player_name_only,
    resolve_player_display_name,
    resolve_player_presentation_name,
)
from migrate_canonical_data import (
    NAME_OVERRIDE_BY_ITF_ID,
    NAME_OVERRIDE_BY_WTA_ID,
    PRESENTATION_BY_PLAYER_KEY,
    PRIMARY_ITF_BY_WTA_ID,
    WTA_BY_ITF_ID,
)

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


class ProjectDataConstraintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = load_player_rows(DATA_DIR / "player_aliases_wta_itf.json")
        cls.index = PlayerIdentityIndex(cls.rows)
        cls.counts = validate_project_data(DATA_DIR)

    def test_all_canonical_tables_validate(self):
        self.assertGreater(self.counts["players"], 11_000)
        self.assertGreater(self.counts["rankings"], 2_000_000)
        self.assertGreater(self.counts["matches"], 40_000)
        self.assertGreater(self.counts["tournaments"], 5_000)

    def test_known_identity_conflicts_are_resolved_by_id(self):
        self.assertEqual(self.index.by_wta_id["130374"].display_name, "Laura Montalvo")
        self.assertEqual(self.index.by_wta_id["130734"].display_name, "Andreea Matei")
        self.assertEqual(
            self.index.by_itf_id["800587892"].player_key,
            self.index.by_wta_id["333499"].player_key,
        )
        self.assertNotIn("337674", self.index.by_wta_id)
        self.assertEqual(self.index.by_wta_id["315683"].display_name, "Sloane Stephens")
        self.assertEqual(
            self.index.by_wta_id["110036"].player_key,
            self.index.by_wta_id["110362"].player_key,
        )
        self.assertNotEqual(
            self.index.by_wta_id["230169"].player_key,
            self.index.by_wta_id["230190"].player_key,
        )
        self.assertEqual(self.index.by_wta_id["190019"].itf_id, "800178321")
        self.assertEqual(self.index.by_wta_id["319280"].itf_id, "800331402")
        self.assertEqual(self.index.by_wta_id["180052"].itf_id, "800180377")
        self.assertNotEqual(
            self.index.by_itf_id["800180377"].player_key,
            self.index.by_itf_id["800279440"].player_key,
        )
        self.assertEqual(
            self.index.by_itf_id["800199860"].player_key,
            self.index.by_itf_id["800209131"].player_key,
        )

    def test_homonyms_are_not_name_resolvable(self):
        for name in ("Ana Cruz", "Carolina Garcia", "Patricia Gomez", "Sofia Rojas"):
            self.assertIsNone(self.index.resolve("wta", name=name), name)
            self.assertNotIn(name.upper(), NAME_LOOKUP)

    def test_homonyms_have_plain_presentation_names(self):
        for player_id in ("314483", "324325"):
            self.assertEqual(self.index.by_wta_id[player_id].presentation_name, "Yue Yuan")
            self.assertEqual(
                resolve_player_presentation_name("wta", player_id=player_id, name="Yue Yuan"),
                "Yue Yuan",
            )

    def test_all_migration_name_exceptions_declare_public_names(self):
        for player_id, (identity_name, presentation_name) in NAME_OVERRIDE_BY_ITF_ID.items():
            record = self.index.by_itf_id[player_id]
            self.assertEqual(record.display_name, identity_name)
            self.assertEqual(record.presentation_name, presentation_name)
        for player_id, (identity_name, presentation_name) in NAME_OVERRIDE_BY_WTA_ID.items():
            record = self.index.by_wta_id[player_id]
            self.assertEqual(record.display_name, identity_name)
            self.assertEqual(record.presentation_name, presentation_name)
        for player_key, presentation_name in PRESENTATION_BY_PLAYER_KEY.items():
            self.assertEqual(self.index.by_key[player_key].presentation_name, presentation_name)

    def test_all_migration_crosswalks_and_primary_profiles_are_persisted(self):
        for itf_id, wta_id in WTA_BY_ITF_ID.items():
            self.assertEqual(self.index.by_itf_id[itf_id].wta_id, wta_id)
        for wta_id, primary_itf_id in PRIMARY_ITF_BY_WTA_ID.items():
            self.assertEqual(self.index.by_wta_id[wta_id].itf_id, primary_itf_id)

    def test_merged_secondary_wta_profiles_are_retained(self):
        expected = {
            "110036": ("110362", "Nora Koves"),
            "110523": ("333540", "Zuzana Kucova"),
        }
        for primary_wta_id, (secondary_wta_id, display_name) in expected.items():
            primary = self.index.by_wta_id[primary_wta_id]
            secondary = self.index.by_wta_id[secondary_wta_id]
            self.assertEqual(primary.player_key, secondary.player_key)
            self.assertEqual(primary.display_name, display_name)

    def test_verified_homonym_crosswalks_remain_distinct(self):
        expected = {
            "10001": "800178944",
            "260014": "800800767",
            "250023": "800179196",
            "312770": "800263174",
            "110124": "800101514",
            "312278": "800240168",
            "30082": "800179171",
            "100096": "800192053",
            "324984": "800393420",
        }
        for wta_id, itf_id in expected.items():
            self.assertEqual(self.index.by_wta_id[wta_id].itf_id, itf_id)
        self.assertNotEqual(
            self.index.by_wta_id["260014"].player_key,
            self.index.by_itf_id["800178614"].player_key,
        )

    def test_united_cup_duplicate_player_id_is_absent(self):
        with (DATA_DIR / "united_cup_matches_arg.csv").open(
            "r", encoding="utf-8-sig", newline=""
        ) as handle:
            self.assertFalse(any(
                row.get("winnerId") == "319112319112" or row.get("loserId") == "319112319112"
                for row in csv.DictReader(handle)
            ))

    def test_camila_romero_resolves_by_source_id(self):
        self.assertEqual(
            resolve_player_display_name(
                "itf", player_id="800409958", name="Camila Romero"
            ),
            "Camila Romero",
        )
        self.assertEqual(
            resolve_player_display_name(
                "itf", player_id="800514927", name="Camila Romero"
            ),
            "Camila Magalí Romero",
        )
        self.assertEqual(
            resolve_player_display_name(
                "wta", player_id="323775", name="Camila Romero"
            ),
            "Camila Romero",
        )

    def test_presented_player_names_do_not_include_source_ids(self):
        self.assertEqual(
            player_name_only("Anne Aallonen (WTA 10001)"),
            "Anne Aallonen",
        )
        self.assertEqual(
            resolve_player_display_name(
                "wta", player_id="10001", name="Anne Aallonen"
            ),
            "Anne Aallonen",
        )
        self.assertEqual(player_name_only("Example Player (WTA 123)"), "Example Player")
        self.assertEqual(player_name_only("Yue Yuan (1998)"), "Yue Yuan (1998)")

    def test_compatibility_mapping_is_independent_of_file_order(self):
        self.assertEqual(
            _build_player_mapping(self.rows),
            _build_player_mapping(list(reversed(self.rows))),
        )

    def test_itf_walkovers_are_explicit(self):
        with (DATA_DIR / "itf_matches_arg.csv").open(
            "r", encoding="utf-8-sig", newline=""
        ) as handle:
            walkovers = [
                row for row in csv.DictReader(handle)
                if (row.get("resultStatusDesc") or "").strip().casefold() == "walkover"
            ]
        self.assertTrue(walkovers)
        self.assertTrue(all(row["result"] == "W/O" for row in walkovers))
        self.assertTrue(all((row["loserName"] or "").strip() for row in walkovers))

if __name__ == "__main__":
    unittest.main()
