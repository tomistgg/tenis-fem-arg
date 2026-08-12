import json
from pathlib import Path

from html_generator import _script_safe_json

PROJECT_DIR = Path(__file__).resolve().parents[1]


def test_script_safe_json_neutralizes_html_and_script_boundaries():
    payload = {
        "player": "</script><img src=x onerror=alert(1)>",
        "separator": "line\u2028paragraph\u2029",
        "ampersand": "A&B",
    }

    serialized = _script_safe_json(payload, ensure_ascii=False)

    assert "</script" not in serialized.lower()
    assert "<" not in serialized
    assert ">" not in serialized
    assert "&" not in serialized
    assert "\u2028" not in serialized
    assert "\u2029" not in serialized
    assert json.loads(serialized) == payload


def test_browser_analytics_sends_country_and_allowlisted_aggregate_dimensions():
    source = (PROJECT_DIR / "assets" / "anonymous-analytics.js").read_text(encoding="utf-8")

    for required in (
        "page_type",
        "content_id",
        "country: country",
        "https://api.country.is/",
        "credentials: 'omit'",
        "referrerPolicy: 'no-referrer'",
        "navigator.globalPrivacyControl",
        "ANALYTICS_FILTER_PARAMS",
        "canonicalFilterContentId",
        "entrylists: ['t', 'prio']",
        "rankings: ['q', 'scope', 'date']",
    ):
        assert required in source

    for forbidden in (
        "previous_page_type",
        "previous_content_id",
        "previousAggregatePage",
        "sessionStorage",
        "localStorage",
        "document.referrer",
        "landing_page",
        "utm_",
        "ipwho.is",
        "ipapi.co",
        "data.ip",
    ):
        assert forbidden not in source


def test_generated_pages_do_not_load_the_removed_attribution_tracker(offline_generated_site):
    html_paths = [
        offline_generated_site / "app.html",
        offline_generated_site / "index.html",
        offline_generated_site / "404.html",
    ]
    html_paths.extend(offline_generated_site.glob("*/index.html"))

    for path in (offline_generated_site / "app.html", offline_generated_site / "index.html"):
        assert "assets/anonymous-analytics.js" in path.read_text(encoding="utf-8-sig")
    for path in html_paths:
        assert "visit-attribution.js" not in path.read_text(encoding="utf-8-sig"), path


def test_apps_script_aggregates_country_without_raw_ip_or_precise_location():
    source = (PROJECT_DIR / "google_apps_script_visit_tracker.gs").read_text(encoding="utf-8")

    assert "recordAggregateEvent_" in source
    assert "ANALYTICS_SCHEMA_VERSION_ = 2" in source
    assert "function doGet()" in source
    assert "accepts_filter_content_ids: true" in source
    assert "function doPost(e)" in source
    assert "ANALYTICS_EVENT_FIELDS_" in source
    assert "LockService.getScriptLock()" in source
    assert "ALLOWED_ANALYTICS_PAGES_" in source
    assert "ANALYTICS_FILTER_PARAMS_" in source
    assert "SAFE_CONTENT_PART_RE_" in source
    assert "SAFE_LEGACY_PLAYER_SLUG_RE_" in source
    assert "entrylists: ['t', 'prio']" in source
    assert "rankings: ['q', 'scope', 'date']" in source
    assert "migrateLegacyPageViews_" in source
    assert "'Anonymous Page Views'" in source
    assert "'Country'" in source
    assert "'Views'" in source
    current_schema = source.split("const PAGE_VIEW_HEADERS_", 1)[1].split("];", 1)[0]
    assert "Updated At" not in current_schema

    for forbidden in (
        "Anonymous Transitions",
        "TRANSITION_HEADERS_",
        "migrateLegacyTransitions_",
        "previousPageType",
        "previousContentId",
        "'IP'",
        "'Region'",
        "'City'",
        "'Landing Page'",
        "'Referrer'",
        "'UTM Source'",
        "appendRow",
    ):
        assert forbidden not in source
