import json
from datetime import date

from populate_data import tournament_sizes_update


def test_draw_lookup_range_is_last_and_current_week_on_monday():
    assert tournament_sizes_update.get_draw_lookup_range(date(2026, 8, 17)) == (
        "2026-08-10",
        "2026-08-23",
    )


def test_draw_lookup_range_is_current_week_tuesday_through_saturday():
    for day in (18, 19, 20, 21, 22):
        assert tournament_sizes_update.get_draw_lookup_range(date(2026, 8, day)) == (
            "2026-08-17",
            "2026-08-23",
        )


def test_draw_lookup_range_is_current_and_next_week_on_sunday():
    assert tournament_sizes_update.get_draw_lookup_range(date(2026, 8, 23)) == (
        "2026-08-17",
        "2026-08-30",
    )


def _wta_tournament(tournament_id, title):
    return {
        "tournamentGroup": {"id": tournament_id},
        "title": title,
        "country": "United States",
        "level": "WTA 125",
        "startDate": "2026-08-17",
        "singlesDrawSize": 32,
    }


def _itf_calendar_item(name, key):
    return {
        "startDate": "2026-08-17T00:00:00",
        "tournamentName": name,
        "tournamentLink": f"/en/tournament/{key}/",
        "category": "W35",
    }


def _published_draw(player_id):
    return {
        "koGroups": [
            {
                "rounds": [
                    {
                        "matches": [
                            {"teams": [{"players": [{"playerId": player_id}]}]},
                        ]
                    }
                ]
            }
        ]
    }


def test_saved_wta_size_skips_qualifying_matches_endpoint(monkeypatch):
    saved = {
        "source": "WTA",
        "date": "2026-08-17",
        "tournamentName": "Philadelphia",
        "tournamentId": "1166",
        "mainDrawSize": 32,
        "qualifyingSize": 8,
    }
    monkeypatch.setattr(
        tournament_sizes_update,
        "_load_wta_calendar_cache",
        lambda *_args: [_wta_tournament(1166, "Philadelphia")],
    )
    monkeypatch.setattr(
        tournament_sizes_update,
        "wta_count_qualifying_players",
        lambda *_args: (_ for _ in ()).throw(AssertionError("saved WTA size was refetched")),
    )

    result = tournament_sizes_update.fetch_wta_updates(
        "2026-08-10",
        "2026-08-30",
        set(),
        tournament_sizes_update._valid_draw_size_aliases([saved]),
    )

    assert result == []


def test_saved_legacy_itf_size_skips_id_and_drawsheet_endpoints(monkeypatch):
    saved = {
        "source": "ITF",
        "date": "2026-08-17",
        "tournamentName": "W35 Sao Paulo",
        "mainDrawSize": 32,
        "qualifyingSize": 32,
    }
    monkeypatch.setattr(
        tournament_sizes_update,
        "_load_itf_calendar_cache",
        lambda *_args: [_itf_calendar_item("W35 São Paulo", "w-itf-bra-2026-001")],
    )
    monkeypatch.setattr(tournament_sizes_update, "_load_itf_id_cache", lambda: {})
    monkeypatch.setattr(
        tournament_sizes_update,
        "_fill_ids_via_selenium",
        lambda *_args: (_ for _ in ()).throw(AssertionError("saved ITF ID was looked up")),
    )
    monkeypatch.setattr(
        tournament_sizes_update,
        "itf_fetch_drawsheet",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("saved ITF draw was refetched")),
    )

    result = tournament_sizes_update.fetch_itf_updates(
        "2026-08-10",
        "2026-08-30",
        [],
        tournament_sizes_update._valid_draw_size_aliases([saved]),
    )

    assert result == []


def test_new_itf_tournament_is_fetched_while_saved_neighbor_is_skipped(monkeypatch):
    known_key = "w-itf-bra-2026-001"
    new_key = "w-itf-arg-2026-002"
    saved = {
        "source": "ITF",
        "date": "2026-08-17",
        "tournamentName": "W35 Sao Paulo",
        "mainDrawSize": 32,
    }
    monkeypatch.setattr(
        tournament_sizes_update,
        "_load_itf_calendar_cache",
        lambda *_args: [
            _itf_calendar_item("W35 Sao Paulo", known_key),
            _itf_calendar_item("W35 Buenos Aires", new_key),
        ],
    )
    monkeypatch.setattr(
        tournament_sizes_update,
        "_load_itf_id_cache",
        lambda: {new_key: "1100209999"},
    )
    monkeypatch.setattr(tournament_sizes_update.time, "sleep", lambda *_args: None)
    draw_calls = []

    def fetch_draw(tournament_id, classification, week_number=0):
        draw_calls.append((tournament_id, classification, week_number))
        return _published_draw(f"{classification}-player")

    monkeypatch.setattr(tournament_sizes_update, "itf_fetch_drawsheet", fetch_draw)

    result = tournament_sizes_update.fetch_itf_updates(
        "2026-08-10",
        "2026-08-30",
        [],
        tournament_sizes_update._valid_draw_size_aliases([saved]),
    )

    assert draw_calls == [
        ("1100209999", "M", 0),
        ("1100209999", "Q", 0),
    ]
    assert [entry["tournamentName"] for entry in result] == ["W35 Buenos Aires"]
    assert result[0]["tournamentId"] == new_key


def test_valid_size_replaces_unresolved_row_and_zero_size_is_not_added():
    existing = [
        {
            "source": "ITF",
            "date": "2026-08-17",
            "tournamentName": "W35 Buenos Aires",
            "mainDrawSize": 0,
            "qualifyingSize": 32,
        }
    ]
    published = {
        "source": "ITF",
        "date": "2026-08-17",
        "tournamentName": "W35 Buenos Aires",
        "tournamentId": "w-itf-arg-2026-002",
        "mainDrawSize": 32,
        "qualifyingSize": 32,
    }
    unresolved = {
        "source": "ITF",
        "date": "2026-08-24",
        "tournamentName": "W35 Cordoba",
        "mainDrawSize": 0,
        "qualifyingSize": 0,
    }

    added, updated = tournament_sizes_update._merge_draw_size_updates(
        existing,
        [published, unresolved],
    )

    assert (added, updated) == (0, 1)
    assert existing == [published]


def test_main_still_scans_calendar_when_next_week_has_a_saved_tournament(monkeypatch, tmp_path):
    points_path = tmp_path / "points_distribution.json"
    points_path.write_text(json.dumps([]), encoding="utf-8")
    existing = [
        {
            "source": "WTA",
            "date": "2026-08-24",
            "tournamentName": "Known Event",
            "tournamentId": "100",
            "mainDrawSize": 32,
        }
    ]
    late_event = {
        "source": "WTA",
        "date": "2026-08-24",
        "tournamentName": "Late Event",
        "tournamentId": "101",
        "mainDrawSize": 32,
        "qualifyingSize": 0,
    }
    calls = []
    saved_rows = []

    monkeypatch.setattr(tournament_sizes_update, "POINTS_DIST_PATH", str(points_path))
    monkeypatch.setattr(tournament_sizes_update, "madrid_today", lambda: date(2026, 8, 19))
    monkeypatch.setattr(tournament_sizes_update, "load_existing", lambda: list(existing))
    monkeypatch.setattr(
        tournament_sizes_update,
        "fetch_wta_updates",
        lambda *args: calls.append(("WTA", args)) or [late_event],
    )
    monkeypatch.setattr(
        tournament_sizes_update,
        "fetch_itf_updates",
        lambda *args: calls.append(("ITF", args)) or [],
    )
    monkeypatch.setattr(
        tournament_sizes_update,
        "save_results",
        lambda rows: saved_rows.extend(rows),
    )

    tournament_sizes_update.main()

    assert [source for source, _args in calls] == ["WTA", "ITF"]
    assert [row["tournamentId"] for row in saved_rows] == ["100", "101"]
