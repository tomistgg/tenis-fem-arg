from datetime import date

import draws
import tstrength
import wta
from populate_data import tournament_sizes_update, wta_load_new


class _MatchesResponse:
    def json(self):
        return {"matches": []}


def test_wta_results_loader_requests_completed_matches_only(monkeypatch):
    requested = []
    monkeypatch.setattr(wta_load_new, "fetch_json", lambda url: requested.append(url) or {"matches": []})

    wta_load_new.fetch_matches(1017, 2026)

    assert requested == ["https://api.wtatennis.com/tennis/tournaments/1017/2026/matches?states=C"]


def test_wta_draw_size_requests_completed_matches_only(monkeypatch):
    requested = []

    def fake_get(url, **_kwargs):
        requested.append(url)
        return _MatchesResponse()

    monkeypatch.setattr(tournament_sizes_update, "get_with_retry", fake_get)

    tournament_sizes_update.wta_count_qualifying_players(1166, 2026)

    assert requested == ["https://api.wtatennis.com/tennis/tournaments/1166/2026/matches?states=C"]


def test_tournament_strength_requests_completed_matches_only(monkeypatch):
    requested = []

    def fake_get(url, **_kwargs):
        requested.append(url)
        return _MatchesResponse()

    monkeypatch.setattr(tstrength, "get_with_retry", fake_get)

    tstrength._fetch_tournament_matches(1017, 2026)

    assert requested == ["https://api.wtatennis.com/tennis/tournaments/1017/2026/matches?states=C"]


def test_wta_player_metadata_lookup_requests_completed_matches_only(monkeypatch):
    requested = []

    class _PlayerResponse:
        def json(self):
            return {"player": {"fullName": "Test Player", "countryCode": "ARG"}}

    def fake_get(url, **kwargs):
        requested.append((url, kwargs["params"]))
        return _PlayerResponse()

    monkeypatch.setattr(wta, "get_with_retry", fake_get)

    assert wta.fetch_player_info(123) == {"name": "Test Player", "country": "ARG"}
    assert requested == [
        (
            "https://api.wtatennis.com/tennis/players/123/matches",
            {"page": 0, "pageSize": 1, "sort": "desc", "states": "C"},
        )
    ]


def test_wta_draw_more_than_two_days_away_skips_pdfs_and_matches_api(monkeypatch):
    pdf_requests = []
    api_requests = []
    monkeypatch.setattr(
        draws,
        "fetch_draw_pdf_bytes",
        lambda tournament_id, year, draw_type: pdf_requests.append((tournament_id, year, draw_type)) or None,
    )
    monkeypatch.setattr(
        draws,
        "_fetch_draw_from_wta_api",
        lambda tournament_id, year: api_requests.append((tournament_id, year)) or None,
    )
    monkeypatch.setattr(draws, "madrid_today", lambda: date(2026, 8, 19))

    result = draws.fetch_tournament_draws(
        "https://www.wtatennis.com/tournaments/1039/monterrey/2026/player-list",
        2026,
        start_date="2026-08-23",
    )

    assert result == {}
    assert pdf_requests == []
    assert api_requests == []


def test_wta_draw_two_days_away_checks_pdfs_without_calling_matches_api(monkeypatch):
    pdf_requests = []
    api_requests = []
    monkeypatch.setattr(
        draws,
        "fetch_draw_pdf_bytes",
        lambda tournament_id, year, draw_type: pdf_requests.append((tournament_id, year, draw_type)) or None,
    )
    monkeypatch.setattr(
        draws,
        "_fetch_draw_from_wta_api",
        lambda tournament_id, year: api_requests.append((tournament_id, year)) or None,
    )
    monkeypatch.setattr(draws, "madrid_today", lambda: date(2026, 8, 21))

    result = draws.fetch_tournament_draws(
        "https://www.wtatennis.com/tournaments/1039/monterrey/2026/player-list",
        2026,
        start_date="2026-08-23",
    )

    assert result == {}
    assert pdf_requests == [("1039", 2026, "MDS"), ("1039", 2026, "QS")]
    assert api_requests == []


def test_started_wta_draw_can_use_completed_matches_fallback(monkeypatch):
    api_draw = {"players": [{"name": "Player"}], "matches": [], "draw_size": 2}
    api_requests = []
    monkeypatch.setattr(draws, "fetch_draw_pdf_bytes", lambda *_args: None)
    monkeypatch.setattr(
        draws,
        "_fetch_draw_from_wta_api",
        lambda tournament_id, year: api_requests.append((tournament_id, year)) or api_draw,
    )
    monkeypatch.setattr(draws, "madrid_today", lambda: date(2026, 8, 19))

    result = draws.fetch_tournament_draws(
        "https://www.wtatennis.com/tournaments/1017/cincinnati/2026/player-list",
        2026,
        start_date="2026-08-13",
    )

    assert result["MDS"] == api_draw
    assert api_requests == [("1017", 2026)]
