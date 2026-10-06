import json
from datetime import date

import pytest

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


@pytest.mark.parametrize("robots", ["NOINDEX, NOFOLLOW", "noindex,nofollow", "noindex, nofollow"])
def test_short_challenge_page_is_recognized_before_parsing(robots):
    class Driver:
        page_source = f'<html><meta name="robots" content="{robots}"></html>'

        def get(self, url):
            pass

    with pytest.raises(ITFProfileBlocked):
        itf_wtn._profile_source(Driver(), "https://www.itftennis.com/profile", 0)


def test_verified_womens_profile_does_not_fall_back_to_juniors():
    preferred = "https://www.itftennis.com/en/players/yexin-ma/800439388/chn/wt/s/overview/"

    urls = player_profile_urls(
        {"player_id": "800439388", "name": "Ye-Xin MA", "country": "CHN"},
        preferred,
    )

    assert urls == [preferred]


def test_profile_wtn_cache_is_shared_and_refetched_after_five_days(tmp_path):
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
    assert len(fetched) == 2

    refresh_entry_list_wtn(
        None, entries, cache_path, today=date(2026, 9, 9), fetch_source=fetch, resolve_itf_player=resolve
    )
    assert len(fetched) == 2
    cached = json.loads(cache_path.read_text(encoding="utf-8"))["800389685"]
    assert cached["wtn"] == 9.3
    assert cached["retrieved_at"] == "2026-09-07"
    assert "weeks" not in cached


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

    fetched = []
    refresh_entry_list_wtn(
        None,
        entries,
        cache_path,
        today=date(2026, 9, 26),
        fetch_source=lambda url: fetched.append(url) or '<script>var props = {"wtnSingles":12.8};</script>',
        resolve_itf_player=lambda _player: None,
    )

    assert fetched == []
    assert entries["https://wta.example/list"][0]["wtn"] == "13.03"


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


def test_live_profile_refresh_waits_before_first_request(tmp_path, monkeypatch):
    events = []

    class Driver:
        page_source = 'var props = {"wtnSingles":10};'

        def get(self, url):
            events.append(("get", url))

        def get_cookies(self):
            return []

        def execute_script(self, _script):
            return "test-agent"

    monkeypatch.setattr(itf_wtn.time, "sleep", lambda seconds: events.append(("sleep", seconds)))
    refresh_entry_list_wtn(
        Driver(), {"itf-current": [{"player_id": "8001", "name": "Player", "country": "ARG"}]},
        tmp_path / "cache.json", today=date(2026, 10, 5), include_itf_entry_players=True,
        profile_batch_cooldown_seconds=61, settle_seconds=0,
    )

    assert events[0] == ("sleep", 61)
    assert events[1][0] == "get"


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


def test_profile_without_singles_wtn_waits_five_days_before_retry(tmp_path):
    cache_path = tmp_path / "itf_wtn_cache.json"
    player = {"player_id": "800000001", "name": "New Player", "country": "ARG"}
    entries = {"https://wta.example/one": [player]}
    fetched = []

    refresh_entry_list_wtn(None, entries, cache_path, today=date(2026, 10, 4),
                           fetch_source=lambda url: fetched.append(url) or 'var props = {"wtnSingles":null};',
                           resolve_itf_player=lambda row: row)
    assert len(fetched) == 1
    assert player["wtn"] == "-"
    cached = json.loads(cache_path.read_text())
    assert cached["800000001"]["no_wtn_checked_at"] == "2026-10-04"
    cached["800000001"]["weeks"] = {"2026-09-21": {"wtn": 15, "source": "profile"}}
    cache_path.write_text(json.dumps(cached))

    refresh_entry_list_wtn(None, entries, cache_path, today=date(2026, 10, 9),
                           fetch_source=lambda _: pytest.fail("No-WTN profile should be cached"),
                           resolve_itf_player=lambda row: row)
    assert player["wtn"] == "-"
    draws = {"w-itf-active": {"endDate": "2026-10-20", "draws": {"QS": {"players": [dict(player)]}}}}
    itf_wtn.refresh_draw_wtn(None, draws, cache_path, today=date(2026, 10, 9),
                             fetch_source=lambda _: pytest.fail("Qualifying draw must reuse no-WTN check"))
    assert draws["w-itf-active"]["draws"]["QS"]["players"][0]["wtn"] == "-"

    refresh_entry_list_wtn(None, entries, cache_path, today=date(2026, 10, 10),
                           fetch_source=lambda url: fetched.append(url) or 'var props = {"wtnSingles":12};',
                           resolve_itf_player=lambda row: row)
    assert len(fetched) == 2
    assert player["wtn"] == "12.0"
    assert "no_wtn_checked_at" not in json.loads(cache_path.read_text())["800000001"]


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
    observation = cached["1"]
    assert observation["wtn"] == 14.2
    assert "/jt/" in observation["profile_url"]
    assert not any("/3/" in url for url in fetched)


def test_first_itf_entry_wtn_uses_publication_date_and_profile_updates_every_list(tmp_path):
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
    cached = json.loads(cache_path.read_text(encoding="utf-8"))["800123456"]
    assert cached["source"] == "entry_list"
    assert cached["observed_on"] == "2026-09-11"

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
    cached = json.loads(cache_path.read_text(encoding="utf-8"))["800123456"]
    assert cached["source"] == "profile"
    assert entries["w-itf-por-example"][0]["wtn"] == "10.37"


def test_latest_list_publication_updates_all_older_lists(tmp_path):
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
    assert all(rows[0]["wtn"] == "10.9" for rows in entries.values())


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
    observation = json.loads(cache_path.read_text(encoding="utf-8"))["8001"]
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


def test_new_entry_list_reuses_recent_wtn_and_updates_shared_rows(tmp_path):
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
    assert len(fetched) == 1
    assert entries[samsun][0]["wtn"] == entries[curitiba][0]["wtn"] == "9.86"
    assert curitiba not in cache[player["player_id"]]["entry_lists_checked"]

    refresh(date(2026, 10, 3), 11)
    assert len(fetched) == 1


def test_new_entry_list_checks_main_and_qual_and_retries_blocked_player(tmp_path):
    cache_path = tmp_path / "cache.json"
    entries = {"https://wta.example/one": [
        {"player_id": str(number), "name": f"Player {number}", "country": "ARG", "type": section}
        for number, section in enumerate(("MAIN", "QUAL", "QUAL", "ALT"), 1)
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
    assert "4" not in cache
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
    assert all(rows[3]["wtn"] == "-" for rows in entries.values())


def test_alt_uses_entry_list_wtn_until_singles_draw(tmp_path):
    path = tmp_path / "cache.json"
    key = "w-itf-bul-2026-007"
    alt = {"player_id": "8001", "name": "Alternate", "country": "BUL", "type": "ALT", "wtn": "22"}
    entries = {key: [alt]}
    fetched = []

    cache = refresh_entry_list_wtn(
        None, entries, path, today=date(2026, 10, 4),
        fresh_itf_entry_lists={key: [dict(alt)]}, tournament_weeks={key: "2026-10-05"},
        include_itf_entry_players=True,
        fetch_source=lambda url: fetched.append(url) or 'var props = {"wtnSingles":19};',
    )
    assert fetched == []
    assert cache["8001"]["source"] == "entry_list"
    assert entries[key][0]["wtn"] == "22"

    draws = {key: {"endDate": "2026-10-11", "draws": {
        "QS": {"players": [{"player_id": "8001", "name": "Alternate", "country": "BUL"}]},
    }}}
    cache = itf_wtn.refresh_draw_wtn(
        None, draws, path, entry_cache=entries, include_entry_players=True,
        today=date(2026, 10, 4), tournament_weeks={key: "2026-10-05"},
        fetch_source=lambda url: fetched.append(url) or 'var props = {"wtnSingles":19};',
    )
    assert len(fetched) == 1
    assert cache["8001"]["source"] == "profile"
    assert draws[key]["draws"]["QS"]["players"][0]["wtn"] == "19.0"


def test_entry_placeholders_and_alts_do_not_report_missing_profiles(tmp_path, monkeypatch):
    issues = []
    monkeypatch.setattr("run_state.report_run_issue", lambda *args, **kwargs: issues.append(kwargs))
    key = "w-itf-bul-2026-007"
    entries = {key: [
        {"name": "(Available Slot)", "type": "MAIN"},
        {"name": "(Special Exempt)", "type": "MAIN"},
        {"name": "Unmapped Alternate", "type": "ALT"},
    ]}
    itf_wtn.refresh_draw_wtn(
        None, {}, tmp_path / "cache.json", entry_cache=entries,
        include_entry_players=True, today=date(2026, 10, 4),
        tournament_weeks={key: "2026-10-05"},
        fetch_source=lambda url: pytest.fail(f"Unexpected profile request: {url}"),
    )
    assert issues == []


def test_cached_itf_rows_cannot_renew_freshness_or_prevent_profile_refresh(tmp_path):
    cache_path = tmp_path / "cache.json"
    cached_record = {"entry_lists_checked": {"https://wta.example/curitiba": "2026-09-23"}, "weeks": {
        "2026-09-28": {"wtn": "9.86", "source": "entry_list", "observed_on": "2026-09-21",
                       "retrieved_at": "2026-10-03"},
        "2026-09-21": {
            "wtn": "9.86", "source": "entry_list", "retrieved_at": "2026-09-23",
        },
    }}
    cache_path.write_text(json.dumps({"800533984": cached_record}), encoding="utf-8")
    player = {"player_id": "800533984", "name": "Julia Riera", "country": "ARG", "wtn": "9.86"}
    entries = {"https://wta.example/curitiba": [dict(player)], "itf-old-list": [dict(player)]}
    cache = refresh_entry_list_wtn(None, entries, cache_path, today=date(2026, 10, 3), fetch_profiles=False)
    assert cache["800533984"] == itf_wtn._normalize_cache({"800533984": cached_record})["800533984"]

    fetched = []
    refresh_entry_list_wtn(
        None, entries, cache_path, today=date(2026, 10, 3),
        resolve_itf_player=lambda row: row,
        fetch_source=lambda url: fetched.append(url) or '<script>var props = {"wtnSingles":10.42};</script>',
    )
    assert len(fetched) == 1
    assert entries["https://wta.example/curitiba"][0]["wtn"] == "10.42"


def test_itf_list_is_consumed_once_and_newer_list_updates_older_rows(tmp_path):
    path = tmp_path / "cache.json"
    old, new = "w-itf-old", "w-itf-new"
    entries = {old: [{"player_id": "8001", "name": "Player", "country": "POR", "wtn": "12"}]}
    starts = {old: "2026-09-28", new: "2026-10-12"}
    refresh_entry_list_wtn(None, entries, path, today=date(2026, 9, 18), fetch_profiles=False,
                           fresh_itf_entry_lists={old: [dict(entries[old][0])]}, tournament_weeks=starts)
    entries[new] = [{**entries[old][0], "wtn": "11"}]
    cache = refresh_entry_list_wtn(None, entries, path, today=date(2026, 9, 25), fetch_profiles=False,
                                   fresh_itf_entry_lists={new: [dict(entries[new][0])]}, tournament_weeks=starts)
    assert cache["8001"]["observed_on"] == "2026-09-25"
    assert all(rows[0]["wtn"] == "11" for rows in entries.values())
    reread = [{**entries[old][0], "wtn": "7"}, {"player_id": "8002", "wtn": "8"}]
    cache = refresh_entry_list_wtn(None, entries, path, today=date(2026, 10, 2), fetch_profiles=False,
                                   fresh_itf_entry_lists={old: reread}, tournament_weeks=starts)
    assert cache["8001"]["wtn"] == "11" and cache["8001"]["retrieved_at"] == "2026-09-25"
    assert "8002" not in cache
    assert all(rows[0]["wtn"] == "11" for rows in entries.values())


def test_first_discovery_of_old_list_cannot_replace_newer_profile(tmp_path):
    path = tmp_path / "cache.json"
    original = {"8001": {"name": "Player", "wtn": 9.3, "source": "profile", "retrieved_at": "2026-09-26"}}
    path.write_text(json.dumps(original))
    entries = {"w-itf-old": [{"player_id": "8001", "wtn": "20"}]}
    cache = refresh_entry_list_wtn(None, entries, path, today=date(2026, 9, 30), fetch_profiles=False,
                                   fresh_itf_entry_lists=entries, tournament_weeks={"w-itf-old": "2026-09-28"})
    assert cache["8001"]["source"] == "profile" and cache["8001"]["retrieved_at"] == "2026-09-26"
    assert entries["w-itf-old"][0]["wtn"] == "9.3"


def test_weekly_migration_keeps_latest_effective_date_and_retry_metadata():
    old = {"8001": {"name": "Player", "entry_lists_checked": {"one": "2026-09-26"},
                    "main_draw_observations": {"draw": {"wtn": 8, "source": "profile"}},
                    "weeks": {
                        "2026-10-05": {"wtn": 20, "source": "entry_list", "observed_on": "2026-09-18",
                                       "retrieved_at": "2026-09-30"},
                        "2026-09-21": {"wtn": 9, "source": "profile", "retrieved_at": "2026-09-26"},
                    }}}
    cache = itf_wtn._normalize_cache(old)
    assert cache["8001"]["wtn"] == 9 and "weeks" not in cache["8001"]
    assert cache["8001"]["main_draw_observations"] == old["8001"]["main_draw_observations"]
    assert cache["8001"]["entry_lists_checked"] == old["8001"]["entry_lists_checked"]
    assert itf_wtn._normalize_cache(cache) == cache


def test_latest_profile_propagates_to_every_entry_and_draw_without_changing_gm():
    entries = {"w-itf-old": [{"player_id": "8001", "wtn": "20"}],
               "https://wta.example/new": [{"itf_id": "8001", "name": "Alice", "wtn": "20"}]}
    cache = {"8001": {"wtn": 9.3, "source": "profile", "retrieved_at": "2026-09-26"}}
    draws = {"w-itf-old": {"draws": {
        "MDS": {"wtn_gm": 20, "players": [{"itf_id": "8001", "name": "Alice", "wtn": "20"}]},
        "MDD": {"players": [{"members": [{"itf_id": "8001", "name": "Alice"}]}]},
    }}}
    itf_wtn.propagate_wtn(entries, cache, draws)
    assert all(rows[0]["wtn"] == "9.3" for rows in entries.values())
    assert draws["w-itf-old"]["draws"]["MDS"]["players"][0]["wtn"] == "9.3"
    assert draws["w-itf-old"]["draws"]["MDD"]["players"][0]["members"][0]["wtn"] == "9.3"
    assert draws["w-itf-old"]["draws"]["MDS"]["wtn_gm"] == 20


@pytest.mark.parametrize("source", ["profile", "entry_list"])
def test_active_singles_refresh_stale_players_without_entry_lists_and_deduplicate(tmp_path, source):
    path = tmp_path / "cache.json"
    record = {"wtn": 20, "source": source, "retrieved_at": "2026-10-04",
              "observed_on": "2026-09-26", "name": "Alice", "country": "ESP",
              "main_draw_observations": {"w-itf-active": {"wtn": 20, "source": "profile"}}}
    path.write_text(json.dumps({"8001": record}), encoding="utf-8")
    player = {"player_id": "8001", "name": "Alice", "country": "ESP", "wtn": "20"}
    draws = {"w-itf-active": {"endDate": "2026-10-04", "draws": {
        "MDS": {"players": [dict(player)], "wtn_gm": 20},
        "QS": {"players": [dict(player), {"name": "Qualifier"}, {"name": "Bye"}]},
    }}}
    fetched = []
    entries = {"w-itf-old": [dict(player)]}
    cache = itf_wtn.refresh_draw_wtn(
        None, draws, path, entry_cache=entries, today=date(2026, 10, 4), profile_batch_size=0,
        fetch_source=lambda url: fetched.append(url) or '<script>var props = {"wtnSingles":9.3};</script>',
    )
    assert len(fetched) == 1
    assert cache["8001"]["retrieved_at"] == "2026-10-04"
    assert cache["8001"]["main_draw_observations"] == record["main_draw_observations"]
    assert draws["w-itf-active"]["draws"]["MDS"]["players"][0]["wtn"] == "9.3"
    assert draws["w-itf-active"]["draws"]["QS"]["players"][0]["wtn"] == "9.3"
    assert entries["w-itf-old"][0]["wtn"] == "9.3"
    assert draws["w-itf-active"]["draws"]["MDS"]["wtn_gm"] == 20


def test_draw_freshness_uses_observation_date_and_skips_doubles_and_finished_events(tmp_path):
    path = tmp_path / "cache.json"
    cache = {
        "8001": {"wtn": 10, "source": "profile", "retrieved_at": "2026-09-29"},
        "8002": {"wtn": 11, "source": "entry_list", "observed_on": "2026-10-02"},
        "8003": {"wtn": 20, "source": "profile", "retrieved_at": "2026-09-01"},
    }
    path.write_text(json.dumps(cache), encoding="utf-8")
    def player(pid):
        return {"player_id": pid, "name": f"Player {pid}", "country": "ESP"}
    draws = {
        "w-itf-active": {"endDate": "2026-10-11", "draws": {
            "MDS": {"players": [player("8001"), player("8002")]},
            "MDD": {"players": [{"members": [player("8003")]}]},
        }},
        "w-itf-finished": {"endDate": "2026-10-03", "draws": {"MDS": {"players": [player("8003")]}}},
    }
    def unexpected_fetch(url):
        pytest.fail(f"Unexpected profile request: {url}")
    itf_wtn.refresh_draw_wtn(None, draws, path, today=date(2026, 10, 4), fetch_source=unexpected_fetch)
    assert draws["w-itf-active"]["draws"]["MDS"]["players"][0]["wtn"] == "10"
    assert draws["w-itf-active"]["draws"]["MDS"]["players"][1]["wtn"] == "11"


def test_blocked_draw_refresh_hides_old_value_and_retries_on_next_run(tmp_path, monkeypatch):
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({"8001": {"wtn": 20, "source": "profile", "retrieved_at": "2026-09-26"}}))
    draws = {"w-itf-active": {"endDate": "2026-10-11", "draws": {
        "MDS": {"wtn_gm": 20, "players": [{"player_id": "8001", "name": "Alice", "country": "ESP"}]}
    }}}
    issues = []
    monkeypatch.setattr("run_state.report_run_issue", lambda *args, **kwargs: issues.append(kwargs))
    def blocked(url):
        raise ITFProfileBlocked("challenge")
    cache = itf_wtn.refresh_draw_wtn(None, draws, path, today=date(2026, 10, 4), fetch_source=blocked)
    assert cache["8001"]["wtn"] == 20
    assert cache["8001"]["retrieved_at"] == "2026-09-26"
    assert draws["w-itf-active"]["draws"]["MDS"]["players"][0]["wtn"] == "-"
    assert issues[0]["context"]["players"][0]["itf_id"] == "8001"
    # Final persistence must not reintroduce a stale value after a blocked refresh.
    itf_wtn.propagate_wtn({}, cache, draws, today=date(2026, 10, 4), fresh_singles_only=True)
    assert draws["w-itf-active"]["draws"]["MDS"]["players"][0]["wtn"] == "-"
    cache = itf_wtn.refresh_draw_wtn(
        None, draws, path, today=date(2026, 10, 4),
        fetch_source=lambda url: '<script>var props = {"wtnSingles":9.3};</script>',
    )
    assert draws["w-itf-active"]["draws"]["MDS"]["players"][0]["wtn"] == "9.3"
    assert draws["w-itf-active"]["draws"]["MDS"]["wtn_gm"] == 20


def test_draw_only_wta_player_resolves_and_missing_wtn_is_fetched(tmp_path):
    path = tmp_path / "cache.json"
    draws = {"https://wta.example/active": {"draws": {
        "QS": {"players": [{"player_id": "123", "name": "Alice", "country": "ESP"}]}
    }}}
    fetched = []
    itf_wtn.refresh_draw_wtn(
        None, draws, path, today=date(2026, 10, 4),
        resolve_itf_player=lambda player: {**player, "player_id": "8001"},
        fetch_source=lambda url: fetched.append(url) or '<script>var props = {"wtnSingles":9.3};</script>',
    )
    assert len(fetched) == 1 and "/8001/" in fetched[0]
    assert draws["https://wta.example/active"]["draws"]["QS"]["players"][0]["wtn"] == "9.3"


def test_unidentified_singles_profile_records_player_and_tournament_for_email(tmp_path, monkeypatch):
    from run_state import initialize_run_state, load_run_state

    status_path = tmp_path / "run_status.json"
    initialize_run_state(status_path, "test-run", tmp_path)
    monkeypatch.setenv("WTARG_RUN_STATUS_PATH", str(status_path))
    draws = {"https://wta.example/active": {"name": "WTA 125 Example", "draws": {
        "QS": {"players": [
            {"player_id": "123", "name": "Unknown Player", "country": "ESP", "wtn": "20"},
            {"name": "Qualifier"},
        ]},
    }}}
    def unexpected_fetch(url):
        pytest.fail("Unidentified profile must not trigger a guessed lookup")
    itf_wtn.refresh_draw_wtn(
        None, draws, tmp_path / "cache.json", today=date(2026, 10, 4),
        resolve_itf_player=lambda player: None, fetch_source=unexpected_fetch,
    )
    state = load_run_state(status_path)
    assert state["status"] == "degraded"
    players = state["issues"][0]["context"]["players"]
    assert len(players) == 1
    assert players[0]["name"] == "Unknown Player"
    assert players[0]["tournament_name"] == "WTA 125 Example"
    assert players[0]["draw"] == "QS"
    assert "profile not identified" in players[0]["reason"]
    assert draws["https://wta.example/active"]["draws"]["QS"]["players"][0]["wtn"] == "-"


def test_combined_entry_queue_uses_current_lists_and_keeps_fresh_first_list_values(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({"8002": {"wtn": 9, "source": "entry_list", "observed_on": "2026-10-02"}}))
    entries = {
        "w-itf-current": [{"player_id": "8001", "name": "Alice", "country": "USA"}],
        "w-itf-current#qual": [{"player_id": "8002", "name": "Bea", "country": "USA"}],
        "w-itf-old": [{"player_id": "8003", "name": "Past list only", "country": "USA"}],
    }
    calls = []
    itf_wtn.refresh_draw_wtn(
        None, {}, path, entry_cache=entries, include_entry_players=True, today=date(2026, 10, 4),
        tournament_weeks={"w-itf-current": "2026-10-05", "w-itf-old": "2026-09-21"},
        fetch_source=lambda url: calls.append(url) or 'var props = {"wtnSingles":16};',
    )
    assert len(calls) == 1 and "/8001/" in calls[0]
    assert entries["w-itf-current"][0]["wtn"] == "16.0"
    assert entries["w-itf-current#qual"][0]["wtn"] == "9"


def test_success_is_checkpointed_before_the_next_profile_can_block(tmp_path):
    path = tmp_path / "cache.json"
    entries = {"https://wta.example/active": [
        {"player_id": "8001", "name": "Alice", "country": "USA", "type": "MAIN"},
        {"player_id": "8002", "name": "Bea", "country": "USA", "type": "MAIN"},
    ]}

    def fetch(url):
        if "/8001/" in url:
            return 'var props = {"wtnSingles":9};'
        assert json.loads(path.read_text())["8001"]["wtn"] == 9
        raise ITFProfileBlocked("HTTP 403")

    itf_wtn.refresh_entry_list_wtn(
        None, entries, path, today=date(2026, 10, 4), fetch_source=fetch,
        resolve_itf_player=lambda player: player, profile_batch_size=100,
    )
    assert json.loads(path.read_text())["8001"]["wtn"] == 9
