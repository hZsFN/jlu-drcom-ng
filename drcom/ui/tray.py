"""System-tray integration.

Flet 1.0 exposes no tray control (``page.window`` can hide the window, but
nothing can bring it back), so a real tray icon needs a separate native
implementation.  We use `pystray <https://pypi.org/project/pystray/>`_, which
is pure Python and only needs Pillow (already a dependency of most desktop
Python stacks).

If pystray is unavailable the app degrades honestly: "关闭时最小化" instead of
"隐藏到托盘", because hiding a window with no way to restore it would strand
the user.  :class:`TrayIcon.available` tells the UI which mode it is in.
"""

from __future__ import annotations

import threading
from typing import Callable

__all__ = ["TrayIcon", "tray_available"]


def tray_available() -> bool:
    """Whether a real tray icon can be created in this environment."""
    try:
        import pystray  # noqa: F401
    except Exception:
        return False
    try:
        from PIL import Image  # noqa: F401
    except Exception:
        return False
    import sys

    return sys.platform in ("win32", "darwin") or bool(__import__("os").environ.get("DISPLAY"))


def _make_icon_image(color: str = "#86FF86", size: int = 64):
    """Draw the app icon: a HUD-style FPV reticle on a dark disc."""
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
        self._thread: threading.Thread | None = None

    @property
    def available(self) -> bool:
        return tray_available()

    @property
    def running(self) -> bool:
        return self._icon is not None

    def start(self) -> bool:
        """Create the icon.  Returns ``False`` when unsupported/failed."""
        if self._icon is not None:
            return True
        if not self.available:
            return False
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
            self._icon = pystray.Icon(
                "jlu-drcom-ng",
                icon=_make_icon_image(),
                title=self.tooltip,
                menu=_menu(),
            )
        except Exception:
            self._icon = None
            return False

        self._thread = threading.Thread(target=self._run, name="drcom-tray", daemon=True)
        self._thread.start()
        return True

    def _run(self) -> None:
        try:
            self._icon.run()  # type: ignore[union-attr]
        except Exception:
            pass

    def _call(self, func: Callable[[], None] | None) -> None:
        if func is None:
            return
        try:
            func()
        except Exception:
            pass

    def update(self, *, tooltip: str | None = None, color: str | None = None,
               online: bool | None = None) -> None:
        """Refresh the icon appearance / hover text."""
        if self._icon is None:
            return
        if tooltip is not None:
            try:
                self._icon.title = tooltip[:127]
            except Exception:
                pass
        if color is not None:
            try:
                self._icon.icon = _make_icon_image(color)
            except Exception:
                pass
        del online

    def notify(self, message: str, title: str = "JLU DrCOM NG") -> None:
        """Show a balloon/toast via the tray, when the backend supports it."""
        if self._icon is None:
            return
        try:
            self._icon.notify(message, title)
        except Exception:
            pass

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None
        self._thread = None
