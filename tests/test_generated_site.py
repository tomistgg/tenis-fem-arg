import json
import re
import tempfile
import unittest
from html import unescape
from pathlib import Path
from unittest.mock import patch

import main as main_module
from html_generator import (
    _CSP_META_RE,
    _display_calendar_tournament_name,
    _display_tournament_name,
    _schedule_tournament_base_name,
    _script_hash_sources,
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

    def test_lorna_simmons_aho_country_uses_local_netherlands_antilles_flag(self):
        players = json.loads(
            (PROJECT_DIR / "data" / "player_aliases_wta_itf.json").read_text(
                encoding="utf-8-sig"
            )
        )
        lorna_simmons = next(
            player for player in players if player["display_name"] == "Lorna Simmons"
        )
        flag_html = country_flag_html(lorna_simmons["country"], show_code=False)

        self.assertEqual(lorna_simmons["country"], "AHO")
        self.assertIn('src="data/flags/aho.svg"', flag_html)
        self.assertTrue((PROJECT_DIR / "data" / "flags" / "aho.svg").is_file())

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

    def test_mobile_schedule_surface_dot_matches_tournament_font_size(self):
        source = _authoring_frontend_source()
        self.assertIn(
            "#view-upcoming .tournament-surface-dot { width: 5px; height: 5px;",
            source,
        )

    def test_mobile_calendar_uses_compact_region_labels_and_column(self):
        expected_labels = {
            "south_america": "SA",
            "north_central_america": "NA",
            "europe": "EUR",
            "africa": "AFR",
            "asia": "ASIA",
            "oceania": "OCE",
        }
        config_source = (PROJECT_DIR / "config.py").read_text(encoding="utf-8")
        generator_source = _authoring_frontend_source()
        app_source = _generated_frontend_source()

        for key, label in expected_labels.items():
            self.assertIn(f'"{key}": "{label}"', config_source)
            self.assertIn(f'class="cal-cont-label-mobile">{label}</span>', app_source)
        self.assertIn(".cal-cont-label-mobile { display: none; }", generator_source)
        self.assertIn("min-width: 36px;", generator_source)
        self.assertIn(".cal-cont-label-mobile { display: none; }", app_source)
        self.assertIn("min-width: 36px;", app_source)

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

    def test_calendar_uses_compact_names_without_edition_numbers(self):
        expected_names = {
            "W15 Alcala de Henares 2": "W15 Alcala de H.",
            "W15 Campos do Jordao": "W15 Campos do J.",
            "W15 Campos do Jordão": "W15 Campos do J.",
            "W50 Cherbourg-en-Cotentin": "W50 Cherbourg",
            "W15 Grodzisk Mazowiecki": "W15 Grodzisk M.",
            "W75 Kursumlijska Banja  2": "W75 K. Banja",
            "W50 Saint-Palais-sur-Mer": "W50 Saint-Palais",
            "W35 Santa Margherita di Pula 12": "W35 St. Marg. di Pula",
            "W15 Sharm ElSheikh 22": "W15 Sharm ES.",
            "WTA 125 Caldas Da Rainha": "WTA 125 Caldas Da R.",
            "W35 Verbier 1": "W35 Verbier",
            "WTA 1000 Cincinnati": "WTA 1000 Cincinnati",
        }
        for source_name, display_name in expected_names.items():
            with self.subTest(source_name=source_name):
                self.assertEqual(_display_calendar_tournament_name(source_name), display_name)

        app_source = _generated_frontend_source()
        self.assertIn(
            'data-cal-filter="wta_tour" data-cal-continent="north_central_america" '
            'data-cal-surface="hard"',
            app_source,
        )
        self.assertIn("WTA Finals", app_source)

        app_source = _generated_frontend_source()
        tournament_names = re.findall(
            r'<span class="calendar-tournament[^>]*>(?:<img[^>]*>\s*)?([^<]+)',
            app_source,
        )
        self.assertTrue(tournament_names)
        self.assertFalse(any(re.search(r"\s\d+\s*$", name) for name in tournament_names))
        self.assertTrue(any("K. Banja" in name for name in tournament_names))
        self.assertTrue(any("St. Marg. di Pula" in name for name in tournament_names))
        self.assertTrue(any("Sharm ES." in name for name in tournament_names))

    def test_calendar_gm_toggle_is_visible_by_default_and_persists_state(self):
        for label, source in (
            ("authoring", _authoring_frontend_source()),
            ("generated", _generated_frontend_source()),
        ):
            with self.subTest(path=label):
                self.assertIn(
                    'class="calendar-gm-toggle active" id="calendar-gm-toggle"',
                    source,
                )
                self.assertIn("Hide Quality", source)
                self.assertIn("Show Quality", source)
                self.assertIn("gmToggle.textContent = gmAction", source)
                self.assertIn("state.gm = '0'", source)
                self.assertIn("params.has('gm')", source)
                self.assertIn("badge.style.display = showGm ? '' : 'none'", source)
                self.assertIn("gmLegend.style.display = showGm ? '' : 'none'", source)

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

    def test_generated_app_csp_allows_every_inline_script(self):
        source = (GENERATED_SITE_DIR / "app.html").read_text(encoding="utf-8-sig")
        match = _CSP_META_RE.search(source)
        self.assertIsNotNone(match)
        allowed = set(re.findall(r"'sha256-[^']+'", unescape(match.group(0))))
        self.assertEqual(allowed, set(_script_hash_sources(source)))

    def test_grand_slam_information_is_collapsed_by_default(self):
        source = (GENERATED_SITE_DIR / "app.html").read_text(encoding="utf-8-sig")
        self.assertIn('<details class="roadtogs-info">', source)
        self.assertIn("Grand Slams information", source)
        self.assertNotIn('<details class="roadtogs-info" open', source)

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

    def test_fed_bjk_series_only_controls_are_hidden_on_other_mobile_views(self):
        source = _generated_frontend_source()
        self.assertIn(
            'id="view-fedbcup" class="single-layout fedbcup-series-active"',
            source,
        )
        self.assertIn(
            "#view-fedbcup:not(.fedbcup-series-active) .fedbcup-filter-left,",
            source,
        )
        self.assertIn(
            "#view-fedbcup:not(.fedbcup-series-active) .fedbcup-record-right { display: none; }",
            source,
        )
        self.assertIn(
            "classList.toggle('fedbcup-series-active', subTab === 'series')",
            source,
        )
        self.assertNotIn(
            ".fedbcup-filter-left { flex: 1; min-width: 0; order: 1; visibility:",
            source,
        )
        self.assertNotIn(
            ".fedbcup-record-right { flex: 1; min-width: 0; order: 1; justify-content: flex-end; visibility:",
            source,
        )

    def test_fed_bjk_player_debuts_moves_tie_flag_into_opponent_column(self):
        source = _generated_frontend_source()
        table_match = re.search(
            r'<table id="national-table">(.*?)</table>',
            source,
            re.DOTALL,
        )
        self.assertIsNotNone(table_match)
        table = table_match.group(1)
        headers = [
            unescape(re.sub(r"<[^>]+>", "", header)).strip()
            for header in re.findall(r"<th\b.*?</th>", table, re.DOTALL)
        ]
        self.assertEqual(
            headers,
            ["#", "PLAYER", "DATE", "EVENT", "PARTNER", "OPPONENT", "SCORE"],
        )
        self.assertTrue(
            all("width:" not in header for header in re.findall(r"<th\b.*?</th>", table, re.DOTALL))
        )

        body_match = re.search(
            r'<tbody id="national-body">(.*?)</tbody>',
            table,
            re.DOTALL,
        )
        self.assertIsNotNone(body_match)
        rows = re.findall(r"<tr>(.*?)</tr>", body_match.group(1), re.DOTALL)
        self.assertGreater(len(rows), 1)
        for row in rows:
            cells = re.findall(r"<td\b.*?</td>", row, re.DOTALL)
            self.assertEqual(len(cells), 7)
            self.assertNotIn("<br>", cells[1])
            self.assertIn('class="national-opponent-cell"', cells[5])
            self.assertIn("country-flag-icons/3x2/", cells[5])
            self.assertEqual(cells[5].count('class="national-opponent-content"'), 2)
            self.assertEqual(cells[5].count('class="national-opponent-flag"'), 2)
            self.assertRegex(cells[6], r'class="score-(?:win|loss)"')
            self.assertIn('class="score-badge"', cells[6])
            self.assertNotIn("<br>", cells[6])

        first_cells = re.findall(r"<td\b.*?</td>", rows[0], re.DOTALL)
        self.assertEqual(unescape(re.sub(r"<[^>]+>", "", first_cells[3])).strip(), "WG")
        event_values = [
            unescape(re.sub(r"<[^>]+>", "", re.findall(r"<td\b.*?</td>", row, re.DOTALL)[3])).strip()
            for row in rows
        ]
        self.assertIn("WG II", event_values)
        self.assertIn("G1 Am", event_values)
        self.assertIn("Qualifiers", event_values)
        self.assertNotIn("G1 Americas", event_values)
        self.assertFalse(any(re.search(r"\s(?:R16|R32|QF|RR)$", event) for event in event_values))
        longest_player_row = next(row for row in rows if "Viviana González Locicero" in row)
        self.assertIn(
            '<span class="mobile-only">Viviana González Locicero</span>',
            longest_player_row,
        )
        first_opponent = first_cells[5]
        self.assertLess(first_opponent.index('alt="BEL"'), first_opponent.index("Christiane Mercelis"))
        self.assertIn(
            '<span class="national-opponent-player">Christiane Mercelis</span>',
            first_opponent,
        )
        doubles_opponent = next(row for row in rows if "Edda Buding / Helga Hosl" in row)
        self.assertIn(
            '<span class="national-opponent-player">Edda Buding</span>'
            '<span class="national-opponent-player">Helga Hosl</span>',
            doubles_opponent,
        )
        self.assertNotIn("national-opponent-divider", doubles_opponent)
        self.assertNotIn("Edda<br>Buding", doubles_opponent)
        self.assertIn(
            "#national-table .national-opponent-player + .national-opponent-player { padding-top: 2px; }",
            source,
        )
        self.assertIn("#national-table .national-opponent-content {", source)
        self.assertIn("padding-left: 1px;", source)
        self.assertIn(
            "#national-table .national-opponent-flag img { margin-right: 0 !important; }",
            source,
        )
        self.assertIn("#national-table td.score-win { background: #166534; }", source)
        self.assertIn("#national-table td.score-loss { background: #b91c1c; }", source)
        self.assertIn(
            "#fedbcup-view-players { width: fit-content; max-width: 100%; margin: 0 auto; }",
            source,
        )
        self.assertIn(
            "#national-table { table-layout: auto; width: max-content; min-width: 0; margin: 0; }",
            source,
        )
        self.assertIn("min-width: max-content;", source)
        self.assertIn("#national-table td:not(:nth-child(6)) { width: 1%; }", source)
        self.assertIn("#national-table td:nth-child(6) { width: 100%; }", source)
        self.assertIn("font-size: 6px;\n                    padding: 1px 0;", source)
        self.assertIn("#view-fedbcup #national-table th,", source)
        self.assertIn("padding-left: 0 !important;\n                    padding-right: 0 !important;", source)
        self.assertNotIn(
            "#national-table th:nth-child(7), #national-table td:nth-child(7) { width: 42px !important;",
            source,
        )
        self.assertNotIn("#national-table th:nth-child(8)", source)
        self.assertNotIn("#national-table th:nth-child(9)", source)
        self.assertNotIn("#national-table th:nth-child(10)", source)

    def test_match_history_filters_use_a_mobile_bottom_sheet_with_active_count(self):
        source = _generated_frontend_source()
        self.assertIn('id="history-mobile-filter-btn"', source)
        self.assertIn('aria-controls="history-filter-panel"', source)
        self.assertIn('id="history-filter-panel"', source)
        self.assertIn("body.history-filters-open #history-filter-panel", source)
        self.assertIn("label.textContent = count ? `Filters · ${count}` : 'Filters';", source)
        self.assertIn("if (state.asRankVal !== null) count += 1;", source)
        self.assertIn("if (state.vsRankVal !== null) count += 1;", source)
        self.assertIn("if (window.innerWidth > 768", source)
        self.assertIn(
            "const additiveSelection = event.ctrlKey || event.metaKey || window.innerWidth <= 768;",
            source,
        )
        self.assertIn("Tap to add or remove filter options.", source)
        self.assertIn(
            ".filter-panel { width: 250px; padding: 15px;",
            source,
        )

    def test_home_button_rows_are_centered(self):
        for label, source in (
            ("index.html", (GENERATED_SITE_DIR / "index.html").read_text(encoding="utf-8-sig")),
            ("generated app", _generated_frontend_source()),
        ):
            with self.subTest(path=label):
                self.assertIn("flex-wrap: wrap;", source)
                self.assertIn("justify-content: center;", source)
                self.assertIn("flex: 0 1 calc((100% - 48px) / 5);", source)
                self.assertIn("flex-basis: calc((100% - 8px) / 2);", source)

if __name__ == "__main__":
    unittest.main()
