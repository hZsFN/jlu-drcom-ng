"""Tests for Phase 1 fixes: stats double-count, ProbeHistory loss, AuthEngine backoff."""

from __future__ import annotations

import drcom.stats as stats_mod
from drcom.config import Account, AppConfig
from drcom.engine import AuthEngine, EngineState, OfflineReason
from drcom.logbus import LogBus
from drcom.netprobe import PingResult, ProbeHistory
from drcom.stats import StatsStore


def test_stats_store_does_not_double_count_session(tmp_path, monkeypatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(stats_mod.time, "time", lambda: clock[0])

    store = StatsStore(tmp_path / "stats.json")
    store.open_session(ip="10.0.0.1", account="20230001")
    assert store.unflushed_session_seconds == 0.0

    clock[0] = 1070.0
    store.checkpoint()
    assert store.today().online_seconds == 70.0
    assert store.unflushed_session_seconds == 0.0
    assert store.as_status()["today_seconds"] == 70.0

    clock[0] = 1100.0
    assert store.unflushed_session_seconds == 30.0
    assert store.as_status()["today_seconds"] == 100.0

    store.close_session(drop=False)
    assert store.today().online_seconds == 100.0
    assert store.unflushed_session_seconds == 0.0
    assert store.week_seconds() == 100.0
    assert store.totals.online_seconds == 100.0
    assert store.as_status()["total_seconds"] == 100.0
    assert store.as_status()["today_seconds"] == 100.0


def test_probe_history_display_summary_loss_and_retain() -> None:
    history = ProbeHistory()
    # Two rounds: 20 sent, 15 received -> overall loss = (20-15)/20 = 25%
    history.add(PingResult("1.1.1.1", sent=10, received=10, rtt_ms=12.0))
    history.add(PingResult("1.1.1.1", sent=10, received=5, rtt_ms=18.0))
    history.add(PingResult("10.100.61.3", sent=5, received=5, rtt_ms=1.0))

    summary = history.display_summary()
    assert summary["target"] == "1.1.1.1"
    assert abs(summary["loss_percent"] - 25.0) < 1e-6

    history.retain_targets(["10.100.61.3"])
    assert "1.1.1.1" not in history.targets()
    assert history.display_summary()["target"] == "10.100.61.3"


def test_auth_engine_resets_consecutive_failures_only_after_keepalive_round(
    tmp_path, monkeypatch
) -> None:
    from types import SimpleNamespace
    import drcom.protocol as proto

    cfg = AppConfig()
    cfg.auth.post_login_delay = 0.0
    cfg.auth.keepalive_interval = 0.01
    acc = Account(account="20230001", mac="AA:BB:CC:DD:EE:FF")
    log = LogBus(log_dir=tmp_path / "logs", file_logging=False)
    engine = AuthEngine(config=cfg, account=acc, password="pw", log=log)

    class DummySock:
        def close(self) -> None:
            pass

    bound = SimpleNamespace(sock=DummySock(), local_port=61440, bind_address="0.0.0.0")
    challenge = SimpleNamespace(seed=b"\x01\x02\x03\x04", ip="10.0.0.5")

    monkeypatch.setattr(engine, "_send", lambda *_a, **_kw: None)
    monkeypatch.setattr(engine, "_recv_until", lambda *_a, **_kw: b"\x04" + b"\x00" * 44)
    monkeypatch.setattr(
        proto,
        "parse_login_response",
        lambda _raw: SimpleNamespace(
            success=True, error_code=None, message="ok", advice="", auth_information=b"\xaa" * 16
        ),
    )

    # 1. If keepalive_1 raises before completing a full round, consecutive_failures increments
    monkeypatch.setattr(engine, "_keepalive_1", lambda _s: (_ for _ in ()).throw(TimeoutError("ka1")))
    engine.consecutive_failures = 3
    res1 = engine._authenticate(bound, challenge)
    assert res1 == "transient"
    assert engine.consecutive_failures == 4

    # 2. When a full keepalive round succeeds, consecutive_failures resets to 0
    ka_calls = [0]

    def ok_ka1(_sock):
        return SimpleNamespace(tail=b"\x00\x00\x00\x00")

    def ok_ka2(_sock, counter, *, first=True, ka1=None):
        del first, ka1
        ka_calls[0] += 1
        engine._stop.set()
        return counter + 1, True

    monkeypatch.setattr(engine, "_keepalive_1", ok_ka1)
    monkeypatch.setattr(engine, "_keepalive_2", ok_ka2)
    engine._stop.clear()
    engine.consecutive_failures = 3
    res2 = engine._authenticate(bound, challenge)
    assert ka_calls[0] == 1
    assert engine.consecutive_failures == 0
    assert res2 == "transient"
    log.close()


def test_traffic_meter_sync_baseline_recovers_closed_ui_bytes(monkeypatch) -> None:
    import drcom.traffic as traffic_mod
    from drcom.netiface import InterfaceInfo
    from drcom.traffic import TrafficMeter

    clock = [5000.0]
    monkeypatch.setattr(traffic_mod.time, "time", lambda: clock[0])

    # Simulated NIC counter: login happened at rx=1_000_000, tx=200_000.
    # While the UI was closed, 500_000 B RX and 100_000 B TX transferred.
    state = {"rx": 1_500_000, "tx": 300_000}

    def fake_list():
        return [
            InterfaceInfo(
                name="Ethernet",
                index=7,
                is_up=True,
                bytes_in=state["rx"],
                bytes_out=state["tx"],
            )
        ]

    monkeypatch.setattr(traffic_mod, "list_interfaces", fake_list)

    meter = TrafficMeter()
    meter.sync_baseline(
        if_index=7,
        rx_base=1_000_000,
        tx_base=200_000,
        session_rx=500_000,
        session_tx=100_000,
    )
    # Immediately after re-attaching (before any tick), closed-UI traffic is visible.
    assert meter.snapshot.total_rx == 500_000
    assert meter.snapshot.total_tx == 100_000

    # Subsequent tick advances smoothly from the same baseline.
    clock[0] = 5002.0
    state["rx"] = 1_750_000
    state["tx"] = 350_000
    snap = meter.sample()
    assert snap.total_rx == 750_000
    assert snap.total_tx == 150_000
    assert abs(snap.rx_rate - 125_000.0) < 1e-3

    # Counter wrap clears baseline cleanly without going negative.
    clock[0] = 5004.0
    state["rx"] = 50_000
    state["tx"] = 10_000
    snap_wrap = meter.sample()
    assert snap_wrap.total_rx == 750_000
    assert snap_wrap.total_tx == 150_000


def test_stats_store_sync_native_session_folds_closed_ui_duration_without_double_count(
    tmp_path, monkeypatch
) -> None:
    clock = [2000.0]
    monkeypatch.setattr(stats_mod.time, "time", lambda: clock[0])

    path = tmp_path / "stats.json"
    s1 = StatsStore(path)
    opened = s1.sync_native_session(
        session_id=1, online_since=2000.0, ip="10.0.0.1", account="20****01"
    )
    assert opened is True
    assert s1.today().sessions == 1
    assert s1.totals.sessions == 1

    # UI stays open for 40s, then closes via _leave_ui_to_core() -> checkpoint(force=True)
    clock[0] = 2040.0
    s1.checkpoint(force=True)
    assert s1.today().online_seconds == 40.0

    # UI stays closed for 300s while drcomd keeps the session alive.
    clock[0] = 2340.0
    s2 = StatsStore(path)
    opened_again = s2.sync_native_session(
        session_id=1, online_since=2000.0, ip="10.0.0.1", account="20****01"
    )
    assert opened_again is False
    assert s2.today().sessions == 1
    assert s2.totals.sessions == 1
    assert abs(s2.today().online_seconds - 340.0) < 1e-6
    assert abs(s2.totals.online_seconds - 340.0) < 1e-6
    assert abs(s2.today().longest_session - 340.0) < 1e-6
    assert abs(s2.as_status()["today_seconds"] - 340.0) < 1e-6

    # A brand new native session increments sessions once.
    clock[0] = 2400.0
    opened_new = s2.sync_native_session(
        session_id=2, online_since=2395.0, ip="10.0.0.2", account="20****01"
    )
    assert opened_new is True
    assert s2.today().sessions == 2
    assert abs(s2.today().online_seconds - 345.0) < 1e-6
