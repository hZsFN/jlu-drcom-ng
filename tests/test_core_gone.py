"""核心退出后窗口的去向：跟着走，还是把它拉回来。

托盘图标属于 drcomd，不属于窗口。所以从托盘菜单退出时，退的是核心——窗口如果
留在原地，就成了一个既没有托盘、也认证不了的空壳，「托盘退出后台还有
JLU-DrCOM-NG.exe 残留」正是这么来的。反过来，核心自己崩掉时没人要求退出，
那就该尝试重启，而不是把窗口也带走。
"""

from __future__ import annotations

import threading

import pytest


@pytest.fixture
def engine(temp_data_dir):
    from drcom.config import Account, AppConfig
    from drcom.logbus import LogBus
    from drcom.remote_engine import RemoteEngine

    log = LogBus(log_dir=temp_data_dir / "logs", level="WARNING")
    config = AppConfig()
    account = Account(account="20000000", mac="AA:BB:CC:DD:EE:FF")
    instance = RemoteEngine(config=config, account=account, password="x", log=log)
    yield instance
    instance._stop.set()


def _collect(engine) -> list:
    events: list = []
    engine.subscribe(events.append)
    return events


def _dead_core(engine, exit_code):
    """Make /status answer nothing, as a daemon that already left would."""

    class FakeProcess:
        def poll(self):
            return exit_code

    engine._process = FakeProcess()


def test_a_zero_exit_code_means_the_tray_was_used(engine, monkeypatch) -> None:
    from drcom import remote_engine

    monkeypatch.setattr(remote_engine, "_request", lambda *a, **k: None)
    events = _collect(engine)
    _dead_core(engine, 0)
    engine._poll_loop()

    assert len(events) == 1
    gone = events[0]
    assert gone.kind == "core_gone", "泛化的 fatal 分不出「用户主动退出」和「核心崩了」"
    assert gone.payload["exit_code"] == 0
    assert "托盘" in gone.detail


def test_a_non_zero_exit_code_is_a_crash(engine, monkeypatch) -> None:
    from drcom import remote_engine

    monkeypatch.setattr(remote_engine, "_request", lambda *a, **k: None)
    events = _collect(engine)
    _dead_core(engine, 9)
    engine._poll_loop()

    assert events[0].payload["exit_code"] == 9
    assert "异常退出" in events[0].detail


def test_a_core_we_never_started_has_no_exit_code(engine, monkeypatch) -> None:
    """An already-resident core belongs to someone else's launch."""
    from drcom import remote_engine

    monkeypatch.setattr(remote_engine, "_request", lambda *a, **k: None)
    events = _collect(engine)
    engine._process = None
    engine._poll_loop()

    assert events[0].payload["exit_code"] is None
    assert "已退出" in events[0].detail


def test_the_exit_code_is_read_not_assumed(engine) -> None:
    class NotYetReaped:
        def poll(self):
            return None

    engine._process = NotYetReaped()
    assert engine._daemon_exit_code() is None


def test_polling_stops_after_the_core_is_gone(engine, monkeypatch) -> None:
    """Returning ends the thread; leaving it looping would spin on a dead port."""
    from drcom import remote_engine

    calls = {"n": 0}

    def counting(*a, **k):
        calls["n"] += 1
        return None

    monkeypatch.setattr(remote_engine, "_request", counting)
    _dead_core(engine, 0)
    engine._poll_loop()
    assert calls["n"] == 1


def test_the_ui_quits_the_program_on_a_deliberate_tray_exit() -> None:
    """A window left behind has no tray and cannot authenticate."""
    from drcom.ui import app as ui_app

    recorded: dict = {}

    class FakeApp:
        _flash = lambda self, message, ok=True: recorded.setdefault("flash", message)
        _set_alert = lambda self, *a, **k: recorded.setdefault("alert", a)
        _quit = lambda self, deliberate=True: recorded.setdefault("quit", deliberate)

    event = type("E", (), {"payload": {"exit_code": 0}, "message": "x", "detail": "d"})()
    ui_app.DrcomApp._on_core_gone(FakeApp(), event)
    assert recorded["quit"] is True


def test_a_crashed_core_asks_the_watchdog_for_a_restart() -> None:
    """退出时不留停止标记，看门狗才会把程序拉回来。"""
    from drcom.ui import app as ui_app

    recorded: dict = {}

    class FakeEngine:
        is_running = False

        def restart_daemon(self):
            recorded["restart"] = True

    class FakeApp:
        controller = type("C", (), {"engine": FakeEngine(), "log": type("L", (), {"error": lambda *a, **k: None})()})()
        palette = type("P", (), {"danger": "red", "amber": "amber"})()
        _set_alert = lambda self, *a, **k: None
        _quit = lambda self, deliberate=True: recorded.setdefault("quit", deliberate)

    event = type("E", (), {"payload": {"exit_code": 9}, "message": "核心没了", "detail": "d"})()
    ui_app.DrcomApp._on_core_gone(FakeApp(), event)
    assert recorded["restart"] is True, "核心崩溃时先尝试把它拉回来"
    assert recorded["quit"] is False, "重启失败才退出，而且不能写停止标记"
