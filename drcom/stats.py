"""Online-time and disconnect statistics.

Persisted to ``stats.json`` and checkpointed while online, so an unclean exit
(a crash, a power cut, the machine sleeping) loses at most one checkpoint
interval instead of the whole session.

Buckets are keyed by *local* date so "今日累计" matches what the user sees on
their clock.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

__all__ = ["DayStats", "StatsStore", "Totals"]


def _today() -> str:
    return date.today().isoformat()


@dataclass
class DayStats:
    """Per-day accumulator."""

    online_seconds: float = 0.0
    drops: int = 0
    sessions: int = 0
    login_failures: int = 0
    bytes_rx: int = 0
    bytes_tx: int = 0
    longest_session: float = 0.0
    first_online_at: float | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "DayStats":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def as_dict(self) -> dict:
        return {
            "online_seconds": round(self.online_seconds, 1),
            "drops": self.drops,
            "sessions": self.sessions,
            "login_failures": self.login_failures,
            "bytes_rx": self.bytes_rx,
            "bytes_tx": self.bytes_tx,
            "longest_session": round(self.longest_session, 1),
            "first_online_at": self.first_online_at,
        }


@dataclass
class Totals:
    """All-time counters."""

    online_seconds: float = 0.0
    drops: int = 0
    sessions: int = 0
    login_failures: int = 0
    bytes_rx: int = 0
    bytes_tx: int = 0
    longest_session: float = 0.0
    last_online_at: float | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "Totals":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def as_dict(self) -> dict:
        return {
            "online_seconds": round(self.online_seconds, 1),
            "drops": self.drops,
            "sessions": self.sessions,
            "login_failures": self.login_failures,
            "bytes_rx": self.bytes_rx,
            "bytes_tx": self.bytes_tx,
            "longest_session": round(self.longest_session, 1),
            "last_online_at": self.last_online_at,
        }


class StatsStore:
    """Tracks session lifetimes and daily totals."""

    CHECKPOINT_INTERVAL = 60.0

    def __init__(self, path: Path, *, keep_days: int = 120) -> None:
        self.path = Path(path)
        self.keep_days = keep_days
        self._lock = threading.RLock()
        self.daily: dict[str, DayStats] = {}
        self.totals = Totals()
        self._session_start: float | None = None
        self._last_checkpoint = 0.0
        self._ip = ""
        self._account = ""
        self.load()

    # -- persistence -----------------------------------------------------
    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        with self._lock:
            self.daily = {
                key: DayStats.from_dict(value)
                for key, value in (raw.get("daily") or {}).items()
                if isinstance(value, dict)
            }
            self.totals = Totals.from_dict(raw.get("totals") or {})

    def save(self) -> None:
        with self._lock:
            payload = {
                "version": 1,
                "daily": {k: v.as_dict() for k, v in self.daily.items()},
                "totals": self.totals.as_dict(),
            }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    # -- session lifecycle ------------------------------------------------
    def open_session(self, *, ip: str, account: str) -> None:
        with self._lock:
            self._session_start = time.time()
            self._last_checkpoint = self._session_start
            self._ip = ip
            self._account = account
            day = self.daily.setdefault(_today(), DayStats())
            day.sessions += 1
            if day.first_online_at is None:
                day.first_online_at = self._session_start
            self.totals.sessions += 1
            self.totals.last_online_at = self._session_start
        self.save()

    def close_session(self, *, drop: bool = True) -> float:
        """Finish the current session; returns its duration in seconds."""
        with self._lock:
            if self._session_start is None:
                return 0.0
            duration = max(0.0, time.time() - self._session_start)
            day = self.daily.setdefault(_today(), DayStats())
            day.online_seconds += duration
            day.longest_session = max(day.longest_session, duration)
            self.totals.online_seconds += duration
            self.totals.longest_session = max(self.totals.longest_session, duration)
            if drop:
                day.drops += 1
                self.totals.drops += 1
            self._session_start = None
        self.save()
        return duration

    def note_login_failure(self) -> None:
        with self._lock:
            self.daily.setdefault(_today(), DayStats()).login_failures += 1
            self.totals.login_failures += 1
        self.save()

    def checkpoint(self, *, force: bool = False) -> None:
        """Fold elapsed online time into today's bucket without ending it."""
        with self._lock:
            if self._session_start is None:
                return
            now = time.time()
            if not force and now - self._last_checkpoint < self.CHECKPOINT_INTERVAL:
                return
            elapsed = now - self._last_checkpoint
            self._last_checkpoint = now
            day = self.daily.setdefault(_today(), DayStats())
            day.online_seconds += elapsed
            day.longest_session = max(day.longest_session, now - self._session_start)
            self.totals.online_seconds += elapsed
            self.totals.longest_session = max(self.totals.longest_session, now - self._session_start)
        self.save()

    def add_traffic(self, rx: int, tx: int) -> None:
        with self._lock:
            day = self.daily.setdefault(_today(), DayStats())
            day.bytes_rx += rx
            day.bytes_tx += tx
            self.totals.bytes_rx += rx
            self.totals.bytes_tx += tx

    # -- queries -----------------------------------------------------------
    @property
    def session_seconds(self) -> float:
        with self._lock:
            if self._session_start is None:
                return 0.0
            return time.time() - self._session_start

    def today(self) -> DayStats:
        with self._lock:
            return self.daily.setdefault(_today(), DayStats())

    def last_days(self, count: int = 7) -> list[tuple[str, DayStats]]:
        with self._lock:
            out: list[tuple[str, DayStats]] = []
            for offset in range(count - 1, -1, -1):
                key = (date.today() - timedelta(days=offset)).isoformat()
                out.append((key, self.daily.get(key, DayStats())))
            return out

    def week_seconds(self) -> float:
        return sum(day.online_seconds for _key, day in self.last_days(7))

    def prune(self) -> None:
        """Drop buckets older than ``keep_days``."""
        if self.keep_days <= 0:
            return
        cutoff = (date.today() - timedelta(days=self.keep_days)).isoformat()
        with self._lock:
            removed = [key for key in self.daily if key < cutoff]
            for key in removed:
                del self.daily[key]
        if removed:
            self.save()

    def as_status(self) -> dict:
        today = self.today()
        return {
            "today_seconds": round(today.online_seconds + (self.session_seconds if self._session_start else 0.0), 1),
            "today_drops": today.drops,
            "today_sessions": today.sessions,
            "today_login_failures": today.login_failures,
            "week_seconds": round(self.week_seconds(), 1),
            "total_seconds": round(self.totals.online_seconds + (self.session_seconds if self._session_start else 0.0), 1),
            "total_drops": self.totals.drops,
            "total_sessions": self.totals.sessions,
            "longest_session": round(self.totals.longest_session, 1),
            "bytes_rx": self.totals.bytes_rx,
            "bytes_tx": self.totals.bytes_tx,
        }
