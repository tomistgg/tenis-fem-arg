import json
from datetime import date

import itf_wtn
from itf_wtn import (
    ITFProfileBlocked,
    entry_list_wtn_status,
    parse_wtn_singles,
    player_profile_urls,
    refresh_entry_list_wtn,
)


def test_parse_wtn_singles_from_profile_props():
    source = '''<script>var props = {"visible":true,"wtnSingles":9.3,"wtnDoubles":11.69};</script>'''
    assert parse_wtn_singles(source) == 9.3


def test_verified_womens_profile_does_not_fall_back_to_juniors():
    preferred = "https://www.itftennis.com/en/players/yexin-ma/800439388/chn/wt/s/overview/"

    urls = player_profile_urls(
        {"player_id": "800439388", "name": "Ye-Xin MA", "country": "CHN"},
        preferred,
    )

    assert urls == [preferred]


def test_profile_wtn_cache_is_shared_and_refetched_after_seven_days(tmp_path):
    cache_path = tmp_path / "itf_wtn_cache.json"
    entries = {
        "https://wta.example/one": [{"player_id": "123", "name": "Eva Lys", "country": "GER"}],
        "https://wta.example/two": [{"player_id": "123", "name": "Eva Lys", "country": "GER"}],
        "itf-one": [{"player_id": "800000001", "name": "ITF Player", "country": "USA", "wtn": "12.1"}],
    }
    fetched = []

    def fetch(url):
        fetched.append(url)
        return '<script>var props = {"wtnSingles":9.3};</script>'

    def resolve(player):
        return {**player, "player_id": "800389685"}

    refresh_entry_list_wtn(
        None, entries, cache_path, today=date(2026, 9, 1), fetch_source=fetch, resolve_itf_player=resolve
    )
    refresh_entry_list_wtn(
        None, entries, cache_path, today=date(2026, 9, 6), fetch_source=fetch, resolve_itf_player=resolve
    )
    assert len(fetched) == 1
    assert all(row[0]["wtn"] == "9.3" for key, row in entries.items() if key.startswith("http"))
    assert entries["itf-one"][0]["wtn"] == "12.1"

    refresh_entry_list_wtn(
        None, entries, cache_path, today=date(2026, 9, 7), fetch_source=fetch, resolve_itf_player=resolve
    )
    assert len(fetched) == 1

    refresh_entry_list_wtn(
        None, entries, cache_path, today=date(2026, 9, 9), fetch_source=fetch, resolve_itf_player=resolve
    )
    assert len(fetched) == 2
    cached = json.loads(cache_path.read_text(encoding="utf-8"))["800389685"]["weeks"]
    assert cached["2026-09-07"]["wtn"] == 9.3
    assert cached["2026-09-07"]["retrieved_at"] == "2026-09-09"


def test_stale_profile_reuses_last_verified_url_across_ranking_weeks(tmp_path):
    cache_path = tmp_path / "cache.json"
    preferred_url = "https://www.itftennis.com/en/players/anna-blinkova/800336598/fra/wt/s/overview/"
    cache_path.write_text(
        json.dumps({
            "800336598": {
                "weeks": {
                    "2026-09-07": {
                        "wtn": 9.0,
                        "source": "profile",
                        "profile_url": preferred_url,
                        "retrieved_at": "2026-09-07",
                    }
                }
            }
        }),
        encoding="utf-8",
    )
    entries = {
        "https://wta.example/list": [
            {"player_id": "324267", "name": "Anna Blinkova", "country": "RUS", "type": "MAIN"}
        ]
    }
    fetched = []

    refresh_entry_list_wtn(
        None,
        entries,
        cache_path,
        today=date(2026, 9, 26),
        fetch_source=lambda url: fetched.append(url) or '<script>var props = {"wtnSingles":9.1};</script>',
        resolve_itf_player=lambda player: {**player, "player_id": "800336598"},
    )

    assert fetched == [preferred_url]


def test_wta_player_uses_unique_recent_itf_entry_identity(tmp_path):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(
        json.dumps({
            "800501322": {
                "name": "NATSUMI KAWAGUCHI",
                "country": "JPN",
                "weeks": {
                    "2026-09-21": {
                        "wtn": "13.03",
                        "source": "entry_list",
                        "retrieved_at": "2026-09-21",
                    }
                },
            }
        }),
        encoding="utf-8",
    )
    entries = {
        "https://wta.example/list": [
            {"player_id": "328919", "name": "Natsumi Kawaguchi", "country": "JPN", "type": "QUAL"}
        ]
    }

    refresh_entry_list_wtn(
        None,
        entries,
        cache_path,
        today=date(2026, 9, 26),
        fetch_source=lambda _url: '<script>var props = {"wtnSingles":12.8};</script>',
        resolve_itf_player=lambda _player: None,
    )

    assert entries["https://wta.example/list"][0]["wtn"] == "12.8"


def test_profile_refresh_checkpoints_and_cools_down_between_batches(tmp_path, monkeypatch):
    entries = {
        "https://wta.example/list": [
            {"player_id": str(index), "name": f"Player {index}", "country": "POR", "type": "MAIN"}
            for index in range(1, 6)
        ]
    }
    sleeps = []
    monkeypatch.setattr(itf_wtn.time, "sleep", sleeps.append)

    refresh_entry_list_wtn(
        None,
        entries,
        tmp_path / "cache.json",
        today=date(2026, 9, 26),
        fetch_source=lambda _url: '<script>var props = {"wtnSingles":10.0};</script>',
        resolve_itf_player=lambda player: player,
        profile_batch_size=2,
        profile_batch_cooldown_seconds=3,
    )

    assert sleeps == [3, 3]


def test_blocked_profile_is_not_cached_as_a_completed_check(tmp_path):
    cache_path = tmp_path / "itf_wtn_cache.json"
    entries = {"https://wta.example/one": [{"player_id": "123", "name": "Blocked", "country": "ARG"}]}

    refresh_entry_list_wtn(
        None,
        entries,
        cache_path,
        today=date(2026, 9, 1),
        fetch_source=lambda _url: "<html>challenge</html>",
        resolve_itf_player=lambda player: player,
    )

    assert json.loads(cache_path.read_text(encoding="utf-8")) == {}
    assert entries["https://wta.example/one"][0]["wtn"] == "-"


def test_junior_profile_fallback_and_challenge_pause(tmp_path):
    cache_path = tmp_path / "itf_wtn_cache.json"
    entries = {
        "https://wta.example/one": [
            {"player_id": "1", "name": "Junior", "country": "ARG"},
            {"player_id": "2", "name": "Blocked", "country": "ARG"},
            {"player_id": "3", "name": "Not attempted", "country": "ARG"},
        ]
    }
    fetched = []

    def fetch(url):
        fetched.append(url)
        if "/1/" in url and "/jt/" in url:
            return '<script>var props = {"wtnSingles":14.2};</script>'
        if "/2/" in url:
            raise ITFProfileBlocked("challenge")
        return "<html>no WTN</html>"

    refresh_entry_list_wtn(
        None,
        entries,
        cache_path,
        today=date(2026, 9, 1),
        fetch_source=fetch,
        resolve_itf_player=lambda player: player,
    )

    cached = json.loads(cache_path.read_text(encoding="utf-8"))
    observation = cached["1"]["weeks"]["2026-08-31"]
    assert observation["wtn"] == 14.2
    assert "/jt/" in observation["profile_url"]
    assert not any("/3/" in url for url in fetched)


def test_current_itf_entry_wtn_is_stored_under_retrieval_week(tmp_path):
    cache_path = tmp_path / "itf_wtn_cache.json"
    entries = {
        "https://wta.example/porto": [{"player_id": "123", "name": "Same Player", "country": "POR"}],
        "w-itf-por-example": [
            {"player_id": "800123456", "name": "Same Player", "country": "POR", "wtn": "10.02"}
        ],
    }
    fetched = []

    def fetch(_url):
        fetched.append(True)
        return '<script>var props = {"wtnSingles":10.37};</script>'

    def resolve(player):
        return {**player, "player_id": "800123456"}

    refresh_entry_list_wtn(
        None,
        entries,
        cache_path,
        today=date(2026, 9, 18),
        fetch_source=fetch,
        resolve_itf_player=resolve,
        fetch_profiles=False,
        fresh_itf_entry_lists={"w-itf-por-example": entries["w-itf-por-example"]},
        tournament_weeks={
            "https://wta.example/porto": "2026-09-21",
            "w-itf-por-example": "2026-09-28",
        },
    )
    assert not fetched
    assert entries["https://wta.example/porto"][0]["wtn"] == "10.02"
    cached = json.loads(cache_path.read_text(encoding="utf-8"))["800123456"]["weeks"]
    assert cached["2026-09-14"]["source"] == "entry_list"

    refresh_entry_list_wtn(
        None,
        entries,
        cache_path,
        today=date(2026, 9, 21),
        fetch_source=fetch,
        resolve_itf_player=resolve,
        tournament_weeks={
            "https://wta.example/porto": "2026-09-21",
            "w-itf-por-example": "2026-09-28",
        },
    )
    assert fetched == [True]
    assert entries["https://wta.example/porto"][0]["wtn"] == "10.37"
    cached = json.loads(cache_path.read_text(encoding="utf-8"))["800123456"]["weeks"]
    assert cached["2026-09-21"]["source"] == "profile"


def test_previous_week_beats_temporary_cross_week_fallback(tmp_path):
    entries = {
        "https://wta.example/porto": [{"player_id": "1", "name": "Player", "country": "POR"}],
        "itf-last-week": [{"player_id": "8001", "name": "Player", "country": "POR", "wtn": "10.1"}],
        "itf-other-week": [{"player_id": "8001", "name": "Player", "country": "POR", "wtn": "10.9"}],
    }
    refresh_entry_list_wtn(
        None,
        entries,
        tmp_path / "cache.json",
        today=date(2026, 9, 18),
        fetch_profiles=False,
        fresh_itf_entry_lists=entries,
        resolve_itf_player=lambda player: {**player, "player_id": "8001"},
        tournament_weeks={
            "https://wta.example/porto": "2026-09-21",
            "itf-last-week": "2026-09-14",
            "itf-other-week": "2026-10-05",
        },
    )
    assert entries["https://wta.example/porto"][0]["wtn"] == "10.1"


def test_profile_batch_prioritizes_main_before_qualifiers_and_alternates(tmp_path):
    entries = {
        "https://wta.example/list": [
            {"player_id": "1", "name": "Qualifier", "country": "POR", "type": "QUAL"},
            {"player_id": "2", "name": "Main", "country": "POR", "type": "MAIN"},
            {"player_id": "3", "name": "Alternate", "country": "POR", "type": "ALT"},
        ]
    }
    fetched = []

    def fetch(url):
        fetched.append(url)
        return '<script>var props = {"wtnSingles":10.0};</script>'

    refresh_entry_list_wtn(
        None,
        entries,
        tmp_path / "cache.json",
        today=date(2026, 9, 18),
        fetch_source=fetch,
        resolve_itf_player=lambda player: player,
        max_profile_fetches=1,
    )
    assert len(fetched) == 1
    assert "/2/" in fetched[0]
    assert entries["https://wta.example/list"][1]["wtn"] == "10.0"
    assert entries["https://wta.example/list"][2]["wtn"] == "-"


def test_profile_batch_prioritizes_never_fetched_player_over_stale_main(tmp_path):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(
        json.dumps({"2": {"weeks": {"2026-09-07": {"wtn": 9.5}}}}),
        encoding="utf-8",
    )
    entries = {
        "https://wta.example/list": [
            {"player_id": "1", "name": "Missing Qualifier", "country": "POR", "type": "QUAL"},
            {"player_id": "2", "name": "Stale Main", "country": "POR", "type": "MAIN"},
        ]
    }
    fetched = []

    def fetch(url):
        fetched.append(url)
        return '<script>var props = {"wtnSingles":10.0};</script>'

    refresh_entry_list_wtn(
        None,
        entries,
        cache_path,
        today=date(2026, 9, 21),
        fetch_source=fetch,
        resolve_itf_player=lambda player: player,
        max_profile_fetches=1,
    )

    assert len(fetched) == 1
    assert "/1/" in fetched[0]
    assert entries["https://wta.example/list"][0]["wtn"] == "10.0"
    assert entries["https://wta.example/list"][1]["wtn"] == "9.5"


def test_legacy_profile_cache_wins_over_same_week_entry_snapshot(tmp_path):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(
        json.dumps({
            "8001": {
                "name": "Player",
                "country": "POR",
                "wtn": 9.3,
                "profile_url": "https://www.itftennis.com/player/8001",
                "retrieved_at": "2026-09-18",
            }
        }),
        encoding="utf-8",
    )
    entries = {
        "https://wta.example/list": [{"player_id": "1", "name": "Player", "country": "POR"}],
        "itf-list": [{"player_id": "8001", "name": "Player", "country": "POR", "wtn": "10.1"}],
    }
    refresh_entry_list_wtn(
        None,
        entries,
        cache_path,
        today=date(2026, 9, 18),
        fetch_profiles=False,
        fresh_itf_entry_lists={"itf-list": entries["itf-list"]},
        resolve_itf_player=lambda player: {**player, "player_id": "8001"},
    )
    assert entries["https://wta.example/list"][0]["wtn"] == "9.3"
    observation = json.loads(cache_path.read_text(encoding="utf-8"))["8001"]["weeks"]["2026-09-14"]
    assert observation["source"] == "profile"


def test_wtn_status_counts_current_old_missing_and_unmapped_rows(tmp_path):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(
        json.dumps({
            "8001": {"weeks": {"2026-09-21": {"wtn": 9.1}}},
            "8002": {"weeks": {"2026-09-14": {"wtn": 9.2}}},
            "8003": {"weeks": {"2026-10-05": {"wtn": 9.3}}},
        }),
        encoding="utf-8",
    )
    entries = {
        "https://wta.example/list": [
            {"player_id": str(number), "name": f"Player {number}", "type": "MAIN"}
            for number in range(1, 6)
        ]
    }

    def resolve(player):
        return None if player["player_id"] == "5" else {**player, "player_id": "800" + player["player_id"]}

    counts = entry_list_wtn_status(
        entries,
        cache_path,
        tournament_weeks={"https://wta.example/list": "2026-09-21"},
        resolve_itf_player=resolve,
    )
    assert counts == {
        "total": 5,
        "current_week": 1,
        "previous_week": 1,
        "other_week": 1,
        "missing": 2,
        "unmapped": 1,
    }


def test_new_entry_list_rechecks_recent_profile_and_updates_shared_rows(tmp_path):
    cache_path = tmp_path / "cache.json"
    player = {"player_id": "800533984", "name": "Julia Riera", "country": "ARG", "type": "MAIN"}
    samsun = "https://wta.example/samsun"
    curitiba = "https://wta.example/curitiba"
    entries = {samsun: [dict(player)]}
    fetched = []

    def refresh(day, value):
        return refresh_entry_list_wtn(
            None, entries, cache_path, today=day,
            resolve_itf_player=lambda row: row,
            fetch_source=lambda url: fetched.append(url) or f'<script>var props = {{"wtnSingles":{value}}};</script>',
        )

    refresh(date(2026, 10, 1), 9.86)
    refresh(date(2026, 10, 2), 10.42)
    assert len(fetched) == 1

    entries[curitiba] = [dict(player)]
    cache = refresh(date(2026, 10, 3), 10.42)
    assert len(fetched) == 2
    assert entries[samsun][0]["wtn"] == entries[curitiba][0]["wtn"] == "10.42"
    assert cache[player["player_id"]]["entry_lists_checked"][curitiba] == "2026-10-03"

    refresh(date(2026, 10, 3), 11)
    assert len(fetched) == 2


def test_new_entry_list_checks_all_sections_and_retries_blocked_player(tmp_path):
    cache_path = tmp_path / "cache.json"
    entries = {"https://wta.example/one": [
        {"player_id": str(number), "name": f"Player {number}", "country": "ARG", "type": section}
        for number, section in enumerate(("MAIN", "QUAL", "ALT"), 1)
    ]}
    # A second list shares the same players: fetch each profile only once.
    entries["https://wta.example/two"] = [dict(row) for row in entries["https://wta.example/one"]]
    fetched = []

    def fetch(url):
        fetched.append(url)
        if "/3/" in url:
            raise ITFProfileBlocked("challenge")
        return '<script>var props = {"wtnSingles":10.42};</script>'

    cache = refresh_entry_list_wtn(
        None, entries, cache_path, today=date(2026, 10, 3),
        resolve_itf_player=lambda row: row, fetch_source=fetch,
    )
    assert len(fetched) == 3
    assert "3" not in cache
    assert set(cache["1"]["entry_lists_checked"]) == set(entries)

    fetched.clear()
    cache = refresh_entry_list_wtn(
        None, entries, cache_path, today=date(2026, 10, 3),
        resolve_itf_player=lambda row: row,
        fetch_source=lambda url: fetched.append(url) or '<script>var props = {"wtnSingles":11.2};</script>',
    )
    assert len(fetched) == 1 and "/3/" in fetched[0]
    assert set(cache["3"]["entry_lists_checked"]) == set(entries)
    assert all(rows[2]["wtn"] == "11.2" for rows in entries.values())


def test_cached_itf_rows_cannot_renew_freshness_or_prevent_profile_refresh(tmp_path):
    cache_path = tmp_path / "cache.json"
    cached_record = {"entry_lists_checked": {"https://wta.example/curitiba": "2026-09-23"}, "weeks": {
        "2026-09-28": {"wtn": "9.86", "source": "entry_list", "retrieved_at": "2026-10-03"},
        "2026-09-21": {
            "wtn": "9.86", "source": "entry_list", "retrieved_at": "2026-09-23",
        },
    }}
    cache_path.write_text(json.dumps({"800533984": cached_record}), encoding="utf-8")
    player = {"player_id": "800533984", "name": "Julia Riera", "country": "ARG", "wtn": "9.86"}
    entries = {"https://wta.example/curitiba": [dict(player)], "itf-old-list": [dict(player)]}
    cache = refresh_entry_list_wtn(None, entries, cache_path, today=date(2026, 10, 3), fetch_profiles=False)
    assert cache["800533984"] == cached_record

    fetched = []
    refresh_entry_list_wtn(
        None, entries, cache_path, today=date(2026, 10, 3),
        resolve_itf_player=lambda row: row,
        fetch_source=lambda url: fetched.append(url) or '<script>var props = {"wtnSingles":10.42};</script>',
    )
    assert len(fetched) == 1
    assert entries["https://wta.example/curitiba"][0]["wtn"] == "10.42"
