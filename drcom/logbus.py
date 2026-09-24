"""Logging with account masking plus an in-memory ring buffer for the UI.

Two sinks:

* a rotating file log (for ``--export-logs`` and for reporting bugs)
* a bounded in-memory deque the GUI drains into its log panel

Hex packet dumps are emitted at ``TRACE``-ish level via the dedicated
:meth:`LogBus.packet` helper so they can be turned off independently — they are
noisy but they are also the single most useful debugging aid we have.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Deque, Iterable

__all__ = ["LogBus", "LogRecordView", "hexdump", "mask_text"]

#: Custom level between DEBUG(10) and INFO(20); used for raw packet dumps.
PACKET_LEVEL = 15
logging.addLevelName(PACKET_LEVEL, "PACKET")

_ACCOUNT_RE = re.compile(r"\b\d{6,14}\b")
_MAC_RE = re.compile(r"\b([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b")


def mask_text(text: str, *, mask_accounts: bool = True) -> str:
    """Redact long digit runs (account numbers) in a log line.

    Deliberately conservative: only digit runs of 6+ characters are touched, so
    IP addresses, ports, timings and error codes survive intact.
    """

    def _sub(match: re.Match[str]) -> str:
        value = match.group(0)
        if len(value) <= 4:
            return value
        return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"

    if mask_accounts:
        text = _ACCOUNT_RE.sub(_sub, text)
    return text


def hexdump(
    data: bytes,
    *,
    prefix: str = "",
    max_bytes: int = 512,
    redact: Iterable[tuple[int, int]] = (),
) -> str:
    """Compact space-separated hex, matching the reference client's format.

    *redact* is a list of ``(start, end)`` byte ranges to blank out.  Redacted
    bytes are rendered as ``--`` rather than being removed, so the offsets in
    the dump still line up with the real packet — which is the whole point of
    having a hex dump when you are diffing against a capture.
    """
    ranges = tuple(redact)
    shown = data[:max_bytes]
    parts = []
    for index, byte in enumerate(shown):
        if any(low <= index < high for low, high in ranges):
            parts.append("--")
        else:
            parts.append(f"{byte:02x}")
    body = " ".join(parts)
    if ranges and any(low < len(shown) for low, _ in ranges):
        body += "  (敏感字段已用 -- 屏蔽)"
    if len(data) > max_bytes:
        body += f" ... (+{len(data) - max_bytes} bytes)"
    return f"{prefix}{body}".strip()


@dataclass(frozen=True)
class LogRecordView:
    """A log line as the GUI sees it."""

    seq: int
    timestamp: float
    level: str
    message: str
    is_packet: bool = False

    @property
    def clock(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.timestamp))


class _RingHandler(logging.Handler):
    """Feeds a bounded deque so the GUI can show recent lines instantly."""

    def __init__(self, bus: "LogBus") -> None:
        super().__init__(level=logging.DEBUG)
        self._bus = bus

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return
        if record.exc_info and record.exc_info[0] is not None:
            message += " | " + repr(record.exc_info[1])
        view = LogRecordView(
            seq=self._bus._next_seq(),
            timestamp=record.created,
            level=record.levelname,
            message=mask_text(message, mask_accounts=self._bus.mask_accounts),
            is_packet=record.levelno == PACKET_LEVEL,
        )
        with self._bus._lock:
            self._bus._buffer.append(view)
            listeners = list(self._bus._listeners)
        for listener in listeners:
            try:
                listener(view)
            except Exception:  # pragma: no cover - a bad listener must not kill logging
                pass


class LogBus:
    """Central logging hub."""

    def __init__(
        self,
        *,
        log_dir: Path,
        level: str = "INFO",
        mask_accounts: bool = True,
        protocol_hex: bool = True,
        protocol_hex_unredacted: bool = False,
        keep_days: int = 14,
        max_file_mb: float = 8.0,
        buffer_size: int = 2000,
    ) -> None:
        self.log_dir = Path(log_dir)
        self.mask_accounts = mask_accounts
        self.protocol_hex = protocol_hex
        #: Escape hatch for protocol debugging: dump the login packet verbatim,
        #: account string included.  Never on by default (spec 9).
        self.protocol_hex_unredacted = protocol_hex_unredacted
        self._buffer: Deque[LogRecordView] = deque(maxlen=buffer_size)
        self._listeners: list[Callable[[LogRecordView], None]] = []
        self._lock = threading.Lock()
        self._seq = 0
        self.logger = logging.getLogger("drcom")
        self.file_path: Path | None = None
        self._configure(level=level, keep_days=keep_days, max_file_mb=max_file_mb)

    # -- setup ---------------------------------------------------------
    def _configure(self, *, level: str, keep_days: int, max_file_mb: float) -> None:
        self.logger.handlers.clear()
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False

        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        self.file_path = self.log_dir / f"drcom-{stamp}.log"

        file_handler = logging.handlers.RotatingFileHandler(
            self.file_path,
            maxBytes=int(max_file_mb * 1024 * 1024),
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
        )
        self.logger.addHandler(file_handler)

        console = logging.StreamHandler(sys.stderr)
        console.setLevel(getattr(logging, level.upper(), logging.INFO))
        console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
        self.logger.addHandler(console)

        self.logger.addHandler(_RingHandler(self))
        self._prune_old_logs(keep_days)

    def _prune_old_logs(self, keep_days: int) -> None:
        if keep_days <= 0:
            return
        cutoff = time.time() - keep_days * 86400
        for path in self.log_dir.glob("drcom-*.log*"):
            if path == self.file_path:
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass

    # -- sequence / listeners -------------------------------------------
    def _next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def add_listener(self, listener: Callable[[LogRecordView], None]) -> None:
        with self._lock:
            self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[LogRecordView], None]) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def snapshot(self) -> list[LogRecordView]:
        with self._lock:
            return list(self._buffer)

    def drain_since(self, seq: int) -> list[LogRecordView]:
        with self._lock:
            return [r for r in self._buffer if r.seq > seq]

    # -- emit ------------------------------------------------------------
    # All of these accept printf-style arguments, mirroring the stdlib logging
    # API, so callers can write log.info("bound %s:%d", host, port) and let the
    # formatter run *after* masking.
    def debug(self, message: str, *args) -> None:
        self.logger.debug(message, *args)

    def info(self, message: str, *args) -> None:
        self.logger.info(message, *args)

    def warning(self, message: str, *args) -> None:
        self.logger.warning(message, *args)

    def error(self, message: str, *args, exc_info: bool = False) -> None:
        self.logger.error(message, *args, exc_info=exc_info)

    def critical(self, message: str, *args) -> None:
        self.logger.critical(message, *args)

    def packet(
        self,
        direction: str,
        label: str,
        data: bytes,
        *,
        redact: Iterable[tuple[int, int]] = (),
    ) -> None:
        """Log a raw packet at :data:`PACKET_LEVEL` (honours ``protocol_hex``).

        *redact* blanks credential-bearing byte ranges.  The login packet is
        the only one that carries user data — the account string sits at
        ``[20 : 20+len)`` — and writing it to a log file verbatim would defeat
        the account masking the rest of this module does, so callers pass the
        ranges to hide.  Set ``protocol_hex_unredacted`` to keep them.
        """
        if not self.protocol_hex:
            return
        if self.protocol_hex_unredacted:
            redact = ()
        arrow = {"tx": "->", "rx": "<-", "note": "  "}.get(direction, "--")
        self.logger.log(
            PACKET_LEVEL,
            "%s %-22s len=%-4d %s",
            arrow,
            label,
            len(data),
            hexdump(data, redact=redact),
        )

    def note(self, message: str) -> None:
        """A human-facing protocol milestone, always shown."""
        self.logger.info("· %s", message)

    # -- export ----------------------------------------------------------
    def export(self, destination: Path, *, since_seq: int = 0) -> Path:
        """Write the buffered lines to *destination* as a shareable report."""
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(r.timestamp))} "
            f"{r.level:<7} {r.message}"
            for r in self.snapshot()
            if r.seq > since_seq
        ]
        destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return destination

    def tail_file(self, max_lines: int = 400) -> Iterable[str]:
        if not self.file_path or not self.file_path.exists():
            return []
        with self.file_path.open("r", encoding="utf-8", errors="replace") as handle:
            return deque(handle, maxlen=max_lines)
