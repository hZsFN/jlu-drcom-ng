"""End-to-end engine tests against :mod:`tests.mock_server`.

These exercise the parts that are genuinely hard to get right: the keepalive
control flow, the transient/fatal error split, backoff, and clean shutdown.
"""

from __future__ import annotations

import time

import pytest

from drcom.config import Account, AppConfig
from drcom.engine import AuthEngine, EngineState, OfflineReason
from drcom.logbus import LogBus
from drcom.stats import StatsStore

from .mock_server import MockAuthServer, ServerBehaviour, free_udp_port


def build_engine(tmp_path, server: MockAuthServer, **auth_overrides) -> AuthEngine:
    config = AppConfig()
    config.auth.server = "127.0.0.1"
    config.auth.port = server.port
    config.auth.bind_port = free_udp_port()
    config.auth.bind_address = "127.0.0.1"
    config.auth.timeout_ms = 800
    config.auth.keepalive_interval = 0.3
    config.auth.post_login_delay = 0.05
    config.auth.challenge_retries = 2
    config.reconnect.min_delay = 0.2
    config.reconnect.max_delay = 1.0
    for key, value in auth_overrides.items():
        setattr(config.auth, key, value)

    log = LogBus(log_dir=tmp_path / "logs", level="WARNING", protocol_hex=True)
    stats = StatsStore(tmp_path / "stats.json")
    account = Account(account="2023000001", mac="AA:BB:CC:DD:EE:FF")
    return AuthEngine(config=config, account=account, password="secret123", log=log, stats=stats)


def wait_for(predicate, timeout: float = 6.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# --------------------------------------------------------------------------
# happy path
# --------------------------------------------------------------------------
def test_full_login_then_keepalive(tmp_path) -> None:
    with MockAuthServer() as server:
        engine = build_engine(tmp_path, server)
        events: list = []
        engine.subscribe(events.append)
        engine.start()

        assert wait_for(lambda: engine.is_online), "engine never reached ONLINE"
        assert engine.ip == "192.0.2.10"
        assert engine.auth_information == server.auth_information

        # Let a couple of keepalive cycles run.
        assert wait_for(lambda: server.cycles >= 2, timeout=6.0), (
            f"only {server.cycles} keepalive cycles ran"
        )
        engine.stop()
        assert engine.join(3.0)

        kinds = [e.kind for e in events]
        assert "online" in kinds
        assert "stopped" in kinds
        assert not server.log.problems, server.log.problems

        # The MAC must appear where the server expects it, and the login packet
        # must have carried the account.
        assert server.log.logins
        login = server.log.logins[0]
        assert b"2023000001" in login
        assert login[58:64] != bytes(6), "MAC-XOR-MD5A field was left zero"


def test_keepalive2_sequence_is_file_then_a_then_c(tmp_path) -> None:
    with MockAuthServer() as server:
        engine = build_engine(tmp_path, server)
        engine.start()
        assert wait_for(lambda: server.cycles >= 2, timeout=6.0)
        engine.stop()
        engine.join(3.0)

    packets = server.log.keepalive2
    assert packets, "no keepalive2 packets were sent"
    # First ever packet must be the file packet.
    assert packets[0][6:8] == bytes((0x0F, 0x27)), "first keepalive2 packet was not the file packet"
    assert packets[0][5] == 1
    # Then the A(1) → C(3) pattern.
    types = [p[5] for p in packets]
    assert types[:3] == [1, 1, 3], types
    # The C packet must echo the tail the server handed out.
    c_packets = [p for p in packets if p[5] == 3]
    assert c_packets and c_packets[0][16:20] == bytes((0xCA, 0xFE, 0xBA, 0xBE))
    # Only the very first packet is a file packet.
    file_packets = [p for p in packets if p[6:8] == bytes((0x0F, 0x27))]
    assert len(file_packets) == 1


def test_counters_increment_per_packet(tmp_path) -> None:
    with MockAuthServer() as server:
        engine = build_engine(tmp_path, server)
        engine.start()
        assert wait_for(lambda: server.cycles >= 2, timeout=6.0)
        engine.stop()
        engine.join(3.0)

    counters = [p[1] for p in server.log.keepalive2]
    assert counters == list(range(len(counters))), f"counter did not advance one by one: {counters}"


def test_server_notice_packet_is_skipped(tmp_path) -> None:
    """A 0x4D notice must be ignored, not treated as the keepalive1 reply."""
    behaviour = ServerBehaviour(send_notice_once=True)
    with MockAuthServer(behaviour) as server:
        engine = build_engine(tmp_path, server)
        engine.start()
        assert wait_for(lambda: engine.is_online), "notice packet broke the handshake"
        assert wait_for(lambda: server.cycles >= 1, timeout=4.0)
        engine.stop()
        engine.join(3.0)
        assert not server.log.problems, server.log.problems


# --------------------------------------------------------------------------
# failures
# --------------------------------------------------------------------------
def test_wrong_password_is_fatal_with_a_clear_message(tmp_path) -> None:
    """Acceptance criterion 4: a wrong password must say so, not 'unknown'."""
    behaviour = ServerBehaviour(login_error_code=0x03)
    with MockAuthServer(behaviour) as server:
        engine = build_engine(tmp_path, server)
        events: list = []
        engine.subscribe(events.append)
        engine.start()

        assert wait_for(lambda: engine.state is EngineState.FATAL, timeout=6.0), (
            f"state stayed {engine.state}"
        )
        engine.join(3.0)

    failures = [e for e in events if e.kind == "login_failed"]
    assert failures, "no login_failed event"
    assert failures[0].message == "密码错误"
    assert "密码" in failures[0].advice
    assert failures[0].error_code == 0x03
    # And it must not keep hammering the server.
    assert len(server.log.logins) == 1, f"retried a fatal error {len(server.log.logins)} times"


def test_server_busy_is_transient_and_backs_off(tmp_path) -> None:
    behaviour = ServerBehaviour(login_error_code=0x02)
    with MockAuthServer(behaviour) as server:
        engine = build_engine(tmp_path, server)
        events: list = []
        engine.subscribe(events.append)
        engine.start()

        assert wait_for(
            lambda: any(e.kind == "retry_scheduled" for e in events), timeout=6.0
        ), "no retry was scheduled for a transient failure"
        engine.stop()
        engine.join(3.0)

    assert engine.state is not EngineState.FATAL
    retries = [e for e in events if e.kind == "retry_scheduled"]
    assert retries[0].payload["delay"] >= 0.2
    assert any(e.kind == "login_failed" and e.message == "服务器忙" for e in events)


def test_mac_mismatch_message(tmp_path) -> None:
    behaviour = ServerBehaviour(login_error_code=0x0B)
    with MockAuthServer(behaviour) as server:
        engine = build_engine(tmp_path, server)
        events: list = []
        engine.subscribe(events.append)
        engine.start()
        assert wait_for(lambda: any(e.kind == "login_failed" for e in events), timeout=6.0)
        engine.stop()
        engine.join(3.0)

    failure = next(e for e in events if e.kind == "login_failed")
    assert failure.message == "MAC 不匹配"
    assert "解绑" in failure.advice


def test_keeps_running_when_reconnect_is_disabled_is_not_applied_to_fatal(tmp_path) -> None:
    """A fatal error stops the loop even with auto-reconnect on."""
    behaviour = ServerBehaviour(login_error_code=0x17)  # must use DHCP
    with MockAuthServer(behaviour) as server:
        engine = build_engine(tmp_path, server)
        engine.start()
        assert wait_for(lambda: engine.state is EngineState.FATAL, timeout=6.0)
        assert len(server.log.logins) == 1
        engine.join(2.0)


def test_keepalive_silence_triggers_reconnect(tmp_path) -> None:
    """Acceptance criterion 3: losing the link must self-heal."""
    behaviour = ServerBehaviour(die_after_cycles=1)
    with MockAuthServer(behaviour) as server:
        engine = build_engine(tmp_path, server)
        events: list = []
        engine.subscribe(events.append)
        engine.start()

        # First login succeeds, keepalives then go unanswered, so the engine
        # must decide it is offline and come back with a fresh challenge.
        assert wait_for(lambda: len(server.log.challenges) >= 2, timeout=12.0), (
            f"client did not re-challenge (challenges={len(server.log.challenges)})"
        )
        engine.stop()
        engine.join(3.0)

    assert any(e.kind == "keepalive_failed" for e in events), [e.kind for e in events]


def test_challenge_timeout_does_not_hang(tmp_path) -> None:
    """A server that never answers the challenge must fail fast, not block."""
    behaviour = ServerBehaviour(challenge_delay=5.0)
    with MockAuthServer(behaviour) as server:
        engine = build_engine(tmp_path, server, timeout_ms=300)
        events: list = []
        engine.subscribe(events.append)
        start = time.monotonic()
        engine.start()
        assert wait_for(
            lambda: any(e.kind == "challenge_failed" for e in events), timeout=8.0
        ), "no challenge_failed event"
        elapsed = time.monotonic() - start
        engine.stop()
        engine.join(3.0)

    # 2 retries × 0.3 s timeout + backoff — well under the 5 s server delay.
    assert elapsed < 4.5, f"challenge failure took {elapsed:.1f}s"


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------
def test_stop_is_prompt_even_mid_keepalive_wait(tmp_path) -> None:
    """注销 must not wait for the 20 s keepalive sleep."""
    with MockAuthServer() as server:
        engine = build_engine(tmp_path, server, keepalive_interval=30.0)
        engine.start()
        assert wait_for(lambda: engine.is_online, timeout=6.0)
        # Give it a moment to be sitting in the long keepalive sleep.
        time.sleep(0.4)
        start = time.monotonic()
        engine.stop()
        assert engine.join(2.0), "engine did not stop promptly"
        elapsed = time.monotonic() - start
        assert elapsed < 2.0, f"stop took {elapsed:.1f}s"


def test_stats_record_the_session(tmp_path) -> None:
    with MockAuthServer() as server:
        engine = build_engine(tmp_path, server)
        engine.start()
        assert wait_for(lambda: engine.is_online, timeout=6.0)
        assert engine.stats is not None
        assert engine.stats.today().sessions == 1
        time.sleep(0.3)
        engine.stop()
        engine.join(3.0)

    stored = StatsStore(tmp_path / "stats.json")
    assert stored.totals.sessions == 1
    assert stored.totals.online_seconds >= 0


def test_offline_reason_is_recorded_on_user_logout(tmp_path) -> None:
    with MockAuthServer() as server:
        engine = build_engine(tmp_path, server)
        engine.start()
        assert wait_for(lambda: engine.is_online, timeout=6.0)
        engine.stop(reason=OfflineReason.USER_LOGOUT)
        engine.join(3.0)
        assert engine.reason is OfflineReason.USER_LOGOUT


# --------------------------------------------------------------------------
# bind-address fallback (found by testing on a machine with Wi-Fi + Ethernet)
# --------------------------------------------------------------------------
def test_bind_candidates_respects_an_explicit_address(tmp_path) -> None:
    with MockAuthServer() as server:
        engine = build_engine(tmp_path, server, bind_address="192.0.2.7")
        assert engine._bind_candidates() == ["192.0.2.7"]


def test_bind_candidates_wildcard_lists_interface_addresses(tmp_path) -> None:
    """With 0.0.0.0 the engine must offer concrete addresses as fallbacks.

    This is the real-world bug: binding 0.0.0.0 succeeds but the kernel routes
    by metric, so a Wi-Fi or VPN link can steal the auth traffic.
    """
    with MockAuthServer() as server:
        engine = build_engine(tmp_path, server, bind_address="0.0.0.0")
        candidates = engine._bind_candidates()
        assert candidates[0] == "0.0.0.0"
        assert len(candidates) >= 1
        assert len(set(candidates)) == len(candidates), "duplicate candidates"
        assert not any(c.startswith(("127.", "169.254.")) for c in candidates)


def test_bind_candidates_can_be_disabled(tmp_path) -> None:
    with MockAuthServer() as server:
        engine = build_engine(tmp_path, server, bind_address="0.0.0.0")
        engine.config.auth.auto_interface_fallback = False
        assert engine._bind_candidates() == ["0.0.0.0"]


def test_falls_back_to_a_working_address_when_the_first_gets_no_reply(tmp_path) -> None:
    """End-to-end: an address that never answers must not end the session.

    This is the exact real-world bug that motivated the fallback — on a machine
    with both Wi-Fi and Ethernet up, binding 0.0.0.0 succeeds but the packet
    leaves through whichever adapter has the better route metric, and the
    campus server never replies.
    """
    with MockAuthServer() as server:
        engine = build_engine(tmp_path, server, bind_address="0.0.0.0")
        engine.config.auth.challenge_retries = 1
        engine.config.auth.timeout_ms = 300
        engine._bind_candidates = lambda: ["0.0.0.0", "127.0.0.1"]  # type: ignore[method-assign]

        # Make the challenge fail for the first address only, then work.
        import socket as _socket

        real_challenge = engine._do_challenge
        attempts = {"count": 0}

        def flaky(sock):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise _socket.timeout("simulated: first address is not routable")
            return real_challenge(sock)

        engine._do_challenge = flaky  # type: ignore[method-assign]

        events: list = []
        engine.subscribe(events.append)
        engine.start()
        assert wait_for(lambda: engine.is_online, timeout=10.0), (
            "engine did not recover by trying the next bind address"
        )
        engine.stop()
        engine.join(3.0)

    assert attempts["count"] >= 2, "the engine never retried on another address"
    assert any(e.kind == "challenge_failed" for e in events), "no first-address failure recorded"
    assert any(e.kind == "online" for e in events), "never recovered"


def test_a_single_dead_address_still_reports_challenge_failed(tmp_path) -> None:
    """With no alternatives, the failure is reported (not silently swallowed)."""
    with MockAuthServer() as server:
        engine = build_engine(tmp_path, server, bind_address="0.0.0.0")
        engine.config.auth.challenge_retries = 1
        engine.config.auth.timeout_ms = 200
        engine._bind_candidates = lambda: ["0.0.0.0"]  # type: ignore[method-assign]
        engine.config.reconnect.enabled = False

        import socket as _socket

        engine._do_challenge = lambda sock: (_ for _ in ()).throw(_socket.timeout("dead"))  # type: ignore[method-assign]

        events: list = []
        engine.subscribe(events.append)
        engine.start()
        assert wait_for(
            lambda: any(e.kind == "challenge_failed" for e in events), timeout=8.0
        )
        engine.stop()
        engine.join(3.0)

    assert engine.state is not EngineState.ONLINE
    failure = next(e for e in events if e.kind == "challenge_failed")
    assert "网线" in failure.detail or "服务器" in failure.detail
