"""Network quality probing: latency, jitter and packet loss (P2).

Two probe kinds, because one of them is not enough on a real machine:

**ICMP** (via the platform ``ping`` binary) is the honest measure of a plain
link, and it is what we use for the campus gateway.  Raw ICMP needs
administrator privileges on Windows, so shelling out keeps the app
unprivileged; output parsing covers the Windows and the iputils formats.

**HTTP** (time to first response byte from a real website) is what we use for
the internet, because on a machine running a VPN in TUN mode ICMP is simply not
usable: the tunnel does not carry it, so public addresses look 100% lost while
the connection is perfectly fine.  TCP connect is no better -- the proxy client
completes the handshake locally, so a connect takes ~1 ms and measures the
proxy, not the network.  An actual HTTP request is the first thing that has to
travel end to end, so that is what we time.

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

__all__ = ["PingResult", "ProbeHistory", "NetworkProbe", "probe_once", "is_http_target"]

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
    #: The individual round trips, not just their mean.  Jitter computed from a
    #: series of means is not jitter -- averaging three pings first hides
    #: exactly the variation we are trying to report.
    rtts: tuple[float, ...] = ()

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

#: HTTP probes get at least this long, whatever the configured timeout says.
#: That setting was chosen for ICMP to a campus box a millisecond away; a real
#: request through a proxy has a long tail (a site answering in 250 ms will
#: occasionally take 2 s), and with the tighter figure those slow-but-fine
#: replies were being counted as packet loss -- bing read 66% loss while
#: answering every time a hand-run test asked.
HTTP_TIMEOUT_FLOOR_MS = 3000

#: URLs we are willing to fetch.  Same reasoning as _SAFE_TARGET, plus: no
#: credentials in the URL, and no scheme other than http(s).
_SAFE_URL = re.compile(r"^https?://[A-Za-z0-9][A-Za-z0-9._:\-]{0,253}(?:/[^\s]*)?$", re.IGNORECASE)


def is_http_target(target: str) -> bool:
    """Whether *target* is a URL (probed over HTTP) rather than a host (ICMP)."""
    return bool(target) and target.lower().startswith(("http://", "https://"))


def is_safe_target(target: str) -> bool:
    """Whether *target* is safe to probe."""
    if is_http_target(target):
        return bool(_SAFE_URL.match(target))
    return bool(_SAFE_TARGET.match(target or "")) and not target.startswith("-")


def _http_request_once(url: str, timeout_ms: int) -> float | None:
    """One request; returns the elapsed milliseconds, or None on failure."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        url,
        headers={
            # Some sites stall or reject the default Python agent outright.
            "User-Agent": "Mozilla/5.0 (compatible; JLU-DrCOM-NG)",
            "Accept": "*/*",
            "Connection": "close",
        },
        method="GET",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=max(0.05, timeout_ms / 1000)) as response:
            response.read(1)  # first byte, so DNS+connect+TLS+server are all counted
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return (time.perf_counter() - started) * 1000.0


def http_once(url: str, *, count: int = 3, timeout_ms: int = 1500) -> PingResult:
    """Time *count* real requests to *url*, up to the first response byte each.

    We stop at the headers: this is a latency measurement, not a download, and
    reading the body would fold the site's payload size into the number.

    Several requests per round, not one, because jitter is the variation between
    *consecutive* round trips.  With a single request per round the two samples
    being differenced are a whole probe interval apart, which measures how the
    network changed over that minute rather than how steady it is -- and it read
    about six times too high.
    """
    if not is_safe_target(url):
        return PingResult(url, count, 0, None)

    # The round owns the time budget, so the floor is applied here rather than
    # buried in the single-request helper.
    timeout_ms = max(timeout_ms, HTTP_TIMEOUT_FLOOR_MS)
    times = [t for t in (_http_request_once(url, timeout_ms) for _ in range(max(1, count)))
             if t is not None]
    if not times:
        return PingResult(url, count, 0, None)
    return PingResult(
        url, count, len(times), sum(times) / len(times), rtts=tuple(times)
    )


def probe_once(
    target: str,
    *,
    count: int = 3,
    timeout_ms: int = 1500,
    source: str = "",
) -> PingResult:
    """Probe *target* with whichever method suits it."""
    if is_http_target(target):
        return http_once(target, count=count, timeout_ms=timeout_ms)
    return ping_once(target, count=count, timeout_ms=timeout_ms, source=source)


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
    return PingResult(target, count, min(received, count), rtt, rtts=tuple(times))


class ProbeHistory:
    """Rolling window of results per target.

    Jitter is the mean absolute difference between *consecutive individual*
    round trips -- the RFC 3550 definition, and the one that actually says
    something about a link.  Differencing per-round averages instead (as this
    used to) reports a fraction of the real figure, because averaging three
    pings is itself a smoothing filter.
    """

    def __init__(self, window: int = 40, sample_window: int = 180) -> None:
        self.window = window
        #: How many individual round trips to keep per target.  A round of ICMP
        #: contributes `count` samples, an HTTP probe one.
        self.sample_window = sample_window
        self.results: dict[str, list[PingResult]] = {}
        self._samples: dict[str, list[float]] = {}
        self._order: list[str] = []

    def add(self, result: PingResult) -> None:
        if result.target not in self._order:
            self._order.append(result.target)
        bucket = self.results.setdefault(result.target, [])
        bucket.append(result)
        del bucket[: max(0, len(bucket) - self.window)]

        samples = self._samples.setdefault(result.target, [])
        samples.extend(result.rtts)
        del samples[: max(0, len(samples) - self.sample_window)]

    def targets(self) -> list[str]:
        return list(self._order)

    def summary(self, target: str) -> dict:
        bucket = self.results.get(target, [])
        samples = self._samples.get(target, [])
        # The method is determined by the target, so it is derived rather than
        # stored: a recorded copy can drift out of step with the thing it
        # describes (and did, the first time this was written).
        kind = "http" if is_http_target(target) else "icmp"
        if not bucket:
            return {
                "target": target,
                "kind": kind,
                "samples": 0,
                "pings": 0,
                "rtt_ms": None,
                "loss_percent": None,
                "jitter_ms": None,
            }

        sent = sum(r.sent for r in bucket)
        received = sum(r.received for r in bucket)
        loss = (sent - received) / sent * 100.0 if sent else 0.0

        # Jitter is computed *within* each round and then averaged across
        # rounds.  Differencing samples from different rounds would measure how
        # the network drifted over the probe interval (a minute apart) rather
        # than how steady the link is -- and it read several times too high.
        round_jitters = [_mean_abs_delta(r.rtts) for r in bucket if len(r.rtts) >= 2]
        jitter = sum(round_jitters) / len(round_jitters) if round_jitters else None

        return {
            "target": target,
            "kind": kind,
            "samples": len(bucket),
            "pings": len(samples),
            "rtt_ms": round(sum(samples) / len(samples), 1) if samples else None,
            "loss_percent": round(loss, 1),
            "jitter_ms": round(jitter, 1) if jitter is not None else None,
        }

    def all_summaries(self) -> list[dict]:
        return [self.summary(target) for target in self._order]

    def display_summary(self) -> dict:
        """The single figure the dashboard shows.

        Internet targets win: the campus gateway answers in about a millisecond
        and says nothing about whether a web page will load.  When there is more
        than one internet target they are combined, so one site having a bad
        minute does not blank the instrument.
        """
        if not self._order:
            return self.summary("—")

        internet = [t for t in self._order if is_http_target(t)]
        picks = [t for t in (internet or self._order) if self.results.get(t)]
        if not picks:
            return self.summary(self._order[0])
        if len(picks) == 1:
            return self.summary(picks[0])

        summaries = [self.summary(t) for t in picks]
        live = [s for s in summaries if s["rtt_ms"] is not None]
        if not live:
            return summaries[0]

        rtts = [s["rtt_ms"] for s in live]
        sent = sum(s["samples"] for s in live)
        return {
            "target": ", ".join(_short_name(s["target"]) for s in live),
            "kind": live[0]["kind"],
            "samples": sum(s["samples"] for s in summaries),
            "pings": sum(s["pings"] for s in summaries),
            "rtt_ms": round(sum(rtts) / len(rtts), 1),
            "loss_percent": round(
                sum(s["loss_percent"] or 0.0 for s in summaries) / max(1, sent), 1
            ),
            "jitter_ms": _mean_or_none([s["jitter_ms"] for s in live]),
        }

    def clear(self) -> None:
        self.results.clear()
        self._samples.clear()
        self._order.clear()



def _mean_abs_delta(values) -> float:
    """Mean absolute difference between consecutive values (RFC 3550 jitter)."""
    seq = list(values)
    if len(seq) < 2:
        return 0.0
    diffs = [abs(seq[i] - seq[i - 1]) for i in range(1, len(seq))]
    return sum(diffs) / len(diffs)
def _mean_or_none(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return round(sum(present) / len(present), 1) if present else None


def _short_name(target: str) -> str:
    """``http://www.baidu.com`` -> ``www.baidu.com`` for the dashboard label."""
    return re.sub(r"^https?://", "", target).rstrip("/") or target


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
            result = probe_once(
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

    def warm_up(self) -> None:
        """One throwaway probe per target, before any result is recorded.

        The first request to a site pays for DNS and for opening the path
        through whatever proxy is running; it can be several times the steady
        figure and, recorded, it dominates a jitter average for the whole
        window.  Warming the path first is what makes the number comparable
        between runs.
        """
        for target in list(self.config.targets):
            if self._stop.is_set():
                return
            probe_once(target, timeout_ms=self.config.timeout_ms, source=self.source_address)

    def _loop(self) -> None:
        # Stagger the first probe so it does not compete with authentication.
        if self._stop.wait(3.0):
            return
        try:
            self.warm_up()
        except Exception:  # pragma: no cover - a warm-up must never kill the loop
            pass
        while not self._stop.is_set():
            if not (self.config.only_when_offline and self._is_online()):
                self.probe_now()
            if self._stop.wait(max(5.0, self.config.interval)):
                return
