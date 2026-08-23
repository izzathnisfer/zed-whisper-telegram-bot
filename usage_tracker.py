import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple


MINUTE_SECONDS = 60
HOUR_SECONDS = 60 * 60
DAY_SECONDS = 24 * 60 * 60
MINIMUM_AUDIO_SECONDS = 10.0


@dataclass(frozen=True)
class LimitUsage:
    """Usage within one rolling rate-limit window."""

    used: float
    limit: float
    window_seconds: int
    next_expiry_seconds: Optional[float]
    full_reset_seconds: Optional[float]

    @property
    def remaining(self) -> float:
        return max(0.0, self.limit - self.used)


@dataclass(frozen=True)
class UsageSnapshot:
    requests_minute: LimitUsage
    requests_day: LimitUsage
    audio_hour: LimitUsage
    audio_day: LimitUsage
    tracking_since: Optional[float]


class UsageTracker:
    """Persist and calculate Groq usage with conservative rolling windows."""

    REQUESTS_PER_MINUTE = 20
    REQUESTS_PER_DAY = 2_000
    AUDIO_SECONDS_PER_HOUR = 7_200
    AUDIO_SECONDS_PER_DAY = 28_800

    def __init__(self, database_path: str):
        self.database_path = os.path.abspath(database_path)
        parent = os.path.dirname(self.database_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database_path, timeout=30)

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS groq_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    requested_at REAL NOT NULL,
                    audio_seconds REAL NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_groq_usage_requested_at "
                "ON groq_usage(requested_at)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS usage_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO usage_metadata(key, value) VALUES (?, ?)",
                ("tracking_started_at", str(time.time())),
            )

    def record_attempt(
        self, audio_seconds: float, requested_at: Optional[float] = None
    ) -> None:
        """Record an API attempt before it is sent to avoid undercounting."""
        timestamp = time.time() if requested_at is None else float(requested_at)
        counted_audio = max(MINIMUM_AUDIO_SECONDS, float(audio_seconds))
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO groq_usage(requested_at, audio_seconds) VALUES (?, ?)",
                (timestamp, counted_audio),
            )
            connection.execute(
                "DELETE FROM groq_usage WHERE requested_at <= ?",
                (timestamp - DAY_SECONDS,),
            )

    def _events(self, now: float) -> Tuple[List[Tuple[float, float]], Optional[float]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT requested_at, audio_seconds
                FROM groq_usage
                WHERE requested_at > ?
                ORDER BY requested_at ASC
                """,
                (now - DAY_SECONDS,),
            ).fetchall()
            tracking_since_row = connection.execute(
                "SELECT value FROM usage_metadata WHERE key = ?",
                ("tracking_started_at",),
            ).fetchone()

        tracking_since = float(tracking_since_row[0]) if tracking_since_row else None
        return [(float(row[0]), float(row[1])) for row in rows], tracking_since

    @staticmethod
    def _metric(
        events: Sequence[Tuple[float, float]],
        now: float,
        window_seconds: int,
        limit: float,
        value: Callable[[Tuple[float, float]], float],
    ) -> LimitUsage:
        active = [event for event in events if event[0] > now - window_seconds]
        used = sum(value(event) for event in active)
        if active:
            next_expiry = max(0.0, active[0][0] + window_seconds - now)
            full_reset = max(0.0, active[-1][0] + window_seconds - now)
        else:
            next_expiry = None
            full_reset = None
        return LimitUsage(used, limit, window_seconds, next_expiry, full_reset)

    def snapshot(self, now: Optional[float] = None) -> UsageSnapshot:
        timestamp = time.time() if now is None else float(now)
        events, tracking_since = self._events(timestamp)
        request_value = lambda _event: 1.0
        audio_value = lambda event: event[1]

        return UsageSnapshot(
            requests_minute=self._metric(
                events,
                timestamp,
                MINUTE_SECONDS,
                self.REQUESTS_PER_MINUTE,
                request_value,
            ),
            requests_day=self._metric(
                events,
                timestamp,
                DAY_SECONDS,
                self.REQUESTS_PER_DAY,
                request_value,
            ),
            audio_hour=self._metric(
                events,
                timestamp,
                HOUR_SECONDS,
                self.AUDIO_SECONDS_PER_HOUR,
                audio_value,
            ),
            audio_day=self._metric(
                events,
                timestamp,
                DAY_SECONDS,
                self.AUDIO_SECONDS_PER_DAY,
                audio_value,
            ),
            tracking_since=tracking_since,
        )

    @staticmethod
    def _capacity_wait(
        events: Sequence[Tuple[float, float]],
        now: float,
        window_seconds: int,
        limit: float,
        amount: float,
        value: Callable[[Tuple[float, float]], float],
    ) -> float:
        active = [event for event in events if event[0] > now - window_seconds]
        used = sum(value(event) for event in active)
        if used + amount <= limit:
            return 0.0

        for event in active:
            used -= value(event)
            if used + amount <= limit:
                return max(0.0, event[0] + window_seconds - now)

        return float(window_seconds)

    def seconds_until_available(
        self,
        audio_seconds: float,
        now: Optional[float] = None,
        request_count: int = 1,
        include_minute_limit: bool = True,
    ) -> float:
        """Return the conservative wait before planned usage fits the limits."""
        timestamp = time.time() if now is None else float(now)
        events, _tracking_since = self._events(timestamp)
        counted_audio = max(MINIMUM_AUDIO_SECONDS, float(audio_seconds))
        planned_requests = max(1, int(request_count))
        request_value = lambda _event: 1.0
        audio_value = lambda event: event[1]

        waits = []
        if include_minute_limit:
            waits.append(
                self._capacity_wait(
                    events,
                    timestamp,
                    MINUTE_SECONDS,
                    self.REQUESTS_PER_MINUTE,
                    float(planned_requests),
                    request_value,
                )
            )
        waits.extend(
            (
                self._capacity_wait(
                    events,
                    timestamp,
                    DAY_SECONDS,
                    self.REQUESTS_PER_DAY,
                    float(planned_requests),
                    request_value,
                ),
                self._capacity_wait(
                    events,
                    timestamp,
                    HOUR_SECONDS,
                    self.AUDIO_SECONDS_PER_HOUR,
                    counted_audio,
                    audio_value,
                ),
                self._capacity_wait(
                    events,
                    timestamp,
                    DAY_SECONDS,
                    self.AUDIO_SECONDS_PER_DAY,
                    counted_audio,
                    audio_value,
                ),
            )
        )
        return max(waits)
