"""Tests for hot-reload config application, lossless daemon attach, and add_account."""

from __future__ import annotations

import threading
from types import SimpleNamespace

from drcom.config import Account
from drcom.controller import AppController


def _make_running_native_stub(controller):
    from drcom.remote_engine import RemoteEngine

    calls: list[str] = []

    class StubNative(RemoteEngine):
        def __init__(self) -> None:
            self.state = SimpleNamespace(value="online")
            self.ip = "10.0.0.99"
            self.attempt = 1
            self.socket_info = None
            self.online_since = 100.0
            self.config = controller.config
            self.account = controller.config.active_account()

        @property
        def is_running(self) -> bool:
            return True

        @property
        def is_online(self) -> bool:
            return True

        def uptime_seconds(self) -> float:
            return 12.0

        def reload_config(self) -> bool:
            calls.append("reload_config")
            return True

        def reconnect(self) -> bool:
            calls.append("reconnect")
            return True

        def stop(self, **_kw) -> None:
            calls.append("stop")

        def join(self, _timeout=None) -> bool:
            return True

        def shutdown_daemon(self) -> None:
            calls.append("shutdown_daemon")

    controller.engine = StubNative()
    return calls


def test_apply_api_probe_reconnect_hot_reload(controller) -> None:
    controller.config.auth.native_core = True
    calls = _make_running_native_stub(controller)

    # 1. Reconnect settings hot-reload without restarting or dropping online session
    controller.config.reconnect.min_delay = 12.0
    ok, _msg = controller.apply_reconnect_settings()
    assert ok
    assert "reload_config" in calls
    assert "reconnect" not in calls

    # 2. Probe settings prune removed targets and update config immediately
    controller.config.probe.targets = ["1.1.1.1"]
    ok, _msg = controller.apply_probe_settings()
    assert ok
    assert controller.probe.config.targets == ["1.1.1.1"]
    controller.probe.stop()

    # 3. API settings bind/rebind cleanly on ephemeral port
    controller.config.api.enabled = True
    controller.config.api.port = 0
    ok, _msg = controller.apply_api_settings()
    assert ok
    assert controller.api.is_running
    controller.config.api.enabled = False
    ok, _msg = controller.apply_api_settings()
    assert ok
    assert not controller.api.is_running


def test_apply_auth_settings_reloads_native_without_killing_daemon(controller) -> None:
    controller.config.auth.native_core = True
    calls = _make_running_native_stub(controller)

    ok, _msg = controller.apply_auth_settings(reconnect_if_needed=True)
    assert ok
    assert "reconnect" in calls
    assert "shutdown_daemon" not in calls


def test_begin_session_attaches_to_running_daemon_without_relogin(tmp_path, monkeypatch) -> None:
    import drcom.remote_engine as remote_engine

    ctrl = AppController(data_dir=tmp_path, log_to_file=False)
    try:
        acc = Account(account="20230001", mac="AA:BB:CC:DD:EE:FF", auto_login=True)
        ctrl.store.add_account(acc, make_active=True)
        ctrl.store.set_password(acc.id, "secret")
        ctrl.config.auth.native_core = True

        started_with: list[dict] = []

        class FakeRemoteEngine:
            def __init__(self, **kw) -> None:
                self.account = kw["account"]
                self.config = kw["config"]
                self.is_running = True
                self.is_online = True
                self.online_since = 123.0
                self.state = SimpleNamespace(value="online")

            def subscribe(self, _cb) -> None:
                pass

            def start(self, *, attach_only: bool = False, force_reconnect: bool = False) -> None:
                started_with.append({"attach_only": attach_only, "force_reconnect": force_reconnect})

            def stop(self, **_kw) -> None:
                self.is_running = False

            def join(self, _timeout=None) -> bool:
                return True

        monkeypatch.setattr(remote_engine, "RemoteEngine", FakeRemoteEngine)
        monkeypatch.setattr(
            remote_engine,
            "daemon_status",
            lambda _port=61441, timeout=0.6: {"state": "online", "ip": "10.1.2.3", "uptime_seconds": 30},
        )
        relogin_called = []
        monkeypatch.setattr(ctrl, "request_login", lambda: relogin_called.append(True) or (True, "ok"))

        ctrl.begin_session()
        assert started_with == [{"attach_only": True, "force_reconnect": False}]
        assert relogin_called == []
    finally:
        ctrl.shutdown()


def test_ui_uses_store_add_account(controller) -> None:
    import inspect
    from drcom.ui.app import DrcomApp

    src = inspect.getsource(DrcomApp._view_account)
    assert "controller.add_account(" not in src
    assert "controller.store.add_account(" in src


def test_reattach_suppresses_online_toast_and_syncs_traffic(tmp_path, monkeypatch) -> None:
    from drcom.remote_engine import RemoteEngine

    ctrl = AppController(data_dir=tmp_path, log_to_file=False)
    try:
        acc = Account(account="20230001", mac="AA:BB:CC:DD:EE:FF", auto_login=True)
        ctrl.store.add_account(acc, make_active=True)
        ctrl.config.notify.desktop = True

        notified: list[str] = []
        monkeypatch.setattr(ctrl.notifier, "notify", lambda ev, **_kw: notified.append(ev.kind))
        monkeypatch.setattr(ctrl, "schedule_update_check", lambda: None)

        synced: list[dict] = []
        monkeypatch.setattr(ctrl.traffic, "sync_baseline", lambda **kw: synced.append(kw))

        eng = RemoteEngine(
            config=ctrl.config,
            account=acc,
            password="",
            log=ctrl.log,
            stats=ctrl.stats,
            config_path=tmp_path / "config.json",
            enable_tray=False,
        )
        ctrl.engine = eng
        eng.subscribe(ctrl._on_engine_event)

        # 1. Re-attaching to an already-online drcomd must NOT pop "校园网已连接"
        #    and MUST forward the NIC traffic baseline to TrafficMeter.
        eng._apply(
            {
                "state": "online",
                "online": True,
                "ip": "172.18.123.63",
                "uptime_seconds": 600.0,
                "session_id": 1,
                "online_since": 1700000000.0,
                "traffic_if_index": 9,
                "traffic_rx_base": 1000,
                "traffic_tx_base": 200,
                "session_rx": 88888,
                "session_tx": 12345,
            },
            initial_attach=True,
        )
        assert notified == []
        assert len(synced) == 1
        assert synced[0]["if_index"] == 9
        assert synced[0]["session_rx"] == 88888

        # 2. A genuine new session after reconnect DOES fire the online notification once.
        eng._fresh_login_requested = True
        eng._apply(
            {
                "state": "online",
                "online": True,
                "ip": "172.18.123.64",
                "uptime_seconds": 0.5,
                "session_id": 2,
                "online_since": 1700000700.0,
                "traffic_if_index": 9,
                "traffic_rx_base": 90000,
                "traffic_tx_base": 15000,
                "session_rx": 512,
                "session_tx": 128,
            },
            initial_attach=False,
        )
        assert notified == ["online"]
        assert len(synced) == 2
        assert synced[1]["rx_base"] == 90000
    finally:
        ctrl.shutdown()
