"""Logging, masking, statistics, the local API and the CLI surface."""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from drcom.logbus import LogBus, hexdump, mask_text
from drcom.stats import DayStats, StatsStore


# --------------------------------------------------------------------------
# masking
# --------------------------------------------------------------------------
def test_account_numbers_are_masked() -> None:
    """Acceptance: logs must not carry a recoverable account number."""
    assert mask_text("账号 2023000001 已上线") == "账号 20******01 已上线"
    assert "230000" not in mask_text("account=2023000001")


def test_masking_leaves_technical_values_alone() -> None:
    """IPs, ports, error codes and timings must survive masking."""
    line = "绑定 0.0.0.0:61440 → 10.100.61.3:61440，错误码 10013，耗时 3000ms，hex 0x03"
    masked = mask_text(line)
    assert "61440" in masked
    assert "10.100.61.3" in masked
    assert "10013" in masked
    assert "3000ms" in masked
    assert "0x03" in masked


def test_short_numbers_are_untouched() -> None:
    assert mask_text("len=338 收 76 字节") == "len=338 收 76 字节"


def test_masking_can_be_disabled() -> None:
    assert mask_text("账号 2023000001", mask_accounts=False) == "账号 2023000001"


# --------------------------------------------------------------------------
# hexdump
# --------------------------------------------------------------------------
def test_hexdump_format_matches_the_reference_client() -> None:
    """The reference log uses space-separated lowercase hex — keep it identical
    so captures can be diffed."""
    assert hexdump(bytes((0x01, 0x02, 0xFF))) == "01 02 ff"


def test_hexdump_truncates_but_reports_the_rest() -> None:
    text = hexdump(bytes(600), max_bytes=512)
    assert "+88 bytes" in text


# --------------------------------------------------------------------------
# log bus
# --------------------------------------------------------------------------
@pytest.fixture
def bus(tmp_path: Path) -> LogBus:
    return LogBus(log_dir=tmp_path / "logs", level="DEBUG", protocol_hex=True)


def test_logbus_captures_into_the_ring_buffer(bus: LogBus) -> None:
    bus.info("hello %s", "world")
    lines = bus.snapshot()
    assert lines
    assert lines[-1].message == "hello world"
    assert lines[-1].level == "INFO"


def test_logbus_applies_printf_style_args(bus: LogBus) -> None:
    """The engine logs like the stdlib; this used to raise TypeError."""
    bus.info("启动认证：账号 %s，服务器 %s:%d", "20****01", "10.100.61.3", 61440)
    assert bus.snapshot()[-1].message == "启动认证：账号 20****01，服务器 10.100.61.3:61440"


def test_logbus_masks_accounts_on_the_way_in(bus: LogBus) -> None:
    bus.info("登录 %s", "2023000001")
    assert "2023000001" not in bus.snapshot()[-1].message


def test_logbus_packet_records(bus: LogBus) -> None:
    bus.packet("tx", "[Challenge sent]", bytes(range(20)))
    record = bus.snapshot()[-1]
    assert record.is_packet
    assert "[Challenge sent]" in record.message
    assert "00 01 02" in record.message


def test_protocol_hex_can_be_switched_off(bus: LogBus) -> None:
    bus.protocol_hex = False
    before = len(bus.snapshot())
    bus.packet("tx", "[Challenge sent]", bytes(20))
    assert len(bus.snapshot()) == before


def test_logbus_listeners_fire_and_failures_are_isolated(bus: LogBus) -> None:
    seen: list[str] = []

    def good(record) -> None:
        seen.append(record.message)

    def bad(record) -> None:
        raise RuntimeError("listener exploded")

    bus.add_listener(good)
    bus.add_listener(bad)
    bus.info("still works")
    assert "still works" in seen


def test_logbus_drain_since(bus: LogBus) -> None:
    bus.info("one")
    marker = bus.snapshot()[-1].seq
    bus.info("two")
    bus.info("three")
    new = bus.drain_since(marker)
    assert [r.message for r in new] == ["two", "three"]


def test_logbus_writes_a_file_and_exports(bus: LogBus, tmp_path: Path) -> None:
    bus.info("written to disk")
    assert bus.file_path is not None and bus.file_path.exists()
    destination = bus.export(tmp_path / "export.log")
    assert "written to disk" in destination.read_text(encoding="utf-8")


def test_logbus_is_thread_safe(bus: LogBus) -> None:
    def worker(index: int) -> None:
        for i in range(50):
            bus.info("thread %d line %d", index, i)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(bus.snapshot()) >= 200


def test_old_logs_are_pruned(tmp_path: Path) -> None:
    import os
    import time

    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stale = log_dir / "drcom-20000101_000000.log"
    stale.write_text("old", encoding="utf-8")
    old = time.time() - 90 * 86400
    os.utime(stale, (old, old))

    LogBus(log_dir=log_dir, level="INFO", keep_days=14)
    assert not stale.exists()


# --------------------------------------------------------------------------
# stats
# --------------------------------------------------------------------------
def test_stats_session_lifecycle(tmp_path: Path) -> None:
    store = StatsStore(tmp_path / "stats.json")
    store.open_session(ip="172.18.1.2", account="20****01")
    assert store.today().sessions == 1
    assert store.session_seconds >= 0
    duration = store.close_session()
    assert duration >= 0
    assert store.session_seconds == 0


def test_stats_persist_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "stats.json"
    store = StatsStore(path)
    store.open_session(ip="1.1.1.1", account="a")
    store.close_session()
    store.note_login_failure()

    reopened = StatsStore(path)
    assert reopened.totals.sessions == 1
    assert reopened.totals.drops == 1
    assert reopened.totals.login_failures == 1


def test_stats_checkpoints_do_not_double_count(tmp_path: Path) -> None:
    store = StatsStore(tmp_path / "stats.json")
    store.open_session(ip="1.1.1.1", account="a")
    store.checkpoint(force=True)
    first = store.today().online_seconds
    store.checkpoint(force=True)  # no time has passed
    assert store.today().online_seconds == pytest.approx(first, abs=0.2)
    store.close_session(drop=False)


def test_stats_close_without_open_is_safe(tmp_path: Path) -> None:
    store = StatsStore(tmp_path / "stats.json")
    assert store.close_session() == 0.0


def test_stats_week_window(tmp_path: Path) -> None:
    store = StatsStore(tmp_path / "stats.json")
    days = store.last_days(7)
    assert len(days) == 7
    assert days[-1][0] == store.last_days(1)[0][0]


def test_stats_corrupt_file_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "stats.json"
    path.write_text("{{{ not json", encoding="utf-8")
    store = StatsStore(path)
    assert store.totals.sessions == 0


def test_stats_status_document(tmp_path: Path) -> None:
    store = StatsStore(tmp_path / "stats.json")
    payload = store.as_status()
    for key in ("today_seconds", "today_drops", "week_seconds", "total_seconds", "bytes_rx"):
        assert key in payload


def test_day_stats_tolerates_unknown_keys() -> None:
    restored = DayStats.from_dict({"online_seconds": 5, "brand_new": 1})
    assert restored.online_seconds == 5


# --------------------------------------------------------------------------
# status payload + HTTP API
# --------------------------------------------------------------------------
def test_status_payload_has_the_documented_keys(controller) -> None:
    from drcom.statusapi import build_status_payload

    payload = build_status_payload(controller)
    for key in ("app", "version", "state", "online", "ip", "account", "uptime_seconds", "timestamp"):
        assert key in payload
    assert payload["app"] == "DrCOM-JLU"
    assert payload["state"] == "idle"


def test_status_payload_survives_a_broken_engine(controller) -> None:
    """The API must not raise just because the engine is half-built."""
    from drcom.statusapi import build_status_payload

    class Broken:
        pass

    controller.engine = Broken()
    payload = build_status_payload(controller)
    assert payload["online"] is False
    assert payload["state"] == "idle"


def test_metrics_rendering(controller) -> None:
    from drcom.statusapi import build_status_payload, render_metrics

    text = render_metrics(build_status_payload(controller))
    assert "drcom_online" in text
    assert "# TYPE drcom_online gauge" in text


def test_status_file_is_written(controller, temp_data_dir: Path) -> None:
    controller._write_status()
    path = temp_data_dir / "status.json"
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["app"] == "DrCOM-JLU"


def test_http_api_serves_status_and_control(controller) -> None:
    ok, message = controller.api.start()
    if not ok:
        pytest.skip(f"cannot bind the status port here: {message}")
    try:
        base = f"http://127.0.0.1:{controller.api.bound_port}"
        with urllib.request.urlopen(f"{base}/status", timeout=5) as response:
            payload = json.loads(response.read())
        assert payload["app"] == "DrCOM-JLU"

        with urllib.request.urlopen(f"{base}/health", timeout=5) as response:
            assert "online" in json.loads(response.read())

        with urllib.request.urlopen(f"{base}/metrics", timeout=5) as response:
            assert b"drcom_online" in response.read()

        with urllib.request.urlopen(f"{base}/diag", timeout=5) as response:
            assert "bind_port" in json.loads(response.read())

        # Unknown routes 404 rather than crash.
        with pytest.raises(urllib.error.HTTPError) as info:
            urllib.request.urlopen(f"{base}/nope", timeout=5)
        assert info.value.code == 404

        # Local control endpoint is reachable from localhost.
        request = urllib.request.Request(f"{base}/logout", data=b"", method="POST")
        with urllib.request.urlopen(request, timeout=5) as response:
            assert json.loads(response.read())["ok"] is True
    finally:
        controller.api.stop()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def test_cli_parser_accepts_the_documented_commands() -> None:
    from drcom.cli import _COMMANDS, build_parser

    parser = build_parser()
    for command in ("login", "status", "diag", "probe", "set", "export-logs"):
        assert command in _COMMANDS
        args = parser.parse_args(["--cli", command])
        assert args.cli == command


def test_cli_status_reports_offline_when_idle(controller, capsys) -> None:
    from drcom.cli import _cmd_status, build_parser

    args = build_parser().parse_args(["--cli", "status"])
    code = _cmd_status(controller, args)
    assert code == 1  # offline
    assert "离线" in capsys.readouterr().out


def test_cli_status_json_is_valid(controller, capsys) -> None:
    from drcom.cli import _cmd_status, build_parser

    args = build_parser().parse_args(["--cli", "status", "--json"])
    _cmd_status(controller, args)
    payload = json.loads(capsys.readouterr().out)
    assert payload["app"] == "DrCOM-JLU"


def test_cli_set_stores_credentials_encrypted(controller, temp_data_dir: Path) -> None:
    from drcom.cli import _cmd_set, build_parser

    args = build_parser().parse_args([
        "--cli", "set", "--account", "2023000001",
        "--mac", "AA:BB:CC:DD:EE:FF", "--password", "SuperSecret",
    ])
    assert _cmd_set(controller, args) == 0

    text = (temp_data_dir / "config.json").read_text(encoding="utf-8")
    assert "SuperSecret" not in text
    assert "2023000001" in text  # accounts are not secret
    reloaded = controller.store.get_password(controller.config.active_account().id)
    assert reloaded == "SuperSecret"


def test_cli_set_rejects_a_bad_mac(controller, capsys) -> None:
    from drcom.cli import _cmd_set, build_parser

    args = build_parser().parse_args(["--cli", "set", "--mac", "not-a-mac"])
    _cmd_set(controller, args)
    assert "MAC" in capsys.readouterr().out


def test_cli_diag_returns_structured_data(controller, capsys) -> None:
    from drcom.cli import _cmd_diag, build_parser

    args = build_parser().parse_args(["--cli", "diag", "--json"])
    _cmd_diag(controller, args)
    payload = json.loads(capsys.readouterr().out)
    assert "bind_port" in payload
    assert "suspects" in payload


def test_cli_unknown_command_is_a_usage_error(controller) -> None:
    from drcom.cli import run_cli

    assert run_cli(["--cli", "frobnicate", "--data-dir", str(controller.data_dir)]) == 2


def test_cli_export_logs(controller, capsys) -> None:
    from drcom.cli import _cmd_export_logs, build_parser

    controller.log.info("something to export")
    args = build_parser().parse_args(["--cli", "export-logs"])
    assert _cmd_export_logs(controller, args) == 0
    assert "已导出" in capsys.readouterr().out


# --------------------------------------------------------------------------
# scheduling helpers
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("start", "end", "hour", "minute", "expected"),
    [
        ("01:00", "06:00", 3, 0, True),
        ("01:00", "06:00", 7, 0, False),
        ("01:00", "06:00", 1, 0, True),
        ("01:00", "06:00", 6, 0, False),
        ("23:00", "06:00", 23, 30, True),   # wraps past midnight
        ("23:00", "06:00", 2, 0, True),
        ("23:00", "06:00", 12, 0, False),
        ("bad", "06:00", 3, 0, False),      # malformed → never "quiet"
        ("05:00", "05:00", 5, 0, False),    # zero-length window
    ],
)
def test_quiet_hours(start: str, end: str, hour: int, minute: int, expected: bool) -> None:
    from drcom.engine import _in_quiet_hours

    now = __import__("time").struct_time((2026, 9, 24, hour, minute, 0, 3, 267, 0))
    assert _in_quiet_hours(start, end, now=now) is expected


def test_backoff_grows_and_is_capped(controller) -> None:
    from drcom.engine import AuthEngine

    engine = AuthEngine(
        config=controller.config,
        account=controller.config.active_account(),
        password="x",
        log=controller.log,
    )
    controller.config.reconnect.min_delay = 2.0
    controller.config.reconnect.max_delay = 16.0
    controller.config.reconnect.factor = 2.0
    controller.config.reconnect.jitter = 0.0
    delays = []
    for failures in range(1, 7):
        engine.consecutive_failures = failures
        delays.append(engine._next_backoff())
    assert delays[0] == pytest.approx(2.0)
    assert delays[1] == pytest.approx(4.0)
    assert delays[2] == pytest.approx(8.0)
    assert all(d <= 16.0 for d in delays)
    assert delays == sorted(delays), f"backoff must not shrink: {delays}"


def test_backoff_jitter_stays_in_range(controller) -> None:
    from drcom.engine import AuthEngine

    engine = AuthEngine(
        config=controller.config,
        account=controller.config.active_account(),
        password="x",
        log=controller.log,
    )
    controller.config.reconnect.min_delay = 10.0
    controller.config.reconnect.max_delay = 10.0
    controller.config.reconnect.jitter = 0.25
    engine.consecutive_failures = 1
    for _ in range(30):
        assert 7.0 <= engine._next_backoff() <= 13.0


# --------------------------------------------------------------------------
# console encoding
# --------------------------------------------------------------------------
def test_diagnostic_text_survives_a_gbk_console() -> None:
    """Regression: a stray bullet character crashed ``--cli diag`` under GBK.

    The diagnostic is exactly what a user runs when the port is blocked, so it
    must never be the thing that fails.  Chinese is GBK-encodable; typographic
    symbols like the bullet are not, and used to abort the command.
    """
    from drcom.binding import WSAEACCES, diagnose_bind_error

    text = diagnose_bind_error(61440, WSAEACCES, deep=False).to_text()
    text.encode("gbk")  # must not raise


def test_cli_output_survives_a_gbk_console(controller, capsys, monkeypatch) -> None:
    """Run the real CLI paths with stdout forced to a non-UTF-8 code page."""
    import io
    import sys

    from drcom.cli import _cmd_diag, _cmd_status, build_parser

    fake = io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="strict", newline="")
    monkeypatch.setattr(sys, "stdout", fake)

    # These used to raise UnicodeEncodeError on the bullet / arrow glyphs.
    _cmd_diag(controller, build_parser().parse_args(["--cli", "diag"]))
    _cmd_status(controller, build_parser().parse_args(["--cli", "status"]))
    fake.flush()


def test_selftest_output_is_gbk_safe(capsys, monkeypatch) -> None:
    """`--selftest` prints lengths and arrows; keep them encodable."""
    import io
    import sys

    import main as entry

    fake = io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="strict", newline="")
    monkeypatch.setattr(sys, "stdout", fake)
    assert entry._selftest() == 0
    fake.flush()


def test_force_utf8_console_is_safe_to_call() -> None:
    import main as entry

    entry._force_utf8_console()  # must not raise in any environment


# --------------------------------------------------------------------------
# packet redaction (spec 9: the account must not land in the log file)
# --------------------------------------------------------------------------
def test_hexdump_redaction_preserves_offsets() -> None:
    """Redacted bytes become `--`, so the dump still aligns with the packet."""
    text = hexdump(bytes(range(12)), redact=[(4, 8)])
    fields = text.split("  (")[0].split()
    assert len(fields) == 12
    assert fields[4:8] == ["--"] * 4
    assert fields[3] == "03"
    assert fields[8] == "08"


def test_hexdump_without_redaction_is_unchanged() -> None:
    assert hexdump(bytes((0x01, 0x02))) == "01 02"


def test_login_packet_account_is_redacted_in_the_log(bus) -> None:
    """The login packet carries the account; it must not be written verbatim."""
    from drcom import protocol

    account, password, mac = "2023000001", "secret123", "AA:BB:CC:DD:EE:FF"
    packet = protocol.build_login_packet(account, password, mac, bytes((1, 2, 3, 4)))
    account_end = 20 + len(account)
    password_length = packet[313]
    bus.packet(
        "tx", "[Login sent]", packet,
        redact=((4, 20), (20, account_end), (58, 80), (314, 312 + 2 + password_length + 16)),
    )
    message = bus.snapshot()[-1].message
    assert account not in message
    assert "敏感字段已用 -- 屏蔽" in message
    # The non-secret framing is still visible, which is what makes the dump useful.
    assert message.startswith("-> [Login sent]")


def test_unredacted_mode_is_opt_in(bus) -> None:
    """The escape hatch exists but is off unless explicitly enabled."""
    from drcom import protocol

    packet = protocol.build_login_packet("2023000001", "secret123", "AA:BB:CC:DD:EE:FF", bytes(4))
    assert bus.protocol_hex_unredacted is False
    bus.protocol_hex_unredacted = True
    bus.packet("tx", "[Login sent]", packet, redact=((20, 30),))
    # With redaction disabled the raw account bytes are present again.
    assert "32 30 32 33 30 30 30 30 30 31" in bus.snapshot()[-1].message


# --------------------------------------------------------------------------
# traffic / probe must follow the interface that actually authenticates
# --------------------------------------------------------------------------
def test_traffic_meter_can_be_pinned_to_a_source_address() -> None:
    """Regression: the meter used to follow the default route instead.

    On a machine with Wi-Fi (better metric) plus campus Ethernet, that meant
    counting bytes on the wrong adapter while the Dr.COM session ran on the
    other one.
    """
    from drcom.traffic import TrafficMeter

    meter = TrafficMeter(server="10.100.61.3", port=61440)
    assert meter.source_address == ""
    meter.set_source_address("172.18.123.63")
    assert meter.source_address == "172.18.123.63"

    # A wildcard address means "no preference" — fall back to route scoring.
    meter.set_source_address("0.0.0.0")
    assert meter.source_address == ""

    # Changing the pin must invalidate the cached interface.
    meter.set_source_address("172.18.123.63")
    meter._iface = object()  # type: ignore[assignment]
    meter.set_source_address("10.0.0.5")
    assert meter._iface is None


def test_traffic_meter_resolves_the_interface_owning_the_address() -> None:
    """Given a real local address, the meter must pick that adapter."""
    from drcom.netiface import _interface_addresses, list_interfaces
    from drcom.traffic import TrafficMeter

    addresses = _interface_addresses()
    target = ""
    for iface in list_interfaces():
        for address in addresses.get(iface.index, ()):
            if iface.if_type in (6, 71) and not address.startswith(("127.", "169.254.")):
                target = address
                expected_index = iface.index
                break
        if target:
            break
    if not target:
        pytest.skip("no usable local IPv4 address in this environment")

    meter = TrafficMeter(server="10.100.61.3", port=61440)
    meter.set_source_address(target)
    resolved = meter._resolve_interface()
    assert resolved is not None
    assert resolved.index == expected_index


def test_traffic_sampling_works_end_to_end() -> None:
    from drcom.traffic import TrafficMeter

    meter = TrafficMeter(server="10.100.61.3", port=61440)
    first = meter.sample()
    assert first.timestamp > 0
    second = meter.sample()
    assert second.timestamp >= first.timestamp
    # Totals must never go backwards even if the adapter resets.
    assert second.total_rx >= 0 and second.total_tx >= 0


def test_probe_can_be_pinned_to_a_source_address(tmp_path) -> None:
    from drcom.config import ProbeConfig
    from drcom.logbus import LogBus
    from drcom.netprobe import NetworkProbe

    log = LogBus(log_dir=tmp_path / "logs", level="WARNING")
    probe = NetworkProbe(ProbeConfig(), log)
    assert probe.source_address == ""
    probe.set_source_address("172.18.123.63")
    assert probe.source_address == "172.18.123.63"
    # A wildcard means "no preference".
    probe.set_source_address("0.0.0.0")
    assert probe.source_address == ""


def test_ping_source_flag_is_platform_appropriate(monkeypatch) -> None:
    """Windows uses -S, iputils uses -I; neither should be passed when unset."""
    from drcom import netprobe

    captured: list[list[str]] = []

    class FakeProc:
        stdout = ""
        returncode = 0

    def fake_run(argv, **kwargs):
        captured.append(argv)
        return FakeProc()

    monkeypatch.setattr(netprobe.subprocess, "run", fake_run)

    netprobe.ping_once("10.0.0.1", count=1)
    netprobe.ping_once("10.0.0.1", count=1, source="172.18.1.2")

    assert len(captured) == 2
    assert not any("-S" in argv or "-I" in argv for argv in captured[:1])
    assert "-S" in captured[1] or "-I" in captured[1]
    assert "172.18.1.2" in captured[1]


def test_status_api_returns_500_instead_of_crashing(controller) -> None:
    """A payload bug must not kill the HTTP handler."""
    import urllib.request

    ok, message = controller.api.start()
    if not ok:
        pytest.skip(f"cannot bind the status port here: {message}")
    try:
        def boom(_controller):
            raise RuntimeError("simulated payload failure")

        import drcom.statusapi as statusapi

        original = statusapi.build_status_payload
        statusapi.build_status_payload = boom   # type: ignore[assignment]
        try:
            with pytest.raises(urllib.error.HTTPError) as info:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{controller.api.bound_port}/status", timeout=5
                )
            assert info.value.code == 500
            body = json.loads(info.value.read())
            assert "status unavailable" in body["error"]
        finally:
            statusapi.build_status_payload = original   # type: ignore[assignment]
    finally:
        controller.api.stop()


def test_foreground_watch_survives_a_user_reconnect() -> None:
    """Regression from the first 2-hour soak run.

    That run exited by itself minutes in, right after ``POST /reconnect``: the
    foreground loop watched ``engine.is_running``, and the instant the old
    engine was stopped (before the new one was swapped in) it decided the
    engine had finished and quit the program.
    """
    from drcom.cli import ForegroundWatch
    from drcom.engine import EngineState

    class FakeEngine:
        def __init__(self, running: bool, state=EngineState.ONLINE) -> None:
            self.is_running = running
            self.state = state

    old = FakeEngine(False, EngineState.STOPPED)
    new = FakeEngine(True)

    watch = ForegroundWatch()
    assert watch.poll(old) == "running"      # first look
    # The window where the old engine is stopped and the new one not yet in
    # place must NOT be read as "finished".
    assert watch.poll(old) == "running"
    assert watch.poll(new) == "restarted"
    assert watch.poll(new) == "running"


def test_foreground_watch_reports_finished_when_the_engine_stays_down() -> None:
    from drcom.cli import ForegroundWatch
    from drcom.engine import EngineState

    class FakeEngine:
        is_running = False
        state = EngineState.STOPPED

    watch = ForegroundWatch()
    engine = FakeEngine()
    verdicts = [watch.poll(engine) for _ in range(ForegroundWatch.GRACE_POLLS + 2)]
    assert verdicts[-1] == "finished"
    assert "finished" not in verdicts[: ForegroundWatch.GRACE_POLLS]


def test_foreground_watch_reports_fatal() -> None:
    from drcom.cli import ForegroundWatch
    from drcom.engine import EngineState

    class FakeEngine:
        is_running = False
        state = EngineState.FATAL

    watch = ForegroundWatch()
    engine = FakeEngine()
    verdict = "running"
    for _ in range(ForegroundWatch.GRACE_POLLS + 2):
        verdict = watch.poll(engine)
    assert verdict == "fatal"


def test_traffic_sampling_is_thread_safe() -> None:
    """Regression: the soak run hit an AttributeError here.

    ``sample()`` is called both by the controller's ticker thread and by HTTP
    request threads (the status payload samples traffic).  Without a lock, one
    thread could null the cached adapter between another thread's None-check and
    its attribute access.
    """
    import threading

    from drcom.traffic import TrafficMeter

    meter = TrafficMeter(server="10.100.61.3", port=61440)
    # A real adapter address makes the resolver take the interesting path.
    meter.set_source_address("172.18.123.63")

    errors: list[BaseException] = []
    stop = threading.Event()

    def worker() -> None:
        try:
            while not stop.is_set():
                meter.sample()
                # Churn the pinned address so the cache is repeatedly invalidated.
                meter.set_source_address("172.18.123.63")
                meter.set_source_address("")
        except BaseException as exc:  # noqa: BLE001 - recorded and asserted
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    import time

    time.sleep(1.0)
    stop.set()
    for thread in threads:
        thread.join(5)

    assert not errors, f"concurrent sampling raised: {errors[:3]}"


def test_traffic_tolerates_a_transient_adapter_miss() -> None:
    """One missed lookup must not drop the cached adapter."""
    from drcom.netiface import InterfaceInfo
    from drcom.traffic import TrafficMeter
    import drcom.traffic as traffic_module

    meter = TrafficMeter(server="10.100.61.3", port=61440)
    cached = InterfaceInfo(name="test0", index=4242, mac="AA:BB:CC:DD:EE:FF", is_up=True)
    meter._iface = cached

    original = traffic_module.list_interfaces
    traffic_module.list_interfaces = lambda: []          # adapter momentarily gone
    try:
        assert meter._resolve_interface() is cached      # still trusted
        assert meter._resolve_interface() is cached
        # After several consecutive misses it gives up and re-picks.
        result = meter._resolve_interface()
        assert result is not cached or meter._iface is None
    finally:
        traffic_module.list_interfaces = original
