import json
from pathlib import Path

import main
from site_renderer import _entry_inputs, _ranking_inputs, _tournament_inputs, _visible_schedule_monday_map


def test_live_schedule_starts_with_wta_ranked_players_only(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(main, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        main,
        "get_wta_rankings_cached",
        lambda *args, **kwargs: (
            [
                {"Player": "WTA RANKED", "Country": "ARG", "Rank": 100, "Key": "WTA RANKED"},
                {"Player": "WTA UNRANKED", "Country": "ARG", "Rank": None, "Key": "WTA UNRANKED"},
                {"Player": "OTHER COUNTRY", "Country": "USA", "Rank": 50, "Key": "OTHER COUNTRY"},
            ],
            {"status": "fresh"},
        ),
    )

    players, names, _, status = main.fetch_arg_players()

    assert [player["Player"] for player in players] == ["WTA RANKED"]
    assert names == {"WTA RANKED"}
    assert set(status["sources"]) == {"wtaRankings"}


def test_schedule_includes_unranked_players_only_from_entry_lists(tmp_path: Path):
    (tmp_path / "wta_rankings_20_29.csv").write_text(
        "week_date,id,rank,points,player,country,dob\n"
        "2026-09-14,1,100,500,WTA Ranked,ARG,2000-01-01\n"
        "2026-09-14,2,,0,WTA Unranked,ARG,2000-01-01\n",
        encoding="utf-8",
    )
    (tmp_path / "itf_rankings_cache.json").write_text(
        json.dumps({"2026-09-14": [
            {"Player": "ITF With Entry", "Rank": "ITF 10", "Country": "ARG", "Key": "ITF WITH ENTRY"},
            {"Player": "ITF No Entry", "Rank": "ITF 20", "Country": "ARG", "Key": "ITF NO ENTRY"},
        ]}),
        encoding="utf-8",
    )

    players, _ = _ranking_inputs(tmp_path, {"ITF WITH ENTRY", "ENTRY ONLY"})

    assert {player["Player"] for player in players} == {"WTA RANKED", "ITF WITH ENTRY", "ENTRY ONLY"}
    assert next(player for player in players if player["Player"] == "ITF WITH ENTRY")["Rank"] == "-"


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


def test_schedule_places_wta_before_itf_regardless_of_player_priority(tmp_path: Path):
    week = "Week of September 21"
    ankara_key = "https://www.wtatennis.com/tournaments/1178/ankara-125/2026/player-list"
    berkeley_key = "w-itf-usa-2026-043"
    snapshot = {
        ankara_key: {
            "name": "WTA 125 Ankara",
            "level": "WTA 125",
            "surface": "Hard",
            "country": "TUR",
            "startDate": "2026-09-21",
            "endDate": "2026-09-27",
            "week": week,
        },
        berkeley_key: {
            "name": "W50 Berkeley, CA",
            "level": "W50",
            "surface": "Hard",
            "country": "USA",
            "startDate": "2026-09-21",
            "endDate": "2026-09-27",
            "week": week,
        },
    }
    entry_cache = {
        ankara_key: [
            {
                "name": "Julia Riera",
                "country": "ARG",
                "priority": "2",
                "pos_num": 17,
                "type": "MAIN",
            }
        ],
        berkeley_key: [
            {
                "name": "Julia Riera",
                "country": "ARG",
                "priority": "1",
                "pos_num": 2,
                "type": "MAIN",
            }
        ],
    }
    (tmp_path / "tournament_snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
    (tmp_path / "entry_lists_cache.json").write_text(json.dumps(entry_cache), encoding="utf-8")

    tournament_groups, _ = _tournament_inputs(tmp_path)
    _, schedule_map, _ = _entry_inputs(tmp_path, tournament_groups)

    assert schedule_map["JULIA RIERA"][week] == "WTA 125 Ankara<br>W50 Berkeley, CA"
