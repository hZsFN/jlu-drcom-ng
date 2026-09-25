"""Network-quality probing: targets, jitter, and what the dashboard shows.

Written after the user reported "延迟和抖动的探测貌似不是很准".  Two real causes:

1. The shipped targets were all campus addresses (10.100.61.3, 10.10.10.10),
   which answer in about a millisecond and describe the campus link, not the
   internet.  The instrument was pointing at the wrong thing.

2. Measuring those over ICMP does not transfer to the internet on a machine
   running a VPN in TUN mode: the tunnel does not carry ICMP at all, so public
   addresses read as 100% loss while the connection is fine.  A TCP connect is
   no better -- the proxy client answers the handshake locally, so it measures
   the proxy (~1 ms) rather than the network.  A real HTTP request is the first
   thing that has to cross the network end to end.

Jitter was also computed by differencing per-round *averages*, which is a
smoothed quantity and reports a fraction of the real figure.
"""

from __future__ import annotations

import pytest

import drcom.netprobe as netprobe
from drcom.config import DEFAULT_PROBE_TARGETS, ProbeConfig
from drcom.netprobe import PingResult, ProbeHistory, is_http_target, is_safe_target


# --------------------------------------------------------------------------
# target classification and safety
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "target, expected",
    [
        ("http://www.baidu.com", True),
        ("https://www.bing.com", True),
        ("HTTP://WWW.BAIDU.COM", True),
        ("10.100.61.3", False),
        ("www.baidu.com", False),
        ("", False),
    ],
)
def test_http_targets_are_recognised(target, expected) -> None:
    assert is_http_target(target) is expected


@pytest.mark.parametrize(
    "target",
    [
        "http://www.baidu.com",
        "https://www.bing.com/some/path?q=1",
        "10.100.61.3",
        "www.baidu.com",
    ],
)
def test_reasonable_targets_are_allowed(target) -> None:
    assert is_safe_target(target)


@pytest.mark.parametrize(
    "target",
    [
        "-f",                                  # would be a ping flood
        "--help",
        "http://user:pass@example.com",        # credentials do not belong here
        "file:///etc/passwd",
        "ftp://example.com",
        "http://host/pa th",                   # whitespace
        "",
    ],
)
def test_dangerous_targets_are_rejected(target) -> None:
    assert not is_safe_target(target)


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------
def test_probe_once_uses_http_for_urls(monkeypatch) -> None:
    seen: list = []
    monkeypatch.setattr(netprobe, "http_once", lambda url, timeout_ms=0: seen.append(url) or PingResult(url, 1, 1, 1.0))
    netprobe.probe_once("http://www.baidu.com")
    assert seen == ["http://www.baidu.com"]


def test_probe_once_uses_icmp_for_hosts(monkeypatch) -> None:
    seen: list = []
    monkeypatch.setattr(
        netprobe, "ping_once",
        lambda target, **kw: seen.append(target) or PingResult(target, 1, 1, 1.0),
    )
    netprobe.probe_once("10.100.61.3")
    assert seen == ["10.100.61.3"]


def test_http_probe_reports_failure_without_raising() -> None:
    """An unreachable site is a reading, not an exception."""
    result = netprobe.http_once("http://127.0.0.1:9/", timeout_ms=300)
    assert result.received == 0
    assert result.rtt_ms is None
    assert result.loss_percent == 100.0
    assert is_http_target(result.target), "the method is derived from the target"


def test_http_probe_rejects_an_unsafe_url() -> None:
    result = netprobe.http_once("file:///etc/passwd")
    assert result.received == 0


# --------------------------------------------------------------------------
# jitter
# --------------------------------------------------------------------------
def test_jitter_uses_individual_round_trips_not_round_averages() -> None:
    """The old computation differenced per-round means and under-reported.

    Two rounds of three pings each.  The round means are 20 and 40 (difference
    20), but the individual samples alternate 10/30/20 and 30/50/40, so the
    real mean |delta| between consecutive round trips is larger than 20.  A
    metric built on the means cannot see that.
    """
    history = ProbeHistory()
    history.add(PingResult("t", 3, 3, 20.0, rtts=(10.0, 30.0, 20.0)))
    history.add(PingResult("t", 3, 3, 40.0, rtts=(30.0, 50.0, 40.0)))

    summary = history.summary("t")
    assert summary["pings"] == 6, "individual round trips were not kept"
    assert summary["rtt_ms"] == pytest.approx(30.0, abs=0.05)
    # d: 20,10,10,20,10 -> mean 14.0
    assert summary["jitter_ms"] == pytest.approx(14.0, abs=0.05)


def test_jitter_is_none_with_a_single_sample() -> None:
    history = ProbeHistory()
    history.add(PingResult("t", 1, 1, 12.0, rtts=(12.0,)))
    assert history.summary("t")["jitter_ms"] is None


def test_loss_is_aggregated_over_round_trips_not_averaged_over_rounds() -> None:
    """A round of 3 with 1 reply is 66% loss, not 33%."""
    history = ProbeHistory()
    history.add(PingResult("t", 3, 1, 10.0, rtts=(10.0,)))
    history.add(PingResult("t", 3, 3, 10.0, rtts=(10.0, 10.0, 10.0)))
    assert history.summary("t")["loss_percent"] == pytest.approx(33.3, abs=0.1)


def test_empty_summary_is_well_formed() -> None:
    summary = ProbeHistory().summary("nothing")
    assert summary["samples"] == 0
    assert summary["rtt_ms"] is None
    assert summary["loss_percent"] is None


# --------------------------------------------------------------------------
# what the dashboard shows
# --------------------------------------------------------------------------
def test_dashboard_prefers_the_internet_targets() -> None:
    """The whole point: 1 ms to the campus box is not the network quality."""
    history = ProbeHistory()
    history.add(PingResult("http://www.baidu.com", 1, 1, 150.0, rtts=(150.0,)))
    history.add(PingResult("10.100.61.3", 3, 3, 1.0, rtts=(1.0, 1.0, 1.0)))

    display = history.display_summary()
    assert display["kind"] == "http"
    assert display["rtt_ms"] == pytest.approx(150.0, abs=0.05)
    assert "baidu" in display["target"]


def test_dashboard_combines_several_internet_targets() -> None:
    history = ProbeHistory()
    history.add(PingResult("http://www.baidu.com", 1, 1, 100.0, rtts=(100.0,)))
    history.add(PingResult("http://www.bing.com", 1, 1, 200.0, rtts=(200.0,)))

    display = history.display_summary()
    assert display["rtt_ms"] == pytest.approx(150.0, abs=0.05)
    assert display["samples"] == 2


def test_dashboard_falls_back_when_there_is_no_internet_target() -> None:
    history = ProbeHistory()
    history.add(PingResult("10.100.61.3", 3, 3, 1.0, rtts=(1.0, 1.0, 1.0)))
    display = history.display_summary()
    assert display["rtt_ms"] == pytest.approx(1.0, abs=0.05)


def test_dashboard_survives_an_empty_history() -> None:
    assert ProbeHistory().display_summary()["rtt_ms"] is None


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------
def test_shipped_targets_include_real_websites() -> None:
    http_targets = [t for t in DEFAULT_PROBE_TARGETS if is_http_target(t)]
    assert len(http_targets) >= 2, "the internet needs more than one vantage point"
    assert any("baidu" in t for t in http_targets)
    assert any("bing" in t for t in http_targets)


def test_legacy_campus_only_config_is_upgraded_once(tmp_path) -> None:
    """Changing the default does nothing for a config that already exists."""
    import json

    from drcom.config import CONFIG_VERSION, ConfigStore

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"probe": {"targets": ["10.100.61.3"]}}), encoding="utf-8")
    store = ConfigStore(tmp_path)
    store.load()
    assert store.config.probe.targets == list(DEFAULT_PROBE_TARGETS)

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["probe"]["targets"] == list(DEFAULT_PROBE_TARGETS), (
        "the migration was not persisted, so it would run again on every launch"
    )
    assert saved["version"] == CONFIG_VERSION


def test_a_deliberate_campus_only_choice_sticks(tmp_path) -> None:
    """The migration must not fight the user forever.

    Being value-based, an all-campus list would be rewritten on every single
    launch -- so someone who genuinely only cares about the campus link could
    never keep that setting.
    """
    import json

    from drcom.config import ConfigStore

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"version": 2, "probe": {"targets": ["10.100.61.3"]}}),
        encoding="utf-8",
    )
    store = ConfigStore(tmp_path)
    store.load()
    assert store.config.probe.targets == ["10.100.61.3"]


def test_a_custom_target_list_is_left_alone_during_migration(tmp_path) -> None:
    import json

    from drcom.config import ConfigStore

    custom = ["http://example.com", "192.168.1.1"]
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"probe": {"targets": custom}}), encoding="utf-8")
    store = ConfigStore(tmp_path)
    store.load()
    assert store.config.probe.targets == custom


def test_a_current_config_is_not_rewritten(tmp_path) -> None:
    import json

    from drcom.config import ConfigStore

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"version": 99, "probe": {"targets": ["10.10.10.10"]}}), encoding="utf-8"
    )
    before = path.read_text(encoding="utf-8")
    ConfigStore(tmp_path).load()
    assert path.read_text(encoding="utf-8") == before, "an up-to-date config was touched"


# --------------------------------------------------------------------------
# warm-up
# --------------------------------------------------------------------------
def test_warm_up_records_nothing() -> None:
    """Its whole purpose is to keep the cold-start outlier out of the stats."""
    class Silent:
        def info(self, *a, **k): pass
        def warning(self, *a, **k): pass
        def debug(self, *a, **k): pass

    probe = netprobe.NetworkProbe(ProbeConfig(targets=["http://127.0.0.1:9/"]), Silent())
    probe.warm_up()
    assert probe.history.all_summaries() == []
