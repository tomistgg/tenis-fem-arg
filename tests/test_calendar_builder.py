from datetime import date

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
