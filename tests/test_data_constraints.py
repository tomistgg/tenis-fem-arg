import csv
import unittest
from pathlib import Path

from canonical_data import PlayerIdentityIndex, load_player_rows, validate_project_data
from config import (
    NAME_LOOKUP,
    _build_player_mapping,
    player_name_only,
    resolve_player_display_name,
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
        self.assertEqual(self.index.by_wta_id["190019"].itf_id, "800178321")
        self.assertEqual(self.index.by_wta_id["319280"].itf_id, "800331402")
        self.assertEqual(
            self.index.by_itf_id["800199860"].player_key,
            self.index.by_itf_id["800209131"].player_key,
        )

    def test_homonyms_are_not_name_resolvable(self):
        for name in ("Ana Cruz", "Carolina Garcia", "Patricia Gomez", "Sofia Rojas"):
            self.assertIsNone(self.index.resolve("wta", name=name), name)
            self.assertNotIn(name.upper(), NAME_LOOKUP)

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
            player_name_only("Lorena Schaedel (ITF 800700710)"),
            "Lorena Schaedel",
        )
        self.assertEqual(
            resolve_player_display_name(
                "itf", player_id="800700710", name="Lorena Schaedel"
            ),
            "Lorena Schaedel",
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
