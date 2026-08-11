import json
from datetime import date

import generate_run_report
from generate_run_report import compute_report, render_email_markdown


def _calendar_row(
    key,
    name,
    *,
    level="W15",
    surface="Clay",
    country="ARG",
    start_date="2026-09-07",
    end_date="2026-09-13",
    week_label="Week of September 7",
):
    return {
        "week_label": week_label,
        "column": "itf",
        "continent": "south_america",
        "name": name,
        "level": level,
        "surface": surface,
        "country": country,
        "startDate": start_date,
        "endDate": end_date,
        "source": "ITF",
        "tournamentKey": key.removeprefix("itf:"),
        "calendarKey": key,
    }


def test_calendar_report_detects_additions_changes_and_cancellations(monkeypatch, tmp_path):
    before_dir = tmp_path / "before"
    after_dir = tmp_path / "after"
    before_dir.mkdir()
    after_dir.mkdir()

    before = [
        _calendar_row("itf:changed", "W15 Cordoba"),
        _calendar_row("itf:cancelled", "W35 Mendoza"),
        _calendar_row(
            "itf:started",
            "W15 Buenos Aires",
            start_date="2026-08-11",
            end_date="2026-08-16",
            week_label="Week of August 10",
        ),
    ]
    after = [
        _calendar_row(
            "itf:changed",
            "W35 Cordoba Open",
            level="W35",
            surface="Hard",
            start_date="2026-09-14",
            end_date="2026-09-20",
            week_label="Week of September 14",
        ),
        _calendar_row("itf:added", "W50 Rosario"),
    ]
    (before_dir / "calendar_snapshot.json").write_text(json.dumps(before), encoding="utf-8")
    (after_dir / "calendar_snapshot.json").write_text(json.dumps(after), encoding="utf-8")
    monkeypatch.setattr(generate_run_report, "madrid_today", lambda: date(2026, 8, 11))

    report = compute_report(str(before_dir), str(after_dir))

    assert [item["name"] for item in report["added_calendar_tournaments"]] == ["W50 Rosario"]
    assert [item["name"] for item in report["cancelled_calendar_tournaments"]] == ["W35 Mendoza"]
    assert len(report["changed_calendar_tournaments"]) == 1
    change = report["changed_calendar_tournaments"][0]
    assert change["after"]["name"] == "W35 Cordoba Open"
    assert {item["field"] for item in change["changes"]} == {
        "name",
        "level",
        "surface",
        "startDate",
        "endDate",
    }

    markdown = render_email_markdown(report)
    assert "## 5) Tournaments Added to Calendar" in markdown
    assert "## 6) Tournaments Changed on Calendar" in markdown
    assert "Category: W15 → W35" in markdown
    assert "## 7) Tournaments Cancelled" in markdown
    assert "W35 Mendoza" in markdown
    assert "W15 Buenos Aires" not in markdown


def test_calendar_week_placement_change_is_not_a_tournament_change():
    before = [_calendar_row("itf:multiweek", "W100 Example", week_label="Week of September 7")]
    after = [_calendar_row("itf:multiweek", "W100 Example", week_label="Week of September 14")]

    added, changed, cancelled = generate_run_report.diff_calendar_tournaments(
        before,
        after,
        today=date(2026, 8, 11),
    )

    assert added == []
    assert changed == []
    assert cancelled == []


def test_new_snapshot_fields_do_not_make_legacy_rows_look_changed():
    before = [_calendar_row("itf:legacy", "W75 Example")]
    for field in ("country", "startDate", "endDate"):
        before[0].pop(field)
    after = [_calendar_row("itf:legacy", "W75 Example")]

    added, changed, cancelled = generate_run_report.diff_calendar_tournaments(
        before,
        after,
        today=date(2026, 8, 11),
    )

    assert added == []
    assert changed == []
    assert cancelled == []
