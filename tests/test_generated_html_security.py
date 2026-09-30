import json
import re
from html import unescape

from html_generator import (
    _apply_content_security_policy,
    _csp_hash,
    _read_frontend_source,
    _schedule_tournament_base_name,
    _script_hash_sources,
    country_flag_html,
)
from site_renderer import _entry_inputs


def test_schedule_escapes_source_labels_but_keeps_its_own_separator(tmp_path):
    week = "Week of September 21"
    tournaments = {
        week: {
            "w-itf-arg-2026-001": {"name": "W50 <script>alert(1)</script> & Open"},
            "w-itf-arg-2026-002": {"name": "W50 Normal"},
            "w-itf-arg-2026-003": {"name": "W50 <script>alert(1)</script> & Open"},
        }
    }
    entries = {
        key: [{"name": "Julia Riera", "country": "ARG", "type": "MAIN"}]
        for key in tournaments[week]
    }
    (tmp_path / "entry_lists_cache.json").write_text(json.dumps(entries), encoding="utf-8")

    _, schedule, _ = _entry_inputs(tmp_path, tournaments)
    cell = schedule["JULIA RIERA"][week]

    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; Open" in cell
    assert "<script>" not in cell
    assert "<br>" in cell
    assert cell.count("&lt;script&gt;") == 1
    assert _schedule_tournament_base_name("W50 A &amp; B (Q)") == "W50 A & B"


def test_csp_hashes_trusted_template_only():
    template = _read_frontend_source("templates/app.html")
    injected_script = "window.injected = true"
    rendered = template.replace(
        "@@WTARG_TABLE_ROWS@@",
        f'<script>{injected_script}</script><img src=x onerror="{injected_script}">',
    )

    protected = _apply_content_security_policy(
        rendered,
        trusted_html=template,
    )
    policy = unescape(
        re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]+)">', protected).group(1)
    )

    assert _csp_hash(injected_script) not in policy
    assert all(source in policy for source in _script_hash_sources(template))


def test_generated_page_csp_covers_only_template_inline_code(offline_generated_site):
    template = _read_frontend_source("templates/app.html")
    rendered = (offline_generated_site / "app.html").read_text(encoding="utf-8-sig")
    policy = unescape(
        re.search(r'<meta http-equiv="Content-Security-Policy" content="([^"]+)">', rendered).group(1)
    )
    allowed_hashes = set(re.findall(r"'sha256-[^']+'", policy))

    assert allowed_hashes == set(_script_hash_sources(template))
    assert set(_script_hash_sources(rendered)) == allowed_hashes


def test_unknown_country_code_is_rendered_as_text():
    assert country_flag_html('<img src=x onerror="alert(1)">') == (
        "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;"
    )
