"""Closing the window: ask / hide to tray / quit, and remembering the answer.

Written for the reported request "点关闭可以选择最小化到托盘（当然可以记忆选择）",
and for the latent bug found while implementing it: the old code set
``event.prevent_default = True`` on the ``WindowEvent``, but Flet 1.0's
``WindowEvent`` has no such field -- it is a plain object, so the assignment was
a stray attribute nobody reads, and the window always closed.  The switch that
actually holds the window open is ``page.window.prevent_close``.
"""

from __future__ import annotations

import asyncio
import time

import pytest

flet = pytest.importorskip("flet")

from tests.ui_fakes import FakeEvent, FakePage, walk  # noqa: E402

from drcom.config import CLOSE_ACTIONS  # noqa: E402


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
class _StubTray:
    """Stands in for a live pystray icon."""

    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def exits(monkeypatch) -> list:
    """Intercept the real process exit.

    Patch ``DrcomApp._hard_exit`` rather than ``os._exit``: the watchdog thread
    exists precisely to call it, and patching ``os`` globally would leave that
    thread armed with the real exit once the test's monkeypatch expired.
    """
    import drcom.ui.app as ui_app

    recorded: list = []
    monkeypatch.setattr(ui_app.DrcomApp, "_hard_exit", lambda self: recorded.append(0))
    return recorded


@pytest.fixture
def app(controller, monkeypatch):
    import drcom.ui.app as ui_app

    # A headless test has no mounted page, so control.update() raises by design.
    monkeypatch.setattr(flet.Control, "update", lambda self, *a, **k: None)
    # Keep the background ticker/API out of the test process.
    monkeypatch.setattr(type(controller), "start_background", lambda self: None)

    instance = ui_app.DrcomApp(controller, enable_tray=False)
    # Long enough that a test which does not finish the shutdown cannot have its
    # watchdog fire into the next test.
    instance.quit_watchdog_seconds = 60.0
    instance.page = FakePage()
    return instance


@pytest.fixture
def app_with_tray(app):
    app._tray = _StubTray()
    return app


def close_event() -> FakeEvent:
    return FakeEvent(type=flet.WindowEventType.CLOSE)


def button_labelled(root, label: str):
    for control in walk(root):
        if isinstance(control, flet.Button) and getattr(control.content, "value", None) == label:
            return control
    raise AssertionError(f"no button labelled {label!r} in the dialog")


def checkbox_in(root) -> flet.Checkbox:
    for control in walk(root):
        if isinstance(control, flet.Checkbox):
            return control
    raise AssertionError("the remember checkbox is missing from the dialog")


def click(control) -> None:
    control.on_click(FakeEvent(control=control))


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------
def test_default_is_to_ask(controller) -> None:
    assert controller.config.ui.close_action == "ask"


def test_unknown_action_falls_back_to_asking() -> None:
    from drcom.config import UiConfig

    assert UiConfig(close_action="explode").close_action == "ask"
    for action in CLOSE_ACTIONS:
        assert UiConfig(close_action=action).close_action == action


def test_legacy_boolean_config_does_not_crash(controller, temp_data_dir) -> None:
    """1.0.1 stored `close_to_tray: bool`; that key must be ignored, not fatal."""
    import json

    from drcom.config import ConfigStore

    path = temp_data_dir / "config.json"
    path.write_text(
        json.dumps({"ui": {"close_to_tray": False, "close_action": "tray"}}),
        encoding="utf-8",
    )
    store = ConfigStore(temp_data_dir)
    store.load()
    assert store.config.ui.close_action == "tray"


def test_legacy_config_without_the_new_key_asks(controller, temp_data_dir) -> None:
    import json

    from drcom.config import ConfigStore

    path = temp_data_dir / "config.json"
    path.write_text(json.dumps({"ui": {"close_to_tray": True}}), encoding="utf-8")
    store = ConfigStore(temp_data_dir)
    store.load()
    assert store.config.ui.close_action == "ask"


# --------------------------------------------------------------------------
# the mechanism that actually keeps the window alive
# --------------------------------------------------------------------------
def test_startup_installs_the_close_intercept(app) -> None:
    asyncio.run(app.main(app.page))
    assert app.page.window.prevent_close is True
    assert app.page.window.on_event is not None, "the close handler was never wired up"


def test_event_object_cannot_prevent_the_close(app) -> None:
    """Pin the reasoning: `prevent_default` is not a Flet field, so it is inert.

    If a future Flet adds it, this test fails and the intercept can be
    simplified -- which is the point of pinning it.
    """
    event = flet.WindowEvent("close", None, type=flet.WindowEventType.CLOSE)
    fields = getattr(type(event), "model_fields", None) or getattr(
        type(event), "__dataclass_fields__", {}
    )
    assert "prevent_default" not in fields
    event.prevent_default = True  # accepted, but nothing reads it
    assert "prevent_default" not in (
        getattr(type(event), "model_fields", None) or getattr(type(event), "__dataclass_fields__", {})
    )


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------
def test_ask_shows_the_chooser(app) -> None:
    app.controller.config.ui.close_action = "ask"
    app._on_window_event(close_event())
    assert len(app.page.dialogs) == 1
    assert app.page.window.visible is True, "asking must not hide the window first"


def test_tray_hides_the_window(app_with_tray) -> None:
    app_with_tray.controller.config.ui.close_action = "tray"
    app_with_tray._on_window_event(close_event())
    assert app_with_tray.page.window.visible is False
    assert app_with_tray.page.window.skip_task_bar is True
    assert not app_with_tray.page.dialogs, "a remembered choice must not ask again"


def test_tray_without_a_tray_minimises_instead(app) -> None:
    """Hiding with no way back would strand the user, so fall back."""
    app.controller.config.ui.close_action = "tray"
    assert app._tray is None
    app._on_window_event(close_event())
    assert app.page.window.minimized is True
    assert app.page.window.visible is True, "without a tray we must not hide the window"


def test_quit_exits(app, exits) -> None:
    """Quit must tear down synchronously and hand the window close to Flet.

    The old code called ``page.window.destroy()`` without awaiting it.  In Flet
    1.0 that method is a coroutine, so the call built an object and dropped it:
    the client was never told to close, Python died, and the native window was
    left on screen with no backend -- alive-looking but inert.  That orphan is
    what made "点设置没反应" look like a broken settings tab.
    """
    app.controller.config.ui.close_action = "quit"
    app._on_window_event(close_event())

    # Teardown happens now...
    assert app.page.window.prevent_close is False, "the intercept must be released"
    # ...and the window close is queued as a task rather than fired and dropped.
    assert app.page.tasks, "the window close was never scheduled"
    handler, _ = app.page.tasks[-1]
    assert handler == app._finish_quit

    asyncio.run(app._finish_quit())  # stands the watchdog down
    assert exits == [0]


def test_finish_quit_awaits_the_destroy(app, exits) -> None:
    """The scheduled finaliser must actually await destroy(), then exit."""
    destroyed: list = []

    class _RecordingWindow:
        prevent_close = False

        async def destroy(self) -> None:      # async, exactly like Flet 1.0
            destroyed.append(True)

    app.page.window = _RecordingWindow()
    asyncio.run(app._finish_quit())

    assert destroyed == [True], "destroy() was not awaited"
    assert exits == [0], "the process never exited"


def test_finish_quit_exits_even_if_destroy_fails(app, exits) -> None:
    class _BrokenWindow:
        async def destroy(self) -> None:
            raise RuntimeError("client is wedged")

    app.page.window = _BrokenWindow()
    asyncio.run(app._finish_quit())
    assert exits == [0], "a wedged client must not strand the process"


def test_quit_starts_a_watchdog(app, exits) -> None:
    """Belt and braces: even if the finaliser never runs, the process exits."""
    app.quit_watchdog_seconds = 0.05
    app._quit()
    time.sleep(0.6)
    assert exits == [0], "the watchdog never fired"


def test_watchdog_stands_down_after_a_clean_shutdown(app, exits) -> None:
    """A completed shutdown must not leave the watchdog armed."""
    app.quit_watchdog_seconds = 0.05
    app._quit()
    asyncio.run(app._finish_quit())
    assert exits == [0]

    exits.clear()
    time.sleep(0.6)
    assert exits == [], "the watchdog fired after the shutdown had finished"


def test_other_window_events_are_ignored(app) -> None:
    app.controller.config.ui.close_action = "quit"
    for name in ("RESIZED", "FOCUS", "MOVE", "MINIMIZE"):
        event_type = getattr(flet.WindowEventType, name, None)
        if event_type is None:
            continue
        app._on_window_event(FakeEvent(type=event_type))
    assert not app.page.dialogs
    assert app.page.window.visible is True


# --------------------------------------------------------------------------
# the chooser itself
# --------------------------------------------------------------------------
def test_choosing_tray_remembers_it(app_with_tray) -> None:
    app_with_tray._on_window_event(close_event())
    dialog = app_with_tray.page.dialogs[-1]
    checkbox_in(dialog).value = True

    click(button_labelled(dialog, "最小化到托盘"))

    assert app_with_tray.controller.config.ui.close_action == "tray"
    assert not app_with_tray.page.dialogs, "the dialog should be closed"
    assert app_with_tray.page.window.visible is False


def test_choosing_quit_remembers_it(app, exits) -> None:
    app._on_window_event(close_event())
    dialog = app.page.dialogs[-1]
    checkbox_in(dialog).value = True

    click(button_labelled(dialog, "退出程序"))

    assert app.controller.config.ui.close_action == "quit"
    assert app.page.window.prevent_close is False
    assert app.page.tasks, "quitting must schedule the window close"


def test_choice_is_not_remembered_unless_ticked(app) -> None:
    app._on_window_event(close_event())
    dialog = app.page.dialogs[-1]
    assert checkbox_in(dialog).value is False

    click(button_labelled(dialog, "最小化到托盘"))

    assert app.controller.config.ui.close_action == "ask", "unticked means ask again next time"
    assert app.page.window.minimized is True


def test_remembered_choice_persists_to_disk(app_with_tray, temp_data_dir) -> None:
    app_with_tray._on_window_event(close_event())
    dialog = app_with_tray.page.dialogs[-1]
    checkbox_in(dialog).value = True
    click(button_labelled(dialog, "最小化到托盘"))

    import json

    saved = json.loads((temp_data_dir / "config.json").read_text(encoding="utf-8"))
    assert saved["ui"]["close_action"] == "tray"


def test_cancel_closes_the_dialog_only(app) -> None:
    app._on_window_event(close_event())
    dialog = app.page.dialogs[-1]

    click(button_labelled(dialog, "取消"))

    assert not app.page.dialogs
    assert app.controller.config.ui.close_action == "ask"
    assert app.page.window.visible is True


def test_chooser_warns_when_there_is_no_tray(app) -> None:
    app._on_window_event(close_event())
    dialog = app.page.dialogs[-1]
    text = " ".join(
        getattr(c, "value", "") for c in walk(dialog) if isinstance(getattr(c, "value", None), str)
    )
    assert "pystray" in text, "the user should be told the tray is unavailable"


# --------------------------------------------------------------------------
# the settings selector
# --------------------------------------------------------------------------
def test_settings_selector_reflects_and_changes_the_action(app) -> None:
    app.view_host = None
    app.active_view = "设置"
    view = app._build_view("设置")

    control = app.close_action_control
    assert control.selected == ["ask"]

    control.selected = ["quit"]
    control.on_change(FakeEvent(control=control, data="quit"))

    assert app.controller.config.ui.close_action == "quit"
    assert app.close_action_note.value, "the hint text should explain the choice"
    assert view is not None


def test_settings_event_shapes_are_all_accepted(app) -> None:
    """SegmentedButton payloads differ across Flet builds; accept the lot."""
    app.view_host = None
    app._build_view("设置")
    control = app.close_action_control

    # Shape 1, the normal one: the control already holds the new selection.
    control.selected = ["tray"]
    app._on_close_action_change(FakeEvent(control=control))
    assert app.controller.config.ui.close_action == "tray"

    # Shape 2: no usable control, the value rides on the event.
    app._on_close_action_change(FakeEvent(control=None, data="quit"))
    assert app.controller.config.ui.close_action == "quit"

    # Shape 3: same, but as a one-element list (Flet's declared type).
    app._on_close_action_change(FakeEvent(control=None, data=["ask"]))
    assert app.controller.config.ui.close_action == "ask"


def test_settings_selector_ignores_rubbish(app) -> None:
    app.view_host = None
    app._build_view("设置")
    app._on_close_action_change(FakeEvent(control=app.close_action_control, data="nonsense"))
    assert app.controller.config.ui.close_action == "ask"


def test_remembering_refreshes_the_settings_control(app_with_tray) -> None:
    # NB: use app_with_tray throughout -- a bare `app` in a test that does not
    # request that fixture resolves to the module-level fixture *function*.
    app_with_tray.view_host = None
    app_with_tray._build_view("设置")
    assert app_with_tray.close_action_control.selected == ["ask"]

    app_with_tray._on_window_event(close_event())
    dialog = app_with_tray.page.dialogs[-1]
    checkbox_in(dialog).value = True
    click(button_labelled(dialog, "最小化到托盘"))

    assert app_with_tray.close_action_control.selected == ["tray"], "the settings view is now stale"
