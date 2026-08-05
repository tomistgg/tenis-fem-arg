import re
import json
import unittest
from html import unescape
from pathlib import Path

from html_generator import (
    _CSP_META_RE,
    _display_tournament_name,
    _schedule_tournament_base_name,
    _script_hash_sources,
    _week_label_sort_key,
)
from calendar_builder import format_week_label, get_monday_from_date


PROJECT_DIR = Path(__file__).resolve().parents[1]


class GeneratedSiteTests(unittest.TestCase):
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

    def test_moved_from_annotation_is_hidden_in_tournament_display_names(self):
        self.assertEqual(
            _display_tournament_name("W15 Pilar (moved from San Salvador de Jujuy)"),
            "W15 Pilar",
        )
        self.assertEqual(
            _display_tournament_name("W25 Ibague (MOVED from 10 Oct)"),
            "W25 Ibague",
        )

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
