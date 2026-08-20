from datetime import date

from html_generator import (
    _apply_special_gs_cutoff_overrides,
    _build_gs_cutoff_boxes,
    _roll_forward_passed_gs_cutoffs,
)


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


def test_australian_open_uses_november_15_cutoff():
    q_cutoff, md_cutoff = _apply_special_gs_cutoff_overrides("Australian Open", 2027, "2026-12-21", "2026-12-07")

    assert q_cutoff == "2026-11-15"
    assert md_cutoff == "2026-11-15"


def test_australian_open_2027_cutoff_box_is_on_november_9_calendar_week():
    boxes = _build_gs_cutoff_boxes(
        [{"name": "Australian Open", "year": 2027, "mdCutoff": "N/A"}],
        set(),
    )

    assert boxes["2026-11-09"] == [(0, "Last week for AO MD/Q")]
