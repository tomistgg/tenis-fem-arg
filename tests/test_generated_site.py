import json
import re
import tempfile
import unittest
from pathlib import Path

import main as main_module
from html_generator import (
    _display_tournament_name,
    _schedule_tournament_base_name,
    _week_label_sort_key,
    country_flag_html,
)
from site_renderer import render_site_from_data
from utils import compact_tournament_name, expand_entry_lists_cache

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

    def test_us_open_is_excluded_from_draws(self):
        self.assertTrue(
            main_module._is_excluded_draw_tournament(
                "https://www.wtatennis.com/tournaments/905/us-open/2026/player-list"
            )
        )
        self.assertTrue(
            main_module._is_excluded_draw_tournament(
                "905-us-open-2026",
                {"name": "US Open"},
            )
        )

    def test_legacy_wta_country_codes_render_flags(self):
        players = json.loads(
            (PROJECT_DIR / "data" / "player_aliases_wta_itf.json").read_text(
                encoding="utf-8-sig"
            )
        )
        players_by_name = {player["display_name"]: player for player in players}
        expected = {
            "Sofia Alekseeva": ("CHE", "CH"),
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

    def test_points_breakdown_has_live_and_grand_slam_cutoff_views(self):
        app = (GENERATED_SITE_DIR / "app.html").read_text(encoding="utf-8-sig")
        roadtogs_js = (GENERATED_SITE_DIR / "assets/js/tabs/roadtogs.js").read_text(
            encoding="utf-8-sig"
        )
        app_js = (GENERATED_SITE_DIR / "assets/js/app.js").read_text(encoding="utf-8-sig")

        selector = re.search(
            r'<select id="roadtogs-cutoff-select"[^>]*>(.*?)</select>',
            app,
            re.DOTALL,
        )
        self.assertIsNotNone(selector)
        options = re.findall(r'<option value="([^"]+)">([^<]+)</option>', selector.group(1))
        self.assertEqual(options[0], ("live", "Live"))
        self.assertEqual(
            set(options[1:]),
            {
                ("ao-md", "Australian Open MD"),
                ("ao-q", "Australian Open Q"),
                ("rg-md", "Roland Garros MD"),
                ("rg-q", "Roland Garros Q"),
                ("wim-md", "Wimbledon MD"),
                ("wim-q", "Wimbledon Q"),
                ("uso-md", "US Open MD"),
                ("uso-q", "US Open Q"),
            },
        )
        self.assertIn("_rtgsComputeBreakdown(selectedPlayer, selectedCutoff.cutoff)", roadtogs_js)
        self.assertIn("_rtgsRenderBreakdown(tbody, breakdown, true)", roadtogs_js)
        self.assertIn("minimumResultsForSearch: Infinity", roadtogs_js)
        self.assertIn("state.cutoff = cutoffSelect.value", app_js)

    def test_information_page_is_reworked_as_milestones(self):
        app = (GENERATED_SITE_DIR / "app.html").read_text(encoding="utf-8-sig")
        index = (GENERATED_SITE_DIR / "index.html").read_text(encoding="utf-8-sig")
        app_js = (GENERATED_SITE_DIR / "assets/js/app.js").read_text(encoding="utf-8-sig")
        router_js = (GENERATED_SITE_DIR / "assets/js/router.js").read_text(encoding="utf-8-sig")
        app_shell_js = (PROJECT_DIR / "assets/app-shell.js").read_text(encoding="utf-8-sig")

        self.assertIn('<h1>Milestones</h1>', app)
        self.assertIn('id="milestones-btn-historical"', app)
        self.assertIn('id="milestones-btn-active"', app)
        self.assertIn('id="milestones-historical-body"', app)
        self.assertIn('id="milestones-historical-metric"', app)
        self.assertIn('<option value="proWin">First pro win</option>', app)
        self.assertIn('id="milestones-player-select"', app)
        self.assertIn('id="milestones-live-body"', app)
        self.assertIn('id="milestones-expired-body"', app)
        self.assertIn('>Expired WTA points</h2>', app)
        expired_table = re.search(
            r'<table class="milestones-points-table milestones-expired-table">(.*?)</table>',
            app,
            re.DOTALL,
        )
        self.assertIsNotNone(expired_table)
        self.assertNotIn("Drop Date", expired_table.group(1))
        self.assertNotIn('<h1>Information</h1>', app)
        self.assertIn('href="app.html#information"', index)
        self.assertIn('>Milestones</span>', index)
        information_route = (GENERATED_SITE_DIR / "information" / "index.html").read_text(encoding="utf-8-sig")
        self.assertIn("#information", information_route)
        self.assertIn("getElementById('view-information')", app_js)
        self.assertIn("function switchMilestonesTab(tabName)", app_js)
        self.assertIn("Total ever WTA points earned:", app_js)
        self.assertIn("Last week with WTA ranking:", app_js)
        self.assertIn("expiredRows, '', false", app_js)
        self.assertIn("'information'", router_js)
        self.assertIn("information: 'Milestones'", app_shell_js)

        generated_data = (GENERATED_SITE_DIR / "assets/js/generated-data.js").read_text(encoding="utf-8-sig")
        payload = json.loads(generated_data.split("=", 1)[1].strip().rstrip(";"))
        marchesini = next(
            player for player in payload["milestones"]["active"] if player["name"] == "Victoria Marchesini"
        )
        self.assertEqual(marchesini["lastRankedWeek"], "2025-11-24")

    def test_history_qualifying_rounds_use_qr_prefix(self):
        cases = {
            "Q1": "QR1",
            "Q2": "QR2",
            "Q3": "QR3",
            "QR1": "QR1",
            "1st Round": "QR1",
            "2nd Round": "QR2",
            "3rd Round": "QR3",
        }

        for source_round, expected in cases.items():
            with self.subTest(source_round=source_round):
                self.assertEqual(
                    main_module.normalize_history_round(
                        source_round,
                        "" if source_round.startswith("Q") else "Q",
                    ),
                    expected,
                )

        self.assertEqual(
            main_module.normalize_history_round("1st Round", "M"),
            "1st Round",
        )

    def test_history_category_uses_canonical_label(self):
        self.assertEqual(main_module.normalize_history_category("WT"), "World Tour")
        self.assertEqual(main_module.normalize_history_category(" wt "), "World Tour")
        self.assertEqual(main_module.normalize_history_category("Tier 2"), "Tier II")
        self.assertEqual(main_module.normalize_history_category(" tier 2 "), "Tier II")
        self.assertEqual(main_module.normalize_history_category("Tier II"), "Tier II")
        self.assertEqual(main_module.normalize_history_category("WTA 500"), "WTA 500")

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

    def test_requested_long_tournament_names_use_compact_labels(self):
        cases = {
            "W15 Viserba di Rimini": "W15 Viserba",
            "W35 Santa Margherita di Pula 7": "W35 Santa Marg.",
            "W35 Villeneuve d'Ascq": "W35 Villeneuve",
            "W100 New Braunfels, TX": "W100 N. Braunfels, TX",
            "W15 Hilton Head Island, SC": "W15 Hilton H.I., SC",
            "W15 Feira de Santana": "W15 F. de Santana",
        }
        for source_name, expected_name in cases.items():
            with self.subTest(source_name=source_name):
                self.assertEqual(compact_tournament_name(source_name), expected_name)

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
