"""Eligibility, source joins and unknown-data boundaries for the planner."""

import csv
import json
from datetime import date

import pytest

from planner import build_planner_data, entry_deadlines, event_available_to, planner_slam_deadlines


def _write(directory, filename, payload):
    (directory / filename).write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize(
    ("source", "level", "today", "main", "qual", "routes"),
    [
        ("ITF", "W35", date(2026, 10, 28), "2026-10-29", "2026-10-29", ["MAIN", "QUAL"]),
        ("ITF", "W35", date(2026, 10, 29), "2026-10-29", "2026-10-29", []),
        ("WTA", "WTA 125", date(2026, 10, 20), "2026-10-19", "2026-10-26", ["QUAL"]),
        ("WTA", "WTA 500", date(2026, 10, 26), "2026-10-19", "2026-10-26", []),
        ("WTA", "WTAFinals", date(2026, 10, 1), None, None, []),
    ],
)
def test_entry_windows_close_on_deadline_day(source, level, today, main, qual, routes):
    event = {"source": source, "level": level, "startDate": "2026-11-16"}
    actual = entry_deadlines(event, today)
    assert (actual["deadlineMain"], actual["deadlineQual"], actual["openRoutes"]) == (main, qual, routes)


def test_published_list_and_closed_window_never_offer_absent_players():
    event = {
        "source": "ITF",
        "level": "W15",
        "startDate": "2026-12-07",
        "entryPublished": True,
        "listedPlayerIds": ["wta:1"],
    }
    event.update(entry_deadlines(event, date(2026, 10, 18)))
    assert not event["openRoutes"]
    assert event_available_to(event, "wta:1")
    assert not event_available_to(event, "wta:2")
    event.update(entryPublished=False, listedPlayerIds=[])
    event.update(entry_deadlines(event, date(2026, 12, 1)))
    assert not event_available_to(event, "wta:2")


def test_stored_deadlines_override_default_rule():
    event = {
        "source": "ITF",
        "level": "W15",
        "startDate": "2026-11-16",
        "entryDeadlineMain": "2026-11-01",
        "entryDeadlineQual": "2026-11-02",
    }
    actual = entry_deadlines(event, date(2026, 10, 30))
    assert actual["deadlineBasis"] == "stored"
    assert actual["openRoutes"] == ["MAIN", "QUAL"]
    assert actual["deadlineMain"] == actual["deadlineQual"] == "2026-11-01"
    assert entry_deadlines(event, date(2026, 11, 1))["openRoutes"] == []


def test_published_wta_main_draw_can_leave_qualifying_entry_open():
    event = {"source": "WTA", "level": "WTA 125", "startDate": "2026-11-16", "entryPublished": True,
             "entries": [{"type": "MAIN"}]}
    assert entry_deadlines(event, date(2026, 10, 20))["openRoutes"] == ["QUAL"]
    event["entries"].append({"type": "QUAL"})
    assert entry_deadlines(event, date(2026, 10, 20))["openRoutes"] == []


def test_planner_reuses_australian_open_deadlines_from_points_breakdown():
    actual = planner_slam_deadlines([{"id": "AO", "name": "Australian Open", "year": 2027,
                                     "mdCutoff": "2026-11-15", "qCutoff": "2026-11-15"}])
    assert actual[0]["mdCutoff"] == "2026-11-15"
    assert actual[0]["qCutoff"] == "2026-11-15"


def _fixture(directory):
    _write(
        directory,
        "player_aliases_wta_itf.json",
        [
            {
                "player_key": "wta:1",
                "display_name": "Player One",
                "country": "ARG",
                "wta_id": "1",
                "itf_id": "800000001",
                "aliases": ["Shared Name"],
            },
            {
                "player_key": "wta:2",
                "display_name": "Player Two",
                "country": "ARG",
                "wta_id": "2",
                "itf_id": "800000002",
                "aliases": ["Shared Name"],
            },
        ],
    )
    _write(
        directory,
        "wta_full_calendar_cache.json",
        {
            "items": [
                {
                    "tournamentGroup": {"id": 100, "name": "Test"},
                    "year": 2026,
                    "level": "WTA 125",
                    "city": "Test",
                    "surface": "Clay",
                    "country": "ARG",
                    "startDate": "2026-11-16",
                    "endDate": "2026-11-22",
                    "singlesDrawSize": 32,
                },
            ]
        },
    )
    _write(
        directory,
        "entry_lists_cache.json",
        {
            "https://www.wtatennis.com/tournaments/100/test/2026/player-list": [
                {"player_id": "999", "name": "Player One", "country": "ARG", "type": "MAIN", "rank": "100"},
                {
                    "player_id": "2",
                    "name": "Player Two",
                    "country": "ARG",
                    "type": "QUAL",
                    "rank": "ITF 20",
                    "wtn": "12.50",
                    "priority": "2",
                },
                {"player_id": "777", "name": "Foreign Player", "country": "FRA", "type": "MAIN", "rank": "80"},
                {"name": "Shared Name", "country": "ARG", "type": "ALT", "rank": "-"},
            ],
            "https://www.wtatennis.com/tournaments/100/test/2025/player-list": [
                {"player_id": "1", "name": "Player One", "country": "ARG", "type": "MAIN", "rank": "100"},
            ],
        },
    )


def test_source_ids_and_seasons_do_not_fall_back_to_matching_names(tmp_path):
    _fixture(tmp_path)
    data = build_planner_data(tmp_path, today=date(2026, 10, 18))
    event = data["events"][0]
    assert event["entryPublished"]
    assert event_available_to(event, "wta:2")
    assert not event_available_to(event, "wta:1")
    assert event["field"]["main"] == {"count": 2, "ranks": [80, 100], "wtns": [], "wtnCount": 0}
    assert event["field"]["qual"] == {"count": 1, "ranks": [], "wtns": [12.5], "wtnCount": 1}
    assert len(event["entries"]) == 3
    assert all(row["country"] == "ARG" for row in event["entries"])
    assert next(row for row in event["entries"] if row["playerKey"] == "wta:2")["priority"] == 2


@pytest.mark.parametrize(("status", "expected"), [("confirmed_changed", "2026-10-12"), ("unconfirmed", "2026-10-05")])
def test_latest_ranking_requires_accepted_complete_publication(tmp_path, status, expected):
    _fixture(tmp_path)
    _write(tmp_path, "wta_ranking_refresh_status.json", {"requested_date": "2026-10-12", "status": status})
    with (tmp_path / "wta_rankings_20_29.csv").open("w", encoding="utf-8", newline="") as target:
        writer = csv.writer(target)
        writer.writerow(["week_date", "id", "rank", "points", "player", "country", "dob"])
        for week in ("2026-10-05", "2026-10-12"):
            for position in range(1, 1001):
                writer.writerow(
                    [
                        week,
                        position,
                        position,
                        1001 - position,
                        f"Player {position}",
                        "ARG" if position == 1 else "FRA",
                        "2000-01-01",
                    ]
                )
    data = build_planner_data(tmp_path, today=date(2026, 10, 18))
    assert data["asOfRanking"] == expected
    assert next(player for player in data["players"] if player["id"] == "wta:1")["points"] == 1000


def test_cancelled_events_and_malformed_entry_cache_are_not_actionable(tmp_path):
    _fixture(tmp_path)
    _write(
        tmp_path,
        "entry_lists_cache.json",
        {
            "https://www.wtatennis.com/tournaments/100/test/2026/player-list": {"error": "unavailable"},
        },
    )
    _write(
        tmp_path,
        "itf_calendar_cache.json",
        {
            "items": [
                {
                    "tournamentKey": "w-itf-arg-2026-001",
                    "tournamentName": "W15 Test (Cancelled)",
                    "category": "W15",
                    "startDate": "2026-11-16",
                    "endDate": "2026-11-22",
                },
            ]
        },
    )
    data = build_planner_data(tmp_path, today=date(2026, 11, 1))
    assert len(data["events"]) == 1
    assert not data["events"][0]["entryPublished"]
    assert not event_available_to(data["events"][0], "wta:1")


def test_cancelled_wta_season_removes_stale_snapshot_but_keeps_other_editions(tmp_path):
    _fixture(tmp_path)
    calendar = json.loads((tmp_path / "wta_full_calendar_cache.json").read_text(encoding="utf-8"))
    miami = {
        "tournamentGroup": {"id": 1169, "name": "Miami", "metadata": {"cancelledSeasons": "2026"}},
        "year": 2026, "level": "WTA 125", "city": "Miami", "country": "USA",
        "startDate": "2026-11-02", "endDate": "2026-11-08",
    }
    calendar["items"].extend([miami, {**miami, "year": 2027,
                                        "startDate": "2027-11-01", "endDate": "2027-11-07"}])
    _write(tmp_path, "wta_full_calendar_cache.json", calendar)
    _write(tmp_path, "calendar_snapshot.json", [{
        "source": "WTA", "tournamentId": "1169", "name": "WTA 125 MIAMI", "level": "WTA 125",
        "country": "USA", "startDate": "2026-11-02", "endDate": "2026-11-08",
    }])
    events = build_planner_data(tmp_path, today=date(2026, 10, 18))["events"]
    assert {event["id"] for event in events} == {"wta:100:2026", "wta:1169:2027"}


def test_cancelled_itf_status_removes_stale_snapshot_without_a_cancelled_name(tmp_path):
    _fixture(tmp_path)
    row = {
        "tournamentKey": "w-itf-arg-2026-099", "tournamentName": "W35 Test", "category": "W35",
        "startDate": "2026-11-16", "endDate": "2026-11-22", "tourStatusCode": "CN",
    }
    _write(tmp_path, "itf_calendar_cache.json", {"items": [row]})
    _write(tmp_path, "calendar_snapshot.json", [{
        "source": "ITF", "tournamentKey": row["tournamentKey"], "name": "W35 Test", "level": "W35",
        "startDate": row["startDate"], "endDate": row["endDate"],
    }])
    events = build_planner_data(tmp_path, today=date(2026, 10, 18))["events"]
    assert [event["id"] for event in events] == ["wta:100:2026"]


def test_historical_fields_keep_qualifying_and_main_draws_at_the_same_level(tmp_path):
    _fixture(tmp_path)
    base = {"id": "100", "level": "WTA 125", "year": "2025", "startDate": "2025-11-17"}
    _write(
        tmp_path,
        "tstrength_cache.json",
        [
            {**base, "draw": "MD", "rankings": [100, 150, 2000], "playerCount": 3},
            {**base, "draw": "Q", "rankings": [300, 400], "playerCount": 2},
            {**base, "draw": "Q", "level": "WTA 250", "startDate": "2025-11-24", "rankings": [200]},
        ],
    )
    event = build_planner_data(tmp_path, today=date(2026, 10, 18))["events"][0]
    assert event["historical"]["ranks"] == [100, 150]
    assert event["historical"]["drawSize"] == 3
    assert event["historicalQual"]["ranks"] == [300, 400]
    assert event["historicalQual"]["drawSize"] == 2
