import re
import json
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
)
from calendar_builder import format_week_label, get_monday_from_date
from utils import expand_entry_lists_cache


PROJECT_DIR = Path(__file__).resolve().parents[1]


class GeneratedSiteTests(unittest.TestCase):
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
        cincinnati = entry_lists[
            "https://www.wtatennis.com/tournaments/1017/cincinnati/2026/player-list"
        ]
        yue_yuan = [
            player for player in cincinnati if player.get("player_id") == "324325"
        ]
        self.assertEqual([player["name"] for player in yue_yuan], ["Yue Yuan"])
        self.assertNotIn(
            "Yue Yuan (1998)",
            (PROJECT_DIR / "app.html").read_text(encoding="utf-8-sig"),
        )

    def test_us_open_pdf_entry_list_is_assigned_to_august_31(self):
        config = json.loads(
            (PROJECT_DIR / "data" / "gs_pdf_urls.json").read_text(encoding="utf-8")
        )
        us_open = config[
            "https://www.wtatennis.com/tournaments/905/us-open/2026/player-list"
        ]["main"]
        self.assertEqual(
            format_week_label(get_monday_from_date(us_open["start_date"])),
            "Week of August 31",
        )

    def test_us_open_qualifying_entry_list_is_assigned_to_august_24(self):
        tournament_key = "https://www.wtatennis.com/tournaments/905/us-open/2026/player-list"
        config = json.loads(
            (PROJECT_DIR / "data" / "gs_pdf_urls.json").read_text(encoding="utf-8")
        )
        qualifying = config[tournament_key]["qual"]
        self.assertTrue(qualifying["manual"])
        self.assertEqual(
            format_week_label(get_monday_from_date(qualifying["start_date"])),
            "Week of August 24",
        )
        self.assertEqual(qualifying["alt_limit"], 20)

        compact_cache = json.loads(
            (PROJECT_DIR / "data" / "entry_lists_cache.json").read_text(encoding="utf-8")
        )
        players = expand_entry_lists_cache(compact_cache)[tournament_key + "#qual"]
        accepted = [player for player in players if player["type"] == "QUAL"]
        alternates = [player for player in players if player["type"] == "ALT"]
        self.assertEqual(len(accepted), 119)
        self.assertEqual(len(alternates), 20)
        self.assertEqual(
            [player["pos"] for player in accepted if player["country"] == "ARG"],
            ["4", "46", "55", "60", "112"],
        )
        self.assertEqual(
            [player["pos"] for player in alternates if player["country"] == "ARG"],
            ["17"],
        )

        app_source = (PROJECT_DIR / "app.html").read_text(encoding="utf-8-sig")
        self.assertNotIn(tournament_key + "#qual#qual", app_source)
        self.assertNotIn("US Open Qualifying Qualifying", app_source)

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
        source = (PROJECT_DIR / "html_generator.py").read_text(encoding="utf-8")
        self.assertIn(
            "#view-upcoming .tournament-surface-dot {{ width: 5px; height: 5px;",
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
        generator_source = (PROJECT_DIR / "html_generator.py").read_text(encoding="utf-8")
        app_source = (PROJECT_DIR / "app.html").read_text(encoding="utf-8-sig")

        for key, label in expected_labels.items():
            self.assertIn(f'"{key}": "{label}"', config_source)
            self.assertIn(f'class="cal-cont-label-mobile">{label}</span>', app_source)
        self.assertIn(".cal-cont-label-mobile {{ display: none; }}", generator_source)
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

        app_source = (PROJECT_DIR / "app.html").read_text(encoding="utf-8-sig")
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
        for relative_path in ("html_generator.py", "app.html"):
            source = (PROJECT_DIR / relative_path).read_text(encoding="utf-8-sig")
            with self.subTest(path=relative_path):
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

    def test_local_file_url_sync_stays_on_app_html(self):
        for relative_path in ("html_generator.py", "app.html"):
            source = (PROJECT_DIR / relative_path).read_text(encoding="utf-8")
            with self.subTest(path=relative_path):
                self.assertIn("location.protocol === 'file:'", source)
                self.assertIn("location.href.split(/[?#]/)[0]", source)
                self.assertIn("URL state update skipped:", source)

    def test_generated_app_csp_allows_every_inline_script(self):
        source = (PROJECT_DIR / "app.html").read_text(encoding="utf-8-sig")
        match = _CSP_META_RE.search(source)
        self.assertIsNotNone(match)
        allowed = set(re.findall(r"'sha256-[^']+'", unescape(match.group(0))))
        self.assertEqual(allowed, set(_script_hash_sources(source)))

    def test_grand_slam_information_is_collapsed_by_default(self):
        source = (PROJECT_DIR / "app.html").read_text(encoding="utf-8-sig")
        self.assertIn('<details class="roadtogs-info">', source)
        self.assertIn("Grand Slams information", source)
        self.assertNotIn('<details class="roadtogs-info" open', source)

    def test_fed_bjk_series_allows_multiple_open_ties_with_latest_tie_open(self):
        source = (PROJECT_DIR / "app.html").read_text(encoding="utf-8-sig")
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
        source = (PROJECT_DIR / "app.html").read_text(encoding="utf-8-sig")
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
        source = (PROJECT_DIR / "app.html").read_text(encoding="utf-8-sig")
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
        source = (PROJECT_DIR / "app.html").read_text(encoding="utf-8-sig")
        self.assertIn('id="history-mobile-filter-btn"', source)
        self.assertIn('aria-controls="history-filter-panel"', source)
        self.assertIn('id="history-filter-panel"', source)
        self.assertIn("body.history-filters-open #history-filter-panel", source)
        self.assertIn("label.textContent = count ? `Filters · ${count}` : 'Filters';", source)
        self.assertIn("if (state.asRankVal !== null) count += 1;", source)
        self.assertIn("if (state.vsRankVal !== null) count += 1;", source)
        self.assertIn("if (window.innerWidth > 768", source)
        self.assertIn(
            ".filter-panel { width: 250px; padding: 15px;",
            source,
        )

    def test_photos_view_shows_retirement_notice_without_gallery(self):
        source = (PROJECT_DIR / "app.html").read_text(encoding="utf-8-sig")
        self.assertIn(
            "Saqué las fotos porque era un quilombo mantenerla, cuando necesiten alguna foto en particular "
            "pueden sacarla de mi instagram @tomistx o pedirme por mensaje privado ahí mismo.",
            source,
        )
        self.assertIn("Voy a sacar esta parte del sitio en una semana.", source)
        self.assertNotIn('id="gallery-player-filter"', source)
        self.assertNotIn('id="gallery-grid"', source)
        self.assertNotIn('id="gallery-lightbox"', source)


if __name__ == "__main__":
    unittest.main()
