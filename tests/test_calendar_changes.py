from datetime import date

from calendar_changes import update_calendar_change_history
from html_generator import _render_calendar_changes


def _calendar_row(
    key,
    name,
    *,
    level,
    country,
    start_date,
    end_date="2026-11-01",
):
    return {
        "week_label": "Week of October 19",
        "column": "itf",
        "continent": "north_central_america",
        "name": name,
        "level": level,
        "surface": "Hard",
        "country": country,
        "startDate": start_date,
        "endDate": end_date,
        "source": "ITF",
        "tournamentKey": key,
        "calendarKey": f"itf:{key}",
    }


def test_calendar_change_history_formats_moves_category_changes_and_renames():
    before = [
        _calendar_row("morelia", "W50 Morelia", level="W50", country="MEX", start_date="2026-10-19"),
        _calendar_row("pilar", "W15 Pilar", level="W15", country="ARG", start_date="2026-10-26"),
        _calendar_row("antalya", "W35 Antalya", level="W35", country="ARG", start_date="2026-10-26"),
        _calendar_row("miami", "W35 Miami", level="W35", country="USA", start_date="2026-10-12"),
    ]
    after = [
        _calendar_row("morelia", "W50 Morelia", level="W50", country="MEX", start_date="2026-10-26"),
        _calendar_row("pilar", "W35 Pilar", level="W35", country="ARG", start_date="2026-10-26"),
        _calendar_row("antalya", "W15 Antalya", level="W15", country="ARG", start_date="2026-10-26"),
        _calendar_row("miami", "W35 Miami, FL", level="W35", country="USA", start_date="2026-10-12"),
    ]

    history = update_calendar_change_history([], before, after, detected_on=date(2026, 9, 6))

    assert history == [
        {
            "date": "2026-09-06",
            "changes": [
                {
                    "country": "USA",
                    "name": "W35 Miami",
                    "startDate": "2026-10-12",
                    "actions": ["Renamed to W35 Miami, FL"],
                },
                {
                    "country": "MEX",
                    "name": "W50 Morelia",
                    "startDate": "2026-10-19",
                    "actions": ["Moved to 2026-10-26"],
                },
                {
                    "country": "ARG",
                    "name": "W15 Pilar",
                    "startDate": "2026-10-26",
                    "actions": ["Upgraded to W35"],
                },
                {
                    "country": "ARG",
                    "name": "W35 Antalya",
                    "startDate": "2026-10-26",
                    "actions": ["Downgraded to W15"],
                },
            ],
        }
    ]

    rendered = _render_calendar_changes(history)
    assert "2026-09-06" in rendered
    assert "country-flag-icons/3x2/MX.svg" in rendered
    assert "country-flag-icons/3x2/AR.svg" in rendered
    assert "country-flag-icons/3x2/US.svg" in rendered
    assert "W50 Morelia" in rendered
    assert "Moved to 2026-10-26" in rendered


def test_calendar_change_history_keeps_three_calendar_days():
    change = {
        "country": "ARG",
        "name": "W15 Example",
        "startDate": "2026-10-26",
        "actions": ["Upgraded to W35"],
    }
    existing = [
        {"date": "2026-09-03", "changes": [change]},
        {"date": "2026-09-04", "changes": [change]},
        {"date": "2026-09-05", "changes": [change]},
    ]

    on_september_6 = update_calendar_change_history(existing, [], [], detected_on=date(2026, 9, 6))
    on_september_7 = update_calendar_change_history(on_september_6, [], [], detected_on=date(2026, 9, 7))

    assert [group["date"] for group in on_september_6] == ["2026-09-04", "2026-09-05"]
    assert [group["date"] for group in on_september_7] == ["2026-09-05"]
