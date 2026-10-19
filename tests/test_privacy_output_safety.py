import json
import shutil
import subprocess
from pathlib import Path

import pytest

from html_generator import _script_safe_json

PROJECT_DIR = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js unavailable")
def test_all_site_routes_and_filter_visits_are_accepted_by_analytics_receiver():
    script = r"""
        const fs = require('fs');
        const vm = require('vm');
        const assert = require('assert/strict');
        const input = JSON.parse(fs.readFileSync(0, 'utf8'));
        const receiver = vm.createContext({Utilities: {formatDate: () => '2026-10-19'}});
        vm.runInContext(input.receiver, receiver);
        function accepted(event) {
            receiver.event = event;
            return vm.runInContext('normalizeAggregateEvent_(event)', receiver);
        }
        function browser(path, navigator = {}) {
            const events = [], listeners = {};
            const context = {URL, Intl, console, location: new URL(path, 'https://wtarg.com/'),
                navigator: {userAgent: 'Mozilla/5.0', ...navigator},
                document: {querySelectorAll: () => [{value: 'Solana Sierra'}]},
                setTimeout: () => 1, clearTimeout() {},
                addEventListener: (name, callback) => { listeners[name] = callback; },
                fetch: (url, options) => {
                    if (options.method === 'POST') {
                        const event = JSON.parse(options.body);
                        assert.deepEqual(Object.keys(event).sort(), ['content_id', 'country', 'page_type']);
                        accepted(event);
                        events.push(event);
                        return Promise.resolve({});
                    }
                    return Promise.resolve({ok: true, json: () => Promise.resolve({country: 'AR'})});
                }};
            context.history = {
                pushState: (_, __, path) => { context.location = new URL(path, context.location.href); },
                replaceState: (_, __, path) => { context.location = new URL(path, context.location.href); }
            };
            context.window = context;
            vm.runInNewContext(input.browser, context);
            return {context, events, listeners};
        }
        const flush = () => new Promise(resolve => setImmediate(resolve));
        (async () => {
            const tabs = [...input.app.match(/const URL_STATE_TABS = new Set\(\[([\s\S]*?)\]\)/)[1]
                .matchAll(/'([^']+)'/g)].map(match => match[1]);
            for (const tab of tabs) {
                for (const path of [tab === 'home' ? '/' : '/' + tab + '/',
                    '/app.html#' + tab, '/index.html#' + tab, '/' + tab + '/index.html']) {
                    const page = browser(path);
                    await flush();
                    assert.equal(page.events.length, 1, path);
                    assert.equal(page.events[0].page_type, tab, path);
                }
            }
            for (const [path, expected] of [
                ['/entrylists/?t=curitiba&prio=1&view=withdrawals&wtn',
                    't=curitiba&prio=1&view=withdrawals&wtn=1'],
                ['/draws/?t=curitiba&type=QS&round=2&wtn', 't=curitiba&type=qs&round=2&wtn=1'],
                ['/calendar/?level=w35&changes=0', 'level=w35&changes=0'],
                ['/tstrength/?mode=wtn&year=2026', 'mode=wtn&year=2026'],
                ['/roadtogs/?player=solana-sierra', 'solana-sierra'],
                ['/roadtogs/?player=Solana%20Sierra&cutoff=ao-2027',
                    'player=solana-sierra&cutoff=ao-2027'],
                ['/planner/?player=solana-sierra&starting=2026-10-26&ending=2026-11-09'
                    + '&schedule=wta%3A111%3A2026&results=wta%3A111%3A2026~R32~MAIN',
                    'player=solana-sierra&starting=2026-10-26&ending=2026-11-09'
                    + '&schedule=wta:111:2026&results=wta:111:2026~r32~main']
            ]) {
                const page = browser(path);
                await flush();
                assert.equal(page.events[0].content_id, expected, path);
            }
            const page = browser('/calendar/');
            await flush();
            page.context.history.pushState(null, '', '/planner/?player=solana-sierra');
            page.context.trackVisit(); // App and history hooks must not double-count.
            await flush();
            page.context.history.replaceState(null, '', '/planner/?player=solana-sierra&ending=2026-11-09');
            await flush();
            page.context.location = new URL('https://wtarg.com/calendar/');
            page.listeners.popstate();
            await flush();
            page.context.location.hash = '#information';
            page.listeners.hashchange();
            await flush();
            assert.deepEqual(page.events.map(event => event.page_type),
                ['calendar', 'planner', 'planner', 'calendar', 'information']);
            const privatePage = browser('/rankings/?scope=arg&email=secret%40example.com');
            await flush();
            assert.equal(privatePage.events[0].content_id, 'scope=arg');
            privatePage.context.history.replaceState(null, '', '/rankings/?scope=arg&ignored=other');
            await flush();
            assert.equal(privatePage.events.length, 2); // Distinct URLs still count without sending unknown fields.
            assert.equal(privatePage.events[1].content_id, 'scope=arg');
            for (const navigator of [{globalPrivacyControl: true}, {doNotTrack: '1'}, {webdriver: true}]) {
                const excluded = browser('/information/', navigator);
                await flush();
                assert.equal(excluded.events.length, 0);
            }
            assert.throws(() => accepted({country: 'Argentina', page_type: 'unknown', content_id: ''}));
            assert.throws(() => accepted({country: 'Argentina', page_type: 'planner',
                content_id: 'schedule=https://example.com'}));
        })().catch(error => { console.error(error); process.exitCode = 1; });
    """
    payload = {
        "browser": (PROJECT_DIR / "assets/anonymous-analytics.js").read_text(encoding="utf-8"),
        "receiver": (PROJECT_DIR / "google_apps_script_visit_tracker.gs").read_text(encoding="utf-8"),
        "app": (PROJECT_DIR / "web/js/app.js").read_text(encoding="utf-8"),
    }
    result = subprocess.run(
        [shutil.which("node"), "-e", script], input=json.dumps(payload), text=True, capture_output=True, timeout=20
    )
    assert result.returncode == 0, result.stderr


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
        "entrylists: ['t', 'prio', 'view', 'wtn']",
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
    assert "entrylists: ['t', 'prio', 'view', 'wtn']" in source
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
