from datetime import date
from pathlib import Path

from html_generator import _milestone_active_names
from milestones import _dense_top_three, build_milestones_data

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _match(match_date, round_name, winner, loser, *, tournament_id="event-1"):
    return {
        "DATE": match_date,
        "TOURNAMENT": "W15 Test",
        "TOURNAMENT_ID": tournament_id,
        "CATEGORY": "W15",
        "MATCH_TYPE": "ITF",
        "DRAW": "M",
        "ROUND": round_name,
        "_winnerName": winner,
        "_winnerCountry": "ARG" if winner == "Test Player" else "USA",
        "_loserName": loser,
        "_loserCountry": "ARG" if loser == "Test Player" else "USA",
    }


def test_dense_top_three_gives_same_number_to_same_day():
    result = _dense_top_three([("2020-01-01", "A"), ("2020-01-01", "B"), ("2020-01-02", "C"), ("2020-01-03", "D")])
    assert [(item["position"], item["name"]) for item in result] == [(1, "A"), (1, "B"), (2, "C")]


def test_dense_top_three_stops_after_three_first_place_ties():
    result = _dense_top_three(
        [("2020-01-01", "A"), ("2020-01-01", "B"), ("2020-01-01", "C"), ("2020-01-02", "D")]
    )
    assert [(item["position"], item["name"]) for item in result] == [(1, "A"), (1, "B"), (1, "C")]


def test_active_names_include_recent_singles_and_current_entries_and_draws():
    history = [
        {"DATE": "2025-10-06", "SCORE": "6-2 6-2", "_winnerName": "Recent Player", "_winnerCountry": "ARG"},
        {"DATE": "2025-10-05", "SCORE": "6-2 6-2", "_winnerName": "Old Player", "_winnerCountry": "ARG"},
        {"DATE": "2026-10-04", "SCORE": "-", "_winnerName": "Bye Player", "_winnerCountry": "ARG"},
        {"DATE": "2026-10-04", "SCORE": "6-2 6-2", "_winnerName": "A / B", "_winnerCountry": "ARG"},
    ]
    entries = {"event": [{"name": "Entry Player", "country": "ARG"}]}
    draws = {
        "event": {
            "endDate": "2026-10-11",
            "draws": {
                "QS": {"players": [{"name": "MONDATI, Ornela Luisana", "country": "ARG", "itf_id": "800753197"}]},
                "MDD": {"players": [{"name": "Doubles Player", "country": "ARG"}]},
            },
        },
        "old": {"endDate": "2026-10-04", "draws": {"MDS": {"players": [{"name": "Past Draw", "country": "ARG"}]}}},
    }

    names = _milestone_active_names([], [], history, entries, draws, date(2026, 10, 5))

    assert "Recent Player" in names
    assert "Entry Player" in names
    assert "Ornela Mondati" in names
    assert not {"Old Player", "Bye Player", "A / B", "Doubles Player", "Past Draw"} & set(names)


def test_active_players_include_current_wta_and_keep_career_details(tmp_path):
    (tmp_path / "points_distribution_history.json").write_text(
        (PROJECT_ROOT / "data" / "points_distribution_history.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / "player_aliases_wta_itf.json").write_text("[]\n", encoding="utf-8")
    rankings = {
        "2025-01-06": [
            {"Player": "Test Player", "Country": "ARG", "DOB": "2010-02-03"},
            {"Player": "Ranked Player", "Country": "ARG", "DOB": "2010-04-05"},
        ],
        "2026-01-05": [{"Player": "Ranked Player", "Country": "ARG", "DOB": "2010-04-05"}],
    }
    history = [
        _match("2025-07-01", "1st Round", "Test Player", "Opponent One"),
        _match("2025-07-02", "2nd Round", "Opponent Two", "Test Player"),
    ]

    result = build_milestones_data(
        history=history,
        ranking_weeks=rankings,
        active_names=["Test Player", "Ranked Player", "Entry Player"],
        draw_sizes=[],
        data_dir=tmp_path,
        today=date(2026, 1, 1),
    )

    assert [player["name"] for player in result["active"]] == ["Entry Player", "Ranked Player", "Test Player"]
    test_player = next(player for player in result["active"] if player["name"] == "Test Player")
    assert test_player["lastRankedWeek"] == "2025-01-06"
    assert test_player["totalEverPoints"] == 1
    assert test_player["livePoints"] == 1
    assert test_player["liveRows"][0]["points"] == 1
    assert result["historical"][-1]["year"] == 2012
    cohort_2010 = next(row for row in result["historical"] if row["year"] == 2010)
    assert cohort_2010["point"][0]["name"] == "Test Player"


def test_zero_point_expired_tournaments_are_not_returned(tmp_path):
    (tmp_path / "points_distribution_history.json").write_text(
        (PROJECT_ROOT / "data" / "points_distribution_history.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / "player_aliases_wta_itf.json").write_text("[]\n", encoding="utf-8")
    history = [_match("2024-01-02", "1st Round", "Opponent One", "Test Player")]

    result = build_milestones_data(
        history=history,
        ranking_weeks={},
        active_names=["Test Player"],
        draw_sizes=[],
        data_dir=tmp_path,
        today=date(2026, 1, 1),
    )

    player = result["active"][0]
    assert player["totalEverPoints"] == 0
    assert player["expiredRows"] == []


def test_arg_identity_overrides_incorrect_match_nationality(tmp_path):
    (tmp_path / "points_distribution_history.json").write_text(
        (PROJECT_ROOT / "data" / "points_distribution_history.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / "player_aliases_wta_itf.json").write_text(
        """[
  {
    "display_name": "Andrea Farulla Di Palma",
    "country": "ARG",
    "dob": "2000-10-04",
    "wta_id": "326153",
    "wta_name": "Andrea Farulla Di Palma",
    "itf_id": "800449848",
    "itf_name": "Andrea Agostina Farulla Di Palma",
    "aliases": []
  }
]\n""",
        encoding="utf-8",
    )
    first = _match("2017-09-11", "1st Round", "Andrea Agostina Farulla Di Palma", "Opponent One")
    first["_winnerCountry"] = "ITA"
    first["_winnerId"] = "800449848"
    second = _match("2017-09-12", "2nd Round", "Opponent Two", "Andrea Agostina Farulla Di Palma")
    second["_loserCountry"] = "ITA"
    second["_loserId"] = "800449848"

    result = build_milestones_data(
        history=[first, second],
        ranking_weeks={},
        active_names=["Andrea Agostina Farulla Di Palma"],
        draw_sizes=[],
        data_dir=tmp_path,
        today=date(2026, 1, 1),
    )

    cohort = next(row for row in result["historical"] if row["year"] == 2000)
    assert cohort["point"][0]["name"] == "Andrea Farulla Di Palma"
    assert [player["name"] for player in result["active"]] == ["Andrea Farulla Di Palma"]


def test_itf_profile_birth_year_adds_player_to_historical_cohort(tmp_path):
    (tmp_path / "points_distribution_history.json").write_text(
        (PROJECT_ROOT / "data" / "points_distribution_history.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / "player_aliases_wta_itf.json").write_text("[]\n", encoding="utf-8")
    (tmp_path / "itf_player_details.json").write_text(
        '[{"playerId":"800000001","displayName":"Test Player","birthYear":2010,'
        '"playHand":"Right Handed","backHandStyle":""}]\n',
        encoding="utf-8",
    )
    history = [_match("2024-01-02", "1st Round", "Test Player", "Opponent One")]

    result = build_milestones_data(
        history=history,
        ranking_weeks={},
        active_names=["Test Player"],
        draw_sizes=[],
        data_dir=tmp_path,
        today=date(2026, 1, 1),
    )

    cohort = next(row for row in result["historical"] if row["year"] == 2010)
    assert cohort["proWin"][0]["name"] == "Test Player"
