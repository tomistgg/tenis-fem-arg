import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import wta
import wta_calendar_cache
from utils import compress_wta_calendar_cache, expand_wta_calendar_cache


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


def _tournament(tournament_id, start_date, *, level="WTA 250"):
    return {
        "tournamentGroup": {"id": tournament_id, "name": f"EVENT {tournament_id}"},
        "year": 2026,
        "startDate": start_date,
        "endDate": start_date,
        "title": f"Event {tournament_id}",
        "level": level,
        "city": "CITY",
        "country": "USA",
        "singlesDrawSize": 32,
    }


def _isolate_cache(monkeypatch, tmp_path, *, run_id="run-20260819"):
    cache_file = tmp_path / "wta_full_calendar_cache.json"
    monkeypatch.setattr(wta_calendar_cache, "WTA_CALENDAR_CACHE_FILE", str(cache_file))
    monkeypatch.setattr(wta_calendar_cache, "madrid_today", lambda: date(2026, 8, 19))
    monkeypatch.setattr(
        wta_calendar_cache,
        "_tstrength_required_calendar_start",
        lambda current: current - timedelta(days=21),
    )
    monkeypatch.setattr(
        wta_calendar_cache,
        "utc_now",
        lambda: datetime(2026, 8, 19, 16, 0, tzinfo=UTC),
    )
    monkeypatch.setattr(wta_calendar_cache, "utc_timestamp", lambda: "2026-08-19T16:00:00Z")
    monkeypatch.setattr(
        wta_calendar_cache,
        "get_cache_timestamp",
        lambda _path, payload=None: (payload or {}).get("fetchedAt"),
    )
    monkeypatch.setattr(wta_calendar_cache, "set_cache_file_meta", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(wta_calendar_cache, "report_run_issue", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(wta_calendar_cache, "_attempted_without_run_id", False)
    if run_id:
        monkeypatch.setenv("WTARG_RUN_ID", run_id)
    else:
        monkeypatch.delenv("WTARG_RUN_ID", raising=False)
    return cache_file


def test_shared_calendar_is_fetched_once_and_filtered_for_each_consumer(monkeypatch, tmp_path):
    cache_file = _isolate_cache(monkeypatch, tmp_path)
    requests = []
    calendar = [
        _tournament(1, "2026-08-13"),
        _tournament(2, "2026-08-24"),
        _tournament(3, "2026-08-24", level="Grand Slam"),
        _tournament(4, "2026-10-05"),
        _tournament(1150, "2026-10-19"),
    ]
    calendar[-1]["tournamentGroup"]["metadata"] = {"cancelledSeasons": "2026"}

    def fake_get(url, **kwargs):
        requests.append((url, kwargs))
        return FakeResponse({"content": calendar, "last": True})

    monkeypatch.setattr(wta_calendar_cache, "get_with_retry", fake_get)

    results_window = wta_calendar_cache.get_shared_wta_calendar(
        "2026-08-10",
        "2026-08-30",
        exclude_levels={"Grand Slam"},
        component="wta-loader",
    )
    future_window = wta_calendar_cache.get_shared_wta_calendar(
        "2026-08-20",
        "2026-12-31",
        component="wta",
    )
    quiet_window = wta_calendar_cache.get_shared_wta_calendar(
        "2026-09-01",
        "2026-09-14",
        component="wta-loader",
    )

    assert [item["tournamentGroup"]["id"] for item in results_window] == [1, 2]
    assert [item["tournamentGroup"]["id"] for item in future_window] == [2, 3, 4]
    assert quiet_window == []
    assert len(requests) == 1
    assert requests[0][0] == wta_calendar_cache.WTA_CALENDAR_URL
    assert requests[0][1]["params"] == {
        "page": 0,
        "pageSize": 500,
        "excludeLevels": "ITF",
        "from": "2026-07-22",
        "to": "2026-12-31",
    }
    saved = json.loads(cache_file.read_text(encoding="utf-8"))
    assert saved["from"] == "2026-07-22"
    assert saved["to"] == "2026-12-31"
    assert saved["lastAttemptRunId"] == "run-20260819"


def test_fresh_disk_calendar_is_used_before_any_live_request(monkeypatch, tmp_path):
    cache_file = _isolate_cache(monkeypatch, tmp_path)
    payload = {
        "from": "2026-07-22",
        "to": "2026-12-31",
        "fetchedAt": "2026-08-19T15:30:00Z",
        "items": [_tournament(10, "2026-09-07")],
    }
    cache_file.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(
        wta_calendar_cache,
        "get_with_retry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("fresh calendar was refetched")),
    )

    assert wta_calendar_cache.get_shared_wta_calendar(component="wta") == payload["items"]


def test_force_refresh_ignores_fresh_disk_calendar(monkeypatch, tmp_path):
    cache_file = _isolate_cache(monkeypatch, tmp_path)
    payload = {
        "from": "2026-07-22",
        "to": "2026-12-31",
        "fetchedAt": "2026-08-19T15:30:00Z",
        "items": [_tournament(1150, "2026-10-19")],
    }
    cache_file.write_text(json.dumps(payload), encoding="utf-8")
    replacement = [_tournament(1174, "2026-10-26")]
    monkeypatch.setattr(
        wta_calendar_cache,
        "get_with_retry",
        lambda *_args, **_kwargs: FakeResponse({"content": replacement, "last": True}),
    )

    result = wta_calendar_cache.get_shared_wta_calendar(component="wta", force_refresh=True)

    assert [item["tournamentGroup"]["id"] for item in result] == [1174]


def test_cancelled_season_is_preserved_in_cache_and_filtered():
    miami = _tournament(1169, "2026-11-02", level="WTA 125")
    miami["tournamentGroup"]["metadata"] = {"cancelledSeasons": "2026", "websiteUrl": "unused"}
    earlier_season = _tournament(1169, "2025-11-03", level="WTA 125")
    earlier_season["year"] = 2025
    earlier_season["tournamentGroup"]["metadata"] = {"cancelledSeasons": "2026"}
    active = _tournament(1176, "2026-11-02", level="WTA 125")

    cached = compress_wta_calendar_cache({"items": [miami, earlier_season, active]})
    assert cached["items"][0]["tournamentGroup"]["metadata"] == {"cancelledSeasons": "2026"}
    restored = expand_wta_calendar_cache(cached)["items"]
    result = wta_calendar_cache.filter_wta_calendar(restored, "2025-01-01", "2026-12-31")

    assert [(item["tournamentGroup"]["id"], item["year"]) for item in result] == [(1169, 2025), (1176, 2026)]


def test_shared_range_expands_when_tournament_strength_needs_catch_up(monkeypatch, tmp_path):
    strength_cache = tmp_path / "tstrength_cache.json"
    strength_cache.write_text(
        json.dumps(
            [
                {
                    "year": "2026",
                    "startDate": "2026-06-01",
                    "playerCount": 32,
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(wta_calendar_cache, "TSTRENGTH_CACHE_FILE", str(strength_cache))

    assert wta_calendar_cache.canonical_wta_calendar_range(date(2026, 8, 19)) == (
        "2026-05-11",
        "2026-12-31",
    )


def test_failed_calendar_refresh_is_not_retried_by_later_consumers(monkeypatch, tmp_path):
    cache_file = _isolate_cache(monkeypatch, tmp_path)
    requests = []
    issues = []
    monkeypatch.setattr(
        wta_calendar_cache,
        "report_run_issue",
        lambda *args, **kwargs: issues.append(kwargs["severity"]),
    )

    def blocked(*_args, **_kwargs):
        requests.append("attempt")
        raise RuntimeError("HTTP 403")

    monkeypatch.setattr(wta_calendar_cache, "get_with_retry", blocked)

    assert wta_calendar_cache.get_shared_wta_calendar(component="wta-loader") == []
    assert wta_calendar_cache.get_shared_wta_calendar(component="tstrength") == []
    assert requests == ["attempt"]
    assert issues == ["partial"]
    assert json.loads(cache_file.read_text(encoding="utf-8"))["lastAttemptRunId"] == "run-20260819"


def test_wta_module_delegates_calendar_loading_to_shared_cache(monkeypatch):
    calls = []
    expected = [_tournament(20, "2026-09-14")]
    monkeypatch.setattr(wta, "_wta_tournaments_raw", None)
    monkeypatch.setattr(
        wta,
        "get_shared_wta_calendar",
        lambda **kwargs: calls.append(kwargs) or expected,
    )

    assert wta._fetch_wta_tournaments_raw() == expected
    assert wta._fetch_wta_tournaments_raw() == expected
    assert calls == [{"component": "wta"}]


def test_only_shared_module_contains_the_wta_calendar_endpoint():
    root = Path(__file__).resolve().parents[1]
    calendar_literal = '"https://api.wtatennis.com/tennis/tournaments/"'
    consumers = [
        root / "wta.py",
        root / "tstrength.py",
        root / "populate_data" / "wta_load_new.py",
        root / "populate_data" / "tournament_sizes_update.py",
    ]

    assert calendar_literal in (root / "wta_calendar_cache.py").read_text(encoding="utf-8")
    for path in consumers:
        assert calendar_literal not in path.read_text(encoding="utf-8")
