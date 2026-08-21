import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main as main_module
from html_generator import (
    _display_tournament_name,
    _schedule_tournament_base_name,
    _week_label_sort_key,
    country_flag_html,
)
from site_renderer import render_site_from_data
from utils import expand_entry_lists_cache

PROJECT_DIR = Path(__file__).resolve().parents[1]
GENERATED_SITE_DIR = PROJECT_DIR
AUTHORING_FRONTEND_FILES = (
    "web/templates/app.html",
    "web/css/app.css",
    "web/js/app.js",
    "web/js/data-loader.js",
    "web/js/router.js",
    "web/js/tabs/draws.js",
    "web/js/tabs/roadtogs.js",
    "web/js/tabs/tstrength.js",
)
GENERATED_FRONTEND_FILES = (
    "app.html",
    "assets/app.css",
    "assets/js/app.js",
    "assets/js/data-loader.js",
    "assets/js/generated-data.js",
    "assets/js/router.js",
    "assets/js/tabs/draws.js",
    "assets/js/tabs/roadtogs.js",
    "assets/js/tabs/tstrength.js",
)


def _combined_source(relative_paths):
    return "\n".join(
        (PROJECT_DIR / relative_path).read_text(encoding="utf-8-sig")
        for relative_path in relative_paths
    )


def _authoring_frontend_source():
    return _combined_source(AUTHORING_FRONTEND_FILES)


def _generated_frontend_source():
    return "\n".join(
        (GENERATED_SITE_DIR / relative_path).read_text(encoding="utf-8-sig")
        for relative_path in GENERATED_FRONTEND_FILES
    )


class GeneratedSiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global GENERATED_SITE_DIR
        cls._generated_site_temp = tempfile.TemporaryDirectory(
            prefix=".wtarg-generated-site-",
            dir=PROJECT_DIR,
        )
        GENERATED_SITE_DIR = Path(cls._generated_site_temp.name)
        render_site_from_data(PROJECT_DIR / "data", GENERATED_SITE_DIR)

    @classmethod
    def tearDownClass(cls):
        global GENERATED_SITE_DIR
        GENERATED_SITE_DIR = PROJECT_DIR
        cls._generated_site_temp.cleanup()

    def test_legacy_wta_country_codes_render_flags(self):
        players = json.loads(
            (PROJECT_DIR / "data" / "player_aliases_wta_itf.json").read_text(
                encoding="utf-8-sig"
            )
        )
        players_by_name = {player["display_name"]: player for player in players}
        expected = {
            "Irina Petru": ("CZS", "CZ"),
            "Jones Elizabeth": ("GRB", "GB"),
            "Yvette Flu": ("NET", "NL"),
            "Seddon Kim": ("SAF", "ZA"),
        }

        for player_name, (country_code, iso_code) in expected.items():
            with self.subTest(player=player_name):
                self.assertEqual(players_by_name[player_name]["country"], country_code)
                self.assertIn(
                    f"country-flag-icons/3x2/{iso_code}.svg",
                    country_flag_html(country_code, show_code=False),
                )

    def test_eritrea_country_code_renders_flag(self):
        flag_html = country_flag_html("ERI", show_code=False)

        self.assertIn("country-flag-icons/3x2/ER.svg", flag_html)

    def test_entry_lists_use_public_name_for_id_disambiguated_players(self):
        players = [{"name": "Yue Yuan (1998)", "player_id": "324325"}]

        main_module._canonicalize_player_names(players, source="wta", names_only=True)

        self.assertEqual(players[0]["name"], "Yue Yuan")

        entry_lists = expand_entry_lists_cache(
            json.loads(
                (PROJECT_DIR / "data" / "entry_lists_cache.json").read_text(
                    encoding="utf-8-sig"
                )
            )
        )
        entry_list_names = [
            player.get("name")
            for tournament_players in entry_lists.values()
            for player in tournament_players
        ]
        self.assertNotIn("Yue Yuan (1998)", entry_list_names)
        self.assertNotIn(
            "Yue Yuan (1998)",
            _generated_frontend_source(),
        )

    def test_manual_entry_list_withdrawal_promotes_and_renumbers_alternates(self):
        main_players = [
            {"name": "Player One", "type": "MAIN", "pos": "1", "pos_num": 1},
            {"name": "Emma Raducanu", "type": "MAIN", "pos": "2", "pos_num": 2},
            {"name": "Player Three", "type": "MAIN", "pos": "3", "pos_num": 3},
        ]
        alt_players = [
            {"name": "First Alternate", "type": "ALT", "pos": "1", "pos_num": 1},
            {"name": "Second Alternate", "type": "ALT", "pos": "2", "pos_num": 2},
        ]

        updated_main, updated_alt = main_module._apply_manual_entry_list_withdrawals(
            main_players,
            alt_players,
            ["EMMA RADUCANU"],
        )

        self.assertEqual(
            [player["name"] for player in updated_main],
            ["Player One", "Player Three", "First Alternate"],
        )
        self.assertEqual([player["pos"] for player in updated_main], ["1", "2", "3"])
        self.assertEqual([player["name"] for player in updated_alt], ["Second Alternate"])
        self.assertEqual(updated_alt[0]["pos"], "1")

        qualifying, _ = main_module._apply_manual_entry_list_withdrawals(
            [{"name": "Qualifier", "type": "MAIN", "pos": "4", "pos_num": 4}],
            [],
            None,
            main_type="QUAL",
        )
        self.assertEqual(qualifying[0]["type"], "QUAL")
        self.assertEqual(qualifying[0]["pos"], "1")

    def test_manual_entry_list_additions_are_appended_in_ranking_order(self):
        main_players = [
            {"name": "Direct Acceptance", "type": "MAIN"},
            {"name": "Already Promoted", "type": "MAIN", "rank": "120"},
        ]
        alt_players = [
            {"name": "Lower Ranked Addition", "type": "ALT"},
            {"name": "Remaining Alternate", "type": "ALT"},
        ]
        additions = [
            {"name": "Lower Ranked Addition", "rank": "300", "entry": "WC"},
            {"name": "Already Promoted", "rank": "100"},
            {"name": "Middle Addition", "rank": "200", "entry": "WC"},
        ]

        updated_main, updated_alt = main_module._apply_manual_entry_list_additions(
            main_players,
            alt_players,
            additions,
        )

        self.assertEqual(
            [player["name"] for player in updated_main],
            ["Direct Acceptance", "Already Promoted", "Middle Addition", "Lower Ranked Addition"],
        )
        self.assertEqual([player["pos"] for player in updated_main], ["1", "2", "3", "4"])
        self.assertEqual([player["name"] for player in updated_alt], ["Remaining Alternate"])

    def test_cached_manual_overrides_promote_alternates_and_reseed_qualifying(self):
        tournament_key = "https://example.test/tournament"
        qualifying_key = tournament_key + "#qual"
        entry_cache = {
            qualifying_key: [
                {"name": "Moved Player", "type": "QUAL", "seed_rank": 10},
                {"name": "Remaining Qualifier", "type": "QUAL", "seed_rank": 30},
                {"name": "Promoted Alternate", "type": "ALT", "seed_rank": 20},
                {"name": "Remaining Alternate", "type": "ALT", "seed_rank": 40},
            ]
        }
        config = {tournament_key: {"qual": {"withdrawals": ["Moved Player"]}}}

        main_module._apply_cached_manual_entry_list_overrides(entry_cache, config)

        qualifying = entry_cache[qualifying_key]
        self.assertEqual(
            [(player["name"], player["type"], player.get("seed")) for player in qualifying],
            [
                ("Remaining Qualifier", "QUAL", 2),
                ("Promoted Alternate", "QUAL", 1),
                ("Remaining Alternate", "ALT", None),
            ],
        )

    def test_metadata_only_qualifying_list_is_injected_into_its_configured_week(self):
        tournament_key = "https://www.wtatennis.com/tournaments/905/us-open/2026/player-list"
        qualifying_key = tournament_key + "#qual"
        config = {
            tournament_key: {
                "qual": {
                    "manual": True,
                    "start_date": "2026-08-24",
                    "display_name": "US Open Qualifying",
                    "level": "Grand Slam",
                    "surface": "Hard",
                    "country": "USA",
                }
            }
        }
        tournament_groups = {
            "Week of August 31": {
                tournament_key: {
                    "name": "US Open",
                    "level": "Grand Slam",
                    "surface": "Hard",
                    "country": "USA",
                }
            }
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "gs_pdf_urls.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with patch.object(main_module, "GS_PDF_URLS_FILE", str(config_path)):
                main_module._refresh_entry_lists_from_pdfs(
                    {qualifying_key: [{"name": "Nadia Podoroska", "type": "QUAL"}]},
                    {},
                    tournament_groups,
                    {"2026-08-24": "Week of August 24"},
                )

        self.assertIn(qualifying_key, tournament_groups["Week of August 24"])
        self.assertEqual(
            tournament_groups["Week of August 24"][qualifying_key]["name"],
            "US Open Qualifying",
        )

    def test_separate_qualifying_list_does_not_create_a_nested_qualifying_list(self):
        tournament_key = "https://www.wtatennis.com/tournaments/905/us-open/2026/player-list"
        qualifying_key = tournament_key + "#qual"
        tournament_groups = {
            "Week of August 24": {
                qualifying_key: {
                    "name": "US Open Qualifying",
                    "level": "Grand Slam",
                    "surface": "Hard",
                    "country": "USA",
                    "startDate": "2026-08-24",
                }
            },
            "Week of August 31": {
                tournament_key: {
                    "name": "US Open",
                    "level": "Grand Slam",
                    "surface": "Hard",
                    "country": "USA",
                    "startDate": "2026-08-30",
                }
            },
        }
        entry_cache = {
            tournament_key: [
                {"name": "SOLANA SIERRA", "country": "ARG", "type": "MAIN", "pos": "1", "pos_num": 1}
            ],
            qualifying_key: [
                {"name": "NADIA PODOROSKA", "country": "ARG", "type": "QUAL", "pos": "1", "pos_num": 1},
                {"name": "NAO HIBINO", "country": "JPN", "type": "ALT", "pos": "1", "pos_num": 1},
            ],
        }
        rankings = [
            {"Player": "SOLANA SIERRA", "Country": "ARG", "Rank": 86},
            {"Player": "NADIA PODOROSKA", "Country": "ARG", "Rank": 514},
            {"Player": "NAO HIBINO", "Country": "JPN", "Rank": 224},
        ]

        with (
            patch.object(main_module, "get_wta_rankings_cached", return_value=rankings),
            patch.object(main_module, "_load_acceptance_state", return_value={}),
        ):
            schedule, _, updated_cache, _ = main_module.process_tournaments(
                None,
                tournament_groups,
                {"2026-08-24": "Week of August 24", "2026-08-31": "Week of August 31"},
                {"SOLANA SIERRA", "NADIA PODOROSKA"},
                entry_cache,
            )

        self.assertNotIn(qualifying_key + "#qual", updated_cache)
        self.assertNotIn(qualifying_key + "#qual", tournament_groups["Week of August 24"])
        self.assertEqual(schedule["NADIA PODOROSKA"]["Week of August 24"], "US Open (Q)")

    def test_week_labels_sort_chronologically(self):
        labels = ["Week of August 24", "Week of September 7", "Week of August 17"]
        self.assertEqual(
            sorted(labels, key=_week_label_sort_key),
            ["Week of August 17", "Week of August 24", "Week of September 7"],
        )

    def test_schedule_surface_lookup_strips_numeric_and_mdo_alt_positions(self):
        self.assertEqual(
            _schedule_tournament_base_name("W75 Kursumlijska Banja (ALT 38)"),
            "W75 Kursumlijska Banja",
        )
        self.assertEqual(
            _schedule_tournament_base_name("<b>W75 Kursumlijska Banja (ALT MDO)</b>"),
            "W75 Kursumlijska Banja",
        )

    def test_moved_from_annotation_is_hidden_in_tournament_display_names(self):
        self.assertEqual(
            _display_tournament_name("W15 Pilar (moved from San Salvador de Jujuy)"),
            "W15 Pilar",
        )
        self.assertEqual(
            _display_tournament_name("W25 Ibague (MOVED from 10 Oct)"),
            "W25 Ibague",
        )

    def test_schedule_shows_surface_dot_for_moved_itf_tournament(self):
        app_source = _generated_frontend_source()
        self.assertRegex(
            app_source,
            r'tournament-surface-dot[^>]*></span><b>W15 Pilar</b>',
        )

    def test_entry_menu_uses_gm_as_category_tiebreaker(self):
        for label, source in (
            ("authoring", _authoring_frontend_source()),
            ("generated", _generated_frontend_source()),
        ):
            with self.subTest(path=label):
                self.assertIn("function sortEntryMenuByCategoryThenGm(rows)", source)
                self.assertIn("if (categoryDiff) return categoryDiff", source)
                self.assertIn("if (hasGmA !== hasGmB) return hasGmA ? -1 : 1", source)
                self.assertIn("if (hasGmA && gmA !== gmB) return gmA - gmB", source)
                self.assertIn("sortEntryMenuByCategoryThenGm(rows);", source)

    def test_local_file_url_sync_stays_on_app_html(self):
        for label, source in (
            ("authoring", _authoring_frontend_source()),
            ("generated", _generated_frontend_source()),
        ):
            with self.subTest(path=label):
                self.assertIn("location.protocol === 'file:'", source)
                self.assertIn("location.href.split(/[?#]/)[0]", source)
                self.assertIn("URL state update skipped:", source)

    def test_fed_bjk_series_allows_multiple_open_ties_with_latest_tie_open(self):
        source = _generated_frontend_source()
        tie_tags = re.findall(
            r'<details class="bjkc-series-block"(?: open)?>',
            source,
        )
        self.assertGreater(len(tie_tags), 1)
        self.assertTrue(tie_tags[0].endswith(" open>"))
        self.assertEqual(sum(" open" in tag for tag in tie_tags), 1)
        self.assertNotIn('name="bjkc-series"', source)
        self.assertEqual(
            source.count('<summary class="bjkc-series-header">'),
            len(tie_tags),
        )
        self.assertIn('class="bjkc-header-arrow" aria-hidden="true"', source)
        self.assertIn(
            "visibleBlocks.forEach(function(block, index) { block.open = index === 0; });",
            source,
        )

if __name__ == "__main__":
    unittest.main()
