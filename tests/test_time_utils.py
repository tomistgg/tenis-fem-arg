import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from time_utils import (
    as_utc,
    madrid_now,
    madrid_today,
    new_york_now,
    parse_utc_timestamp,
    utc_timestamp,
)


class TimeUtilsTests(unittest.TestCase):
    def test_madrid_spring_dst_transition_uses_zone_database(self):
        before = datetime(2026, 3, 29, 0, 59, tzinfo=UTC)
        after = datetime(2026, 3, 29, 1, 0, tzinfo=UTC)
        self.assertEqual(madrid_now(before).strftime("%Y-%m-%d %H:%M %z"), "2026-03-29 01:59 +0100")
        self.assertEqual(madrid_now(after).strftime("%Y-%m-%d %H:%M %z"), "2026-03-29 03:00 +0200")

    def test_madrid_autumn_dst_transition_uses_zone_database(self):
        before = datetime(2026, 10, 25, 0, 59, tzinfo=UTC)
        after = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)
        self.assertEqual(madrid_now(before).strftime("%Y-%m-%d %H:%M %z"), "2026-10-25 02:59 +0200")
        self.assertEqual(madrid_now(after).strftime("%Y-%m-%d %H:%M %z"), "2026-10-25 02:00 +0100")

    def test_madrid_6pm_rule_is_correct_on_dst_start_weekend(self):
        before = datetime(2026, 3, 29, 15, 59, tzinfo=UTC)
        after = datetime(2026, 3, 29, 16, 0, tzinfo=UTC)
        self.assertFalse(madrid_now(before).hour >= 18)
        self.assertTrue(madrid_now(after).hour >= 18)

    def test_new_york_spring_dst_transition_uses_zone_database(self):
        before = datetime(2026, 3, 8, 6, 59, tzinfo=UTC)
        after = datetime(2026, 3, 8, 7, 0, tzinfo=UTC)
        self.assertEqual(new_york_now(before).strftime("%Y-%m-%d %H:%M %z"), "2026-03-08 01:59 -0500")
        self.assertEqual(new_york_now(after).strftime("%Y-%m-%d %H:%M %z"), "2026-03-08 03:00 -0400")

    def test_business_date_is_derived_at_the_madrid_boundary(self):
        instant = datetime(2026, 1, 1, 23, 30, tzinfo=UTC)
        self.assertEqual(str(madrid_today(instant)), "2026-01-02")

    def test_timestamp_round_trip_is_aware_utc(self):
        parsed = parse_utc_timestamp("2026-07-22T10:15:30Z")
        self.assertEqual(parsed.utcoffset(), timedelta(0))
        self.assertEqual(utc_timestamp(parsed), "2026-07-22T10:15:30Z")

    def test_naive_instants_are_rejected(self):
        with self.assertRaises(ValueError):
            as_utc(datetime(2026, 7, 22, 10, 15))
        with self.assertRaises(ValueError):
            parse_utc_timestamp("2026-07-22T10:15:30")

    def test_application_modules_do_not_read_naive_system_time(self):
        project_dir = Path(__file__).resolve().parents[1]
        forbidden = (
            "datetime.now(",
            "datetime.utcnow(",
            "datetime.today(",
            "date.today(",
        )
        violations = []
        for path in project_dir.rglob("*.py"):
            if path.name in {"time_utils.py", Path(__file__).name} or any(part.startswith(".") for part in path.parts):
                continue
            source = path.read_text(encoding="utf-8-sig")
            for expression in forbidden:
                if expression in source:
                    violations.append(f"{path.relative_to(project_dir)}: {expression}")
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
