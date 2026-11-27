"""System-tray integration.

Flet 1.0 exposes no tray control (``page.window`` can hide the window, but
nothing can bring it back), so a real tray icon needs a separate backend:

* **Linux (primary)**: StatusNotifierItem (SNI) + ``com.canonical.dbusmenu``
  over the user's D-Bus session bus (:mod:`drcom.ui.sni_tray`).  Works on both
  Wayland and X11 across KDE Plasma, GNOME (with AppIndicator extension),
  XFCE4, Cinnamon, MATE, LXQt, and Waybar — without GTK/PyGObject/Xlib.
* **Fallback / Windows / macOS**: `pystray <https://pypi.org/project/pystray/>`_
  (with startup verification so a backend crash inside the tray thread is never
  silently mistaken for a live icon).

If no tray backend can register an icon, the app degrades honestly instead of
hiding a window with no way to bring it back.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Callable

from .sni_tray import LinuxSniTray, session_bus_configured, sni_module_available

__all__ = ["TrayIcon", "tray_available"]

_ICON_IMAGE_CACHE: dict[tuple[str, int], object] = {}


def _pil_available() -> bool:
    try:
        from PIL import Image  # noqa: F401
    except Exception:
        return False
    return True


def _pystray_available() -> bool:
    try:
        import pystray  # noqa: F401
    except Exception:
        return False
    return _pil_available() and (
        sys.platform in ("win32", "darwin")
        or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    )


def _linux_sni_available() -> bool:
    return (
        sys.platform == "linux"
        and _pil_available()
        and session_bus_configured()
        and sni_module_available()
    )


def tray_available() -> bool:
    """Whether a real tray icon can be created in this environment."""
    if sys.platform == "linux" and _linux_sni_available():
        return True
    return _pystray_available()


def _make_icon_image(color: str = "#86FF86", size: int = 64):
    """Draw the app icon (cached per ``(color, size)`` to avoid allocating RGBA bitmaps)."""
    key = (color, size)
    cached = _ICON_IMAGE_CACHE.get(key)
    if cached is not None:
        return cached
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    margin = size * 0.06
    draw.ellipse([margin, margin, size - margin, size - margin], fill=(10, 14, 20, 235))
    line_color = color

    def rgb(value: str) -> tuple[int, int, int]:
        value = value.lstrip("#")
        return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]

    stroke = rgb(line_color)
    width = max(2, size // 22)
    centre = size / 2
    radius = size * 0.20
    draw.ellipse(
        [centre - radius, centre - radius, centre + radius, centre + radius],
        outline=stroke,
        width=width,
    )
    draw.line([centre - size * 0.34, centre, centre - radius - 2, centre], fill=stroke, width=width)
    draw.line([centre + radius + 2, centre, centre + size * 0.34, centre], fill=stroke, width=width)
    draw.ellipse([centre - 2, centre - 2, centre + 2, centre + 2], fill=stroke)
    _ICON_IMAGE_CACHE[key] = image
    return image


class TrayIcon:
    """A tray icon with a small menu, driven from its own thread.

    All callbacks are invoked on the tray thread, so the UI side must marshal
    them back onto the Flet event loop (the app does this via a queue).
    """

    def __init__(
        self,
        *,
        tooltip: str = "JLU DrCOM NG",
        on_show: Callable[[], None] | None = None,
        on_toggle: Callable[[], None] | None = None,
        on_reconnect: Callable[[], None] | None = None,
        on_quit: Callable[[], None] | None = None,
        status_provider: Callable[[], str] | None = None,
    ) -> None:
        self.tooltip = tooltip
        self.on_show = on_show
        self.on_toggle = on_toggle
        self.on_reconnect = on_reconnect
        self.on_quit = on_quit
        self.status_provider = status_provider
        self._icon = None
        self._sni: LinuxSniTray | None = None
        self._thread: threading.Thread | None = None
        self._last_color: str = ""
        self._last_tooltip: str = ""
        self._backend_name: str = ""

    @property
    def available(self) -> bool:
        return tray_available()

    @property
    def running(self) -> bool:
        if self._sni is not None and self._sni.running:
            return True
        return self._icon is not None

    @property
    def backend_name(self) -> str:
        return self._backend_name

    def start(self) -> bool:
        """Create the icon.  Returns ``False`` when unsupported/failed."""
        if self.running:
            return True

        # On Linux prefer native D-Bus StatusNotifierItem (works on Wayland +
        # X11 across KDE/GNOME-AppIndicator/XFCE/Waybar without GI or Xlib).
        if sys.platform == "linux" and _linux_sni_available():
            if self._start_sni():
                return True

        if not _pystray_available():
            return False
        return self._start_pystray()

    def _start_sni(self) -> bool:
        sni = LinuxSniTray(
            tooltip=self.tooltip,
            icon_factory=_make_icon_image,
            on_show=lambda: self._call(self.on_show),
            on_toggle=lambda: self._call(self.on_toggle),
            on_reconnect=lambda: self._call(self.on_reconnect),
            on_quit=lambda: self._call(self.on_quit),
        )
        if sni.start():
            self._sni = sni
            self._backend_name = "sni"
            return True
        return False

    def _start_pystray(self) -> bool:
        try:
            import pystray
        except Exception:
            return False

        def _menu() -> "pystray.Menu":
            return pystray.Menu(
                pystray.MenuItem("显示主窗口", lambda *_: self._call(self.on_show), default=True),
                pystray.MenuItem("登录 / 注销", lambda *_: self._call(self.on_toggle)),
                pystray.MenuItem("重新连接", lambda *_: self._call(self.on_reconnect)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", lambda *_: self._call(self.on_quit)),
            )

        try:
            icon = pystray.Icon(
                "jlu-drcom-ng",
                icon=_make_icon_image(),
                title=self.tooltip,
                menu=_menu(),
            )
        except Exception:
            self._icon = None
            return False

        # Verify startup instead of blindly returning True: on Linux Wayland or
        # headless X11 without an XEmbed tray, pystray's run() thread throws
        # immediately. Waiting briefly for setup() catches that honestly.
        started = threading.Event()
        failed = threading.Event()

        def _setup(active_icon) -> None:
            try:
                active_icon.visible = True
            except Exception:
                pass
            started.set()

        def _run() -> None:
            try:
                icon.run(setup=_setup)
            except Exception:
                failed.set()
                started.set()
            finally:
                if not started.is_set():
                    failed.set()
                    started.set()

        self._icon = icon
        self._thread = threading.Thread(target=_run, name="drcom-tray", daemon=True)
        self._thread.start()

        if started.wait(1.5) and failed.is_set():
            self._icon = None
            self._thread = None
            return False
        if not self._thread.is_alive():
            self._icon = None
            self._thread = None
            return False

        self._backend_name = "pystray"
        return True

    def _call(self, func: Callable[[], None] | None) -> None:
        if func is None:
            return
        try:
            func()
        except Exception:
            pass

    def update(
        self,
        *,
        tooltip: str | None = None,
        color: str | None = None,
        online: bool | None = None,
    ) -> None:
        """Refresh the icon appearance / hover text (only mutates on actual change)."""
        del online
        new_tip: str | None = None
        if tooltip is not None:
            trimmed = tooltip[:127]
            if trimmed != self._last_tooltip:
                new_tip = trimmed

        new_color: str | None = None
        if color is not None and color != self._last_color:
            new_color = color

        if new_tip is None and new_color is None:
            return

        if self._sni is not None and self._sni.running:
            self._sni.update(tooltip=new_tip, color=new_color)
            if new_tip is not None:
                self._last_tooltip = new_tip
            if new_color is not None:
                self._last_color = new_color
            return

        if self._icon is None:
            return
        if new_tip is not None:
            try:
                self._icon.title = new_tip
                self._last_tooltip = new_tip
            except Exception:
                pass
        if new_color is not None:
            try:
                self._icon.icon = _make_icon_image(new_color)
                self._last_color = new_color
            except Exception:
                pass

    def notify(self, message: str, title: str = "JLU DrCOM NG") -> None:
        """Show a balloon/toast via the tray, when the backend supports it."""
        if self._sni is not None and self._sni.running:
            self._sni.notify(message, title)
            return
        if self._icon is None:
            return
        try:
            self._icon.notify(message, title)
        except Exception:
            pass

    def stop(self) -> None:
        if self._sni is not None:
            try:
                self._sni.stop()
            except Exception:
                pass
            self._sni = None
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None
        self._thread = None
        self._backend_name = ""
