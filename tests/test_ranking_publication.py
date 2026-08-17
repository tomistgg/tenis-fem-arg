from datetime import datetime

import pytest

from ranking_publication import effective_wta_ranking_date
from time_utils import NEW_YORK


@pytest.mark.parametrize(
    ("eastern_now", "status", "expected"),
    [
        (datetime(2026, 8, 17, 11, 59, tzinfo=NEW_YORK), {}, "2026-08-10"),
        (datetime(2026, 8, 17, 12, 0, tzinfo=NEW_YORK), {}, "2026-08-17"),
        (
            datetime(2026, 8, 17, 12, 1, tzinfo=NEW_YORK),
            {
                "requested_date": "2026-08-17",
                "previous_date": "2026-08-10",
                "status": "pending_publication",
            },
            "2026-08-10",
        ),
        (
            datetime(2026, 8, 17, 12, 1, tzinfo=NEW_YORK),
            {
                "requested_date": "2026-08-17",
                "previous_date": "2026-08-10",
                "status": "confirmed_frozen",
            },
            "2026-08-17",
        ),
    ],
)
def test_effective_wta_ranking_date_respects_publication_state(eastern_now, status, expected):
    assert effective_wta_ranking_date(eastern_now, status).isoformat() == expected
