import main


def test_duplicate_player_is_reported_only_once(monkeypatch):
    tournament_key = "https://www.wtatennis.com/tournaments/1000/example/2026/player-list"
    players = [
        {"name": "Repeated Player", "player_id": "123", "type": "MAIN"},
        {"name": "Repeated Player", "player_id": "123", "type": "QUAL"},
    ]
    cache_meta = {}
    reported = []

    monkeypatch.setattr(main, "get_cache_entry_meta", lambda *_args: dict(cache_meta))

    def save_meta(_cache_file, _entry_key, **meta):
        cache_meta.update(meta)

    monkeypatch.setattr(main, "set_cache_entry_meta", save_meta)
    monkeypatch.setattr(main, "report_run_issue", lambda *args, **kwargs: reported.append((args, kwargs)))

    first_cleaned = main._remove_duplicate_entry_players(players, tournament_key, "WTA Example")
    second_cleaned = main._remove_duplicate_entry_players(players, tournament_key, "WTA Example")

    assert [row["type"] for row in first_cleaned] == ["MAIN"]
    assert second_cleaned == first_cleaned
    assert cache_meta["duplicatePlayersAlerted"] == ["Repeated Player"]
    assert len(reported) == 1
