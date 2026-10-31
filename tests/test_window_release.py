"""Releasing the Flet client on close, and putting a new window back.

The Flet desktop client is a separate process (flet.exe) that sits on
100-190 MB.  ``page.window.visible = False`` hides it and gives none of that
back, which is what the owner asked to change ("关闭前端窗口的时候释放那一部分
内存").

The mechanism: ``Window.destroy()`` ends the client, ``ft.run`` returns, and
``run_gui`` waits for the tray to ask for the window again before calling
``ft.run`` a second time.  These tests pin down the pieces that make that safe:
the wait/quit handshake, and the fact that nothing from the dead window is
carried into the rebuild.
"""

from __future__ import annotations

import threading

import pytest

flet = pytest.importorskip("flet")

from tests.ui_fakes import FakePage  # noqa: E402

from drcom.config import UiConfig  # noqa: E402


class _StubTray:
    def __init__(self, **kwargs) -> None:
        # Accepts whatever the app passes (tooltip=..., on_show=...), so this
        # can stand in for TrayIcon as well as for a bare icon.
        self.kwargs = kwargs
        self.stopped = False
        self.tooltips: list[str] = []
        self.balloons: list[tuple[str, str]] = []

    def stop(self) -> None:
        self.stopped = True

    def update(self, *, tooltip: str = "", color=None, online=None) -> None:
        self.tooltips.append(tooltip)

    def notify(self, message: str, title: str = "") -> None:
        self.balloons.append((title, message))

    def start(self) -> bool:
        return True


@pytest.fixture
def app(controller, monkeypatch):
    import drcom.ui.app as ui_app

    monkeypatch.setattr(flet.Control, "update", lambda self, *a, **k: None)
    monkeypatch.setattr(type(controller), "start_background", lambda self: None)

    instance = ui_app.DrcomApp(controller, enable_tray=False)
    instance.quit_watchdog_seconds = 60.0
    instance.page = FakePage()
    return instance


# --------------------------------------------------------------------------
# the config switch
# --------------------------------------------------------------------------
def test_releasing_is_the_default() -> None:
    """Hiding a 190 MB client was the surprise, so releasing is opt-out."""
    assert UiConfig().release_ui_on_close is True


# --------------------------------------------------------------------------
# the handshake between the tray thread and run_gui
# --------------------------------------------------------------------------
def test_wait_for_show_returns_true_on_a_show_request(app) -> None:
    threading.Timer(0.05, app._show_requested.set).start()
    assert app.wait_for_show() is True


def test_wait_for_show_returns_false_when_quit_is_asked_first(app) -> None:
    app._quit_requested.set()
    assert app.wait_for_show() is False


def test_quit_wins_even_though_it_wakes_the_same_loop(app) -> None:
    """``_quit`` sets *both* events, because it has to wake this loop up.

    Without the re-check inside ``wait_for_show`` that wake-up reads as "open
    the window again", and a tray quit with no window rebuilds the UI instead
    of exiting -- observed in a live run before this test existed.
    """
    threading.Timer(0.05, lambda: (app._quit_requested.set(), app._show_requested.set())).start()
    assert app.wait_for_show() is False


def test_tray_quit_without_a_window_sets_the_request(app) -> None:
    app.page = None
    app._tray = _StubTray()
    app._tray_quit()
    assert app._quit_requested.is_set()
    assert app._show_requested.is_set(), "the run_gui loop has to be woken up"


def test_quit_without_a_window_does_not_hard_exit(app, monkeypatch) -> None:
    """No window means nothing to destroy -- let run_gui return normally."""
    import drcom.ui.app as ui_app

    exited: list[int] = []
    monkeypatch.setattr(ui_app.DrcomApp, "_hard_exit", lambda self: exited.append(1))

    app.page = None
    app._quit()

    assert app._quit_requested.is_set()
    assert exited == [], "os._exit would skip the port release"


def test_tray_show_without_a_window_wakes_the_loop(app) -> None:
    app.page = None
    app._tray_show()
    assert app._show_requested.is_set()


def test_engine_commands_work_without_a_window(app, monkeypatch) -> None:
    """The engine does not need a page, so the tray menu still works."""
    app.page = None
    calls: list[str] = []

    monkeypatch.setattr(app.controller, "request_login", lambda: (calls.append("login"), (True, ""))[1])
    monkeypatch.setattr(app.controller, "request_logout", lambda: (calls.append("logout"), (True, ""))[1])
    monkeypatch.setattr(app.controller, "request_reconnect", lambda: (calls.append("reconnect"), (True, ""))[1])

    monkeypatch.setattr(type(app.controller), "online", property(lambda self: False), raising=False)
    app._tray_engine("tray_toggle")      # offline → login
    monkeypatch.setattr(type(app.controller), "online", property(lambda self: True), raising=False)
    app._tray_engine("tray_toggle")      # online → logout
    app._tray_engine("tray_reconnect")

    assert calls == ["login", "logout", "reconnect"]


# --------------------------------------------------------------------------
# rebuilding
# --------------------------------------------------------------------------
def test_prepare_new_window_drops_the_dead_widgets(app) -> None:
    app._log_rows = [object(), object()]
    app.log_list = object()
    app.message_text = object()
    app.alert_panel = object()
    app.close_action_control = object()
    app._page_size = (1084.0, 741.0)
    app.minimized = True
    app.log_queue.put(object())
    app.events.put(object())

    app.prepare_new_window()

    assert app.page is None, "an old page must never be reused for a new client"
    assert app._log_rows == []
    assert app.log_list is None
    assert app.message_text is None
    assert app.alert_panel is None
    assert app.close_action_control is None
    assert app._page_size == (0.0, 0.0)
    assert app.log_queue.empty(), "queued lines belong to the destroyed window"
    assert app.events.empty()
    assert app.minimized is False, "the user asked for the window, do not re-hide it"


def test_controller_events_still_reach_the_tray_without_a_window(app) -> None:
    """Otherwise a login failure while the window is closed is silent."""
    from drcom.controller import ControllerEvent
    from drcom.engine import EngineEvent, EngineState

    app.page = None
    app._tray = _StubTray()
    event = ControllerEvent(
        kind="engine",
        engine_event=EngineEvent(kind="login_failed", state=EngineState.RETRY_WAIT, message="密码错误"),
    )

    app._on_controller_event(event)

    assert app._tray.balloons == [("认证失败", "密码错误")]
    assert app._tray.tooltips, "the tooltip should follow the state too"


def test_log_records_are_dropped_while_headless(app) -> None:
    """No window means nobody to show them to; the log bus keeps its own copy."""
    app.page = None
    before = app.log_queue.qsize()
    app._on_log_record(_fake_record())
    assert app.log_queue.qsize() == before


def test_main_re_registers_listeners_instead_of_stacking_them(app) -> None:
    """``main()`` runs once per window, and a duplicate listener doubles events."""
    import inspect

    source = inspect.getsource(type(app).main)
    assert source.index("remove_listener(self._on_controller_event)") < source.index(
        "add_listener(self._on_controller_event)"
    )
    assert source.index("remove_listener(self._on_log_record)") < source.index(
        "add_listener(self._on_log_record)"
    )


def test_tray_is_created_once_across_rebuilds(app, monkeypatch) -> None:
    """A second icon would be an orphan the app can never stop."""
    import drcom.ui.app as ui_app

    created: list = []

    class _CountingTray(_StubTray):
        def start(self) -> bool:
            created.append(self)
            return True

    monkeypatch.setattr(ui_app, "TrayIcon", _CountingTray)
    monkeypatch.setattr(ui_app, "tray_available", lambda: True)
    app.enable_tray = True

    app._setup_tray()
    app._setup_tray()
    app._setup_tray()

    assert len(created) == 1


def _fake_record():
    import time

    from drcom.logbus import LogRecordView

    return LogRecordView(seq=1, timestamp=time.time(), level="INFO", message="x")
