import base64
import json
from datetime import date
from pathlib import Path

import pdfplumber

from draws import parse_draw_pdf
from itf import parse_itf_entry_list
from main import _parse_gs_entry_list_pdf
from populate_data import itf_load_new
from populate_data.bjkc_load_new import parse_tie_matches
from populate_data.wta_load_new import parse_match

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_saved_wta_match_response_parser():
    fixture = load_fixture("wta_match_response.json")
    parsed = parse_match(fixture["match"], fixture["meta"])
    assert parsed["matchId"] == "LS016"
    assert parsed["date"] == "2026-07-21"
    assert parsed["winnerName"] == "Maria Carle"
    assert parsed["winnerCountry"] == "ARG"
    assert parsed["result"] == "6-4 3-6 7-5 ret."
    assert parsed["resultStatusDesc"] == "Retired"


def test_saved_itf_drawsheet_response_parser(monkeypatch):
    fixture = load_fixture("itf_drawsheet_response.json")
    monkeypatch.setattr(itf_load_new, "madrid_today", lambda: date(2026, 7, 22))
    parsed = itf_load_new.parse_drawsheet(fixture["drawsheet"], fixture["tournament"], "M")
    assert len(parsed) == 1
    assert parsed[0]["matchId"] == "1100209999"
    assert parsed[0]["date"] == "2026-07-22"
    assert parsed[0]["winnerName"] == "Julia Riera"
    assert parsed[0]["result"] == "6-3 7-6(4)"


def test_saved_bjkc_tie_response_parser():
    fixture = load_fixture("bjkc_tie_response.json")
    parsed = parse_tie_matches(fixture["tie"], fixture["tie_id"], current_year=2026)
    assert len(parsed) == 1
    assert parsed[0]["matchId"] == "fixture-match-1"
    assert parsed[0]["winnerName"] == "Solana Sierra"
    assert parsed[0]["winnerCountry"] == "ARG"
    assert parsed[0]["result"] == "7-6(5) 6-2"


def test_saved_pdf_fixture_parser():
    encoded = (FIXTURES / "wta_draw_fixture.pdf.b64").read_text(encoding="ascii")
    pdf_bytes = base64.b64decode(encoded)
    parsed = parse_draw_pdf(pdf_bytes)
    assert parsed["tournament_name"] == "Fixture Open"
    assert parsed["draw_size"] == 2
    assert [player["country"] for player in parsed["players"]] == ["ARG", "ESP"]
    assert parsed["matches"][0]["winner_name"] == "A. Playera"
    assert parsed["matches"][0]["score"] == "64 63"


def test_itf_acceptance_parser_preserves_id_for_ambiguous_name():
    parsed = parse_itf_entry_list([{
        "entryClassificationCode": "MDA",
        "entries": [{
            "positionDisplay": "1",
            "players": [{
                "playerId": "800409958",
                "givenName": "Camila",
                "familyName": "Romero",
                "nationalityCode": "ECU",
            }],
        }],
    }])
    assert parsed[0]["player_id"] == "800409958"
    assert parsed[0]["name"] == "Camila Romero"


def test_itf_acceptance_parser_formats_special_entries_and_suppresses_filled_placeholders():
    parsed = parse_itf_entry_list([
        {
            "entryClassificationCode": "MDA",
            "entries": [
                {"positionDisplay": "17", "isExemption": True, "players": []},
                {"positionDisplay": "18", "isExemption": True, "players": []},
            ],
        },
        {
            "entryClassificationCode": "JA",
            "entries": [{
                "positionDisplay": "17",
                "priority": 1,
                "players": [{
                    "playerId": "800631038",
                    "givenName": "Hannah",
                    "familyName": "Klugman",
                    "nationalityCode": "",
                    "atpWtaRank": 365,
                    "itfBTRank": 1,
                }],
            }],
        },
        {
            "entryClassificationCode": "CA",
            "entries": [{
                "positionDisplay": "18",
                "priority": 1,
                "players": [{
                    "playerId": "800537726",
                    "givenName": "Reese",
                    "familyName": "Brantmeier",
                    "nationalityCode": None,
                    "atpWtaRank": 431,
                    "worldRating": 12.3,
                }],
            }],
        },
    ])

    assert [(row["pos"], row["name"], row["country"], row["rank"], row["entry"]) for row in parsed] == [
        ("17", "Hannah Klugman", "GBR", "JA (365)", "JA"),
        ("18", "Reese Brantmeier", "USA", "CA (431)", "CA"),
    ]


def test_itf_acceptance_parser_special_entry_without_wta_rank_uses_dash():
    for class_code in ("JR", "JE", "SE", "WC"):
        parsed = parse_itf_entry_list([{
            "entryClassificationCode": class_code,
            "entries": [{
                "positionDisplay": "1",
                "players": [{
                    "givenName": "No",
                    "familyName": "WTA Rank",
                    "nationalityCode": "ARG",
                    "itfBTRank": 12,
                    "worldRating": 18.2,
                }],
            }],
        }])

        assert parsed[0]["rank"] == f"{class_code} (-)"
        assert parsed[0]["entry"] == class_code


def test_us_open_parser_removes_withdrawals_and_promotes_alternates(monkeypatch):
    def word(text, x0, top, width=20):
        return {
            "text": text,
            "x0": x0,
            "x1": x0 + width,
            "top": top,
            "height": 10,
        }

    def player_words(rank, surname, given_name, country, top):
        return [
            word(str(rank), 68, top, 15),
            word(f"{surname},", 147, top, 45),
            word(given_name, 195, top, 50),
            word(country, 316, top, 60),
        ]

    class FakePage:
        rects = []

        def __init__(self, text, rows, struck_tops=()):
            self._text = text
            self._words = [item for row in rows for item in row]
            self.lines = [
                {
                    "x0": 147,
                    "x1": 245,
                    "top": top + 5,
                    "bottom": top + 5,
                    "width": 98,
                    "height": 0,
                }
                for top in struck_tops
            ]

        def extract_text(self):
            return self._text

        def extract_words(self, **_kwargs):
            return self._words

    class FakePdf:
        def __init__(self, pages):
            self.pages = pages

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    pages = [
        FakePage(
            "US OPEN WOMEN'S SINGLES ENTRY LIST",
            [
                player_words(80, "Active", "One", "France", 100),
                player_words(82, "Begu", "Irina-Camelia", "Romania", 120),
                player_words(94, "Tomljanovic", "Ajla", "Australia", 140),
                player_words(102, "Active", "Two", "Germany", 160),
            ],
            struck_tops=(120, 140),
        ),
        FakePage(
            "Alternates to Main Draw",
            [
                player_words(103, "Seidel", "Ella", "Germany", 100),
                player_words(104, "Jacquemot", "Elsa", "France", 120),
                player_words(105, "Blinkova", "Anna", "", 140),
            ],
        ),
    ]
    monkeypatch.setattr(pdfplumber, "open", lambda *_args, **_kwargs: FakePdf(pages))

    main_players, alternates = _parse_gs_entry_list_pdf(b"%PDF-fixture")

    assert [player["name"] for player in main_players] == [
        "One Active",
        "Two Active",
        "Ella Seidel",
        "Elsa Jacquemot",
    ]
    assert [player["pos"] for player in main_players] == ["1", "2", "3", "4"]
    assert [player["name"] for player in alternates] == ["Anna Blinkova"]
    assert alternates[0]["pos"] == "1"
