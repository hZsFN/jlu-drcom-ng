"""Tests for Linux system tray (SNI D-Bus + pystray fallback) and window close behavior."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

flet = pytest.importorskip("flet")

from drcom.ui import sni_tray, tray as tray_mod
from tests.ui_fakes import FakeEvent, FakePage, walk


# --------------------------------------------------------------------------
# SNI D-Bus protocol helpers & interface tests (mocked D-Bus primitives)
# --------------------------------------------------------------------------
class _FakeVariant:
    def __init__(self, signature: str, value) -> None:
        self.signature = signature
        self.value = value


class _FakePropertyAccess:
    READ = "read"


class _FakeServiceInterface:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeMessageType:
    METHOD_CALL = 1
    METHOD_RETURN = 2
    ERROR = 3
    SIGNAL = 4


class _FakeMessage:
    def __init__(self, **kwargs) -> None:
        self.message_type = kwargs.get("message_type", _FakeMessageType.METHOD_CALL)
        self.destination = kwargs.get("destination", "")
        self.path = kwargs.get("path", "")
        self.interface = kwargs.get("interface", "")
        self.member = kwargs.get("member", "")
        self.signature = kwargs.get("signature", "")
        self.body = kwargs.get("body", [])


def _fake_dbus_backend(fake_bus_cls=None):
    return {
        "name": "fake_dbus",
        "BusType": SimpleNamespace(SESSION="session"),
        "Message": _FakeMessage,
        "MessageType": _FakeMessageType,
        "Variant": _FakeVariant,
        "MessageBus": fake_bus_cls,
        "PropertyAccess": _FakePropertyAccess,
        "ServiceInterface": _FakeServiceInterface,
        "dbus_property": lambda **_kw: (lambda fn: fn),
        "method": lambda **_kw: (lambda fn: fn),
        "signal": lambda **_kw: (lambda fn: fn),
    }


def test_rgba_to_argb32_converts_pixel_order() -> None:
    # 2 pixels: RGBA (10, 20, 30, 255) and (1, 2, 3, 128)
    rgba = bytes([10, 20, 30, 255, 1, 2, 3, 128])
    assert sni_tray.rgba_to_argb32(rgba) == bytes([255, 10, 20, 30, 128, 1, 2, 3])
    with pytest.raises(ValueError):
        sni_tray.rgba_to_argb32(b"\x01\x02")


def test_sni_interfaces_expose_valid_annotations_and_dispatch_callbacks() -> None:
    """D-Bus signature annotations must be real strings, not quoted forward refs."""
    dbus = _fake_dbus_backend()
    SniIface, MenuIface = sni_tray._build_interfaces(dbus)

    # Verify annotations are raw D-Bus signatures (no `from __future__ import annotations`).
    assert SniIface.Category.__annotations__["return"] == "s"
    assert SniIface.WindowId.__annotations__["return"] == "i"
    assert SniIface.IconPixmap.__annotations__["return"] == "a(iiay)"
    assert SniIface.ToolTip.__annotations__["return"] == "(sa(iiay)ss)"
    assert MenuIface.GetLayout.__annotations__["return"] == "u(ia{sv}av)"

    calls: list[str] = []
    sni = SniIface(
        app_id="jlu-drcom-ng",
        title="JLU DrCOM NG",
        subtitle="在线 10.0.0.1",
        icon_name="jlu-drcom-ng-86ff86",
        icon_theme_path="/tmp/icons",
        icon_pixmap=[(32, 32, b"\x00" * (32 * 32 * 4))],
        on_activate=lambda: calls.append("show"),
        on_secondary=lambda: calls.append("toggle"),
    )
    assert sni.Id() == "jlu-drcom-ng"
    assert sni.ItemIsMenu() is False
    assert sni.Menu() == "/MenuBar"
    assert sni.ToolTip() == ("", [], "JLU DrCOM NG", "在线 10.0.0.1")

    sni.Activate(10, 20)
    sni.SecondaryActivate(10, 20)
    assert calls == ["show", "toggle"]

    menu = MenuIface(
        icon_theme_path="/tmp/icons",
        actions={
            1: lambda: calls.append("menu_show"),
            2: lambda: calls.append("menu_toggle"),
            3: lambda: calls.append("menu_reconnect"),
            5: lambda: calls.append("menu_quit"),
        },
    )
    rev, layout = menu.GetLayout(0, -1, [])
    assert rev == 1
    root_id, root_props, children = layout
    assert root_id == 0
    assert root_props["children-display"].value == "submenu"
    assert len(children) == 5

    menu.Event(1, "clicked", _FakeVariant("i", 0), 0)
    menu.Event(3, "clicked", _FakeVariant("i", 0), 0)
    menu.Event(5, "clicked", _FakeVariant("i", 0), 0)
    menu.Event(1, "hovered", _FakeVariant("i", 0), 0)  # non-click ignored
    assert calls == ["show", "toggle", "menu_show", "menu_reconnect", "menu_quit"]


def test_linux_sni_tray_lifecycle_register_update_and_re_register(monkeypatch) -> None:
    calls_made: list[tuple[str, list]] = []
    exported: dict[str, object] = {}
    handlers: list = []

    class FakeBus:
        def __init__(self, bus_type=None) -> None:
            self.bus_type = bus_type
            self.disconnected = False

        async def connect(self):
            return self

        def export(self, path: str, iface) -> None:
            exported[path] = iface

        async def request_name(self, name: str) -> None:
            calls_made.append(("request_name", [name]))

        def add_message_handler(self, fn) -> None:
            handlers.append(fn)

        async def call(self, msg: _FakeMessage) -> _FakeMessage:
            calls_made.append((msg.member, list(msg.body)))
            if msg.member == "NameHasOwner":
                return _FakeMessage(message_type=_FakeMessageType.METHOD_RETURN, body=[True])
            if msg.member == "Get":
                return _FakeMessage(
                    message_type=_FakeMessageType.METHOD_RETURN,
                    body=[_FakeVariant("b", True)],
                )
            return _FakeMessage(message_type=_FakeMessageType.METHOD_RETURN, body=[])

        def disconnect(self) -> None:
            self.disconnected = True

    monkeypatch.setenv("DRCOM_FORCE_SNI", "1")
    monkeypatch.setattr(sni_tray, "session_bus_configured", lambda: True)
    monkeypatch.setattr(sni_tray, "_load_dbus_backend", lambda: _fake_dbus_backend(FakeBus))

    sni = sni_tray.LinuxSniTray(
        tooltip="JLU DrCOM NG · 待机",
        icon_factory=tray_mod._make_icon_image,
        on_show=lambda: None,
        on_toggle=lambda: None,
        on_reconnect=lambda: None,
        on_quit=lambda: None,
    )
    assert sni.start(timeout=2.0) is True
    assert sni.running is True
    assert "/StatusNotifierItem" in exported
    assert "/MenuBar" in exported
    assert any(member == "RegisterStatusNotifierItem" for member, _ in calls_made)
    assert sni._theme_dir is not None and sni._theme_dir.is_dir()

    # Update tooltip and icon color without errors
    sni.update(tooltip="JLU DrCOM NG · 在线 10.67.1.2", color="#33FF99")
    sni.stop()
    assert sni.running is False


def test_linux_sni_tray_returns_false_when_no_host_registered(monkeypatch) -> None:
    class NoHostBus:
        def __init__(self, bus_type=None) -> None:
            pass

        async def connect(self):
            return self

        async def call(self, msg: _FakeMessage) -> _FakeMessage:
            if msg.member == "NameHasOwner":
                return _FakeMessage(message_type=_FakeMessageType.METHOD_RETURN, body=[True])
            if msg.member == "Get":
                return _FakeMessage(
                    message_type=_FakeMessageType.METHOD_RETURN,
                    body=[_FakeVariant("b", False)],
                )
            return _FakeMessage(message_type=_FakeMessageType.METHOD_RETURN, body=[])

        def disconnect(self) -> None:
            pass

    monkeypatch.setenv("DRCOM_FORCE_SNI", "1")
    monkeypatch.setattr(sni_tray, "session_bus_configured", lambda: True)
    monkeypatch.setattr(sni_tray, "_load_dbus_backend", lambda: _fake_dbus_backend(NoHostBus))

    sni = sni_tray.LinuxSniTray(
        tooltip="JLU DrCOM NG",
        icon_factory=tray_mod._make_icon_image,
        on_show=lambda: None,
        on_toggle=lambda: None,
        on_reconnect=lambda: None,
        on_quit=lambda: None,
    )
    assert sni.start(timeout=2.0) is False
    assert sni.running is False


def test_pystray_start_returns_false_when_run_thread_crashes(monkeypatch) -> None:
    """A crashed pystray thread must not pretend to have created a tray icon."""

    class BrokenIcon:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def run(self, setup=None) -> None:
            raise RuntimeError("X11 BadWindow: no XEmbed system tray")

    fake_pystray = SimpleNamespace(
        Icon=BrokenIcon,
        Menu=lambda *items: items,
        MenuItem=lambda *args, **kwargs: (args, kwargs),
    )
    fake_pystray.Menu.SEPARATOR = "---"

    import sys

    monkeypatch.setitem(sys.modules, "pystray", fake_pystray)
    monkeypatch.setattr(tray_mod, "_linux_sni_available", lambda: False)
    monkeypatch.setattr(tray_mod, "_pystray_available", lambda: True)

    icon = tray_mod.TrayIcon()
    assert icon.start() is False
    assert icon.running is False
    assert icon._icon is None


# --------------------------------------------------------------------------
# Linux window close / release-to-tray / teardown behavior
# --------------------------------------------------------------------------
@pytest.fixture
def linux_app(controller, monkeypatch):
    import drcom.ui.app as ui_app

    monkeypatch.setattr(ui_app.sys, "platform", "linux")
    monkeypatch.setattr(flet.Control, "update", lambda self, *a, **k: None)
    monkeypatch.setattr(type(controller), "start_background", lambda self: None)

    instance = ui_app.DrcomApp(controller, enable_tray=False)
    instance.quit_watchdog_seconds = 60.0
    instance.page = FakePage()
    instance.page.window.prevent_close = True
    instance.page.window.on_event = instance._on_window_event
    return instance


def test_destroy_window_clears_and_flushes_prevent_close_before_destroy(linux_app) -> None:
    """Linux GTK blocks destroy/close if prevent_close is still True."""
    order: list[str] = []

    class TrackingWindow:
        def __init__(self) -> None:
            self._prevent_close = True
            self.on_event = object()
            self.destroyed = False

        @property
        def prevent_close(self):
            return self._prevent_close

        @prevent_close.setter
        def prevent_close(self, val):
            self._prevent_close = val
            order.append(f"prevent_close={val}")

        async def destroy(self) -> None:
            order.append("destroy")
            self.destroyed = True

    orig_update = linux_app.page.update

    def tracking_update() -> None:
        order.append("update")
        orig_update()

    linux_app.page.window = TrackingWindow()
    linux_app.page.update = tracking_update

    asyncio.run(linux_app._destroy_window())
    assert linux_app.page.window.destroyed is True
    assert linux_app.page.window.on_event is None
    assert order.index("prevent_close=False") < order.index("update") < order.index("destroy")


def test_teardown_falls_back_to_close_and_terminate_when_linux_client_sticks(linux_app) -> None:
    """If window.destroy() leaves a Linux GTK client alive, close() and terminate() kick in."""
    actions: list[str] = []

    class StuckProc:
        returncode = None

        def terminate(self) -> None:
            actions.append("terminate")
            self.returncode = -15

        def kill(self) -> None:
            actions.append("kill")
            self.returncode = -9

    class StuckWindow:
        prevent_close = True
        on_event = object()

        async def destroy(self) -> None:
            actions.append("destroy")

        async def close(self) -> None:
            actions.append("close")

    linux_app.page.window = StuckWindow()
    linux_app._view_proc = StuckProc()

    asyncio.run(linux_app._destroy_window())
    assert actions == ["destroy", "close", "terminate"]
    assert linux_app._view_proc is None


def test_linux_with_native_core_and_tray_releases_window_to_tray(linux_app, monkeypatch) -> None:
    """On Linux with drcomd + SNI tray, closing releases the window and keeps the tray."""

    class StubTray:
        def stop(self) -> None:
            pass

    monkeypatch.setattr(linux_app, "_native_core_active", lambda: True)
    linux_app._tray = StubTray()
    linux_app.controller.config.ui.close_action = "ask"

    linux_app._on_window_event(FakeEvent(type=flet.WindowEventType.CLOSE))
    dialog = linux_app.page.dialogs[-1]
    labels = [
        getattr(c.content, "value", "")
        for c in walk(dialog)
        if isinstance(c, flet.Button)
    ]
    assert "最小化到托盘" in labels

    # Choosing tray releases the Flet client instead of exiting the Python tray host
    linux_app.controller.config.ui.close_action = "tray"
    linux_app._on_window_event(FakeEvent(type=flet.WindowEventType.CLOSE))
    assert linux_app.relaunch_wanted is True
    handler, _ = linux_app.page.tasks[-1]
    assert handler == linux_app._destroy_window


def test_linux_with_native_core_without_tray_still_closes_window(linux_app, monkeypatch) -> None:
    """Without a tray host (e.g. stock GNOME), closing exits UI while drcomd stays online."""
    import drcom.ui.app as ui_app

    exits: list[int] = []
    monkeypatch.setattr(ui_app.DrcomApp, "_hard_exit", lambda self: exits.append(0))
    monkeypatch.setattr(linux_app, "_native_core_active", lambda: True)
    linux_app._tray = None
    linux_app.controller.config.ui.close_action = "tray"

    linux_app._on_window_event(FakeEvent(type=flet.WindowEventType.CLOSE))
    assert linux_app.relaunch_wanted is False
    assert linux_app.page.tasks
    handler, _ = linux_app.page.tasks[-1]
    assert handler == linux_app._destroy_window_then_exit

    asyncio.run(linux_app._destroy_window_then_exit())
    assert linux_app.page.window.destroyed is True
    assert exits == [0]


def test_shutdown_from_main_thread_stops_native_daemon(linux_app) -> None:
    """Quitting from the Linux tray while the window is closed must also stop drcomd."""
    stopped = []

    class FakeRemoteEngine:
        def stop(self) -> None:
            stopped.append("stop")

        def shutdown_daemon(self) -> None:
            stopped.append("shutdown_daemon")

    linux_app.controller.engine = FakeRemoteEngine()
    linux_app.shutdown_from_main_thread()
    assert "shutdown_daemon" in stopped
