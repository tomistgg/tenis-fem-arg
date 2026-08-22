import ast
import json
import re
from pathlib import Path

import pytest

import build_deploy_site

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_ASSET_PAIRS = (
    ("web/css/app.css", "assets/app.css"),
    ("web/js/app.js", "assets/js/app.js"),
    ("web/js/data-loader.js", "assets/js/data-loader.js"),
    ("web/js/router.js", "assets/js/router.js"),
    ("web/js/tabs/draws.js", "assets/js/tabs/draws.js"),
    ("web/js/tabs/roadtogs.js", "assets/js/tabs/roadtogs.js"),
    ("web/js/tabs/tstrength.js", "assets/js/tabs/tstrength.js"),
)


def test_frontend_sources_are_separate_from_python_generator():
    generator_path = PROJECT_ROOT / "html_generator.py"
    source = generator_path.read_text(encoding="utf-8-sig")
    module = ast.parse(source)
    generate_html = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "generate_html"
    )
    joined_string_sizes = [
        sum(
            len(value.value)
            for value in node.values
            if isinstance(value, ast.Constant) and isinstance(value.value, str)
        )
        for node in ast.walk(generate_html)
        if isinstance(node, ast.JoinedStr)
    ]

    assert (PROJECT_ROOT / "web/templates/app.html").is_file()
    assert (PROJECT_ROOT / "web/templates/index.html").is_file()
    assert max(joined_string_sizes, default=0) < 10_000
    assert "html_template = f" not in source


@pytest.mark.parametrize(("source_name", "generated_name"), STATIC_ASSET_PAIRS)
def test_generated_static_frontend_assets_match_authoring_sources(
    source_name,
    generated_name,
    offline_generated_site,
):
    source = (PROJECT_ROOT / source_name).read_text(encoding="utf-8")
    generated = (offline_generated_site / generated_name).read_text(encoding="utf-8")
    assert generated == source
    assert "@@WTARG_" not in generated


def test_generated_app_loads_data_before_static_application_scripts(offline_generated_site):
    app = (offline_generated_site / "app.html").read_text(encoding="utf-8-sig")
    expected_scripts = (
        "assets/js/generated-data.js",
        "assets/js/data-loader.js",
        "assets/js/tabs/tstrength.js",
        "assets/js/app.js",
        "assets/js/tabs/roadtogs.js",
        "assets/js/tabs/draws.js",
        "assets/js/router.js",
    )
    positions = [app.index(f'src="{script}"') for script in expected_scripts]

    assert positions == sorted(positions)
    assert re.search(r'href="assets/app\.css(?:\?[^"]*)?"', app)
    assert "<style>" not in app
    inline_scripts = re.findall(
        r"<script\b(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script>",
        app,
        re.IGNORECASE | re.DOTALL,
    )
    assert len([script for script in inline_scripts if script.strip()]) == 1


def test_generated_frontend_data_is_a_versioned_json_payload(offline_generated_site):
    source = (offline_generated_site / "assets/js/generated-data.js").read_text(encoding="utf-8")
    prefix = "window.__WTARG_GENERATED_DATA__ = "
    assert source.startswith(prefix)
    assert source.endswith(";\n")
    payload = json.loads(source[len(prefix) : -2])

    assert payload["schemaVersion"] == 1
    assert payload["gsThresholdQ"] < payload["gsThresholdMd"]
    assert isinstance(payload["tournaments"], dict)
    assert isinstance(payload["rankingsDatesIndex"], dict)


def test_history_filter_options_use_csp_safe_delegated_clicks(offline_generated_site):
    app_js = (offline_generated_site / "assets/js/app.js").read_text(encoding="utf-8")

    assert 'onclick="toggleFilterOption(event, this)"' not in app_js
    assert "historyFilterPanel.addEventListener('click'" in app_js
    assert "toggleFilterOption(event, element);" in app_js


def test_deploy_builder_copies_static_assets_then_renders_site(tmp_path, monkeypatch):
    base_dir = tmp_path / "project"
    output_dir = base_dir / ".site"
    data_dir = base_dir / "data"
    (base_dir / "assets/vendor").mkdir(parents=True)
    data_dir.mkdir()
    (base_dir / "assets/vendor/library.js").write_text("vendor", encoding="utf-8")

    def fake_render(source_data_dir, site_root):
        assert source_data_dir == data_dir
        generated = site_root / "assets/js/app.js"
        generated.parent.mkdir(parents=True)
        generated.write_text("generated", encoding="utf-8")

    monkeypatch.setattr(build_deploy_site, "BASE_DIR", base_dir)
    monkeypatch.setattr(build_deploy_site, "DATA_DIR", data_dir)
    monkeypatch.setattr(build_deploy_site, "render_site_from_data", fake_render)
    monkeypatch.setattr(build_deploy_site, "COPY_ROOT_FILES", ())
    monkeypatch.setattr(build_deploy_site, "COPY_ROOT_DIRS", ("assets",))
    monkeypatch.setattr(build_deploy_site, "COPY_DATA_DIRS", ())

    build_deploy_site._build_site_contents(output_dir)

    assert (output_dir / "assets/vendor/library.js").read_text(encoding="utf-8") == "vendor"
    assert (output_dir / "assets/js/app.js").read_text(encoding="utf-8") == "generated"
