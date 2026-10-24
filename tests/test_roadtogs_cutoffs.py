from datetime import date

from html_generator import (
    _apply_gs_start_date_overrides,
    _apply_special_gs_cutoff_overrides,
    _build_gs_cutoff_boxes,
    _roll_forward_passed_gs_cutoffs,
)


def test_ao_cutoff_is_monday_november_16_with_matching_points_weeks():
    qualifying, main = _apply_special_gs_cutoff_overrides("Australian Open", 2027, "N/A", "N/A")
    assert qualifying == main == "2026-11-16"
    assert date.fromisoformat(main).weekday() == 0
    boxes = _build_gs_cutoff_boxes([
        {"name": "Australian Open", "year": 2027, "qCutoff": qualifying, "mdCutoff": main},
    ], set())
    assert "Last week for AO in W50+" in [label for _, label in boxes["2026-11-09"]]
    assert "Last week for AO in W15/35" in [label for _, label in boxes["2026-11-02"]]


def _us_open_2026():
    return [{
        "id": "USO",
        "name": "US Open",
        "qCutoff": "2026-08-03",
        "mdCutoff": "2026-07-20",
        "year": 2026,
    }]


def test_us_open_stays_on_current_edition_after_main_draw_cutoff():
    cutoffs = _us_open_2026()

    _roll_forward_passed_gs_cutoffs(cutoffs, date(2026, 7, 22))

    assert cutoffs[0]["year"] == 2026
    assert cutoffs[0]["qCutoff"] == "2026-08-03"
    assert cutoffs[0]["mdCutoff"] == "2026-07-20"


def test_us_open_stays_on_current_edition_on_qualifying_cutoff_day():
    cutoffs = _us_open_2026()

    _roll_forward_passed_gs_cutoffs(cutoffs, date(2026, 8, 3))

    assert cutoffs[0]["year"] == 2026


def test_us_open_rolls_forward_after_qualifying_cutoff_day():
    cutoffs = _us_open_2026()

    _roll_forward_passed_gs_cutoffs(cutoffs, date(2026, 8, 4))

    assert cutoffs[0]["year"] == 2027
    assert cutoffs[0]["qCutoff"] == "2027-08-02"
    assert cutoffs[0]["mdCutoff"] == "2027-07-19"


def test_2027_grand_slam_cutoffs_use_published_start_dates():
    cutoffs = [
        {"name": "Roland Garros", "year": 2027},
        {"name": "Wimbledon", "year": 2027},
        {"name": "US Open", "year": 2027},
    ]

    _apply_gs_start_date_overrides(cutoffs)

    assert [(gs["name"], gs["qCutoff"], gs["mdCutoff"]) for gs in cutoffs] == [
        ("Roland Garros", "2027-04-26", "2027-04-12"),
        ("Wimbledon", "2027-05-31", "2027-05-17"),
        ("US Open", "2027-08-02", "2027-07-19"),
    ]
