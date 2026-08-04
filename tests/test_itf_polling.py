from datetime import date, datetime
import subprocess
from types import SimpleNamespace

import pandas as pd

import itf
import main
from populate_data import itf_load_new, tournament_sizes_update


def _tournaments():
    return pd.DataFrame(
        [
            {
                "startDate": "2026-07-20",
                "tournamentKey": "current-week",
                "tournamentId": "100",
            },
            {
                "startDate": "2026-07-27",
                "tournamentKey": "next-week",
                "tournamentId": "",
            },
        ]
    )


def test_next_week_missing_id_is_deferred_before_friday_id_validation():
    eligible, skipped = itf_load_new._filter_tournaments_for_polling(
        _tournaments(),
        today=date(2026, 7, 24),
    )

    assert eligible["tournamentKey"].tolist() == ["current-week"]
    assert int((eligible["tournamentId"] == "").sum()) == 0
    assert skipped == 1


def test_next_week_tournaments_become_eligible_on_saturday():
    eligible, skipped = itf_load_new._filter_tournaments_for_polling(
        _tournaments(),
        today=date(2026, 7, 25),
    )

    assert eligible["tournamentKey"].tolist() == ["current-week", "next-week"]
    assert skipped == 0


def test_current_week_itf_tournaments_remain_grouped_on_tuesday(monkeypatch):
    current_week = "Week of August 3"
    future_weeks = {
        "2026-08-10": "Week of August 10",
        "2026-08-17": "Week of August 17",
        "2026-08-24": "Week of August 24",
        "2026-08-31": "Week of August 31",
    }
    tournament_groups = {
        current_week: {
            "wta-current": {
                "name": "WTA Current",
                "level": "WTA 1000",
                "startDate": "2026-08-03",
                "endDate": "2026-08-09",
            }
        }
    }
    current_itf = {
        "tournamentName": "W35 Chacabuco",
        "tournamentKey": "W-ITF-ARG-2026-006",
        "surfaceDesc": "Clay",
        "hostNationCode": "ARG",
        "startDate": "2026-08-03T00:00:00",
        "endDate": "2026-08-09T00:00:00",
    }

    monkeypatch.setattr(main, "build_tournament_groups", lambda: tournament_groups)
    monkeypatch.setattr(main, "generate_dynamic_monday_map", lambda num_weeks: dict(future_weeks))
    monkeypatch.setattr(main, "madrid_now", lambda: datetime(2026, 8, 4, 12, 0))
    monkeypatch.setattr(main, "get_dynamic_itf_calendar", lambda driver, num_weeks: [current_itf])
    monkeypatch.setattr(main, "save_json_file", lambda *args, **kwargs: None)

    grouped, monday_map = main.build_all_tournament_groups(driver=None)

    assert "2026-08-03" in monday_map
    assert "w-itf-arg-2026-006" in grouped[current_week]


def test_missing_acceptance_data_does_not_suppress_started_itf_draw():
    tournament = {
        "startDate": "2026-08-03T00:00:00",
        "endDate": "2026-08-09T00:00:00",
    }

    reason = main._itf_draw_skip_reason(
        "w-itf-test-2026-001",
        tournament,
        acceptance_players=[],
        cached_draw_entry={},
        today=datetime(2026, 8, 4, 12, 0),
    )

    assert reason is None


def test_real_acceptance_list_without_arg_still_skips_started_itf_draw():
    tournament = {
        "startDate": "2026-08-03T00:00:00",
        "endDate": "2026-08-09T00:00:00",
    }
    acceptance_players = [{"name": "Player", "country": "ESP", "type": "MAIN"}]

    reason = main._itf_draw_skip_reason(
        "w-itf-test-2026-002",
        tournament,
        acceptance_players=acceptance_players,
        cached_draw_entry={},
        today=datetime(2026, 8, 4, 12, 0),
    )

    assert reason == "event already started and no ARG in acceptance list"


def test_missing_tournament_ids_never_change_run_status(monkeypatch, capsys):
    recorded_issues = []
    monkeypatch.setattr(
        itf_load_new,
        "record_run_issue",
        lambda *args, **kwargs: recorded_issues.append((args, kwargs)),
    )

    unresolved = itf_load_new._warn_unresolved_tournament_ids(_tournaments())

    assert unresolved == 1
    assert recorded_issues == []
    assert "Skipping 1 ITF tournament(s) with no source ID" in capsys.readouterr().out


def test_stale_draw_fallback_does_not_navigate_tournament_page(monkeypatch):
    stale_draws = {
        "Q": {"koGroups": [{"rounds": []}], "draw": "qualifying"},
        "M": {"koGroups": [{"rounds": []}], "draw": "main"},
    }
    navigations = []

    class FakeDriver:
        def get_cookies(self):
            return []

        def get(self, url):
            navigations.append(url)

    def cached_drawsheet(tournament_id, code, week_number, allow_stale=False):
        return stale_draws[code] if allow_stale else None

    monkeypatch.setattr(itf_load_new, "get_cached_drawsheet", cached_drawsheet)
    monkeypatch.setattr(
        itf_load_new.requests,
        "get",
        lambda *args, **kwargs: SimpleNamespace(
            status_code=403,
            text="<meta name='robots' content='noindex,nofollow'>",
        ),
    )
    monkeypatch.setattr(itf_load_new.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(itf_load_new, "record_run_issue", lambda *args, **kwargs: None)

    result = itf_load_new.fetch_tournament_draw_data(
        123,
        "Cached Event",
        ["Q", "M"],
        max_attempts=1,
        external_driver=FakeDriver(),
    )

    assert result == stale_draws
    assert navigations == []


def test_draw_size_fetch_uses_stale_cache_when_optional_api_is_blocked(monkeypatch):
    stale_draw = {"koGroups": [{"rounds": []}]}
    cache_calls = []
    request_kwargs = {}

    def cached_drawsheet(tournament_id, code, week_number, allow_stale=False):
        cache_calls.append(allow_stale)
        return stale_draw if allow_stale else None

    def blocked_request(*args, **kwargs):
        request_kwargs.update(kwargs)
        raise RuntimeError("HTTP 403")

    monkeypatch.setattr(
        tournament_sizes_update,
        "get_cached_drawsheet",
        cached_drawsheet,
    )
    monkeypatch.setattr(
        tournament_sizes_update,
        "post_with_retry",
        blocked_request,
    )

    result = tournament_sizes_update.itf_fetch_drawsheet(123, "M")

    assert result == stale_draw
    assert cache_calls == [False, True]
    assert request_kwargs["failure_status"] == "degraded"


def test_invalid_itf_browser_session_fast_fails_to_http_fallback(monkeypatch):
    calls = {"browser": 0, "http": 0}

    class InvalidDriver:
        def execute_async_script(self, *args):
            calls["browser"] += 1
            raise itf.InvalidSessionIdException("invalid session id")

        def get_cookies(self):
            raise AssertionError("dead browser should not be queried again")

    def http_fallback(*args, **kwargs):
        calls["http"] += 1
        return {"tournamentId": 123}

    monkeypatch.setattr(itf, "_itf_browser_unavailable", False)
    monkeypatch.setattr(itf, "_itf_session_warmed", True)
    monkeypatch.setattr(itf, "_itf_wait_for_rate_limit", lambda: None)
    monkeypatch.setattr(itf, "_fetch_itf_json_via_requests", http_fallback)

    result = itf._fetch_itf_json(
        InvalidDriver(),
        "https://example.test/GetEventFilters",
        retries=3,
    )

    assert result == {"tournamentId": 123}
    assert calls == {"browser": 1, "http": 1}
    assert itf._itf_browser_unavailable is True


def test_uncached_blocked_draw_retries_without_poisoning_browser_session(monkeypatch):
    navigations = []
    recorded_issues = []
    request_count = 0

    class FakeDriver:
        def get_cookies(self):
            return []

        def get(self, url):
            navigations.append(url)

    monkeypatch.setattr(itf_load_new, "get_cached_drawsheet", lambda *args, **kwargs: None)
    def blocked_request(*args, **kwargs):
        nonlocal request_count
        request_count += 1
        return SimpleNamespace(
            status_code=403,
            text="<meta name='robots' content='noindex,nofollow'>",
        )

    monkeypatch.setattr(itf_load_new.requests, "get", blocked_request)
    monkeypatch.setattr(itf_load_new.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        itf_load_new,
        "record_run_issue",
        lambda *args, **kwargs: recorded_issues.append((args, kwargs)),
    )

    result = itf_load_new.fetch_tournament_draw_data(
        456,
        "Uncached Event",
        ["Q", "M"],
        max_attempts=1,
        external_driver=FakeDriver(),
    )

    assert result == {}
    assert navigations == []
    assert request_count == 4
    assert recorded_issues
    assert {kwargs.get("severity") for _, kwargs in recorded_issues} == {"degraded"}


def test_drawsheet_block_recovers_after_quiet_period_without_browser_cookies(monkeypatch):
    payload = {"koGroups": [{"rounds": []}]}
    responses = iter([
        SimpleNamespace(
            status_code=403,
            text="<meta name='robots' content='noindex,nofollow'>",
        ),
        SimpleNamespace(
            status_code=200,
            text='{"koGroups":[{"rounds":[]}]}',
            json=lambda: payload,
        ),
    ])
    request_kwargs = []
    sleeps = []
    saved = []
    recorded_issues = []

    monkeypatch.setattr(itf_load_new, "get_cached_drawsheet", lambda *args, **kwargs: None)
    monkeypatch.setattr(itf_load_new, "_wait_for_itf_drawsheet_request_slot", lambda: None)
    monkeypatch.setattr(
        itf_load_new.requests,
        "get",
        lambda *args, **kwargs: (
            request_kwargs.append(kwargs), next(responses)
        )[1],
    )
    monkeypatch.setattr(itf_load_new.time, "sleep", sleeps.append)
    monkeypatch.setattr(
        itf_load_new,
        "save_drawsheet",
        lambda *args: saved.append(args),
    )
    monkeypatch.setattr(
        itf_load_new,
        "record_run_issue",
        lambda *args, **kwargs: recorded_issues.append((args, kwargs)),
    )

    result = itf_load_new.fetch_api_data(
        1100203744,
        "M",
        tournament_name="W35 Roehampton",
    )

    assert result == payload
    assert len(request_kwargs) == 2
    assert all("cookies" not in kwargs for kwargs in request_kwargs)
    assert sleeps == [itf_load_new.ITF_DRAWSHEET_BLOCK_COOLDOWN_SECONDS]
    assert saved == [(1100203744, "M", 0, payload)]
    assert recorded_issues == []


def test_driver_uses_bounded_eager_page_loading(monkeypatch):
    configured = {}

    class FakeOptions:
        page_load_strategy = None

        def add_argument(self, argument):
            pass

    class FakeDriver:
        def set_page_load_timeout(self, seconds):
            configured["page_load_timeout"] = seconds

        def set_script_timeout(self, seconds):
            configured["script_timeout"] = seconds

    options = FakeOptions()
    monkeypatch.setattr(itf_load_new.uc, "ChromeOptions", lambda: options)
    monkeypatch.setattr(itf_load_new.uc, "Chrome", lambda **kwargs: FakeDriver())
    monkeypatch.setattr(itf_load_new, "_get_chrome_major_version", lambda: None)
    monkeypatch.setattr(itf_load_new, "_get_chrome_executable_path", lambda: None)

    itf_load_new.create_driver()

    assert options.page_load_strategy == "eager"
    assert configured == {
        "page_load_timeout": itf_load_new.ITF_PAGE_LOAD_TIMEOUT_SECONDS,
        "script_timeout": itf_load_new.ITF_SCRIPT_TIMEOUT_SECONDS,
    }


def test_chrome_version_probe_is_bounded(monkeypatch):
    captured = {}

    def version_output(command, *, stderr, timeout):
        captured.update(command=command, stderr=stderr, timeout=timeout)
        return b"Google Chrome 140.0.0.0"

    monkeypatch.setattr(
        itf_load_new,
        "_get_chrome_executable_path",
        lambda: "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    )
    monkeypatch.setattr(subprocess, "check_output", version_output)

    assert itf_load_new._get_chrome_major_version() == 140
    assert captured["command"][-1] == "--version"
    assert captured["timeout"] == 5
