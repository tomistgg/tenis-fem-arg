import json
from datetime import date
from pathlib import Path

from populate_data import itf_load_new
from utils import (
    _POINTS_DISTRIBUTION_FIELDS,
    compress_calendar_snapshot,
    compress_draws_snapshot,
    compress_draws_store_cache,
    compress_entry_lists_cache,
    compress_itf_calendar_cache,
    compress_itf_drawsheet_cache,
    compress_itf_rankings_cache,
    compress_points_distribution,
    compress_tournament_draw_sizes,
    compress_tournament_snapshot,
    compress_tstrength_cache,
    compress_wta_calendar_cache,
    expand_calendar_snapshot,
    expand_draws_snapshot,
    expand_draws_store_cache,
    expand_entry_lists_cache,
    expand_itf_calendar_cache,
    expand_itf_drawsheet_cache,
    expand_itf_rankings_cache,
    expand_points_distribution,
    expand_tournament_draw_sizes,
    expand_tournament_snapshot,
    expand_tstrength_cache,
    expand_wta_calendar_cache,
)


FIXTURES = Path(__file__).parent / "fixtures"


def assert_round_trip(payload, compress, expand):
    assert expand(compress(payload)) == payload


def test_entry_list_round_trip():
    payload = {
        "wta:fixture": [
            {
                "pos": 1,
                "name": "Maria Carle",
                "country": "ARG",
                "rank": 45,
                "priority": 1,
                "pos_num": 1,
                "entry": "DA",
                "seed_rank": 45,
                "seed": "1",
                "type": "MAIN",
            }
        ]
    }
    assert_round_trip(payload, compress_entry_lists_cache, expand_entry_lists_cache)


def test_entry_list_player_id_round_trip():
    payload = {
        "wta:fixture": [
            {
                "pos": 1,
                "name": "Maria Carle",
                "country": "ARG",
                "rank": 45,
                "priority": 1,
                "pos_num": 1,
                "entry": "DA",
                "seed_rank": 45,
                "seed": "1",
                "player_id": "321692",
                "type": "MAIN",
            }
        ]
    }
    assert_round_trip(payload, compress_entry_lists_cache, expand_entry_lists_cache)


def test_calendar_cache_round_trips():
    wta = {"items": [{"title": "Fixture Open", "startDate": "2026-07-20", "level": "WTA 125"}]}
    itf = {"items": [{"tournamentName": "W75 Fixture", "tournamentKey": "w-itf-arg-2026-001"}]}
    assert_round_trip(wta, compress_wta_calendar_cache, expand_wta_calendar_cache)
    assert_round_trip(itf, compress_itf_calendar_cache, expand_itf_calendar_cache)


def test_snapshot_round_trips():
    tournament = {
        "wta:1:2026": {
            "name": "Fixture Open",
            "level": "WTA 125",
            "surface": "Clay",
            "country": "Argentina",
            "startDate": "2026-07-20",
            "endDate": "2026-07-26",
            "week": "2026-07-20",
        }
    }
    calendar = [{
        "week_label": "20 Jul",
        "column": 1,
        "continent": "South America",
        "name": "Fixture Open",
        "level": "WTA 125",
        "surface": "Clay",
        "source": "wta",
        "tournamentKey": "wta:1:2026",
        "tournamentId": "1",
        "calendarKey": "fixture",
    }]
    draws = {"wta:1:2026": {"name": "Fixture Open", "types": ["M", "Q"]}}
    assert_round_trip(tournament, compress_tournament_snapshot, expand_tournament_snapshot)
    assert_round_trip(calendar, compress_calendar_snapshot, expand_calendar_snapshot)
    assert_round_trip(draws, compress_draws_snapshot, expand_draws_snapshot)


def test_fixed_row_cache_round_trips():
    points = [{field: index for index, field in enumerate(_POINTS_DISTRIBUTION_FIELDS)}]
    draw_sizes = [{
        "source": "wta",
        "date": "2026-07-20",
        "tournamentName": "Fixture Open",
        "tournamentId": "1",
        "category": "WTA 125",
        "mainDrawSize": 32,
        "qualifyingSize": 16,
        "description": "fixture",
    }]
    strength = [{
        "id": "1", "name": "Fixture Open", "city": "Madrid", "level": "WTA 125",
        "startDate": "2026-07-20", "surface": "Clay", "country": "ESP", "region": "Europe",
        "year": 2026, "draw": 32, "participantsLocked": True, "rankings": [1, 2],
        "hm": 1.0, "gm": 2.0, "playerCount": 2,
    }]
    itf_rankings = {"2026-07-20": [{"Player": "Julia Riera", "Rank": 1, "Country": "ARG", "Key": "8001"}]}
    assert_round_trip(points, compress_points_distribution, expand_points_distribution)
    assert_round_trip(draw_sizes, compress_tournament_draw_sizes, expand_tournament_draw_sizes)
    assert_round_trip(strength, compress_tstrength_cache, expand_tstrength_cache)
    assert_round_trip(itf_rankings, compress_itf_rankings_cache, expand_itf_rankings_cache)


def test_draw_store_round_trip():
    payload = {
        "wta:1:2026": {
            "name": "Fixture Open",
            "level": "WTA 125",
            "week": "2026-07-20",
            "startDate": "2026-07-20",
            "endDate": "2026-07-26",
            "fetchedAt": "2026-07-20T10:00:00Z",
            "arg_visibility": True,
            "draws": {
                "M": {
                    "tournament_name": "Fixture Open",
                    "location": "Madrid",
                    "dates": "20-26 July",
                    "prize": "$125,000",
                    "surface": "Clay",
                    "draw_type": "Main Draw",
                    "players": [{"pos": 1, "seed": "1", "entry": "DA", "name": "CARLE, Maria", "country": "ARG"}],
                    "matches": [{"round": 1, "match_num": 0, "winner_name": "M. Carle", "score": "64 63"}],
                    "byes": [],
                    "qualifiers": [],
                    "round_labels": ["Final"],
                }
            },
        }
    }
    assert_round_trip(payload, compress_draws_store_cache, expand_draws_store_cache)


def test_itf_drawsheet_compression_preserves_parser_output(monkeypatch):
    fixture = json.loads((FIXTURES / "itf_drawsheet_response.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(itf_load_new, "madrid_today", lambda: date(2026, 7, 22))
    original = itf_load_new.parse_drawsheet(fixture["drawsheet"], fixture["tournament"], "M")
    expanded = expand_itf_drawsheet_cache(compress_itf_drawsheet_cache(fixture["drawsheet"]))
    assert itf_load_new.parse_drawsheet(expanded, fixture["tournament"], "M") == original
