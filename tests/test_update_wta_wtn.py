from datetime import date
from types import SimpleNamespace

import pytest

from populate_data import update_wta_wtn as cli


@pytest.fixture
def maintenance_data(monkeypatch):
    state = SimpleNamespace(
        entries={"https://wta.example/list": [{"player_id": "1", "name": "Alice", "wtn": "12"}]},
        weeks={"https://wta.example/list": "2026-10-19"},
        draws={"w-itf-current": {"meta": {}, "draws": {
            "MDS": {"players": [{"itf_id": "8001", "name": "Alice", "wtn": "12"}]},
            "QS": {"players": [{"itf_id": "8001", "name": "Alice", "wtn": "12"}]},
            "MDD": {"players": [{"members": [{"itf_id": "8002", "name": "Bea", "wtn": "20"}]}]},
        }}},
        history={"w-itf-previous": {"startDate": "2026-10-05", "draws": {}}},
        saved=[], loaded=[], browser_events=[], status=[],
    )
    state.driver = SimpleNamespace(quit=lambda: state.browser_events.append("quit"))
    monkeypatch.setattr(cli, "load_current_data", lambda: (state.entries, state.weeks))
    monkeypatch.setattr(cli, "load_cache", lambda path: state.loaded.append(path) or {
        cli.DRAWS_STORE_FILE: state.draws, cli.WTN_DRAW_HISTORY_FILE: state.history,
    }[path])
    monkeypatch.setattr(
        cli, "LazyBrowserSession", lambda factory: state.browser_events.append("create") or state.driver
    )
    monkeypatch.setattr(cli, "madrid_today", lambda: date(2026, 10, 16))
    monkeypatch.setattr(cli, "save_cache", lambda path, data, **kwargs: state.saved.append((path, data)))
    monkeypatch.setattr(cli, "print_status", lambda entries, weeks: state.status.append((entries, weeks)))
    monkeypatch.setattr(cli, "refresh_entry_list_wtn", lambda *args, **kwargs: pytest.fail("Unexpected entry refresh"))
    monkeypatch.setattr(cli, "refresh_draw_wtn", lambda *args, **kwargs: pytest.fail("Unexpected draw refresh"))
    return state


def test_draws_cli_uses_shared_queue_and_counts_changed_singles_rows(monkeypatch, capsys, maintenance_data):
    state = maintenance_data
    monkeypatch.setattr(cli.sys, "argv", ["update_wta_wtn.py", "--draws", "--limit", "2"])

    def refresh(driver, draws, path, **options):
        assert driver is state.driver
        assert path == cli.WTN_CACHE_FILE
        assert options["entry_cache"] is state.entries
        assert options["today"] == date(2026, 10, 16)
        assert options["max_profile_fetches"] == 2
        assert options["require_previous_week_draw"] is True
        assert not options.get("include_entry_players", False)
        assert dict(options["recent_draws"]) == state.history
        for kind in ("MDS", "QS"):
            draws["w-itf-current"]["draws"][kind]["players"][0]["wtn"] = "8"
        draws["w-itf-current"]["draws"]["MDD"]["players"][0]["members"][0]["wtn"] = "9"
        state.entries["https://wta.example/list"][0]["wtn"] = "8"

    monkeypatch.setattr(cli, "refresh_draw_wtn", refresh)

    cli.main()

    assert "Draw WTN rows updated: 2" in capsys.readouterr().out
    assert [path for path, _ in state.saved] == [cli.ENTRY_LISTS_CACHE_FILE, cli.DRAWS_STORE_FILE]
    assert state.saved[0][1]["https://wta.example/list"][0]["wtn"] == "8"
    assert state.saved[1][1]["w-itf-current"]["draws"]["QS"]["players"][0]["wtn"] == "8"
    assert state.browser_events == ["create", "quit"]


def test_entry_cli_keeps_shared_entry_refresh_and_default_limit(monkeypatch, maintenance_data):
    state = maintenance_data
    monkeypatch.setattr(cli.sys, "argv", ["update_wta_wtn.py"])
    calls = []
    monkeypatch.setattr(cli, "refresh_entry_list_wtn", lambda *args, **kwargs: calls.append((args, kwargs)))

    cli.main()

    args, options = calls[0]
    assert args == (state.driver, state.entries, cli.WTN_CACHE_FILE)
    assert options["tournament_weeks"] is state.weeks
    assert options["max_profile_fetches"] == 8
    recent = dict(options["recent_draws"])
    assert set(recent) == set(state.draws) | set(state.history)
    assert recent["w-itf-previous"] == state.history["w-itf-previous"]
    assert recent["w-itf-current"]["draws"]["MDS"]["players"][0]["itf_id"] == "8001"
    assert [path for path, _ in state.saved] == [cli.ENTRY_LISTS_CACHE_FILE]
    assert state.browser_events == ["create", "quit"]


def test_status_cli_does_not_refresh_or_write(monkeypatch, maintenance_data):
    state = maintenance_data
    monkeypatch.setattr(cli.sys, "argv", ["update_wta_wtn.py", "--status", "--draws"])

    cli.main()

    assert state.status == [(state.entries, state.weeks)]
    assert state.loaded == state.saved == state.browser_events == []


def test_failed_draw_refresh_closes_browser_without_saving_caches(monkeypatch, maintenance_data):
    state = maintenance_data
    monkeypatch.setattr(cli.sys, "argv", ["update_wta_wtn.py", "--draws"])

    def fail(*args, **kwargs):
        raise RuntimeError("Refresh failed")

    monkeypatch.setattr(cli, "refresh_draw_wtn", fail)

    with pytest.raises(RuntimeError, match="Refresh failed"):
        cli.main()

    assert state.browser_events == ["create", "quit"]
    assert state.saved == []
