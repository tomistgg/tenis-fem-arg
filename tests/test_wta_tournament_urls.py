from datetime import date

import wta

ANTALYA_URL = (
    "https://www.wtatennis.com/tournaments/1173/"
    "antalya-125-atik/2026/player-list"
)
CINCINNATI_URL = "https://www.wtatennis.com/tournaments/1017/cincinnati/2026/player-list"


def _antalya_tournament():
    return {
        "tournamentGroup": {
            "id": 1173,
            "name": "ANTALYA 125 (ATIK)",
        },
        "year": 2026,
        "level": "WTA 125",
        "city": "ANTALYA",
        "country": "TUR",
        "surface": "Clay",
        "startDate": "2026-09-07",
        "endDate": "2026-09-13",
    }


def _cincinnati_tournament():
    return {
        "tournamentGroup": {
            "id": 1017,
            "name": "CINCINNATI",
        },
        "year": 2026,
        "level": "WTA 1000",
        "city": "CINCINNATI",
        "country": "USA",
        "surface": "Hard",
        "startDate": "2026-08-13",
        "endDate": "2026-08-23",
    }


def _wta_finals_tournament():
    return {
        "tournamentGroup": {"id": 808, "name": "WTA FINALS"},
        "year": 2026,
        "level": "Finals",
        "city": "INDIAN WELLS",
        "country": "USA",
        "surface": "Hard",
        "startDate": "2026-11-08",
        "endDate": "2026-11-15",
    }


def test_wta_parenthetical_tournament_name_uses_public_site_slug():
    clean_name, suffix, url_slug = wta._wta_tournament_name_parts("ANTALYA 125 (ATIK)")

    assert clean_name == "ANTALYA 125 (ATIK)"
    assert suffix == ""
    assert url_slug == "antalya-125-atik"


def test_wta_numbered_tournament_name_keeps_display_suffix_and_url_slug():
    clean_name, suffix, url_slug = wta._wta_tournament_name_parts("ANTALYA 125 #1")

    assert clean_name == "ANTALYA 125"
    assert suffix == " 1"
    assert url_slug == "antalya-125-1"


def test_entry_list_groups_use_normalized_parenthetical_slug(monkeypatch):
    monkeypatch.setattr(wta, "madrid_today", lambda: date(2026, 8, 12))
    monkeypatch.setattr(wta, "get_next_monday", lambda: date(2026, 8, 17))
    monkeypatch.setattr(wta, "_fetch_wta_tournaments_raw", lambda: [_antalya_tournament()])

    groups = wta.build_tournament_groups()

    assert ANTALYA_URL in groups["Week of September 7"]


def test_entry_list_groups_drop_current_week_on_tuesday(monkeypatch):
    monkeypatch.setattr(wta, "madrid_today", lambda: date(2026, 8, 18))
    monkeypatch.setattr(wta, "get_next_monday", lambda: date(2026, 8, 24))
    monkeypatch.setattr(wta, "_fetch_wta_tournaments_raw", lambda: [_cincinnati_tournament()])

    groups = wta.build_tournament_groups()

    assert groups == {}


def test_draw_groups_use_normalized_parenthetical_slug(monkeypatch):
    monkeypatch.setattr(wta, "madrid_today", lambda: date(2026, 9, 1))
    monkeypatch.setattr(wta, "_fetch_wta_tournaments_raw", lambda: [_antalya_tournament()])

    groups = wta.get_draws_tournament_list()

    assert ANTALYA_URL in groups["Week of September 7"]


def test_draw_groups_keep_ongoing_tournament_from_previous_week(monkeypatch):
    monkeypatch.setattr(wta, "madrid_today", lambda: date(2026, 8, 17))
    monkeypatch.setattr(wta, "_fetch_wta_tournaments_raw", lambda: [_cincinnati_tournament()])

    groups = wta.get_draws_tournament_list()

    assert CINCINNATI_URL in groups["Week of August 10"]


def test_draw_groups_drop_tournament_after_end_date(monkeypatch):
    monkeypatch.setattr(wta, "madrid_today", lambda: date(2026, 8, 24))
    monkeypatch.setattr(wta, "_fetch_wta_tournaments_raw", lambda: [_cincinnati_tournament()])

    groups = wta.get_draws_tournament_list()

    assert groups == {}
