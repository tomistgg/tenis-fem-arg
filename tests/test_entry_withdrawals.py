import json
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import main
import wta
from data_quality import _validate_json_schema
from entry_withdrawals import (
    load_withdrawals,
    public_withdrawals,
    record_itf_withdrawals,
    record_wta_withdrawals,
    withdrawal_deadline,
)
from utils import save_json_file


def player(identifier, name="Example Player", position="3", draw="QUAL", rank="123"):
    return {
        "player_id": str(identifier),
        "name": name,
        "pos": position,
        "type": draw,
        "country": "ARG",
        "rank": rank,
    }


def withdrawal(identifier, information, position="98", *, wta_rank="", itf_rank=""):
    return {
        "positionDisplay": position,
        "information": information,
        "players": [
            {
                "playerId": identifier,
                "givenName": "Example",
                "familyName": "Player",
                "nationalityCode": "ARG",
                "atpWtaRank": wta_rank,
                "itfWorldTennisRanking": itf_rank,
            }
        ],
    }


@pytest.mark.parametrize("start", ["2026-09-28", "2026-09-29T00:00:00"])
def test_deadline_is_wednesday_twelve_days_before_start_week(start):
    assert withdrawal_deadline(start) == date(2026, 9, 16)


def test_itf_excludes_early_withdrawals_and_preserves_previous_draw_position(tmp_path):
    state = {}
    previous = [player(90001), player(90002, "Early Player"), player(90005, "Alternate", draw="ALT")]
    classifications = [
        {
            "entryClassificationCode": "W",
            "entries": [
                withdrawal(90001, "W 16 Sep 2026"),
                withdrawal(90002, "W 15 Sep 2026"),
                withdrawal(90003, "W 17 Sep 2026"),
                withdrawal(90004, "W invalid"),
                withdrawal(90005, "W 16 Sep 2026"),
            ],
        }
    ]
    record_itf_withdrawals(state, "itf-example", previous, [], classifications, "2026-09-28", "2026-09-16")
    rows = state["itf-example"]["withdrawals"]
    assert [(row["player_id"], row["pos"], row["type"], row["rank"], row["date"]) for row in rows] == [
        ("90001", "3", "QUAL", "123", "2026-09-16"),
        ("90005", "3", "ALT", "123", "2026-09-16"),
    ]

    # Simulate a separate run after the player is no longer in the cached list.
    path = tmp_path / "entry_withdrawals.json"
    save_json_file(path, state)
    restored = load_withdrawals(path)
    record_itf_withdrawals(restored, "itf-example", [], [], classifications, "2026-09-28", "2026-09-17")
    assert restored["itf-example"]["withdrawals"][:2] == rows
    assert len(restored["itf-example"]["withdrawals"]) == 3
    groups = {"Week": {"itf-example": {"startDate": "2026-09-28"}}}
    exposed = public_withdrawals(restored, groups)["itf-example"]
    assert exposed["deadline"] == "2026-09-16"
    assert [(row["position"], row["date"]) for row in exposed["rows"]] == [
        ("—", "2026-09-17"),
        ("3-ALT", "2026-09-16"),
        ("3-Q", "2026-09-16"),
    ]
    _validate_json_schema(restored, Path("schemas/entry_withdrawals.schema.json"), path)


def test_itf_retains_last_seen_position_until_withdrawal_section_catches_up():
    state = {}
    record_itf_withdrawals(
        state, "itf-example", [player(90001, draw="MAIN", position="1")], [], [], "2026-09-28", "2026-09-16"
    )
    record_itf_withdrawals(
        state,
        "itf-example",
        [],
        [],
        [
            {
                "entryClassificationCode": "W",
                "entries": [
                    withdrawal(90001, "W 16 Sep 2026"),
                ],
            }
        ],
        "2026-09-28",
        "2026-09-18",
    )
    row = state["itf-example"]["withdrawals"][0]
    assert (row["pos"], row["type"], row["date"]) == ("1", "MAIN", "2026-09-16")


def test_itf_does_not_record_withdrawals_before_deadline():
    state = {}
    record_itf_withdrawals(
        state,
        "itf-example",
        [player(90001)],
        [],
        [
            {
                "entryClassificationCode": "W",
                "entries": [withdrawal(90001, "W 15 Sep 2026")],
            }
        ],
        "2026-09-28",
        "2026-09-15",
    )
    assert state["itf-example"]["withdrawals"] == []


def test_itf_withdrawal_rank_uses_wta_then_itf_world_ranking():
    state = {}
    classifications = [{
        "entryClassificationCode": "W",
        "entries": [
            withdrawal(90001, "W 16 Sep 2026", wta_rank="321", itf_rank="45"),
            withdrawal(90002, "W 16 Sep 2026", itf_rank="67"),
        ],
    }]
    record_itf_withdrawals(state, "itf-example", [], [], classifications, "2026-09-28", "2026-09-16")
    assert [row["rank"] for row in state["itf-example"]["withdrawals"]] == ["321", "ITF 67"]


def test_wta_uses_first_missing_date_without_treating_draw_moves_as_withdrawals(tmp_path):
    old = [player(1, "Moved Player"), player(2, "Withdrawn Player", "1", "MAIN")]
    current = [player(1, "Moved Player", "10", "MAIN")]
    observed = {"complete_sections": ["MAIN", "QUAL"], "player_ids": ["1"]}
    state = {}
    record_wta_withdrawals(state, "wta-example", old, current, observed, "2026-09-16")
    path = tmp_path / "entry_withdrawals.json"
    save_json_file(path, state)
    state = load_withdrawals(path)
    record_wta_withdrawals(state, "wta-example", old, current, observed, "2026-09-17")
    rows = state["wta-example"]["withdrawals"]
    assert len(rows) == 1
    assert (rows[0]["name"], rows[0]["pos"], rows[0]["type"], rows[0]["date"]) == (
        "Withdrawn Player",
        "1",
        "MAIN",
        "2026-09-16",
    )
    assert rows[0]["rank"] == "123"
    # A restored player must not remain displayed as withdrawn.
    record_wta_withdrawals(state, "wta-example", current, old, {**observed, "player_ids": ["1", "2"]}, "2026-09-18")
    assert state["wta-example"]["withdrawals"] == []


def test_wta_ignores_missing_sections_and_failed_profile_resolution():
    state = {}
    old = [player(1, "Unresolved Player", draw="MAIN"), player(2, "Missing Qualifier")]
    record_wta_withdrawals(state, "wta-example", old, [], {}, "2026-09-16")
    assert state["wta-example"]["withdrawals"] == []
    record_wta_withdrawals(
        state, "wta-example", old, [], {"complete_sections": ["MAIN"], "player_ids": ["1"]}, "2026-09-16"
    )
    assert state["wta-example"]["withdrawals"] == []


def test_wta_partial_response_does_not_erase_the_last_known_position():
    state = {}
    old = [player(1, "Remaining", "1", "MAIN"), player(2, "Withdrawn", "3", "MAIN")]
    current = [old[0]]
    record_wta_withdrawals(state, "wta-example", old, current, {}, "2026-09-16")
    assert state["wta-example"]["withdrawals"] == []
    record_wta_withdrawals(
        state, "wta-example", current, current, {"complete_sections": ["MAIN"], "player_ids": ["1"]}, "2026-09-17"
    )
    record = state["wta-example"]["withdrawals"][0]
    assert (record["name"], record["pos"], record["date"]) == ("Withdrawn", "3", "2026-09-17")


@pytest.mark.parametrize("declared_count,expected_sections", [(2, ["MAIN"]), (3, [])])
def test_wta_scraper_exposes_raw_ids_and_checks_provider_counts(monkeypatch, declared_count, expected_sections):
    jsonld = {
        "@type": "SportsEvent",
        "@id": "https://example.com/player-list",
        "subEvent": [
            {"name": "Singles", "performer": [{}] * declared_count},
        ],
    }
    html = '<script type="application/ld+json">' + json.dumps(jsonld) + "</script>"
    html += '<a href="/players/90001/first-player"></a><a href="/players/90002/second-player"></a>'
    monkeypatch.setattr(wta, "get_with_retry", lambda *args, **kwargs: SimpleNamespace(text=html))
    observation = {}
    wta.scrape_tournament_players(
        "https://example.com/player-list", [], [], [player(90001), player(90002)], observation=observation
    )
    assert observation["player_ids"] == ["90001", "90002"]
    assert observation["complete_sections"] == expected_sections


def test_tournament_pass_persists_wta_withdrawal_before_replacing_entry_cache(monkeypatch, tmp_path):
    path = tmp_path / "entry_withdrawals.json"
    monkeypatch.setattr(main, "ENTRY_WITHDRAWALS_FILE", str(path))
    monkeypatch.setattr(main, "utc_now", lambda: datetime(2026, 9, 16, 10, tzinfo=UTC))
    monkeypatch.setattr(main, "_load_acceptance_state", lambda: {})
    monkeypatch.setattr(main, "get_wta_rankings_cached", lambda *args, **kwargs: [])
    key = "https://example.com/tournaments/1000/example/2026/player-list"
    old = [player(90001, "Remains", "1", "MAIN"), player(90002, "Withdraws", "2", "MAIN")]

    def fetch(*args, observation, **kwargs):
        observation.update(complete_sections=["MAIN"], player_ids=["90001"])
        return [old[0]], {}

    monkeypatch.setattr(main, "scrape_tournament_players", fetch)
    _, _, updated, _ = main.process_tournaments(
        None,
        {"Week": {key: {"name": "WTA 250 Example", "level": "WTA 250", "startDate": "2026-09-28"}}},
        {"2026-09-28": "Week"},
        set(),
        {key: old},
    )
    assert [row["player_id"] for row in updated[key]] == ["90001"]
    assert load_withdrawals(path)[key]["withdrawals"][0]["date"] == "2026-09-16"


def test_itf_initial_withdrawal_fetch_is_not_skipped_by_acceptance_polling(monkeypatch, tmp_path):
    path = tmp_path / "entry_withdrawals.json"
    monkeypatch.setattr(main, "ENTRY_WITHDRAWALS_FILE", str(path))
    monkeypatch.setattr(main, "utc_now", lambda: datetime(2026, 9, 16, 14, tzinfo=UTC))
    key = "w-itf-arg-2026-fixture"
    acceptance = {key: {"last_changed_date": "2026-09-16"}}
    monkeypatch.setattr(main, "_load_acceptance_state", lambda: acceptance)
    monkeypatch.setattr(main, "_save_acceptance_state", lambda state: None)
    monkeypatch.setattr(main, "get_wta_rankings_cached", lambda *args, **kwargs: [])
    previous = [player(90001)]
    current = [player(90002, "Remaining Player", "1", "MAIN")]
    monkeypatch.setattr(main, "parse_itf_entry_list", lambda entries: current)
    fetches = []

    def fetch(*args):
        fetches.append(args)
        return [{"entryClassificationCode": "W", "entries": [withdrawal(90001, "W 16 Sep 2026")]}], {}

    monkeypatch.setattr(main, "get_itf_players", fetch)
    groups = {"Week": {key: {"name": "W15 Example", "level": "W15", "startDate": "2026-09-28"}}}
    main.process_tournaments(None, groups, {"2026-09-28": "Week"}, set(), {key: previous})
    assert len(fetches) == 1
    assert load_withdrawals(path)[key]["withdrawals"][0]["date"] == "2026-09-16"
    main.process_tournaments(None, groups, {"2026-09-28": "Week"}, set(), {key: current})
    assert len(fetches) == 1
