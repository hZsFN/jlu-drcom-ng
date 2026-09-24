"""Network quality probing: latency and packet loss (P2).

Uses the platform ``ping`` binary rather than raw sockets: raw ICMP needs
administrator privileges on Windows, and shelling out keeps the app
unprivileged.  Output parsing covers both the Windows and the iputils formats.

The probe deliberately runs *while offline as well as online*, so the UI can
show the "before vs after authentication" comparison the brief asks for.
"""

from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

__all__ = ["PingResult", "ProbeHistory", "NetworkProbe"]

IS_WINDOWS = sys.platform == "win32"

_WIN_REPLY = re.compile(r"(?:time|时间)[=<]\s*(\d+)\s*ms", re.IGNORECASE)
_WIN_LOSS = re.compile(r"\((\d+)%\s*(?:loss|丢失)\)", re.IGNORECASE)
_NIX_TIME = re.compile(r"time[=<]([\d.]+)\s*ms")
_NIX_LOSS = re.compile(r"(\d+(?:\.\d+)?)%\s*packet loss")


@dataclass(frozen=True)
class PingResult:
    """One probe of one target."""

    target: str
    sent: int
    received: int
    rtt_ms: float | None
    timestamp: float = field(default_factory=time.time)

    @property
    def loss_percent(self) -> float:
        if self.sent <= 0:
            return 0.0
        return (self.sent - self.received) / self.sent * 100.0

    @property
    def ok(self) -> bool:
        return self.received > 0


#: Hostnames/IPs only.  A target beginning with "-" would otherwise be read by
#: ping as an option (e.g. "-f" = flood), turning a config typo into a flood.
_SAFE_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,253}$")


def is_safe_target(target: str) -> bool:
    """Whether *target* is safe to hand to ``ping`` as a positional argument."""
    return bool(_SAFE_TARGET.match(target or "")) and not target.startswith("-")


def ping_once(
    target: str,
    *,
    count: int = 3,
    timeout_ms: int = 1500,
    source: str = "",
) -> PingResult:
    """Send *count* ICMP echoes and summarise the replies.

    *source* pins the probe to a specific local address.  Without it the OS
    routes by metric, so on a laptop with both Wi-Fi and campus Ethernet the
    probe measures the wrong link — which is exactly how you end up reporting
    "100% loss to the auth server" while happily authenticated over Ethernet.
    """
    if not is_safe_target(target):
        return PingResult(target, count, 0, None)
    if source and not is_safe_target(source):
        source = ""

    if IS_WINDOWS:
        argv = ["ping", "-n", str(count), "-w", str(timeout_ms)]
        if source:
            argv += ["-S", source]
        argv.append(target)
    else:
        timeout_s = max(1, timeout_ms // 1000)
        argv = ["ping", "-c", str(count), "-W", str(timeout_s)]
        if source:
            argv += ["-I", source]
        argv += ["--", target]  # end of options

    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_ms / 1000 + count * 2.0,
            creationflags=0x08000000 if IS_WINDOWS else 0,
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return PingResult(target, count, 0, None)

    output = proc.stdout or ""
    times = [float(m) for m in (_WIN_REPLY if IS_WINDOWS else _NIX_TIME).findall(output)]
    loss_match = (_WIN_LOSS if IS_WINDOWS else _NIX_LOSS).search(output)
    received = len(times)
    if loss_match:
        percent = float(loss_match.group(1))
        received = max(received, round(count * (1 - percent / 100.0)))
    rtt = sum(times) / len(times) if times else None
    return PingResult(target, count, min(received, count), rtt)


class ProbeHistory:
    """Rolling window of results per target."""

    def __init__(self, window: int = 40) -> None:
        self.window = window
        self.results: dict[str, list[PingResult]] = {}

    def add(self, result: PingResult) -> None:
        bucket = self.results.setdefault(result.target, [])
        bucket.append(result)
        del bucket[: max(0, len(bucket) - self.window)]

    def summary(self, target: str) -> dict:
        bucket = self.results.get(target, [])
        if not bucket:
            return {"target": target, "samples": 0, "rtt_ms": None, "loss_percent": None, "jitter_ms": None}
        rtts = [r.rtt_ms for r in bucket if r.rtt_ms is not None]
        loss = sum(r.loss_percent for r in bucket) / len(bucket)
        jitter = None
        if len(rtts) >= 2:
            diffs = [abs(rtts[i] - rtts[i - 1]) for i in range(1, len(rtts))]
            jitter = sum(diffs) / len(diffs)
        return {
            "target": target,
            "samples": len(bucket),
            "rtt_ms": round(sum(rtts) / len(rtts), 1) if rtts else None,
            "loss_percent": round(loss, 1),
            "jitter_ms": round(jitter, 1) if jitter is not None else None,
        }

    def all_summaries(self) -> list[dict]:
        return [self.summary(target) for target in self.results]

    def clear(self) -> None:
        self.results.clear()


class NetworkProbe:
    """Background prober; callbacks fire on the probe thread."""

    def __init__(self, config, log, *, on_result=None) -> None:
        self.config = config
        self.log = log
        self.on_result = on_result
        self.history = ProbeHistory()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._is_online = lambda: False
        #: Optional local address to pin probes to (set from the auth engine).
        self.source_address = ""

    def set_source_address(self, address: str) -> None:
        self.source_address = address if address and address != "0.0.0.0" else ""

    def set_online_predicate(self, predicate) -> None:
        self._is_online = predicate

    def start(self) -> None:
        if not self.config.enabled or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="drcom-probe", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def probe_now(self, target: str | None = None) -> list[PingResult]:
        targets = [target] if target else list(self.config.targets)
        out: list[PingResult] = []
        for item in targets:
            if self._stop.is_set():
                break
            result = ping_once(
                item, timeout_ms=self.config.timeout_ms, source=self.source_address
            )
            self.history.add(result)
            out.append(result)
            if self.on_result:
                try:
                    self.on_result(result)
                except Exception:  # pragma: no cover
                    pass
        return out

    def _loop(self) -> None:
        # Stagger the first probe so it does not compete with authentication.
        if self._stop.wait(3.0):
            return
        while not self._stop.is_set():
            if not (self.config.only_when_offline and self._is_online()):
                self.probe_now()
            if self._stop.wait(max(5.0, self.config.interval)):
                return
