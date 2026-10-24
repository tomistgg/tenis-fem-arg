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
    promote_wta_qualifying_entries,
    public_withdrawals,
    record_itf_withdrawals,
    record_wta_withdrawals,
    remove_wta_draw_absences,
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


def test_wta_draw_promotions_use_entry_ranks_and_do_not_create_withdrawals():
    main_player = player(99000001, "Existing Main", draw="MAIN", rank="200")
    justina = {**player(99000002, "Justina Example", rank="440"), "wtn": "14.0"}
    florencia = player(99000003, "Florencia Example", rank="444")
    best_qualifier = {**player(99000004, "Fernanda Example", rank="462"), "seed_rank": 400}
    worse = player(99000005, "Worse Entry", rank="471")
    previous = [main_player, justina, florencia, best_qualifier, worse]
    current = [dict(main_player), dict(best_qualifier)]
    history = {"withdrawals": [], "last_seen": {}}
    draw = {"players": [{"name": "EXAMPLE, Fernanda", "seed": "1", "rank": "400"}]}
    updated = promote_wta_qualifying_entries(previous, current, draw, history, "2026-10-24")
    record_wta_withdrawals(
        {"event": history}, "event", previous, updated,
        {"complete_sections": ["MAIN", "QUAL"], "player_ids": ["99000001", "99000004"]}, "2026-10-24",
    )
    assert [p["player_id"] for p in updated if p["type"] == "MAIN"] == ["99000001", "99000002", "99000003"]
    assert next(p for p in updated if p["player_id"] == "99000002")["wtn"] == "14.0"
    assert [p["player_id"] for p in history["withdrawals"]] == ["99000005"]


@pytest.mark.parametrize("rank", ["462", "471", "-", "WTN 12.00", "0"])
def test_wta_draw_promotions_require_a_strictly_better_numeric_entry_rank(rank):
    candidate = player(99000001, "Absent Player", rank=rank)
    qualifying = player(99000002, "Draw Player", rank="462")
    current = [qualifying]
    assert promote_wta_qualifying_entries(
        [candidate, qualifying], current, {"players": [{"name": "PLAYER, Draw"}]}, {}, "2026-10-24",
    ) == current


def test_wta_draw_promotions_recover_only_draw_day_absences_and_need_known_entry_ranks():
    recent = {**player(99000001, "Recent Absence", rank="440"), "date": "2026-10-24"}
    earlier = {**player(99000002, "Earlier Withdrawal", rank="430"), "date": "2026-10-16"}
    qualifier = player(99000003, "Draw Player", rank="462")
    history = {"withdrawals": [recent, earlier], "last_seen": {}}
    assert promote_wta_qualifying_entries(
        [], [qualifier], {"players": [
            {"name": "Unknown Player", "seed": "1"}, {"name": "PLAYER, Draw"},
        ]}, history, "2026-10-24",
    ) == [qualifier]
    updated = promote_wta_qualifying_entries(
        [], [qualifier], {"players": [{"name": "PLAYER, Draw"}]}, history, "2026-10-24",
    )
    assert [(p["player_id"], p["type"]) for p in updated] == [("99000001", "MAIN"), ("99000003", "QUAL")]
    assert history["withdrawals"] == [earlier]
    assert "date" not in updated[0]
    assert promote_wta_qualifying_entries(
        updated, updated, {"players": [{"name": "PLAYER, Draw"}]}, history, "2026-10-24",
    ) == updated


@pytest.mark.parametrize("draw_type", ["MAIN", "QUAL"])
def test_wta_draw_publication_stops_inferring_withdrawals_from_missing_entries(draw_type):
    old = player(99000001, "Earlier Withdrawal", draw=draw_type)
    absent = player(99000002, "Possible Wildcard", draw=draw_type)
    state = {"event": {"withdrawals": [{**old, "date": "2026-10-23"}], "last_seen": {}}}
    record_wta_withdrawals(
        state, "event", [absent], [], {"complete_sections": [draw_type]}, "2026-10-24",
        qualifying_draw_date="2026-10-24",
    )
    assert [p["player_id"] for p in state["event"]["withdrawals"]] == [old["player_id"]]
    assert state["event"]["last_seen"]["id:99000002"]["name"] == "Possible Wildcard"


def test_wta_draw_absence_cleanup_preserves_earlier_withdrawals_and_promotion_evidence():
    earlier = {**player(99000001, "Earlier Withdrawal", rank="430"), "date": "2026-10-23"}
    unknown = player(99000002, "Possible Wildcard", rank="471")
    better = player(99000003, "Possible Promotion", rank="440")
    history = {"withdrawals": [earlier, *[{**p, "date": "2026-10-24"} for p in [unknown, better]]],
               "last_seen": {"id:99000002": unknown, "id:99000003": better}}
    remove_wta_draw_absences(history, "2026-10-24")
    assert history["withdrawals"] == [earlier]
    qualifier = player(99000004, "Draw Player", rank="462")
    updated = promote_wta_qualifying_entries(
        [], [qualifier], {"players": [{"name": "PLAYER, Draw"}]}, history, "2026-10-24",
    )
    assert [(p["player_id"], p["type"]) for p in updated] == [("99000003", "MAIN"), ("99000004", "QUAL")]
    assert history["withdrawals"] == [earlier]


def test_published_wta_qualifying_draw_restores_promotions_to_entries_and_schedule(monkeypatch, tmp_path):
    from site_renderer import _entry_inputs

    key = "https://www.wtatennis.com/tournaments/1180/example/2026/player-list"
    qualifiers = [player(99000001, "First Promotion", rank="440"), player(99000002, "Second Promotion", rank="444")]
    possible_wildcard = {**player(99000005, "Possible Wildcard", rank="471"), "date": "2026-10-24"}
    history = {"withdrawals": [{**p, "date": "2026-10-24"} for p in qualifiers] + [possible_wildcard],
               "last_seen": {}}
    path = tmp_path / "entry_withdrawals.json"
    save_json_file(path, {key: history})
    monkeypatch.setattr(main, "ENTRY_WITHDRAWALS_FILE", str(path))
    monkeypatch.setattr(main, "utc_now", lambda: datetime(2026, 10, 25, 10, tzinfo=UTC))
    monkeypatch.setattr(main, "_load_acceptance_state", lambda: {
        key: {"qualifying_draw_available_date": "2026-10-24"},
    })
    monkeypatch.setattr(main, "get_wta_rankings_cached", lambda *args, **kwargs: [])
    monkeypatch.setattr(main, "scrape_tournament_players", lambda *args, **kwargs: pytest.fail("Polling is closed"))
    groups = {"Week": {key: {"name": "WTA 125 Example", "level": "WTA 125", "startDate": "2026-10-26"}}}
    draw = {"QS": {"players": [{"name": "PLAYER, Draw", "country": "CHI"}]}}
    store, cache = main.process_tournaments(
        None, groups, {"2026-10-26": "Week"}, {key: [player(99000003, "Draw Player", rank="462")]},
        wta_draws={key: {"draws": draw}},
    )
    assert {p["name"] for p in store[key] if p["type"] == "MAIN"} == {p["name"] for p in qualifiers}
    assert load_withdrawals(path)[key]["withdrawals"] == []
    save_json_file(tmp_path / "entry_lists_cache.json", cache)
    _, schedule, _ = _entry_inputs(tmp_path, groups, {key: {"draws": draw}})
    for p in qualifiers:
        assert schedule[p["name"].upper()]["Week"] == "WTA 125 Example"
    # A main draw is authoritative; the inference applies only while it is pending.
    assert main._promote_wta_entries_for_draws(
        key, qualifiers, [], {**draw, "MDS": {"players": [{"name": "Other Player"}]}}, {}, "2026-10-24",
    ) == []


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


@pytest.mark.parametrize("status", ["W", "LWQ", "LWM", "PFLWMD"])
def test_itf_accepts_all_withdrawal_status_date_formats(status):
    state = {}
    classifications = [{
        "entryClassificationCode": "W",
        "entries": [withdrawal(90001, f"{status} 16 Sep 2026")],
    }]

    record_itf_withdrawals(state, "itf-example", [], [], classifications, "2026-09-28", "2026-09-16")

    assert state["itf-example"]["withdrawals"][0]["date"] == "2026-09-16"


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


def test_wta_only_allows_qualifying_players_to_move_up_without_a_withdrawal():
    old = [player(1, "Moves Up"), player(2, "Leaves Main", draw="MAIN")]
    current = [player(1, "Moves Up", draw="MAIN"), player(2, "Leaves Main", draw="QUAL")]
    observation = {
        "complete_sections": ["MAIN", "QUAL"],
        "player_ids": ["1", "2"],
        "section_player_ids": {"MAIN": ["1"], "QUAL": ["2"]},
    }
    state = {}

    record_wta_withdrawals(state, "wta-example", old, current, observation, "2026-09-16")

    assert [row["player_id"] for row in state["wta-example"]["withdrawals"]] == ["2"]


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
    entries = wta.scrape_tournament_players(
        "https://example.com/player-list", [], [], [player(90001), player(90002)], observation=observation
    )
    assert sorted((row["player_id"], row["type"]) for row in entries) == [("90001", "MAIN"), ("90002", "MAIN")]
    assert observation["player_ids"] == ["90001", "90002"]
    assert observation["section_player_ids"] == {"MAIN": ["90001", "90002"], "QUAL": []}
    assert observation["complete_sections"] == expected_sections


def test_wta_scraper_accepts_combined_main_and_qualifying_count(monkeypatch):
    jsonld = {
        "@type": "SportsEvent",
        "@id": "https://example.com/player-list",
        "subEvent": [{"name": "Singles", "performer": [{}] * 4}],
    }
    html = '<script type="application/ld+json">' + json.dumps(jsonld) + "</script>"
    html += '<a href="/players/90001/first-player"></a><a href="/players/90002/second-player"></a>'
    html += '<div data-ui-tab="qualifying"><a href="/players/90003/third-player"></a>'
    html += '<a href="/players/90004/fourth-player"></a></div>'
    cached = [player(identifier) for identifier in range(90001, 90005)]
    monkeypatch.setattr(wta, "get_with_retry", lambda *args, **kwargs: SimpleNamespace(text=html))
    observation = {}

    entries = wta.scrape_tournament_players(
        "https://example.com/player-list", [], [], cached, observation=observation
    )

    assert sorted((row["player_id"], row["type"]) for row in entries) == [
        ("90001", "MAIN"), ("90002", "MAIN"), ("90003", "QUAL"), ("90004", "QUAL"),
    ]
    assert observation["complete_sections"] == ["MAIN", "QUAL"]


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
        return [old[0]]

    monkeypatch.setattr(main, "scrape_tournament_players", fetch)
    _, updated = main.process_tournaments(
        None,
        {"Week": {key: {"name": "WTA 250 Example", "level": "WTA 250", "startDate": "2026-09-28"}}},
        {"2026-09-28": "Week"},
        {key: old},
    )
    assert [row["player_id"] for row in updated[key]] == ["90001"]
    assert load_withdrawals(path)[key]["withdrawals"][0]["date"] == "2026-09-16"


def test_tournament_pass_preserves_an_incomplete_wta_section(monkeypatch, tmp_path):
    path = tmp_path / "entry_withdrawals.json"
    monkeypatch.setattr(main, "ENTRY_WITHDRAWALS_FILE", str(path))
    monkeypatch.setattr(main, "utc_now", lambda: datetime(2026, 9, 16, 10, tzinfo=UTC))
    monkeypatch.setattr(main, "_load_acceptance_state", lambda: {})
    monkeypatch.setattr(main, "get_wta_rankings_cached", lambda *args, **kwargs: [])
    key = "https://example.com/tournaments/1000/example/2026/player-list"
    old = [player(90001, "Remains", "1", "MAIN"), player(90002, "Unconfirmed", "2", "MAIN")]

    def fetch(*args, observation, **kwargs):
        observation.update(complete_sections=[], player_ids=["90001"])
        return [old[0]]

    monkeypatch.setattr(main, "scrape_tournament_players", fetch)
    _, updated = main.process_tournaments(
        None,
        {"Week": {key: {"name": "WTA 250 Example", "level": "WTA 250", "startDate": "2026-09-28"}}},
        {"2026-09-28": "Week"},
        {key: old},
    )

    assert [row["player_id"] for row in updated[key]] == ["90001", "90002"]
    assert load_withdrawals(path)[key]["withdrawals"] == []


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
        return [{"entryClassificationCode": "W", "entries": [withdrawal(90001, "W 16 Sep 2026")]}]

    monkeypatch.setattr(main, "get_itf_players", fetch)
    groups = {"Week": {key: {"name": "W15 Example", "level": "W15", "startDate": "2026-09-28"}}}
    main.process_tournaments(None, groups, {"2026-09-28": "Week"}, {key: previous})
    assert len(fetches) == 1
    assert load_withdrawals(path)[key]["withdrawals"][0]["date"] == "2026-09-16"
    main.process_tournaments(None, groups, {"2026-09-28": "Week"}, {key: current})
    assert len(fetches) == 1
