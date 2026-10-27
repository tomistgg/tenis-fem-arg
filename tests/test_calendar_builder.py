from datetime import date

import pytest

import calendar_builder


def test_w35_sao_paulo_is_rendered_in_start_week_only(monkeypatch):
    monkeypatch.setattr(calendar_builder, "get_next_monday", lambda: date(2026, 11, 16))

    weeks = calendar_builder.build_calendar_data(
        [
            {
                "name": "W35 Sao Paulo",
                "level": "W35",
                "surface": "Clay",
                "country": "BRA",
                "startDate": "2026-11-16",
                "endDate": "2026-11-29",
                "source": "ITF",
                "tournamentKey": "w-itf-bra-2026-014",
                "calendarKey": "itf:w-itf-bra-2026-014",
            }
        ]
    )

    entries = [
        item
        for week in weeks
        for item in week["columns"]["itf"]["south_america"]
        if item["calendarKey"] == "itf:w-itf-bra-2026-014"
    ]

    assert len(entries) == 1
    assert entries[0]["startDate"] == "2026-11-16"
    assert entries[0]["endDate"] == "2026-11-22"


def test_calendar_includes_published_next_year_tournaments(monkeypatch):
    monkeypatch.setattr(calendar_builder, "get_next_monday", lambda: date(2026, 12, 21))

    weeks = calendar_builder.build_calendar_data(
        [
            {
                "name": "W35 Buenos Aires",
                "level": "W35",
                "country": "ARG",
                "startDate": "2026-12-21",
                "endDate": "2026-12-27",
                "calendarKey": "itf:current-year",
            },
            {
                "name": "W35 Buenos Aires",
                "level": "W35",
                "country": "ARG",
                "startDate": "2027-01-04",
                "endDate": "2027-01-10",
                "calendarKey": "itf:next-year",
            },
        ]
    )

    assert [week["monday_date"] for week in weeks] == ["2026-12-21", "2026-12-28", "2027-01-04"]
    assert not weeks[1]["has_any"]
    assert weeks[-1]["columns"]["itf"]["south_america"][0]["calendarKey"] == "itf:next-year"


@pytest.mark.parametrize(
    ("end_date", "expected_mondays"),
    [
        ("2027-01-06", ["2026-12-28"]),
        ("2027-01-07", ["2026-12-28", "2027-01-04"]),
    ],
)
def test_cross_year_tournaments_keep_four_day_overlap_and_trim_empty_weeks(monkeypatch, end_date, expected_mondays):
    monkeypatch.setattr(calendar_builder, "get_next_monday", lambda: date(2026, 12, 28))

    weeks = calendar_builder.build_calendar_data(
        [
            {
                "name": "W35 Buenos Aires",
                "level": "W35",
                "country": "ARG",
                "startDate": "2026-12-28",
                "endDate": end_date,
            }
        ]
    )

    assert [week["monday_date"] for week in weeks] == expected_mondays
    assert all(week["has_any"] for week in weeks)
