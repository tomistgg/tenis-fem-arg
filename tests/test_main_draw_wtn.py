import json
import math
from datetime import date
from types import SimpleNamespace

import pytest

import draws
import itf
import main_draw_wtn
from draws import _parse_itf_draw
from generate_run_report import compute_report, render_email_markdown
from itf_wtn import ITFProfileBlocked
from utils import dumps_draws_store_cache, expand_draws_store_cache


def tournament(*players, qualifiers=None):
    return {
        "name": "W15 Example",
        "level": "W15",
        "startDate": "2026-10-05",
        "draws": {
            "MDS": {
                "draw_size": len(players) + len(qualifiers or []),
                "players": [
                    dict(pos=i, name=name, country="USA", itf_id=pid) for i, (name, pid) in enumerate(players, 1)
                ],
                "qualifiers": qualifiers or [],
            }
        },
    }


def refresh(tmp_path, tournaments, fetch, **options):
    return main_draw_wtn.refresh_main_draw_wtn(
        None,
        tournaments,
        tmp_path / "archive.json",
        tmp_path / "profiles.json",
        tmp_path / "main_draw_wtn_errors.json",
        today=options.pop("today", date(2026, 10, 4)),
        fetch_source=fetch,
        profile_batch_cooldown_seconds=0,
        **options,
    )


def test_archive_uses_every_live_profile_and_freezes_mean_without_player_values(tmp_path):
    calls = []
    draw = tournament(("A, Alice", "800000001"), ("B, Bea", "800000002"))
    for player in draw["draws"]["MDS"]["players"]:
        player["wtn"] = "1"  # Display cache cannot substitute for live profiles.

    def fetch(url):
        calls.append(url)
        return 'var props = {"wtnSingles":%s};' % (4 if "800000001" in url else 16)

    archive = refresh(tmp_path, [("w-itf-usa-2026-044", draw)], fetch)
    record = archive["w-itf-usa-2026-044"]
    assert record["wtn_gm"] == 8
    assert record["status"] == "complete"
    assert record["playerCount"] == record["wtnPlayerCount"] == 2
    assert len(calls) == 2
    assert "Alice" not in json.dumps(archive) and "players" not in record
    refresh(tmp_path, [("w-itf-usa-2026-044", draw)], fetch)
    refresh(tmp_path, [], fetch)  # Finished draws disappear; archive stays.
    assert len(calls) == 2
    assert json.loads((tmp_path / "archive.json").read_text())["w-itf-usa-2026-044"]["wtn_gm"] == 8


def test_missing_player_prevents_partial_gm_and_retries_then_clears_email_errors(tmp_path):
    draw = tournament(("A, Alice", "800000001"), ("B, Bea", "800000002"))
    before = tmp_path / "before"
    before.mkdir()

    def fetch(url):
        return 'var props = {"wtnSingles":%s};' % (4 if "800000001" in url else "null")

    record = refresh(tmp_path, [("w-itf-usa-2026-044", draw)], fetch)["w-itf-usa-2026-044"]
    assert record["wtn_gm"] == round((4 * 30) ** (1 / 2), 4)
    assert record["wtnPlayerCount"] == 1
    report = compute_report(str(before), str(tmp_path))
    email = render_email_markdown(report)
    assert "Singles Players Missing Current WTN" in email
    assert "Bea B" in email and "no valid singles WTN" in email
    assert "itftennis.com/en/players/" in email
    assert len(report["main_draw_players_missing_wtn"][0]["players"]) == 1
    profiles = json.loads((tmp_path / "profiles.json").read_text())
    assert profiles["800000002"]["no_wtn_checked_at"] == "2026-10-04"
    assert "wtn" not in profiles["800000002"]

    refresh(tmp_path, [("w-itf-usa-2026-044", draw)],
            lambda _: pytest.fail("No-WTN profile must wait five days"), today=date(2026, 10, 9))

    record = refresh(tmp_path, [("w-itf-usa-2026-044", draw)],
                     lambda url: 'var props = {"wtnSingles":%s};' % (4 if "800000001" in url else 9),
                     today=date(2026, 10, 10))[
        "w-itf-usa-2026-044"
    ]
    assert record["wtn_gm"] == 6
    assert json.loads((tmp_path / "main_draw_wtn_errors.json").read_text()) == []


def test_blocked_profiles_never_use_old_values_and_notify_for_unattempted_players(tmp_path):
    draw = tournament(("A, Alice", "800000001"), ("B, Bea", "800000002"))
    (tmp_path / "profiles.json").write_text(
        json.dumps(
            {
                "800000001": {"weeks": {"2026-09-21": {"wtn": 8, "source": "profile", "retrieved_at": "2026-09-26"}}},
            }
        )
    )
    calls = []

    def blocked(url):
        calls.append(url)
        raise ITFProfileBlocked("HTTP 403")

    archive = refresh(tmp_path, [("w-itf-usa-2026-044", draw)], blocked)
    assert archive["w-itf-usa-2026-044"]["wtn_gm"] == 30
    errors = json.loads((tmp_path / "main_draw_wtn_errors.json").read_text())
    assert len(errors[0]["players"]) == 2
    assert all("blocked" in p["reason"] for p in errors[0]["players"])
    assert len(calls) == 1


def test_pending_retry_uses_fresh_entry_wtn_after_another_block(tmp_path):
    draw = tournament(("A, Alice", "800000001"), ("B, Bea", "800000002"), ("C, Carla", "800000003"))

    def first_fetch(url):
        if "800000001" in url:
            return 'var props = {"wtnSingles":4};'
        raise ITFProfileBlocked("HTTP 403")

    refresh(tmp_path, [("one", draw)], first_fetch)
    profiles = json.loads((tmp_path / "profiles.json").read_text())
    profiles["800000001"]["wtn"] = 25
    profiles["800000001"]["source"] = "entry_list"
    profiles["800000001"]["retrieved_at"] = "2026-10-05"
    (tmp_path / "profiles.json").write_text(json.dumps(profiles))
    # A retry can encounter the blocked player before an already saved success.
    draw["draws"]["MDS"]["players"].reverse()
    for player in draw["draws"]["MDS"]["players"]:
        player.pop("wtn", None)
    calls = []

    def blocked(url):
        calls.append(url)
        raise ITFProfileBlocked("HTTP 403")

    record = refresh(tmp_path, [("one", draw)], blocked, today=date(2026, 10, 5))["one"]
    assert record["wtnPlayerCount"] == 1
    assert next(p for p in draw["draws"]["MDS"]["players"] if p["itf_id"] == "800000001")["wtn"] == "25"
    assert len(calls) == 1
    errors = json.loads((tmp_path / "main_draw_wtn_errors.json").read_text())
    assert {p["name"] for p in errors[0]["players"]} == {"Bea B", "Carla C"}
    calls.clear()

    def recovered(url):
        calls.append(url)
        assert "800000001" not in url
        return 'var props = {"wtnSingles":%s};' % (9 if "800000002" in url else 16)

    record = refresh(tmp_path, [("one", draw)], recovered, today=date(2026, 10, 6))["one"]
    assert record["wtn_gm"] == round((25 * 9 * 16) ** (1 / 3), 4)
    assert record["status"] == "complete"
    assert len(calls) == 2
    assert json.loads((tmp_path / "main_draw_wtn_errors.json").read_text()) == []
    assert "main_draw_observations" not in json.dumps(record)


def test_another_tournament_reuses_fresh_profile_then_refreshes_after_five_days(tmp_path):
    draw = tournament(("A, Alice", "800000001"))
    refresh(tmp_path, [("one", draw)], lambda _: 'var props = {"wtnSingles":4};')
    calls = []

    def fetch(url):
        calls.append(url)
        return 'var props = {"wtnSingles":16};'

    record = refresh(tmp_path, [("two", draw)], fetch)["two"]
    assert len(calls) == 0
    assert record["wtn_gm"] == 4
    record = refresh(tmp_path, [("three", draw)], fetch, today=date(2026, 10, 10))["three"]
    assert len(calls) == 1
    assert record["wtn_gm"] == 16


def test_pending_gm_uses_latest_fresh_profile_and_never_expired_draw_observations(tmp_path):
    draw = tournament(("Alice", "800000001"), ("Bea", "800000002"))
    refresh(tmp_path, [("one", draw)], lambda url: 'var props = {"wtnSingles":%s};' % (
        4 if "800000001" in url else "null"
    ))
    path = tmp_path / "profiles.json"
    profiles = json.loads(path.read_text())
    profiles["800000001"].update(wtn=9, retrieved_at="2026-10-05")
    path.write_text(json.dumps(profiles))
    record = refresh(tmp_path, [("one", draw)],
                     lambda _: pytest.fail("Archive must not fetch after combined pass"),
                     fetch_profiles=False, today=date(2026, 10, 5))["one"]
    assert record["wtnPlayerCount"] == 1
    assert json.loads(path.read_text())["800000001"]["main_draw_observations"]["one"]["wtn"] == 9
    record = refresh(tmp_path, [("one", draw)], lambda _: pytest.fail("No second lookup pass"),
                     fetch_profiles=False, today=date(2026, 10, 13))["one"]
    assert record["status"] == "pending" and record["wtnPlayerCount"] == 0
    assert record["wtn_gm"] == 30


def test_qualifiers_and_replaced_players_recompute_roster_without_treating_byes_as_players(tmp_path):
    draw = tournament(("A, Alice", "800000001"), qualifiers=[2])
    record = refresh(tmp_path, [("one", draw)], lambda _: 'var props = {"wtnSingles":4};')["one"]
    assert record["status"] == "pending" and record["unfilledPositions"] == 1
    draw["draws"]["MDS"]["players"].append({"pos": 2, "name": "B, Bea", "country": "USA", "itf_id": "800000002"})
    record = refresh(tmp_path, [("one", draw)], lambda _: 'var props = {"wtnSingles":9};')["one"]
    assert record["status"] == "complete" and record["wtn_gm"] == 6
    draw["draws"]["MDS"]["players"][1]["name"] = "C, Carla"
    draw["draws"]["MDS"]["players"][1]["itf_id"] = "800000003"
    record = refresh(tmp_path, [("one", draw)], lambda _: 'var props = {"wtnSingles":16};')["one"]
    assert record["wtn_gm"] == 8
    draw["draws"]["MDS"]["players"].pop()
    draw["draws"]["MDS"]["byes"] = [2]
    record = refresh(tmp_path, [("one", draw)], lambda _: 'var props = {"wtnSingles":4};')["one"]
    assert record["status"] == "complete" and record["playerCount"] == 1


@pytest.mark.parametrize("value", [None, "-", 0, -1, math.nan, math.inf, 41])
def test_invalid_wtn_is_missing(value):
    assert main_draw_wtn._positive_wtn(value) is None


def test_shared_player_fetched_once_per_run_and_unmapped_player_is_reported(tmp_path):
    calls = []

    def fetch(url):
        calls.append(url)
        return 'var props = {"wtnSingles":9};'

    refresh(
        tmp_path,
        [
            ("one", tournament(("A, Alice", "800000001"))),
            ("two", tournament(("A, Alice", "800000001"), ("Mystery Player", ""))),
        ],
        fetch,
        resolve_itf_player=lambda _: None,
    )
    assert len(calls) == 1
    error = json.loads((tmp_path / "main_draw_wtn_errors.json").read_text())[0]
    assert error["tournament_key"] == "two"
    assert error["players"][0]["reason"] == "No unambiguous ITF player ID"


def test_combined_queue_covers_hidden_main_draws_entries_and_qualifying_without_gm_refetch(tmp_path):
    from itf_wtn import refresh_draw_wtn

    calls = []
    hidden = tournament(("Alice", "800000001"), ("Bea", "800000002"))
    visible = tournament(("Alice", "800000001"))
    visible["draws"]["QS"] = {"players": [{"name": "Carla", "itf_id": "800000003", "country": "USA"}]}
    visible["draws"]["MDD"] = {"players": [{"members": [{"name": "Doubles only", "itf_id": "800000004"}]}]}
    entries = {"https://wta.example/event": [{"name": "Bea", "player_id": "123", "country": "USA"}]}
    def resolve(player):
        return {**player, "player_id": "800000002"} if player.get("player_id") == "123" else None
    failures = {}
    refresh_draw_wtn(
        None, {"w-itf-visible": visible}, tmp_path / "profiles.json",
        main_draws=[("w-itf-hidden", hidden)], entry_cache=entries, include_entry_players=True,
        today=date(2026, 10, 4), resolve_itf_player=resolve, profile_failures=failures,
        profile_batch_cooldown_seconds=0,
        fetch_source=lambda url: calls.append(url) or 'var props = {"wtnSingles":9};',
    )
    assert len(calls) == 3
    assert all("800000004" not in url for url in calls)
    archive = refresh(
        tmp_path, [("w-itf-hidden", hidden), ("w-itf-visible", visible)],
        lambda _: pytest.fail("GM archive must not repeat profile lookups"), fetch_profiles=False,
        profile_failures=failures,
    )
    assert all(record["wtn_gm"] == 9 for record in archive.values())
    assert entries["https://wta.example/event"][0]["wtn"] == "9.0"


def test_gm_accepts_fresh_entry_wtn_without_profile_lookup(tmp_path):
    from itf_wtn import refresh_draw_wtn

    (tmp_path / "profiles.json").write_text(json.dumps({
        "800000001": {"wtn": 4, "source": "entry_list", "retrieved_at": "2026-10-04"},
        "800000002": {"wtn": 16, "source": "profile", "retrieved_at": "2026-10-04"},
    }))
    draw = tournament(("Alice", "800000001"), ("Bea", "800000002"))
    failures = {}
    calls = []

    refresh_draw_wtn(
        None, {}, tmp_path / "profiles.json", main_draws=[("w-itf-hidden", draw)],
        today=date(2026, 10, 4),
        fetch_source=lambda url: calls.append(url) or 'var props = {"wtnSingles":9};',
        profile_failures=failures,
    )
    assert calls == []
    record = refresh(tmp_path, [("w-itf-hidden", draw)],
                     lambda _: pytest.fail("Fresh entry-list WTN must avoid a profile request"),
                     fetch_profiles=False, profile_failures=failures)["w-itf-hidden"]
    assert record["status"] == "complete" and record["wtnPlayerCount"] == 2
    assert record["wtn_gm"] == 8
    assert json.loads((tmp_path / "main_draw_wtn_errors.json").read_text()) == []


def test_entry_list_wtn_is_reused_for_five_days_then_refreshed_for_new_draw(tmp_path):
    (tmp_path / "profiles.json").write_text(json.dumps({
        "800000001": {"wtn": 4, "source": "entry_list", "observed_on": "2026-10-04",
                      "retrieved_at": "2026-10-09"},
    }))
    draw = tournament(("Alice", "800000001"))
    calls = []

    record = refresh(
        tmp_path, [("one", draw)],
        lambda url: calls.append(url) or 'var props = {"wtnSingles":9};',
        today=date(2026, 10, 9),
    )["one"]
    assert calls == [] and record["wtn_gm"] == 4
    saved = json.loads((tmp_path / "profiles.json").read_text())["800000001"]
    assert saved["main_draw_observations"]["one"]["observed_on"] == "2026-10-04"

    record = refresh(
        tmp_path, [("two", draw)],
        lambda url: calls.append(url) or 'var props = {"wtnSingles":9};',
        today=date(2026, 10, 10),
    )["two"]
    assert len(calls) == 1 and record["wtn_gm"] == 9


def test_itf_collection_includes_no_arg_main_draws_and_preserves_ids(monkeypatch):
    raw = {
        "koGroups": [
            {
                "rounds": [
                    {
                        "roundDesc": "Final",
                        "matches": [
                            {
                                "teams": [
                                    {
                                        "players": [
                                            {
                                                "playerId": 800000001,
                                                "givenName": "Alice",
                                                "familyName": "A",
                                                "nationality": "USA",
                                            }
                                        ]
                                    },
                                    {
                                        "players": [
                                            {
                                                "playerId": 800000002,
                                                "givenName": "Bea",
                                                "familyName": "B",
                                                "nationality": "ESP",
                                            }
                                        ]
                                    },
                                ]
                            }
                        ],
                    }
                ]
            }
        ]
    }
    assert _parse_itf_draw(raw) is None
    parsed = _parse_itf_draw(raw, include_all_players=True)
    assert parsed["players"][0]["itf_id"] == "800000001"
    calls = []

    def fetch(tid, **options):
        calls.append((tid, options))
        return {"MDS": parsed}

    monkeypatch.setattr(main_draw_wtn, "fetch_itf_tournament_draws", fetch)
    results = list(
        main_draw_wtn.collect_main_draws(
            None,
            {},
            {
                "week": {
                    "W-ITF-USA-2026-044": {"name": "No ARG tournament", "tournamentId": 123},
                }
            },
            {},
        )
    )
    assert results[0][0] == "w-itf-usa-2026-044"
    assert calls[0][1]["include_all_players"] is True
    assert calls[0][1]["draw_types"] == ["MDS"]
    # IDs survive the same cache format used by active website draws.
    restored = expand_draws_store_cache(json.loads(dumps_draws_store_cache(dict(results))))
    assert restored[results[0][0]]["draws"]["MDS"]["players"][0]["itf_id"] == "800000001"


def test_hidden_wta_tournaments_are_collected_and_finished_cache_is_captured(monkeypatch):
    draw = tournament(("A, Alice", "800000001"))
    calls = []

    def fetch(key, year, **options):
        calls.append((key, year, options))
        return draw["draws"]

    monkeypatch.setattr(main_draw_wtn, "fetch_tournament_draws", fetch)
    key = "https://www.wtatennis.com/tournaments/904/roland-garros/2026/player-list"
    result = dict(
        main_draw_wtn.collect_main_draws(
            None,
            {
                "week": {
                    key: {"name": "Roland Garros", "startDate": "2026-05-24"},
                }
            },
            {},
            {"finished": draw},
            today=date(2026, 5, 24),
        )
    )
    assert set(result) == {key, "finished"}
    assert calls[0][1] == "2026"


def test_corrupt_archive_is_not_overwritten(tmp_path):
    (tmp_path / "archive.json").write_text("broken")
    with pytest.raises(json.JSONDecodeError):
        refresh(tmp_path, [], lambda _: "")
    assert (tmp_path / "archive.json").read_text() == "broken"


def test_completed_archive_restores_active_player_wtn_after_draw_refresh(tmp_path):
    draw = tournament(("A, Alice", "800000001"))
    refresh(tmp_path, [("one", draw)], lambda _: 'var props = {"wtnSingles":9};')
    draw["draws"]["MDS"]["players"][0].pop("wtn")

    def unexpected_fetch(_):
        pytest.fail("A completed unchanged roster should not refetch profiles")

    refresh(tmp_path, [("one", draw)], unexpected_fetch)
    assert draw["draws"]["MDS"]["players"][0]["wtn"] == "9.0"
    assert draw["draws"]["MDS"]["wtn_gm"] == 9
    profiles = json.loads((tmp_path / "profiles.json").read_text())
    profiles["800000001"]["wtn"] = 16
    (tmp_path / "profiles.json").write_text(json.dumps(profiles))
    refresh(tmp_path, [("one", draw)], unexpected_fetch)
    assert draw["draws"]["MDS"]["players"][0]["wtn"] == "16"
    assert draw["draws"]["MDS"]["wtn_gm"] == 9


def test_pending_itf_tournament_is_retried_after_leaving_calendar(monkeypatch, tmp_path):
    key = "w-itf-usa-2026-044"
    draw = tournament(("A, Alice", "800000001"))
    draw["tournamentId"] = 123
    refresh(tmp_path, [(key, draw)], lambda _: 'var props = {"wtnSingles":null};')
    calls = []

    def fetch(tid, **options):
        calls.append(tid)
        return draw["draws"]

    monkeypatch.setattr(main_draw_wtn, "fetch_itf_tournament_draws", fetch)
    collected = list(
        main_draw_wtn.collect_main_draws(
            None,
            {},
            {},
            {},
            archive_path=tmp_path / "archive.json",
        )
    )
    assert calls == [123]
    assert collected[0][0] == key
    record = refresh(tmp_path, collected, lambda _: 'var props = {"wtnSingles":9};',
                     today=date(2026, 10, 10))[key]
    assert record["status"] == "complete" and record["wtn_gm"] == 9


def test_wtn_collection_can_poll_early_wta_main_draws(monkeypatch):
    monkeypatch.setattr(draws, "madrid_today", lambda: date(2026, 10, 5))
    calls = []
    monkeypatch.setattr(draws, "fetch_draw_pdf_bytes", lambda *args: calls.append(args) or b"PDF")
    monkeypatch.setattr(draws, "parse_draw_pdf", lambda _: tournament(("A, Alice", "800000001"))["draws"]["MDS"])
    key = "https://www.wtatennis.com/tournaments/123/test/2026/player-list"
    assert draws.fetch_tournament_draws(key, "2026", start_date="2026-10-12", draw_types=["MDS"]) == {}
    assert calls == []
    collected = list(
        main_draw_wtn.collect_main_draws(
            None,
            {
                "week": {
                    key: {"startDate": "2026-10-12", "name": "Early main draw"},
                }
            },
            {},
            {},
        )
    )
    assert collected[0][0] == key
    assert calls == [("123", "2026", "MDS")]


def test_wtn_calendar_includes_next_week_itf_events_on_weekdays(monkeypatch):
    monkeypatch.setattr(itf, "madrid_today", lambda: date(2026, 10, 5))
    monkeypatch.setattr(
        itf,
        "_fetch_itf_calendar_raw",
        lambda _: [
            {"tournamentKey": "w-itf-usa-2026-001", "tournamentName": "W15 One", "startDate": "2026-10-05"},
            {"tournamentKey": "w-itf-usa-2026-002", "tournamentName": "W15 Two", "startDate": "2026-10-12"},
        ],
    )
    monkeypatch.setattr(
        itf,
        "_load_itf_event_filters_cache",
        lambda: {
            "w-itf-usa-2026-001": 1,
            "w-itf-usa-2026-002": 2,
        },
    )
    regular = itf.get_draws_itf_tournament_list(None)
    early = itf.get_draws_itf_tournament_list(None, include_next_week=True)
    assert sum(len(events) for events in regular.values()) == 1
    assert sum(len(events) for events in early.values()) == 2


def test_missing_pdf_country_uses_unique_canonical_identity_for_itf_lookup(monkeypatch):
    import config

    monkeypatch.setattr(
        config,
        "PLAYER_IDENTITY_INDEX",
        SimpleNamespace(
            resolve=lambda source, name: SimpleNamespace(country="RUS"),
        ),
    )

    def resolve(player):
        if player.get("country") == "RUS":
            return {**player, "player_id": "800522013"}
        return None

    resolved = main_draw_wtn._resolve_player(
        {"name": "SIDOROVA, Kristiana", "country": ""},
        "https://wta.example",
        resolve,
    )
    assert resolved["player_id"] == "800522013"
    assert resolved["country"] == "RUS"
