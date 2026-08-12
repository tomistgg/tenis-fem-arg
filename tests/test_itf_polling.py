from datetime import date, datetime, timezone
import subprocess
from types import SimpleNamespace

import pandas as pd

import draws
import itf
import itf_drawsheet_cache
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
            "https://www.wtatennis.com/tournaments/1000/current/2026/player-list": {
                "name": "WTA Current",
                "level": "WTA 1000",
                "surface": "Hard",
                "country": "CAN",
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


def test_current_week_acceptance_is_refetched_and_withdrawal_removed(monkeypatch):
    tournament_key = "w-itf-bra-2026-010"
    week = "Week of August 10"
    cached_players = [
        {
            "pos": "1",
            "name": "Carla Markus",
            "country": "ARG",
            "type": "MAIN",
            "pos_num": 1,
        }
    ]
    fresh_players = [
        {
            "pos": "1",
            "name": "Lan Mi",
            "country": "CHN",
            "type": "MAIN",
            "pos_num": 1,
        }
    ]
    fetches = []

    monkeypatch.setattr(
        main,
        "utc_now",
        lambda: datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(main, "_load_acceptance_state", lambda: {})
    monkeypatch.setattr(main, "_save_acceptance_state", lambda state: None)
    monkeypatch.setattr(main, "get_wta_rankings_cached", lambda *args, **kwargs: [])
    monkeypatch.setattr(main, "parse_itf_entry_list", lambda entries: fresh_players)

    def fetch_players(key, driver):
        fetches.append(key)
        return ["fresh acceptance response"], {"LAN MI": ""}

    monkeypatch.setattr(main, "get_itf_players", fetch_players)

    _, tournament_store, updated_cache, _ = main.process_tournaments(
        driver=None,
        tournament_groups={
            week: {
                tournament_key: {
                    "name": "W15 Campos do Jordao",
                    "level": "W15",
                    "surface": "Hard",
                    "country": "BRA",
                    "startDate": "2026-08-10T00:00:00",
                    "endDate": "2026-08-16T00:00:00",
                }
            }
        },
        monday_map={"2026-08-10": week},
        arg_names_set={"CARLA MARKUS"},
        entry_cache={tournament_key: cached_players},
    )

    assert fetches == [tournament_key]
    assert {player["name"].upper() for player in tournament_store[tournament_key]} == {"LAN MI"}
    assert {player["name"].upper() for player in updated_cache[tournament_key]} == {"LAN MI"}


def test_published_main_draw_permanently_closes_acceptance_refresh(monkeypatch):
    tournament_key = "w-itf-bra-2026-010"
    week = "Week of August 10"
    acceptance_state = {}
    cached_players = [
        {
            "pos": "1",
            "name": "Carla Markus",
            "country": "ARG",
            "type": "MAIN",
            "pos_num": 1,
        }
    ]
    tournament_groups = {
        week: {
            tournament_key: {
                "name": "W15 Campos do Jordao",
                "level": "W15",
                "surface": "Hard",
                "country": "BRA",
                "startDate": "2026-08-10T00:00:00",
                "endDate": "2026-08-16T00:00:00",
            }
        }
    }

    current_time = [datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc)]
    monkeypatch.setattr(main, "utc_now", lambda: current_time[0])
    monkeypatch.setattr(main, "_load_acceptance_state", lambda: acceptance_state)
    monkeypatch.setattr(main, "_save_acceptance_state", lambda state: None)
    monkeypatch.setattr(main, "get_wta_rankings_cached", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        main,
        "get_itf_players",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("closed acceptance list was fetched")
        ),
    )

    process_args = {
        "driver": None,
        "tournament_groups": tournament_groups,
        "monday_map": {"2026-08-10": week},
        "arg_names_set": {"CARLA MARKUS"},
        "entry_cache": {tournament_key: cached_players},
        "force_itf_acceptance": True,
    }
    main.process_tournaments(
        **process_args,
        itf_main_draw_available_keys={tournament_key},
    )

    assert acceptance_state[tournament_key]["main_draw_available_date"] == "2026-08-10"

    current_time[0] = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)
    main.process_tournaments(
        **process_args,
        itf_main_draw_available_keys=set(),
    )


def test_published_main_draw_sources_map_to_canonical_itf_keys(monkeypatch):
    monkeypatch.setattr(
        main,
        "tournament_ids_with_published_main_draw",
        lambda tournament_ids: {"101"},
    )

    result = main._itf_keys_with_published_main_draw(
        {
            "Week of August 10": {
                "W-ITF-BRA-2026-010": {"tournamentId": 101},
            }
        },
        {
            "W-ITF-USA-2026-020": {
                "draws": {"MDS": {"players": ["cached player"], "matches": []}},
            }
        },
        {
            "W-ITF-ESP-2026-030": {
                "MDS": {"players": ["prefetched player"], "matches": []},
            },
            "W-ITF-EMPTY-2026-040": {
                "MDS": {"players": [], "matches": ["empty bracket slot"]},
            }
        },
    )

    assert result == {
        "w-itf-bra-2026-010",
        "w-itf-usa-2026-020",
        "w-itf-esp-2026-030",
    }


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


def _published_draw(nationalities):
    return {
        "koGroups": [{
            "rounds": [{
                "matches": [{
                    "teams": [
                        {"players": [{"nationality": nationality}]}
                        for nationality in nationalities
                    ]
                }]
            }]
        }]
    }


def _published_draw_with_singular_players(nationalities):
    return {
        "koGroups": [{
            "rounds": [{
                "matches": [{
                    "teams": [
                        {"player": {"nationality": nationality}}
                        for nationality in nationalities
                    ]
                }]
            }]
        }]
    }


def _empty_draw_slots(match_count=16):
    return {
        "koGroups": [{
            "rounds": [{
                "matches": [
                    {"teams": [{"entryStatus": None}, {"entryStatus": None}]}
                    for _ in range(match_count)
                ]
            }]
        }]
    }


def test_published_qualifying_and_main_draws_can_prove_no_arg(monkeypatch):
    cache = {
        "123_Q_0": {"data": _published_draw(["ESP", "BRA"])},
        "123_M_0": {"data": _published_draw(["USA", "FRA"])},
        "456_Q_0": {"data": _published_draw(["ESP", "ARG"])},
        "456_M_0": {"data": _published_draw_with_singular_players(["USA", "FRA"])},
        "789_Q_0": {"data": _published_draw(["ESP", "BRA"])},
        "999_M_0": {"data": _empty_draw_slots()},
    }
    monkeypatch.setattr(itf_drawsheet_cache, "_load_raw_cache", lambda: cache)

    result = itf_drawsheet_cache.tournament_ids_with_definitive_no_nationality(
        [123, 456, 789],
        "ARG",
    )
    published_main_draws = itf_drawsheet_cache.tournament_ids_with_published_main_draw(
        [123, 456, 789, 999],
    )

    assert result == {"123"}
    assert published_main_draws == {"123", "456"}

    draw_codes = (
        itf_drawsheet_cache.tournament_draw_codes_with_definitive_no_nationality(
            [123, 456, 789],
            "ARG",
        )
    )
    assert draw_codes == {
        "123": {"Q", "M"},
        "789": {"Q"},
    }


def test_website_fetch_can_request_only_arg_relevant_draw_type(monkeypatch):
    payload = _published_draw(["ARG", "ESP"])

    monkeypatch.setattr(draws, "get_cached_drawsheet", lambda *args, **kwargs: payload)
    monkeypatch.setattr(
        draws,
        "_parse_itf_draw",
        lambda data: {"players": ["ARG"], "matches": []},
    )

    result = draws.fetch_itf_tournament_draws(
        123,
        draw_types=["MDS"],
    )

    assert result == {"MDS": {"players": ["ARG"], "matches": []}}


def test_definitive_no_arg_draw_skips_website_polling():
    reason = main._itf_draw_skip_reason(
        "w-itf-test-2026-003",
        {
            "startDate": "2026-08-03T00:00:00",
            "endDate": "2026-08-09T00:00:00",
        },
        acceptance_players=[],
        cached_draw_entry={},
        today=datetime(2026, 8, 5, 12, 0),
        definitive_no_arg_draw=True,
    )

    assert reason == "published qualifying and main draws contain no ARG players"


def test_match_history_filter_skips_definitive_no_arg_regular_event(monkeypatch):
    tournaments = pd.DataFrame([
        {
            "tournamentKey": "w-itf-test-2026-001",
            "category": "W35",
        },
        {
            "tournamentKey": "w-itf-test-2026-002",
            "category": "W75",
        },
    ])
    monkeypatch.setattr(
        itf_load_new,
        "tournament_ids_with_definitive_no_nationality",
        lambda tournament_ids, nationality: {"101"},
    )

    eligible, skipped = itf_load_new._filter_tournaments_with_possible_arg_draws(
        tournaments,
        {
            "w-itf-test-2026-001": "101",
            "w-itf-test-2026-002": "202",
        },
    )

    assert eligible["tournamentKey"].tolist() == ["w-itf-test-2026-002"]
    assert skipped == 1


def test_missing_tournament_ids_never_change_run_status(monkeypatch):
    recorded_issues = []
    monkeypatch.setattr(
        itf_load_new,
        "record_run_issue",
        lambda *args, **kwargs: recorded_issues.append((args, kwargs)),
    )

    unresolved = itf_load_new._warn_unresolved_tournament_ids(_tournaments())

    assert unresolved == 1
    assert recorded_issues == []


def test_non_windows_chrome_detection_skips_registry_probe(monkeypatch):
    monkeypatch.setattr(itf_load_new.os, "name", "posix")
    monkeypatch.setattr(itf_load_new.shutil, "which", lambda executable: None)
    monkeypatch.setattr(
        itf_load_new.logger,
        "warning",
        lambda message: (_ for _ in ()).throw(AssertionError(message)),
    )
    for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        monkeypatch.delenv(env_name, raising=False)

    assert itf_load_new._get_chrome_executable_path() is None


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
        "get_with_retry",
        blocked_request,
    )

    result = tournament_sizes_update.itf_fetch_drawsheet(123, "M")

    assert result == stale_draw
    assert cache_calls == [False, True]
    assert request_kwargs["failure_status"] == "degraded"
    assert request_kwargs["params"] == {
        "eventClassificationCode": "M",
        "matchTypeCode": "S",
        "tourType": "N",
        "tournamentId": "123",
        "weekNumber": 0,
    }
    assert "json" not in request_kwargs


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


def test_website_draw_fetch_uses_direct_get_without_touching_browser(monkeypatch):
    payloads = {
        "M": {"koGroups": [{"rounds": []}], "draw": "main"},
        "Q": {"koGroups": [{"rounds": []}], "draw": "qualifying"},
    }
    direct_calls = []

    class BlockedBrowser:
        def get(self, url):
            raise AssertionError(f"Drawsheet must not prime the protected print page: {url}")

        def execute_async_script(self, *args):
            raise AssertionError("Drawsheet must not use the browser's blocked session")

    def direct_fetch(tournament_id, classification, week_number=0):
        direct_calls.append((tournament_id, classification, week_number))
        return payloads[classification]

    monkeypatch.setattr(draws, "get_cached_drawsheet", lambda *args, **kwargs: None)
    monkeypatch.setattr(draws, "_fetch_itf_drawsheet", direct_fetch)
    monkeypatch.setattr(draws, "save_drawsheet", lambda *args: None)
    monkeypatch.setattr(
        draws,
        "_parse_itf_draw",
        lambda data: {"players": [data["draw"]], "matches": []},
    )

    result, meta = draws.fetch_itf_tournament_draws(
        1100204032,
        driver=BlockedBrowser(),
        tournament_name="W100 Landisville, PA",
        return_meta=True,
    )

    assert result == {
        "MDS": {"players": ["main"], "matches": []},
        "QS": {"players": ["qualifying"], "matches": []},
    }
    assert direct_calls == [
        (1100204032, "M", 0),
        (1100204032, "Q", 0),
    ]
    assert meta == {"blocked_responses": []}


def test_website_drawsheet_direct_get_uses_query_parameters(monkeypatch):
    payload = {"koGroups": [{"rounds": []}]}
    captured = {}

    def direct_get(url, **kwargs):
        captured.update(url=url, kwargs=kwargs)
        return SimpleNamespace(
            status_code=200,
            text='{"koGroups":[{"rounds":[]}]}',
            json=lambda: payload,
        )

    monkeypatch.setattr(draws, "_wait_for_itf_drawsheet_request_slot", lambda: None)
    monkeypatch.setattr(draws.requests, "get", direct_get)

    result = draws._fetch_itf_drawsheet(1100204032, "M", 0)

    assert result == payload
    assert captured["url"] == draws._ITF_DRAWSHEET_URL
    assert captured["kwargs"]["params"] == {
        "eventClassificationCode": "M",
        "matchTypeCode": "S",
        "tourType": "N",
        "tournamentId": "1100204032",
        "weekNumber": 0,
    }
    assert "json" not in captured["kwargs"]


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
