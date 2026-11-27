"""Linux StatusNotifierItem (SNI) D-Bus system tray backend.

Works on both Wayland and X11 across modern Linux desktops (KDE Plasma, GNOME
with AppIndicator/KStatusNotifierItem extension, XFCE4, Cinnamon, MATE, LXQt,
Sway/Hyprland + Waybar) without requiring GTK, PyGObject, or Xlib/XEmbed.

Requires ``dbus-fast`` (preferred, pure-Python compatible) or ``dbus-next`` on
the session D-Bus.  Deliberately avoids ``from __future__ import annotations``
so D-Bus type-signature annotations (``'s'``, ``'i'``, ``'a(iiay)'``, ...)
stay real ``str`` objects as expected by ``dbus_fast`` / ``dbus_next``.
"""

import asyncio
import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Optional

__all__ = [
    "LinuxSniTray",
    "rgba_to_argb32",
    "sni_module_available",
    "session_bus_configured",
]

_WATCHER_BUS = "org.kde.StatusNotifierWatcher"
_WATCHER_PATH = "/StatusNotifierWatcher"
_WATCHER_IFACE = "org.kde.StatusNotifierWatcher"
_SNI_PATH = "/StatusNotifierItem"
_SNI_IFACE = "org.kde.StatusNotifierItem"
_MENU_PATH = "/MenuBar"
_MENU_IFACE = "com.canonical.dbusmenu"

_INSTANCE_SEQ = 0
_INSTANCE_SEQ_LOCK = threading.Lock()


def _next_instance_seq() -> int:
    global _INSTANCE_SEQ
    with _INSTANCE_SEQ_LOCK:
        _INSTANCE_SEQ += 1
        return _INSTANCE_SEQ


def session_bus_configured() -> bool:
    """Whether a D-Bus session bus address appears to exist in this environment."""
    if os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return True
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir and Path(runtime_dir, "bus").exists():
        return True
    return False


def _load_dbus_backend() -> Optional[dict[str, Any]]:
    """Import ``dbus_fast`` (preferred) or ``dbus_next`` dynamically."""
    for pkg in ("dbus_fast", "dbus_next"):
        try:
            root = importlib.import_module(pkg)
            aio = importlib.import_module(f"{pkg}.aio")
            service = importlib.import_module(f"{pkg}.service")
            return {
                "name": pkg,
                "BusType": root.BusType,
                "Message": root.Message,
                "MessageType": root.MessageType,
                "Variant": root.Variant,
                "MessageBus": aio.MessageBus,
                "PropertyAccess": service.PropertyAccess,
                "ServiceInterface": service.ServiceInterface,
                "dbus_property": service.dbus_property,
                "method": service.method,
                "signal": service.signal,
            }
        except Exception:
            continue
    return None


def sni_module_available() -> bool:
    """Whether a Python D-Bus library suitable for SNI is importable."""
    return _load_dbus_backend() is not None


def rgba_to_argb32(rgba_bytes: bytes) -> bytes:
    """Convert tightly-packed RGBA bytes into network-byte-order ARGB32 bytes.

    StatusNotifierItem's ``IconPixmap`` expects ``(width, height, bytes)`` in
    big-endian ARGB32 order (A, R, G, B per pixel).
    """
    n = len(rgba_bytes)
    if n % 4 != 0:
        raise ValueError("RGBA buffer length must be a multiple of 4")
    out = bytearray(n)
    out[0::4] = rgba_bytes[3::4]
    out[1::4] = rgba_bytes[0::4]
    out[2::4] = rgba_bytes[1::4]
    out[3::4] = rgba_bytes[2::4]
    return bytes(out)


def _icon_pixmap_from_image(image) -> list[tuple[int, int, bytes]]:
    """Build ``a(iiay)`` icon pixmaps (64x64 and 32x32) from a PIL Image."""
    pixmaps: list[tuple[int, int, bytes]] = []
    try:
        rgba = image.convert("RGBA")
        for size in (64, 32):
            scaled = rgba if rgba.size == (size, size) else rgba.resize((size, size))
            pixmaps.append((size, size, rgba_to_argb32(scaled.tobytes())))
    except Exception:
        pass
    return pixmaps


def _split_tooltip(tooltip: str) -> tuple[str, str]:
    """Split ``'JLU DrCOM NG · 在线 ...'`` into SNI ``(title, subtitle)``."""
    text = (tooltip or "JLU DrCOM NG").strip()
    if " · " in text:
        head, tail = text.split(" · ", 1)
        return head.strip() or "JLU DrCOM NG", tail.strip()
    return text or "JLU DrCOM NG", ""


def _build_interfaces(dbus: dict[str, Any]):
    """Construct ``StatusNotifierItem`` and ``DBusMenu`` classes for *dbus*."""
    ServiceInterface = dbus["ServiceInterface"]
    PropertyAccess = dbus["PropertyAccess"]
    Variant = dbus["Variant"]
    dbus_property = dbus["dbus_property"]
    method = dbus["method"]
    signal = dbus["signal"]

    class _StatusNotifierItemInterface(ServiceInterface):
        def __init__(
            self,
            *,
            app_id: str,
            title: str,
            subtitle: str,
            icon_name: str,
            icon_theme_path: str,
            icon_pixmap: list[tuple[int, int, bytes]],
            on_activate: Callable[[], None],
            on_secondary: Callable[[], None],
        ) -> None:
            super().__init__(_SNI_IFACE)
            self._app_id = app_id
            self._title = title
            self._subtitle = subtitle
            self._icon_name = icon_name
            self._icon_theme_path = icon_theme_path
            self._icon_pixmap = icon_pixmap
            self._on_activate = on_activate
            self._on_secondary = on_secondary

        def set_tooltip(self, title: str, subtitle: str) -> None:
            self._title = title
            self._subtitle = subtitle
            self.NewTitle()
            self.NewToolTip()

        def set_icon(self, icon_name: str, icon_pixmap: list[tuple[int, int, bytes]]) -> None:
            self._icon_name = icon_name
            self._icon_pixmap = icon_pixmap
            self.NewIcon()

        @dbus_property(access=PropertyAccess.READ)
        def Category(self) -> "s":
            return "ApplicationStatus"

        @dbus_property(access=PropertyAccess.READ)
        def Id(self) -> "s":
            return self._app_id

        @dbus_property(access=PropertyAccess.READ)
        def Title(self) -> "s":
            return self._title if not self._subtitle else f"{self._title} · {self._subtitle}"

        @dbus_property(access=PropertyAccess.READ)
        def Status(self) -> "s":
            return "Active"

        @dbus_property(access=PropertyAccess.READ)
        def WindowId(self) -> "i":
            return 0

        @dbus_property(access=PropertyAccess.READ)
        def IconName(self) -> "s":
            return self._icon_name

        @dbus_property(access=PropertyAccess.READ)
        def IconPixmap(self) -> "a(iiay)":
            return self._icon_pixmap

        @dbus_property(access=PropertyAccess.READ)
        def IconThemePath(self) -> "s":
            return self._icon_theme_path

        @dbus_property(access=PropertyAccess.READ)
        def OverlayIconName(self) -> "s":
            return ""

        @dbus_property(access=PropertyAccess.READ)
        def OverlayIconPixmap(self) -> "a(iiay)":
            return []

        @dbus_property(access=PropertyAccess.READ)
        def AttentionIconName(self) -> "s":
            return ""

        @dbus_property(access=PropertyAccess.READ)
        def AttentionIconPixmap(self) -> "a(iiay)":
            return []

        @dbus_property(access=PropertyAccess.READ)
        def AttentionMovieName(self) -> "s":
            return ""

        @dbus_property(access=PropertyAccess.READ)
        def ToolTip(self) -> "(sa(iiay)ss)":
            return ("", [], self._title, self._subtitle)

        @dbus_property(access=PropertyAccess.READ)
        def ItemIsMenu(self) -> "b":
            return False

        @dbus_property(access=PropertyAccess.READ)
        def Menu(self) -> "o":
            return _MENU_PATH

        @method()
        def ContextMenu(self, x: "i", y: "i"):
            del x, y

        @method()
        def Activate(self, x: "i", y: "i"):
            del x, y
            self._on_activate()

        @method()
        def SecondaryActivate(self, x: "i", y: "i"):
            del x, y
            self._on_secondary()

        @method()
        def Scroll(self, delta: "i", orientation: "s"):
            del delta, orientation

        @signal()
        def NewTitle(self):
            return None

        @signal()
        def NewIcon(self):
            return None

        @signal()
        def NewAttentionIcon(self):
            return None

        @signal()
        def NewOverlayIcon(self):
            return None

        @signal()
        def NewToolTip(self):
            return None

        @signal()
        def NewStatus(self, status: "s") -> "s":
            return status

    class _DBusMenuInterface(ServiceInterface):
        def __init__(
            self,
            *,
            icon_theme_path: str,
            actions: dict[int, Callable[[], None]],
        ) -> None:
            super().__init__(_MENU_IFACE)
            self._icon_theme_path = icon_theme_path
            self._actions = actions
            self._revision = 1
            self._items: list[tuple[int, dict[str, Any]]] = [
                (
                    1,
                    {
                        "type": Variant("s", "standard"),
                        "label": Variant("s", "显示主窗口"),
                        "enabled": Variant("b", True),
                        "visible": Variant("b", True),
                    },
                ),
                (
                    2,
                    {
                        "type": Variant("s", "standard"),
                        "label": Variant("s", "登录 / 注销"),
                        "enabled": Variant("b", True),
                        "visible": Variant("b", True),
                    },
                ),
                (
                    3,
                    {
                        "type": Variant("s", "standard"),
                        "label": Variant("s", "重新连接"),
                        "enabled": Variant("b", True),
                        "visible": Variant("b", True),
                    },
                ),
                (
                    4,
                    {
                        "type": Variant("s", "separator"),
                        "visible": Variant("b", True),
                    },
                ),
                (
                    5,
                    {
                        "type": Variant("s", "standard"),
                        "label": Variant("s", "退出"),
                        "enabled": Variant("b", True),
                        "visible": Variant("b", True),
                    },
                ),
            ]
            self._item_map: dict[int, dict[str, Any]] = {
                item_id: props for item_id, props in self._items
            }

        def _filter_props(self, props: dict[str, Any], names: list[str]) -> dict[str, Any]:
            if not names:
                return dict(props)
            return {k: v for k, v in props.items() if k in names}

        @dbus_property(access=PropertyAccess.READ)
        def Version(self) -> "u":
            return 3

        @dbus_property(access=PropertyAccess.READ)
        def TextDirection(self) -> "s":
            return "ltr"

        @dbus_property(access=PropertyAccess.READ)
        def Status(self) -> "s":
            return "normal"

        @dbus_property(access=PropertyAccess.READ)
        def IconThemePath(self) -> "as":
            return [self._icon_theme_path] if self._icon_theme_path else []

        @method()
        def GetLayout(
            self, parent_id: "i", recursion_depth: "i", property_names: "as"
        ) -> "u(ia{sv}av)":
            if parent_id == 0:
                children = []
                if recursion_depth != 0:
                    for item_id, props in self._items:
                        filtered = self._filter_props(props, property_names)
                        children.append(Variant("(ia{sv}av)", (item_id, filtered, [])))
                root_props = self._filter_props(
                    {"children-display": Variant("s", "submenu")}, property_names
                )
                return [self._revision, (0, root_props, children)]
            props = self._item_map.get(parent_id, {})
            return [self._revision, (parent_id, self._filter_props(props, property_names), [])]

        @method()
        def GetGroupProperties(self, ids: "ai", property_names: "as") -> "a(ia{sv})":
            target_ids = list(ids) if ids else [item_id for item_id, _ in self._items]
            result: list[tuple[int, dict[str, Any]]] = []
            for item_id in target_ids:
                if item_id in self._item_map:
                    result.append((item_id, self._filter_props(self._item_map[item_id], property_names)))
            return result

        @method()
        def GetProperty(self, id: "i", name: "s") -> "v":
            props = self._item_map.get(id, {})
            if name in props:
                return props[name]
            return Variant("s", "")

        @method()
        def Event(self, id: "i", event_id: "s", data: "v", timestamp: "u"):
            del data, timestamp
            if event_id == "clicked":
                action = self._actions.get(id)
                if action is not None:
                    action()

        @method()
        def EventGroup(self, events: "a(isvu)") -> "ai":
            for item_id, event_id, data, timestamp in events:
                self.Event(item_id, event_id, data, timestamp)
            return []

        @method()
        def AboutToShow(self, id: "i") -> "b":
            del id
            return False

        @method()
        def AboutToShowGroup(self, ids: "ai") -> "aiai":
            del ids
            return [[], []]

        @signal()
        def ItemsPropertiesUpdated(
            self,
            updated_props: "a(ia{sv})",
            removed_props: "a(ias)",
        ) -> "a(ia{sv})a(ias)":
            return [updated_props, removed_props]

        @signal()
        def LayoutUpdated(self, revision: "u", parent: "i") -> "ui":
            return [revision, parent]

        @signal()
        def ItemActivationRequested(self, id: "i", timestamp: "u") -> "iu":
            return [id, timestamp]

    return _StatusNotifierItemInterface, _DBusMenuInterface


class LinuxSniTray:
    """StatusNotifierItem + DBusMenu tray icon running on its own asyncio thread."""

    def __init__(
        self,
        *,
        tooltip: str = "JLU DrCOM NG",
        icon_factory: Callable[[str, int], Any],
        on_show: Callable[[], None],
        on_toggle: Callable[[], None],
        on_reconnect: Callable[[], None],
        on_quit: Callable[[], None],
        initial_color: str = "#86FF86",
    ) -> None:
        self._tooltip = tooltip
        self._icon_factory = icon_factory
        self._on_show = on_show
        self._on_toggle = on_toggle
        self._on_reconnect = on_reconnect
        self._on_quit = on_quit
        self._color = initial_color

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_future: Optional[asyncio.Future] = None
        self._bus = None
        self._sni_iface = None
        self._menu_iface = None
        self._service_name = ""
        self._theme_dir: Optional[Path] = None
        self._icon_files: dict[str, str] = {}
        self._running = False
        self._last_error = ""

    @property
    def running(self) -> bool:
        return self._running

    @property
    def last_error(self) -> str:
        return self._last_error

    def _prepare_icon_assets(self, color: str) -> tuple[str, list[tuple[int, int, bytes]]]:
        """Write a PNG into ``_theme_dir`` and build ARGB32 pixmaps for *color*."""
        image = self._icon_factory(color, 64)
        pixmaps = _icon_pixmap_from_image(image)
        slug = color.lstrip("#").lower() or "default"
        icon_name = f"jlu-drcom-ng-{slug}"
        if self._theme_dir is not None and icon_name not in self._icon_files:
            try:
                png_path = self._theme_dir / f"{icon_name}.png"
                image.convert("RGBA").save(png_path, format="PNG")
                self._icon_files[icon_name] = str(png_path)
            except Exception:
                pass
        return icon_name, pixmaps

    def start(self, *, timeout: float = 3.0) -> bool:
        """Register the SNI item on the session bus.

        Blocks briefly until registration succeeds or fails so the caller knows
        honestly whether a StatusNotifierHost accepted the icon.
        """
        if self._running:
            return True
        if sys.platform != "linux" and not os.environ.get("DRCOM_FORCE_SNI"):
            return False
        if not session_bus_configured():
            self._last_error = "no D-Bus session bus"
            return False
        dbus = _load_dbus_backend()
        if dbus is None:
            self._last_error = "dbus-fast / dbus-next not installed"
            return False

        self._theme_dir = Path(tempfile.mkdtemp(prefix="jlu-drcom-tray-"))
        ready = threading.Event()
        result = {"ok": False}

        def runner() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            try:
                loop.run_until_complete(self._serve(dbus, ready, result))
            except Exception as exc:
                self._last_error = repr(exc)
                if not ready.is_set():
                    ready.set()
            finally:
                self._running = False
                self._bus = None
                self._sni_iface = None
                self._menu_iface = None
                self._stop_future = None
                try:
                    loop.close()
                except Exception:
                    pass
                self._loop = None
                self._cleanup_theme_dir()

        self._thread = threading.Thread(target=runner, name="drcom-tray-sni", daemon=True)
        self._thread.start()
        if not ready.wait(timeout):
            self._last_error = "SNI registration timed out"
            self.stop()
            return False
        if not result["ok"]:
            self.stop()
            return False
        return True

    async def _watcher_has_host(self, bus, dbus: dict[str, Any]) -> bool:
        """Return True only when StatusNotifierWatcher exists and has a host."""
        Message = dbus["Message"]
        MessageType = dbus["MessageType"]

        # 1. Check whether org.kde.StatusNotifierWatcher is owned on the bus.
        owner_reply = await bus.call(
            Message(
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
                member="NameHasOwner",
                signature="s",
                body=[_WATCHER_BUS],
            )
        )
        if owner_reply.message_type == MessageType.ERROR or not owner_reply.body:
            return False
        if not bool(owner_reply.body[0]):
            return False

        # 2. Check IsStatusNotifierHostRegistered property.
        prop_reply = await bus.call(
            Message(
                destination=_WATCHER_BUS,
                path=_WATCHER_PATH,
                interface="org.freedesktop.DBus.Properties",
                member="Get",
                signature="ss",
                body=[_WATCHER_IFACE, "IsStatusNotifierHostRegistered"],
            )
        )
        if prop_reply.message_type == MessageType.ERROR or not prop_reply.body:
            # Some minimal SNI watchers omit the Properties.Get check; fall
            # through and let RegisterStatusNotifierItem decide.
            return True
        val = prop_reply.body[0]
        return bool(getattr(val, "value", val))

    async def _register_with_watcher(self, bus, dbus: dict[str, Any], service_name: str) -> bool:
        Message = dbus["Message"]
        MessageType = dbus["MessageType"]
        reply = await bus.call(
            Message(
                destination=_WATCHER_BUS,
                path=_WATCHER_PATH,
                interface=_WATCHER_IFACE,
                member="RegisterStatusNotifierItem",
                signature="s",
                body=[service_name],
            )
        )
        return reply.message_type != MessageType.ERROR

    async def _serve(
        self,
        dbus: dict[str, Any],
        ready: threading.Event,
        result: dict[str, bool],
    ) -> None:
        bus = await dbus["MessageBus"](bus_type=dbus["BusType"].SESSION).connect()
        self._bus = bus
        try:
            if not await self._watcher_has_host(bus, dbus):
                self._last_error = "no StatusNotifierHost registered on session D-Bus"
                ready.set()
                return

            SniIface, MenuIface = _build_interfaces(dbus)
            title, subtitle = _split_tooltip(self._tooltip)
            icon_name, pixmaps = self._prepare_icon_assets(self._color)
            theme_str = str(self._theme_dir) if self._theme_dir else ""

            self._sni_iface = SniIface(
                app_id="jlu-drcom-ng",
                title=title,
                subtitle=subtitle,
                icon_name=icon_name,
                icon_theme_path=theme_str,
                icon_pixmap=pixmaps,
                on_activate=self._on_show,
                on_secondary=self._on_toggle,
            )
            self._menu_iface = MenuIface(
                icon_theme_path=theme_str,
                actions={
                    1: self._on_show,
                    2: self._on_toggle,
                    3: self._on_reconnect,
                    5: self._on_quit,
                },
            )
            bus.export(_SNI_PATH, self._sni_iface)
            bus.export(_MENU_PATH, self._menu_iface)

            service_name = f"org.kde.StatusNotifierItem-{os.getpid()}-{_next_instance_seq()}"
            self._service_name = service_name
            await bus.request_name(service_name)

            if not await self._register_with_watcher(bus, dbus, service_name):
                self._last_error = "RegisterStatusNotifierItem failed"
                ready.set()
                return

            # Re-register automatically if Plasma / GNOME Shell / Waybar restarts.
            await self._subscribe_watcher_restart(bus, dbus, service_name)

            self._stop_future = asyncio.get_running_loop().create_future()
            self._running = True
            result["ok"] = True
            ready.set()
            await self._stop_future
        finally:
            try:
                bus.disconnect()
            except Exception:
                pass

    async def _subscribe_watcher_restart(
        self, bus, dbus: dict[str, Any], service_name: str
    ) -> None:
        """Listen for NameOwnerChanged on StatusNotifierWatcher to re-register."""
        Message = dbus["Message"]
        MessageType = dbus["MessageType"]
        rule = (
            "type='signal',"
            "sender='org.freedesktop.DBus',"
            "interface='org.freedesktop.DBus',"
            "member='NameOwnerChanged',"
            f"arg0='{_WATCHER_BUS}'"
        )
        try:
            await bus.call(
                Message(
                    destination="org.freedesktop.DBus",
                    path="/org/freedesktop/DBus",
                    interface="org.freedesktop.DBus",
                    member="AddMatch",
                    signature="s",
                    body=[rule],
                )
            )
        except Exception:
            return

        loop = asyncio.get_running_loop()

        def _handler(msg):
            if (
                msg.message_type == MessageType.SIGNAL
                and msg.interface == "org.freedesktop.DBus"
                and msg.member == "NameOwnerChanged"
                and len(msg.body) >= 3
                and msg.body[0] == _WATCHER_BUS
                and msg.body[2]
            ):
                loop.create_task(self._register_with_watcher(bus, dbus, service_name))
            return False

        try:
            bus.add_message_handler(_handler)
        except Exception:
            pass

    def update(self, *, tooltip: Optional[str] = None, color: Optional[str] = None) -> None:
        loop = self._loop
        sni = self._sni_iface
        if not self._running or loop is None or sni is None:
            return

        if tooltip is not None:
            self._tooltip = tooltip
            title, subtitle = _split_tooltip(tooltip)
            try:
                loop.call_soon_threadsafe(sni.set_tooltip, title, subtitle)
            except Exception:
                pass

        if color is not None:
            self._color = color
            try:
                icon_name, pixmaps = self._prepare_icon_assets(color)
                loop.call_soon_threadsafe(sni.set_icon, icon_name, pixmaps)
            except Exception:
                pass

    def notify(self, message: str, title: str = "JLU DrCOM NG") -> None:
        exe = shutil.which("notify-send")
        if not exe:
            return
        try:
            subprocess.Popen(
                [exe, "-a", "JLU DrCOM NG", title, message],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except Exception:
            pass

    def stop(self) -> None:
        self._running = False
        loop = self._loop
        fut = self._stop_future
        if loop is not None and fut is not None:
            try:
                def _cancel() -> None:
                    if not fut.done():
                        fut.set_result(None)

                loop.call_soon_threadsafe(_cancel)
            except Exception:
                pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.5)
        self._thread = None
        self._cleanup_theme_dir()

    def _cleanup_theme_dir(self) -> None:
        path = self._theme_dir
        self._theme_dir = None
        self._icon_files.clear()
        if path is not None:
            shutil.rmtree(path, ignore_errors=True)
