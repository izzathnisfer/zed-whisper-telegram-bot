import tempfile
import unittest
from pathlib import Path

from usage_tracker import UsageTracker


class UsageTrackerTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database_path = str(Path(self.temp_directory.name) / "usage.sqlite3")
        self.tracker = UsageTracker(self.database_path)

    def tearDown(self):
        self.temp_directory.cleanup()

    def test_records_all_four_rolling_metrics(self):
        self.tracker.record_attempt(300, requested_at=1_000)

        snapshot = self.tracker.snapshot(now=1_010)

        self.assertEqual(snapshot.requests_minute.used, 1)
        self.assertEqual(snapshot.requests_day.used, 1)
        self.assertEqual(snapshot.audio_hour.used, 300)
        self.assertEqual(snapshot.audio_day.used, 300)
        self.assertEqual(snapshot.audio_hour.remaining, 6_900)

    def test_short_audio_counts_as_ten_seconds(self):
        self.tracker.record_attempt(2.5, requested_at=1_000)

        snapshot = self.tracker.snapshot(now=1_001)

        self.assertEqual(snapshot.audio_hour.used, 10)
        self.assertEqual(snapshot.audio_day.used, 10)

    def test_minute_and_hour_windows_expire_independently(self):
        self.tracker.record_attempt(300, requested_at=1_000)

        after_minute = self.tracker.snapshot(now=1_061)
        self.assertEqual(after_minute.requests_minute.used, 0)
        self.assertEqual(after_minute.requests_day.used, 1)
        self.assertEqual(after_minute.audio_hour.used, 300)

        after_hour = self.tracker.snapshot(now=4_601)
        self.assertEqual(after_hour.audio_hour.used, 0)
        self.assertEqual(after_hour.audio_day.used, 300)

    def test_request_minute_limit_returns_wait_until_oldest_expires(self):
        for _index in range(UsageTracker.REQUESTS_PER_MINUTE):
            self.tracker.record_attempt(10, requested_at=1_000)

        wait = self.tracker.seconds_until_available(10, now=1_015)

        self.assertEqual(wait, 45)

    def test_hourly_audio_limit_returns_wait(self):
        for _index in range(24):
            self.tracker.record_attempt(300, requested_at=1_000)

        wait = self.tracker.seconds_until_available(300, now=1_100)

        self.assertEqual(wait, 3_500)

    def test_usage_survives_tracker_restart(self):
        self.tracker.record_attempt(300, requested_at=1_000)

        reopened_tracker = UsageTracker(self.database_path)
        snapshot = reopened_tracker.snapshot(now=1_010)

        self.assertEqual(snapshot.requests_day.used, 1)
        self.assertEqual(snapshot.audio_day.used, 300)

    def test_long_job_preflight_checks_all_planned_daily_requests(self):
        for _index in range(1_990):
            self.tracker.record_attempt(10, requested_at=1_000)

        wait = self.tracker.seconds_until_available(
            60,
            now=1_100,
            request_count=11,
            include_minute_limit=False,
        )

        self.assertEqual(wait, 86_300)


if __name__ == "__main__":
    unittest.main()
