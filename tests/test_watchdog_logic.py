import datetime
import unittest


UTC = datetime.timezone.utc


def find_oldest_missing_slot(now, completed):
    slots = []
    start_day = now.date() - datetime.timedelta(days=3)
    for offset in range(5):
        day = start_day + datetime.timedelta(days=offset)
        for name, hour in (("morning", 3), ("day", 9), ("evening", 15)):
            expected = datetime.datetime.combine(day, datetime.time(hour, 5), tzinfo=UTC)
            slot_id = f"{day.isoformat()}-{name}"
            if expected + datetime.timedelta(minutes=45) <= now and slot_id not in completed:
                slots.append((expected, slot_id))
    slots.sort()
    return slots[0][1] if slots else ""


class WatchdogLogicTests(unittest.TestCase):
    def test_evening_slot_is_recovered_late_same_day(self):
        now = datetime.datetime(2026, 9, 1, 21, 50, tzinfo=UTC)
        completed = {
            "2026-09-01-morning": "2026-09-01T05:03:00+00:00",
            "2026-09-01-day": "2026-09-01T10:04:00+00:00",
        }
        self.assertEqual(find_oldest_missing_slot(now, completed), "2026-09-01-evening")

    def test_old_missing_slot_is_not_lost_after_24_hours(self):
        now = datetime.datetime(2026, 9, 2, 20, 0, tzinfo=UTC)
        completed = {
            "2026-09-01-morning": "2026-09-01T05:00:00+00:00",
            "2026-09-01-day": "2026-09-01T10:00:00+00:00",
        }
        self.assertEqual(find_oldest_missing_slot(now, completed), "2026-09-01-evening")

    def test_completed_slot_is_never_returned(self):
        now = datetime.datetime(2026, 9, 1, 22, 0, tzinfo=UTC)
        completed = {
            "2026-09-01-morning": "2026-09-01T05:00:00+00:00",
            "2026-09-01-day": "2026-09-01T10:00:00+00:00",
            "2026-09-01-evening": "2026-09-01T20:00:00+00:00",
        }
        self.assertEqual(find_oldest_missing_slot(now, completed), "")


if __name__ == "__main__":
    unittest.main()
