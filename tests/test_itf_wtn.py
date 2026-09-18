import json
from datetime import date

from itf_wtn import ITFProfileBlocked, entry_list_wtn_status, parse_wtn_singles, refresh_entry_list_wtn


def test_parse_wtn_singles_from_profile_props():
    source = '''<script>var props = {"visible":true,"wtnSingles":9.3,"wtnDoubles":11.69};</script>'''
    assert parse_wtn_singles(source) == 9.3


def test_profile_wtn_cache_is_shared_and_refetched_in_a_new_ranking_week(tmp_path):
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
    cached = json.loads(cache_path.read_text(encoding="utf-8"))["800389685"]["weeks"]
    assert cached["2026-09-07"]["wtn"] == 9.3
    assert cached["2026-09-07"]["retrieved_at"] == "2026-09-07"


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
    assert not fetched
    assert entries["https://wta.example/porto"][0]["wtn"] == "10.02"
    cached = json.loads(cache_path.read_text(encoding="utf-8"))["800123456"]["weeks"]
    assert cached["2026-09-21"]["source"] == "entry_list"


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
        resolve_itf_player=lambda player: {**player, "player_id": "8001"},
        tournament_weeks={
            "https://wta.example/porto": "2026-09-21",
            "itf-last-week": "2026-09-14",
            "itf-other-week": "2026-10-05",
        },
    )
    assert entries["https://wta.example/porto"][0]["wtn"] == "10.1"


def test_profile_batch_prioritizes_main_and_excludes_alternates(tmp_path):
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
