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


def test_calendar_end_date_change_is_not_a_tournament_change():
    before = [_calendar_row("itf:end-date", "W50 Morelia", end_date="2026-10-24")]
    after = [_calendar_row("itf:end-date", "W50 Morelia", end_date="2026-11-01")]

    added, changed, cancelled = generate_run_report.diff_calendar_tournaments(
        before,
        after,
        today=date(2026, 9, 6),
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


def test_email_alerts_when_category_free_tournament_name_newly_exceeds_15_characters(tmp_path):
    before_dir = tmp_path / "before"
    after_dir = tmp_path / "after"
    before_dir.mkdir()
    after_dir.mkdir()

    before = [
        _calendar_row("itf:unchanged-long", "W15 unchangedlongname"),
        _calendar_row("itf:renamed-long", "W15 Cordoba"),
    ]
    after = [
        _calendar_row("itf:unchanged-long", "W15 unchangedlongname"),
        _calendar_row("itf:renamed-long", "W15 abcdefghijklmnopqr"),
        _calendar_row("itf:added-long", "WTA 125 abcdefghijklmnop", level="WTA125"),
        _calendar_row("itf:exact-limit", "W50 abcdefghijklmno", level="W50"),
        _calendar_row("itf:already-compact", "W50 Saint-Palais-sur-Mer", level="W50"),
    ]
    (before_dir / "calendar_snapshot.json").write_text(json.dumps(before), encoding="utf-8")
    (after_dir / "calendar_snapshot.json").write_text(json.dumps(after), encoding="utf-8")

    report = compute_report(str(before_dir), str(after_dir))

    alerts = report["long_tournament_name_alerts"]
    assert {item["name"] for item in alerts} == {
        "W15 abcdefghijklmnopqr",
        "WTA 125 abcdefghijklmnop",
    }
    assert {item["name_without_category"] for item in alerts} == {
        "abcdefghijklmnopqr",
        "abcdefghijklmnop",
    }
    assert {item["character_count"] for item in alerts} == {16, 18}

    markdown = render_email_markdown(report)
    assert "## 9) Tournament Names Over 15 Characters" in markdown
    assert "16 characters without counting the category" in markdown
    alert_section = markdown.split("## 9) Tournament Names Over 15 Characters", 1)[1]
    assert "W15 unchangedlongname" not in alert_section
    assert "Saint-Palais-sur-Mer" not in alert_section


def test_email_does_not_report_che_as_a_country_without_a_flag(tmp_path):
    before_dir = tmp_path / "before"
    after_dir = tmp_path / "after"
    before_dir.mkdir()
    after_dir.mkdir()
    (after_dir / "wta_rankings_20_29.csv").write_text(
        "week_date,id,rank,points,player,country,dob\n"
        "2026-08-31,335808,1433,5,Sofia Alekseeva,CHE,2007-11-20\n",
        encoding="utf-8",
    )

    report = compute_report(str(before_dir), str(after_dir))

    assert report["flagless_player_countries"] == []
    assert "CHE: Sofia Alekseeva" not in render_email_markdown(report)


def _player_row(player_key, display_name, *, country="", dob="", wta_id="", itf_id=""):
    return {
        "player_key": player_key,
        "display_name": display_name,
        "country": country,
        "dob": dob,
        "wta_id": wta_id,
        "wta_name": display_name if wta_id else "",
        "itf_id": itf_id,
        "itf_name": display_name if itf_id else "",
        "bjkc_id": "",
        "bjkc_name": "",
        "aliases": [],
        "additional_wta_ids": [],
        "additional_itf_ids": [],
        "additional_bjkc_ids": [],
    }


def test_email_reports_player_identity_link(tmp_path):
    before_dir = tmp_path / "before"
    after_dir = tmp_path / "after"
    before_dir.mkdir()
    after_dir.mkdir()
    before = [
        _player_row("wta:123", "Same Name (WTA 123)", country="ARG", dob="2000-01-01", wta_id="123"),
        _player_row("itf:800000001", "Same Name", country="ARG", dob="2000-01-01", itf_id="800000001"),
    ]
    before[0]["presentation_name"] = "Same Name"
    after = [
        {
            **_player_row(
                "wta:123",
                "Same Name",
                country="ARG",
                dob="2000-01-01",
                wta_id="123",
                itf_id="800000001",
            ),
            "itf_name": "Same Name",
        }
    ]
    (before_dir / "player_aliases_wta_itf.json").write_text(json.dumps(before), encoding="utf-8")
    (after_dir / "player_aliases_wta_itf.json").write_text(json.dumps(after), encoding="utf-8")

    report = compute_report(str(before_dir), str(after_dir))

    assert report["player_identity_links"][0]["source_ids"] == ["ITF 800000001", "WTA 123"]
    markdown = render_email_markdown(report)
    assert "## Player Identities Linked Automatically" in markdown
    assert "Same Name: ITF 800000001, WTA 123" in markdown


def test_email_reports_similar_player_identity_for_review(tmp_path):
    before_dir = tmp_path / "before"
    after_dir = tmp_path / "after"
    before_dir.mkdir()
    after_dir.mkdir()
    existing = _player_row(
        "wta:310974",
        "Laura Vallverdu-Zaira",
        country="ESP",
        dob="1987-04-22",
        wta_id="310974",
    )
    candidate = _player_row(
        "itf:800240123",
        "Laura Vallverdu-Zafra",
        country="VEN",
        itf_id="800240123",
    )
    (before_dir / "player_aliases_wta_itf.json").write_text(json.dumps([existing]), encoding="utf-8")
    (after_dir / "player_aliases_wta_itf.json").write_text(
        json.dumps([existing, candidate]),
        encoding="utf-8",
    )

    report = compute_report(str(before_dir), str(after_dir))

    review = report["player_identity_review_candidates"]
    assert len(review) == 1
    assert review[0]["name_match"] == "similar"
    assert review[0]["similarity"] >= 90
    markdown = render_email_markdown(report)
    assert "## Player Identity Candidates for Manual Review" in markdown
    assert "Laura Vallverdu-Zafra" in markdown
    assert "country conflicts (VEN vs ESP)" in markdown
