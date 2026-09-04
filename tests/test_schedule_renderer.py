import json
from pathlib import Path

from site_renderer import _entry_inputs, _tournament_inputs, _visible_schedule_monday_map


def test_schedule_shows_four_chronological_unique_weeks(tmp_path: Path):
    snapshot = {
        "w-itf-arg-2026-001": {
            "name": "ITF Example",
            "level": "W35",
            "surface": "Clay",
            "country": "ARG",
            "startDate": "2026-08-24",
            "endDate": "2026-08-30",
            "week": "Week of August 24",
        },
        "w-itf-arg-2026-002": {
            "name": "ITF Earlier",
            "level": "W35",
            "surface": "Clay",
            "country": "ARG",
            "startDate": "2026-08-17",
            "endDate": "2026-08-23",
            "week": "Week of August 17",
        },
        "https://www.wtatennis.com/tournaments/1001/later/2026/player-list": {
            "name": "WTA Later",
            "level": "WTA 250",
            "surface": "Hard",
            "country": "USA",
            "startDate": "2026-08-31",
            "endDate": "2026-09-06",
            "week": "Week of August 31",
        },
        "https://www.wtatennis.com/tournaments/1002/future/2026/player-list": {
            "name": "WTA Future",
            "level": "WTA 250",
            "surface": "Hard",
            "country": "USA",
            "startDate": "2026-09-07",
            "endDate": "2026-09-13",
            "week": "Week of September 7",
        },
        "https://www.wtatennis.com/tournaments/1003/too-far/2026/player-list": {
            "name": "WTA Too Far",
            "level": "WTA 250",
            "surface": "Hard",
            "country": "USA",
            "startDate": "2026-09-14",
            "endDate": "2026-09-20",
            "week": "Week of September 14",
        },
    }
    (tmp_path / "tournament_snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")

    _, monday_map = _tournament_inputs(tmp_path)
    visible_map = _visible_schedule_monday_map(monday_map)

    assert list(visible_map.items()) == [
        ("2026-08-17", "Week of August 17"),
        ("2026-08-24", "Week of August 24"),
        ("2026-08-31", "Week of August 31"),
        ("2026-09-07", "Week of September 7"),
    ]


def test_schedule_orders_same_week_tournaments_by_player_priority(tmp_path: Path):
    tournaments = [
        ("w-itf-esp-2026-023", "W50 Yecla", "3"),
        ("w-itf-usa-2026-043", "W50 Berkeley, CA", "1"),
        ("w-itf-bul-2026-006", "W50 Plovdiv", "2"),
        ("w-itf-ita-2026-033", "W35 Santa Margherita di Pula", "4"),
    ]
    snapshot = {
        key: {
            "name": name,
            "level": name.split()[0],
            "surface": "Hard",
            "country": "USA",
            "startDate": "2026-09-21",
            "endDate": "2026-09-27",
            "week": "Week of September 21",
        }
        for key, name, _ in tournaments
    }
    entry_cache = {
        key: [
            {
                "pos": "16",
                "name": "Martina Capurro",
                "country": "ARG",
                "priority": priority,
                "pos_num": 16,
                "type": "MAIN",
            }
        ]
        for key, _, priority in tournaments
    }
    (tmp_path / "tournament_snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
    (tmp_path / "entry_lists_cache.json").write_text(json.dumps(entry_cache), encoding="utf-8")

    tournament_groups, _ = _tournament_inputs(tmp_path)
    _, schedule_map, _ = _entry_inputs(tmp_path, tournament_groups)

    assert schedule_map["MARTINA CAPURRO"]["Week of September 21"] == (
        "W50 Berkeley, CA<br>W50 Plovdiv<br>W50 Yecla<br>W35 Santa Marg."
    )
