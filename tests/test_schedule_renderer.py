import json
from pathlib import Path

from site_renderer import _tournament_inputs, _visible_schedule_monday_map


def test_schedule_shows_four_chronological_unique_weeks(tmp_path: Path):
    snapshot = {
        "w-itf-arg-2026-001": {
            "name": "ITF Example",
            "level": "W35",
            "surface": "Clay",
            "country": "ARG",
            "startDate": "2026-08-24",
            "endDate": "2026-08-30",
            "week": "Week of August 24",
        },
        "w-itf-arg-2026-002": {
            "name": "ITF Earlier",
            "level": "W35",
            "surface": "Clay",
            "country": "ARG",
            "startDate": "2026-08-17",
            "endDate": "2026-08-23",
            "week": "Week of August 17",
        },
        "https://www.wtatennis.com/tournaments/1001/later/2026/player-list": {
            "name": "WTA Later",
            "level": "WTA 250",
            "surface": "Hard",
            "country": "USA",
            "startDate": "2026-08-31",
            "endDate": "2026-09-06",
            "week": "Week of August 31",
        },
        "https://www.wtatennis.com/tournaments/1002/future/2026/player-list": {
            "name": "WTA Future",
            "level": "WTA 250",
            "surface": "Hard",
            "country": "USA",
            "startDate": "2026-09-07",
            "endDate": "2026-09-13",
            "week": "Week of September 7",
        },
        "https://www.wtatennis.com/tournaments/1003/too-far/2026/player-list": {
            "name": "WTA Too Far",
            "level": "WTA 250",
            "surface": "Hard",
            "country": "USA",
            "startDate": "2026-09-14",
            "endDate": "2026-09-20",
            "week": "Week of September 14",
        },
    }
    (tmp_path / "tournament_snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")

    _, monday_map = _tournament_inputs(tmp_path)
    visible_map = _visible_schedule_monday_map(monday_map)

    assert list(visible_map.items()) == [
        ("2026-08-17", "Week of August 17"),
        ("2026-08-24", "Week of August 24"),
        ("2026-08-31", "Week of August 31"),
        ("2026-09-07", "Week of September 7"),
    ]
