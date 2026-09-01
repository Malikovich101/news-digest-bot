import datetime
import unittest


UTC = datetime.timezone.utc


def find_oldest_missing_recent_slot(now, completed):
    slots = []
    for day in (now.date() - datetime.timedelta(days=1), now.date()):
        for name, hour in (("morning", 3), ("day", 9), ("evening", 15)):
            expected = datetime.datetime.combine(day, datetime.time(hour, 5), tzinfo=UTC)
            slot_id = f"{day.isoformat()}-{name}"
            if expected + datetime.timedelta(minutes=45) <= now and slot_id not in completed:
                slots.append((expected, slot_id))
    slots.sort()
    return slots[0][1] if slots else ""


def completed_recent_slots(now):
    completed = {}
    for day in (now.date() - datetime.timedelta(days=1), now.date()):
        for name, hour in (("morning", 3), ("day", 9), ("evening", 15)):
            expected = datetime.datetime.combine(day, datetime.time(hour, 5), tzinfo=UTC)
            if expected + datetime.timedelta(minutes=45) <= now:
                completed[f"{day.isoformat()}-{name}"] = now.isoformat()
    return completed


class WatchdogLogicTests(unittest.TestCase):
    def test_evening_slot_is_recovered_late_same_day(self):
        now = datetime.datetime(2026, 9, 1, 21, 50, tzinfo=UTC)
        completed = {
            "2026-08-31-morning": now.isoformat(),
            "2026-08-31-day": now.isoformat(),
            "2026-08-31-evening": now.isoformat(),
            "2026-09-01-morning": now.isoformat(),
            "2026-09-01-day": now.isoformat(),
        }
        self.assertEqual(find_oldest_missing_recent_slot(now, completed), "2026-09-01-evening")

    def test_oldest_missing_recent_slot_is_recovered_first(self):
        now = datetime.datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
        completed = {
            "2026-08-31-morning": now.isoformat(),
            "2026-08-31-day": now.isoformat(),
            "2026-08-31-evening": now.isoformat(),
            "2026-09-02-morning": now.isoformat(),
        }
        self.assertEqual(find_oldest_missing_recent_slot(now, completed), "2026-09-01-morning")

    def test_slot_older_than_yesterday_is_not_recovered(self):
        now = datetime.datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
        completed = completed_recent_slots(now)
        self.assertEqual(find_oldest_missing_recent_slot(now, completed), "")

    def test_completed_slots_are_skipped(self):
        now = datetime.datetime(2026, 9, 1, 22, 0, tzinfo=UTC)
        completed = completed_recent_slots(now)
        self.assertEqual(find_oldest_missing_recent_slot(now, completed), "")


if __name__ == "__main__":
    unittest.main()
