import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from canonical_data import (
    CanonicalConstraintError,
    PlayerIdentityIndex,
    source_match_key,
    sync_itf_players,
    sync_wta_match_players,
    sync_wta_players,
    tournament_key,
    write_player_rows,
)


def player(player_key, display_name, *, wta_id="", itf_id="", aliases=None):
    return {
        "player_key": player_key,
        "display_name": display_name,
        "wta_id": wta_id,
        "itf_id": itf_id,
        "aliases": aliases or [],
    }


class PlayerIdentityIndexTests(unittest.TestCase):
    def test_source_ids_are_unique(self):
        rows = [
            player("wta:1", "First", wta_id="1"),
            player("itf:2", "Second", wta_id="1", itf_id="2"),
        ]
        with self.assertRaisesRegex(CanonicalConstraintError, "WTA ID 1"):
            PlayerIdentityIndex(rows)

    def test_ambiguous_names_do_not_resolve_by_row_order(self):
        rows = [
            player("wta:1", "Alex Smith (ARG)", wta_id="1", aliases=["Alex Smith"]),
            player("wta:2", "Alex Smith (BRA)", wta_id="2", aliases=["Alex Smith"]),
        ]
        forward = PlayerIdentityIndex(rows)
        reverse = PlayerIdentityIndex(reversed(rows))

        self.assertIsNone(forward.resolve("wta", name="Alex Smith"))
        self.assertIsNone(reverse.resolve("wta", name="Alex Smith"))
        self.assertEqual(forward.resolve("wta", player_id="1").player_key, "wta:1")
        self.assertEqual(reverse.resolve("wta", player_id="1").player_key, "wta:1")

    def test_display_names_and_source_id_formats_are_enforced(self):
        with self.assertRaisesRegex(CanonicalConstraintError, "display name"):
            PlayerIdentityIndex([
                player("wta:1", "Same Name", wta_id="1"),
                player("wta:2", "Same Náme", wta_id="2"),
            ])
        with self.assertRaisesRegex(CanonicalConstraintError, "invalid WTA ID"):
            PlayerIdentityIndex([player("wta:bad", "Bad ID", wta_id="not-an-id")])

    def test_new_homonym_is_added_without_name_based_merge(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "players.json"
            existing = player("itf:800000001", "Same Name", itf_id="800000001")
            write_player_rows(path, [existing])
            added = sync_wta_players(path, [{
                "id": "123",
                "player": "Same Name",
                "country": "ARG",
                "dob": "2000-01-01",
            }])
            self.assertEqual(added, 1)
            index = PlayerIdentityIndex(json.loads(path.read_text(encoding="utf-8")))
            self.assertEqual(index.by_wta_id["123"].display_name, "Same Name (WTA 123)")
            self.assertEqual(index.by_wta_id["123"].presentation_name, "Same Name")
            self.assertIsNone(index.resolve("wta", name="Same Name"))

    def test_exact_name_country_and_dob_link_new_wta_id(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "players.json"
            existing = {
                **player("itf:800000001", "Same Name", itf_id="800000001"),
                "country": "ARG",
                "dob": "2000-01-01",
                "itf_name": "Same Name",
            }
            write_player_rows(path, [existing])

            added = sync_wta_players(path, [{
                "id": "123",
                "player": "Same Name",
                "country": "ARG",
                "dob": "2000-01-01",
            }])

            self.assertEqual(added, 1)
            rows = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(rows), 1)
            index = PlayerIdentityIndex(rows)
            record = index.by_wta_id["123"]
            self.assertEqual(record.player_key, "wta:123")
            self.assertEqual(record.itf_id, "800000001")
            self.assertEqual(record.display_name, "Same Name")

    def test_matching_name_and_country_without_dob_stays_separate(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "players.json"
            existing = {
                **player("itf:800000001", "Same Name", itf_id="800000001"),
                "country": "ARG",
                "itf_name": "Same Name",
            }
            write_player_rows(path, [existing])

            sync_wta_players(path, [{
                "id": "123",
                "player": "Same Name",
                "country": "ARG",
                "dob": "2000-01-01",
            }])

            rows = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(rows), 2)
            index = PlayerIdentityIndex(rows)
            self.assertEqual(index.by_wta_id["123"].display_name, "Same Name (WTA 123)")

    def test_new_itf_identity_is_synced_from_match_participants(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "players.json"
            write_player_rows(path, [player("wta:123", "Same Name", wta_id="123")])
            added = sync_itf_players(path, [{
                "winnerId": "800000001",
                "winnerName": "Same Name",
                "winnerCountry": "ARG",
                "loserId": "Unknown",
                "loserName": "Unknown",
            }])
            self.assertEqual(added, 1)
            index = PlayerIdentityIndex(json.loads(path.read_text(encoding="utf-8")))
            self.assertEqual(
                index.by_itf_id["800000001"].display_name,
                "Same Name (ITF 800000001)",
            )
            self.assertEqual(index.by_itf_id["800000001"].presentation_name, "Same Name")
            self.assertIsNone(index.resolve("itf", name="Same Name"))

    def test_new_wta_identity_is_synced_from_match_participants(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "players.json"
            write_player_rows(path, [])
            added = sync_wta_match_players(path, [{
                "winnerId": "123",
                "winnerName": "New Player",
                "winnerCountry": "ARG",
                "loserId": "800000001",
                "loserName": "ITF Player",
            }])
            self.assertEqual(added, 1)
            index = PlayerIdentityIndex(json.loads(path.read_text(encoding="utf-8")))
            self.assertEqual(index.by_wta_id["123"].display_name, "New Player")


class NaturalKeyTests(unittest.TestCase):
    def test_wta_key_uses_tournament_season_and_match(self):
        april = {"tournamentId": "10", "date": "1987-04-13", "matchId": "20"}
        september = {"tournamentId": "10", "date": "1987-09-14", "matchId": "20"}
        self.assertEqual(source_match_key(april, "wta"), source_match_key(september, "wta"))

    def test_united_cup_reused_match_id_has_distinct_keys(self):
        base = {
            "date": "2026-01-03",
            "matchId": "RS002",
            "winnerId": "319001",
            "loserId": "329081",
        }
        round_robin = {**base, "roundName": "Round Robin"}
        quarterfinal = {**base, "roundName": "Quarter Finals"}
        self.assertNotEqual(
            source_match_key(round_robin, "united_cup"),
            source_match_key(quarterfinal, "united_cup"),
        )

    def test_tournament_key_is_source_specific(self):
        row = {
            "tournamentId": "9999",
            "date": "2026-01-03",
            "tournamentName": "United Cup",
        }
        self.assertNotEqual(tournament_key(row, "wta"), tournament_key(row, "united_cup"))


if __name__ == "__main__":
    unittest.main()
