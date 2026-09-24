"""The Flet front-end: a HUD-styled control panel.

Layout
------
::

    ┌──────────────────────────────────────────────────────────────┐
    │ DRCOM · JLU          ▓ keepalive / heading tape ▓     12:00  │  header
    ├──────────────────────────────────────────────────────────────┤
    │ [状态] [日志] [账号] [设置] [关于]                             │  nav
    ├──────────────────────────────────────────────────────────────┤
    │  ┌────────────────────────────────────────────────────────┐  │
    │  │   HUD canvas: FPV · pitch ladder · tapes · corners     │  │
    │  └────────────────────────────────────────────────────────┘  │
    │   状态      IP            在线时长        账号                 │  hero readouts
    │  [登录] [注销] [重连] [端口诊断]                               │  actions
    │   今日 / 本周 / 掉线 / 延迟 / 丢包 / 流量                      │  stats
    └──────────────────────────────────────────────────────────────┘

Threading
---------
The controller publishes from worker threads, so the listener only pushes onto
a :class:`queue.Queue`.  A single ``asyncio`` task (``_ui_tick``) drains that
queue and mutates widgets — all Flet access stays on the UI event loop.
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from pathlib import Path

import flet as ft

from ..config import CLOSE_ACTION_LABELS, CLOSE_ACTIONS
from ..controller import AppController, ControllerEvent
from ..engine import EngineState
from ..logbus import LogRecordView
from ..traffic import format_bytes
from .hud import HudTelemetry, HudPainter
from .theme import HUD, hud_of
from .tray import TrayIcon, tray_available
from flet import canvas as cv

__all__ = ["DrcomApp", "run_gui"]

VIEWS = ("状态", "日志", "账号", "设置", "关于")

STATE_LABELS = {
    EngineState.IDLE: "待机",
    EngineState.BINDING: "绑定端口",
    EngineState.CHALLENGING: "获取挑战码",
    EngineState.AUTHENTICATING: "认证中",
    EngineState.ONLINE: "在线",
    EngineState.RETRY_WAIT: "等待重试",
    EngineState.FATAL: "需处理",
    EngineState.STOPPED: "已停止",
}


class DrcomApp:
    """Builds and drives the UI.  One instance per Flet page."""

    #: Hard ceiling on shutdown.  If the client never acknowledges the window
    #: destroy we exit anyway, so a wedged client cannot keep the process --
    #: and port 61440 -- alive.
    quit_watchdog_seconds: float = 4.0

    def __init__(
        self,
        controller: AppController,
        *,
        minimized: bool = False,
        enable_tray: bool = True,
    ) -> None:
        self.controller = controller
        self.minimized = minimized
        self.enable_tray = enable_tray
        #: Set once shutdown has finished, so the quit watchdog can stand down.
        self._exit_now = threading.Event()

        self.page: ft.Page | None = None
        self.hud = self._make_hud()
        self.events: "queue.Queue[ControllerEvent]" = queue.Queue()
        self.log_queue: "queue.Queue[LogRecordView]" = queue.Queue()

        self.active_view = VIEWS[0]
        self._tray: TrayIcon | None = None
        self._last_keepalive_at = 0.0
        self._telemetry = HudTelemetry()
        self._canvas_size = (820.0, 360.0)
        self._log_rows: list[ft.Control] = []
        self._log_paused = False
        self._pending_log_count = 0
        self._last_tooltip = ""
        self._last_status_snapshot = ""

        # Exponential moving averages for the instrument feeds.  Raw readings
        # are far too jittery to point an instrument at: a needle that twitches
        # every sample is harder to read than one that lags by a second.
        self._ema: dict[str, float] = {}
        self._ema_alpha = 0.25
        # Signature of the last painted frame, so an unchanged HUD is not
        # rebuilt (rebuilding 280 shapes every second also caused visible churn).
        self._hud_signature: tuple | None = None
        #: Real client-area size, as reported by ``page.on_resize``.  This is
        #: the only trustworthy source: ``page.window.width`` echoes back the
        #: value we assigned, which is how the canvas ended up wider than its
        #: container and got clipped on the right.
        self._page_size: tuple[float, float] = (0.0, 0.0)
        #: Canvas height, decided once at build time (never from a resize event).
        self._canvas_height = 380.0

        # widget refs created in build()
        self.hero_status: ft.Text | None = None
        self.hero_ip: ft.Text | None = None
        self.hero_uptime: ft.Text | None = None
        self.hero_account: ft.Text | None = None
        self.state_pill: ft.Container | None = None
        self.state_pill_text: ft.Text | None = None
        self.clock_text: ft.Text | None = None
        self.phase_bar: ft.ProgressBar | None = None
        self.message_text: ft.Text | None = None
        self.advice_text: ft.Text | None = None
        self.alert_panel: ft.Container | None = None
        self.stat_cells: dict[str, ft.Text] = {}
        self.log_list: ft.ListView | None = None
        self.log_file_text: ft.Text | None = None
        self.nav_buttons: dict[str, ft.Container] = {}
        self.view_host: ft.Container | None = None
        self.decor_canvas: cv.Canvas | None = None
        self.sweep_canvas: cv.Canvas | None = None
        self.instrument_canvas: cv.Canvas | None = None
        self.canvas_host: ft.Container | None = None
        self.sweep_band: ft.Container | None = None

    # ------------------------------------------------------------------
    # token / style helpers
    # ------------------------------------------------------------------
    def _make_hud(self) -> HUD:
        ui = self.controller.config.ui
        return hud_of(
            high_contrast=ui.high_contrast,
            reduce_motion=ui.reduce_motion,
            decorations=ui.hud_decorations,
            scanline_animation=ui.scanline_animation,
        )

    @property
    def palette(self):
        return self.hud.palette

    def _glow(self, color: str, blur: float = 8.0):
        return self.hud.glow_shadow(color, blur)

    def _text(
        self,
        value: str,
        *,
        color: str | None = None,
        size: int | None = None,
        mono: bool = False,
        weight=None,
        glow: float | None = None,
        selectable: bool = False,
        text_align=None,
        expand: bool | None = None,
    ) -> ft.Text:
        color = color or self.palette.text
        style = ft.TextStyle(
            color=color,
            size=size or self.hud.size_body,
            font_family=self.hud.font_mono if mono else (self.hud.font_ui or None),
            shadow=self._glow(color, glow) if glow else None,
        )
        return ft.Text(
            value,
            style=style,
            weight=weight,
            selectable=selectable,
            text_align=text_align,
            expand=expand,
        )

    def _button(
        self,
        label: str,
        on_click,
        *,
        accent: str | None = None,
        icon: str | None = None,
        primary: bool = False,
        tooltip: str | None = None,
    ) -> ft.Control:
        accent = accent or self.palette.cyan
        style = ft.ButtonStyle(
            color=self.palette.bg if primary else accent,
            bgcolor=accent if primary else "transparent",
            overlay_color=self.palette.panel_alt,
            side=ft.BorderSide(1, accent),
            shape=ft.RoundedRectangleBorder(radius=self.hud.radius),
            padding=ft.Padding.symmetric(horizontal=18, vertical=12),
            text_style=ft.TextStyle(
                size=self.hud.size_body,
                font_family=self.hud.font_mono,
                color=self.palette.bg if primary else accent,
            ),
        )
        return ft.Button(
            content=self._text(label, color=self.palette.bg if primary else accent, mono=True),
            icon=icon,
            icon_color=self.palette.bg if primary else accent,
            style=style,
            on_click=on_click,
            tooltip=tooltip,
            height=42,
        )

    def _panel(
        self,
        content: ft.Control,
        *,
        title: str | None = None,
        accent: str | None = None,
        padding: int | None = None,
        expand: bool | None = None,
        height: int | None = None,
    ) -> ft.Container:
        """A HUD card: opaque surface, hairline border, corner accent."""
        accent = accent or self.palette.cyan
        blocks: list[ft.Control] = []
        if title:
            blocks.append(
                ft.Row(
                    [
                        ft.Container(width=3, height=13, bgcolor=accent),
                        self._text(title, color=accent, size=self.hud.size_label,
                                   mono=True, weight=ft.FontWeight.W_600),
                    ],
                    spacing=8,
                )
            )
        blocks.append(content)
        return ft.Container(
            content=ft.Column(blocks, spacing=self.hud.gap, tight=True),
            bgcolor=self.palette.panel,
            border=ft.Border.all(1, self.palette.border),
            border_radius=self.hud.radius,
            padding=padding if padding is not None else self.hud.pad,
            expand=expand,
            height=height,
        )

    # ------------------------------------------------------------------
    # entry point
    # ------------------------------------------------------------------
    async def main(self, page: ft.Page) -> None:
        self.page = page
        cfg = self.controller.config

        page.title = "JLU DrCOM NG · 吉林大学校园网认证"
        page.bgcolor = self.palette.bg
        page.padding = 0
        page.spacing = 0
        page.theme_mode = ft.ThemeMode.DARK
        page.theme = ft.Theme(font_family=self.hud.font_ui or None)

        # -- window ------------------------------------------------------
        try:
            # Arm the close intercept first.  The native window appears as soon
            # as the client connects, well before this method finishes building
            # the shell, so installing it at the end left a window (seconds
            # long) where the X button would really close the app.
            page.window.prevent_close = True
            page.window.on_event = self._on_window_event
        except Exception:
            pass
        try:
            page.window.width = max(1000, int(cfg.ui.window_width))
            page.window.height = max(700, int(cfg.ui.window_height))
            page.window.min_width = 900
            page.window.min_height = 660
            page.window.bgcolor = self.palette.bg
            # Spec 3.4: keep window opacity at 1 — the HUD look comes from the
            # opaque base + translucent decoration layer, not from fading the
            # whole window (which would fade the text too).
            page.window.opacity = 1.0
        except Exception:
            pass

        page.add(self._build_shell())
        try:
            page.update()
        except Exception:
            pass

        self.controller.add_listener(self._on_controller_event)
        self.controller.log.add_listener(self._on_log_record)

        # Seed the log panel with whatever is already buffered.
        for record in self.controller.log.snapshot()[-200:]:
            self.log_queue.put(record)

        try:
            page.on_resize = lambda event: page.run_task(self._on_page_resize, event)
        except Exception:
            pass

        page.run_task(self._ui_tick)
        page.run_task(self._clock_tick)
        if self.hud.scanline_animation:
            page.run_task(self._sweep_tick)

        if self.enable_tray:
            self._setup_tray()

        if self.minimized and (cfg.ui.start_minimized or cfg.ui.minimize_to_tray):
            self._hide_window()
        elif cfg.ui.auto_login_on_launch:
            self.controller.start_background()
            account = cfg.active_account()
            if account is not None and account.auto_login and account.has_password():
                ok, message = self.controller.request_login()
                self._flash(message if not ok else "已按「启动时自动登录」开始认证", ok)
            else:
                self.controller.start_background()
        else:
            self.controller.start_background()

        try:
            # Re-assert the intercept: the window may have been hidden at
            # launch, and a client reconnect can drop window state.
            page.window.prevent_close = True
            page.window.on_event = self._on_window_event
        except Exception:
            pass

    # ------------------------------------------------------------------
    # shell
    # ------------------------------------------------------------------
    def _build_shell(self) -> ft.Control:
        return ft.Column(
            [
                self._build_header(),
                self._build_nav(),
                ft.Container(
                    content=self._build_view(VIEWS[0]),
                    expand=True,
                    padding=ft.Padding.only(left=self.hud.pad, right=self.hud.pad, bottom=self.hud.pad),
                ),
            ],
            spacing=0,
            expand=True,
        )

    def _build_header(self) -> ft.Control:
        self.state_pill_text = self._text("待机", color=self.palette.offline, size=self.hud.size_label, mono=True)
        self.state_pill = ft.Container(
            content=ft.Row(
                [
                    ft.Container(width=8, height=8, border_radius=4, bgcolor=self.palette.offline,
                                 key="pill_dot"),
                    self.state_pill_text,
                ],
                spacing=7,
                tight=True,
            ),
            bgcolor=self.palette.panel_alt,
            border=ft.Border.all(1, self.palette.border),
            border_radius=3,
            padding=ft.Padding.symmetric(horizontal=12, vertical=6),
        )

        self.clock_text = self._text("--:--:--", color=self.palette.text_dim, size=self.hud.size_body, mono=True)
        self.phase_bar = ft.ProgressBar(
            value=0.0,
            width=120,
            height=4,
            color=self.palette.green,
            bgcolor=self.palette.panel_alt,
        )

        return ft.Container(
            content=ft.Row(
                [
                    ft.Container(width=4, height=26, bgcolor=self.palette.green),
                    self._text(
                        "JLU · DRCOM NG",
                        color=self.palette.green,
                        size=self.hud.size_title + 3,
                        mono=True,
                        weight=ft.FontWeight.W_700,
                        glow=10,
                    ),
                    self._text("校园网认证", color=self.palette.text_muted, size=self.hud.size_label),
                    self.state_pill,
                    ft.Container(expand=True),
                    ft.Column(
                        [
                            self._text("保活周期", color=self.palette.text_muted, size=self.hud.size_micro, mono=True),
                            self.phase_bar,
                        ],
                        spacing=3,
                        tight=True,
                    ),
                    self.clock_text,
                ],
                spacing=14,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            bgcolor=self.palette.panel,
            padding=ft.Padding.symmetric(horizontal=self.hud.pad, vertical=12),
            border=ft.Border.only(bottom=ft.BorderSide(1, self.palette.border)),
        )

    def _build_nav(self) -> ft.Control:
        items = []
        for name in VIEWS:
            button = ft.Container(
                content=self._text(name, color=self.palette.text_muted, size=self.hud.size_body),
                padding=ft.Padding.symmetric(horizontal=16, vertical=9),
                border_radius=self.hud.radius,
                on_click=lambda e, n=name: self._switch_view(n),
                ink=True,
            )
            self.nav_buttons[name] = button
            items.append(button)
        self._paint_nav()
        return ft.Container(
            content=ft.Row(items, spacing=6),
            padding=ft.Padding.only(left=self.hud.pad, right=self.hud.pad, top=8, bottom=4),
        )

    def _paint_nav(self) -> None:
        for name, button in self.nav_buttons.items():
            active = name == self.active_view
            button.bgcolor = self.palette.cyan_wash if active else None
            button.border = ft.Border.only(bottom=ft.BorderSide(2, self.palette.cyan)) if active else None
            text = button.content
            if isinstance(text, ft.Text):
                text.style.color = self.palette.cyan if active else self.palette.text_muted
                text.style.shadow = self._glow(self.palette.cyan, 6) if (active and self.hud.glow) else None

    def _switch_view(self, name: str) -> None:
        if name == self.active_view or self.view_host is None:
            return
        self.active_view = name
        self._paint_nav()
        self.view_host.content = self._build_view(name)
        self._safe_update()

    def _build_view(self, name: str) -> ft.Control:
        builder = {
            "状态": self._view_dashboard,
            "日志": self._view_logs,
            "账号": self._view_account,
            "设置": self._view_settings,
            "关于": self._view_about,
        }[name]
        control = builder()
        if self.view_host is None:
            self.view_host = ft.Container(content=control, expand=True)
            return self.view_host
        return control

    # ------------------------------------------------------------------
    # view: 状态 (the HUD dashboard)
    # ------------------------------------------------------------------
    def _view_dashboard(self) -> ft.Control:
        # Choose the HUD height once.  Deriving it from a resize event would make
        # the container height depend on its own measurement.
        self._canvas_height = self._initial_canvas_height()
        self._canvas_size = (self._canvas_size[0], self._canvas_height)

        # --- HUD canvas stack ------------------------------------------
        self.decor_canvas = self._make_decor_canvas()
        self.instrument_canvas = cv.Canvas(shapes=[], width=self._canvas_size[0], height=self._canvas_size[1])
        # Positioned children (left/top set) do not contribute to the Stack's
        # size in Flutter, so the Stack fills its container and the canvases can
        # never push it wider than the available space.
        self.decor_canvas.left = 0
        self.decor_canvas.top = 0
        self.instrument_canvas.left = 0
        self.instrument_canvas.top = 0
        stack_controls: list[ft.Control] = [self.decor_canvas, self.instrument_canvas]
        # The sweeping band only exists when motion is allowed (spec 3.4 rule 5).
        self.sweep_band = None
        if self.hud.scanline_animation:
            self.sweep_band = ft.Container(
                width=self._canvas_size[0],
                height=2,
                bgcolor=self.palette.glow_soft,
                top=-10,
                left=0,
            )  # left/top set -> positioned, so it does not size the Stack
            stack_controls.append(self.sweep_band)

        self.canvas_host = ft.Container(
            content=ft.Stack(stack_controls, expand=True),
            bgcolor=self.palette.panel_sunk,
            border=ft.Border.all(1, self.palette.border),
            border_radius=self.hud.radius,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            expand=True,
            height=int(self._canvas_size[1]),
        )

        # --- hero readouts ---------------------------------------------
        self.hero_status = self._text("离线", color=self.palette.offline, size=self.hud.size_hero,
                                      mono=True, weight=ft.FontWeight.W_700, glow=14)
        self.hero_ip = self._text("—", color=self.palette.text, size=22, mono=True, selectable=True)
        self.hero_uptime = self._text("00:00:00", color=self.palette.cyan, size=22, mono=True)
        self.hero_account = self._text("(未设置)", color=self.palette.text_dim, size=16, mono=True)

        hero = ft.Container(
            content=ft.ResponsiveRow(
                [
                    ft.Container(self._readout_block("状态", self.hero_status), col={"sm": 6, "md": 3}),
                    ft.Container(self._readout_block("本机 IP", self.hero_ip), col={"sm": 6, "md": 3}),
                    ft.Container(self._readout_block("在线时长", self.hero_uptime), col={"sm": 6, "md": 3}),
                    ft.Container(self._readout_block("账号", self.hero_account), col={"sm": 6, "md": 3}),
                ],
                run_spacing=self.hud.gap,
                spacing=self.hud.gap,
            ),
            bgcolor=self.palette.panel,
            border=ft.Border.all(1, self.palette.border),
            border_radius=self.hud.radius,
            padding=self.hud.pad,
        )

        # --- message / advice banner -----------------------------------
        self.message_text = self._text("尚未开始认证。", color=self.palette.text_dim, size=self.hud.size_body)
        self.advice_text = self._text("", color=self.palette.amber, size=self.hud.size_body)
        self.alert_panel = ft.Container(
            content=ft.Column(
                [self.message_text, self.advice_text],
                spacing=6,
                tight=True,
            ),
            bgcolor=self.palette.panel_alt,
            border=ft.Border.only(left=ft.BorderSide(3, self.palette.cyan)),
            padding=ft.Padding.symmetric(horizontal=14, vertical=10),
            border_radius=self.hud.radius,
            visible=False,
        )

        # --- actions ----------------------------------------------------
        actions = ft.Row(
            [
                self._button("登录", self._on_login, primary=True, accent=self.palette.green, icon=ft.Icons.POWER_SETTINGS_NEW),
                self._button("注销", self._on_logout, accent=self.palette.amber, icon=ft.Icons.LOGOUT),
                self._button("重新连接", self._on_reconnect, accent=self.palette.cyan, icon=ft.Icons.REFRESH),
                self._button("端口诊断", self._on_diagnose, accent=self.palette.violet, icon=ft.Icons.MEDICAL_SERVICES),
                self._button("导出日志", self._on_export_logs, accent=self.palette.text_dim, icon=ft.Icons.SAVE_ALT),
            ],
            spacing=10,
            wrap=True,
        )

        # --- stats grid --------------------------------------------------
        stats = ft.ResponsiveRow(
            [
                ft.Container(self._stat_cell("today", "今日在线"), col={"sm": 4, "md": 2}),
                ft.Container(self._stat_cell("week", "本周在线"), col={"sm": 4, "md": 2}),
                ft.Container(self._stat_cell("drops", "掉线次数"), col={"sm": 4, "md": 2}),
                ft.Container(self._stat_cell("rtt", "延迟"), col={"sm": 4, "md": 2}),
                ft.Container(self._stat_cell("loss", "丢包"), col={"sm": 4, "md": 2}),
                ft.Container(self._stat_cell("traffic", "本次流量"), col={"sm": 4, "md": 2}),
            ],
            run_spacing=self.hud.gap,
            spacing=self.hud.gap,
        )

        return ft.Column(
            [
                self.canvas_host,
                hero,
                self.alert_panel,
                actions,
                stats,
            ],
            spacing=self.hud.gap,
            expand=True,
        )

    def _initial_canvas_height(self) -> float:
        """A one-time HUD height based on the window we asked for."""
        try:
            window_height = float(getattr(self.page.window, "height", 0) or 0)
        except (TypeError, ValueError):
            window_height = 0.0
        if window_height <= 0:
            window_height = 780.0
        return min(
            self.HUD_MAX_HEIGHT,
            max(self.HUD_MIN_HEIGHT, window_height * self.HUD_HEIGHT_RATIO),
        )

    def _make_decor_canvas(self) -> cv.Canvas:
        """Static decoration layer, drawn once per resize."""
        width, height = self._canvas_size
        painter = HudPainter(self.hud, width=width, height=height)
        painter.shapes = []
        painter.grid()
        painter.scanlines(phase=0.0)
        painter.frame_marks()
        return cv.Canvas(shapes=painter.shapes, width=width, height=height)

    def _readout_block(self, label: str, value: ft.Text) -> ft.Control:
        return ft.Column(
            [
                self._text(label, color=self.palette.text_muted, size=self.hud.size_label, mono=True),
                value,
            ],
            spacing=3,
            tight=True,
        )

    def _stat_cell(self, key: str, label: str) -> ft.Control:
        value = self._text("—", color=self.palette.text, size=15, mono=True)
        self.stat_cells[key] = value
        return ft.Column(
            [
                self._text(label, color=self.palette.text_muted, size=self.hud.size_micro, mono=True),
                value,
            ],
            spacing=2,
            tight=True,
        )

    # ------------------------------------------------------------------
    # view: 日志
    # ------------------------------------------------------------------
    def _view_logs(self) -> ft.Control:
        self.log_list = ft.ListView(
            controls=list(self._log_rows[-400:]),
            spacing=1,
            padding=8,
            auto_scroll=True,
            expand=True,
        )
        self.log_file_text = self._text(
            f"日志文件：{self.controller.log.file_path}",
            color=self.palette.text_muted,
            size=self.hud.size_micro,
            mono=True,
            selectable=True,
        )

        def toggle_pause(_event) -> None:
            self._log_paused = not self._log_paused
            self._flash("日志已暂停刷新" if self._log_paused else "日志已恢复刷新", True)

        def toggle_hex(_event) -> None:
            self.controller.log.protocol_hex = not self.controller.log.protocol_hex
            self.controller.config.logging.protocol_hex = self.controller.log.protocol_hex
            self.controller.store.save()
            self._flash(f"协议十六进制转储：{'开' if self.controller.log.protocol_hex else '关'}", True)

        controls = ft.Row(
            [
                self._button("暂停/继续", toggle_pause, accent=self.palette.amber),
                self._button("协议转储开关", toggle_hex, accent=self.palette.cyan),
                self._button("导出日志", self._on_export_logs, accent=self.palette.green),
                self._button("打开日志目录", self._on_open_log_dir, accent=self.palette.violet),
            ],
            spacing=10,
            wrap=True,
        )

        note = self._text(
            "账号在写入日志前已脱敏（形如 20****26）；密码从不写入日志。",
            color=self.palette.text_muted,
            size=self.hud.size_micro,
        )

        return ft.Column(
            [
                controls,
                ft.Container(
                    content=self.log_list,
                    bgcolor=self.palette.panel_sunk,
                    border=ft.Border.all(1, self.palette.border),
                    border_radius=self.hud.radius,
                    expand=True,
                ),
                self.log_file_text,
                note,
            ],
            spacing=self.hud.gap,
            expand=True,
        )

    def _log_row(self, record: LogRecordView) -> ft.Control:
        color = {
            "ERROR": self.palette.danger,
            "CRITICAL": self.palette.danger,
            "WARNING": self.palette.amber,
            "PACKET": self.palette.text_muted,
            "INFO": self.palette.text_dim,
        }.get(record.level, self.palette.text_dim)
        if record.is_packet:
            color = self.palette.violet
        return ft.Row(
            [
                self._text(record.clock, color=self.palette.text_muted, size=self.hud.size_micro, mono=True),
                self._text(f"{record.level:<7}", color=color, size=self.hud.size_micro, mono=True),
                self._text(record.message, color=color, size=self.hud.size_micro, mono=True,
                           selectable=True, expand=True),
            ],
            spacing=8,
            tight=True,
        )

    # ------------------------------------------------------------------
    # view: 账号
    # ------------------------------------------------------------------
    def _view_account(self) -> ft.Control:
        from ..config import Account

        controller = self.controller
        account = controller.config.active_account()

        account_field = self._field("学号 / 账号", account.account if account else "",
                                    hint="例如 2023xxxxxxxx")
        mac_field = self._field("网卡 MAC", account.mac if account else "",
                                hint="AA:BB:CC:DD:EE:FF")
        label_field = self._field("备注名（可选）", account.label if account else "", hint="例如 宿舍以太网")
        password_field = self._field("密码", "", hint="留空表示不修改", password=True)

        auto_switch = ft.Switch(
            label="开机后自动登录此账号",
            value=bool(account.auto_login) if account else False,
            active_color=self.palette.green,
        )

        def detect_mac(_event) -> None:
            from ..netiface import pick_relevant_interface

            # pick_relevant_interface is keyword-only on purpose; passing these
            # positionally raised TypeError on every click of this button.
            iface = pick_relevant_interface(
                server=controller.config.auth.server, port=controller.config.auth.port
            )
            if iface and iface.mac:
                mac_field.value = iface.mac
                mac_field.update()
                self._flash(f"已填入 {iface.label} 的 MAC：{iface.mac}（本地地址可能是虚拟网卡，请核对）", True)
            else:
                self._flash("没有自动识别到网卡，请手动填写", False)

        def save(_event) -> None:
            target = controller.config.active_account()
            if target is None:
                target = Account()
                controller.add_account(target, make_active=True)
                controller.config.active_account_id = target.id
            target.account = (account_field.value or "").strip()
            target.mac = (mac_field.value or "").strip().upper()
            target.label = (label_field.value or "").strip()
            target.auto_login = bool(auto_switch.value)

            if not target.account:
                self._flash("账号不能为空", False)
                return
            try:
                from ..protocol import mac_to_bytes

                if target.mac:
                    mac_to_bytes(target.mac)
            except ValueError as exc:
                self._flash(str(exc), False)
                return

            notes = []
            if password_field.value:
                _ok, message = controller.save_password(password_field.value)
                notes.append(message)
                password_field.value = ""
            controller.store.save()
            notes.append("配置已保存")
            self._flash("；".join(notes), True)
            self._refresh_credentials_note()

        def test_bind(_event) -> None:
            self._on_diagnose(None)

        self.credential_note = self._text("", color=self.palette.text_muted, size=self.hud.size_micro)

        body = ft.Column(
            [
                self._panel(
                    ft.Column(
                        [
                            self._text(
                                "凭据保存在本机配置中，密码使用 Windows DPAPI 加密"
                                "（绑定当前 Windows 账号），配置文件里看不到明文。",
                                color=self.palette.text_dim,
                                size=self.hud.size_body,
                            ),
                            account_field,
                            mac_field,
                            label_field,
                            password_field,
                            auto_switch,
                            self.credential_note,
                            ft.Row(
                                [
                                    self._button("保存", save, primary=True, accent=self.palette.green,
                                                 icon=ft.Icons.SAVE),
                                    self._button("自动识别 MAC", detect_mac, accent=self.palette.cyan,
                                                 icon=ft.Icons.SEARCH),
                                    self._button("测试端口占用", test_bind, accent=self.palette.violet),
                                ],
                                spacing=10,
                                wrap=True,
                            ),
                        ],
                        spacing=self.hud.gap,
                    ),
                    title="账号与网卡",
                ),
                self._accounts_panel(),
            ],
            spacing=self.hud.gap,
            scroll=ft.ScrollMode.AUTO,
            expand=True,
        )
        self._refresh_credentials_note()
        return body

    def _accounts_panel(self) -> ft.Control:
        controller = self.controller
        rows: list[ft.Control] = []
        for account in controller.config.accounts:
            active = account.id == controller.config.active_account_id
            rows.append(
                ft.Container(
                    content=ft.Row(
                        [
                            ft.Container(
                                width=8, height=8, border_radius=4,
                                bgcolor=self.palette.green if active else self.palette.border,
                            ),
                            self._text(account.display_name, color=self.palette.text, size=self.hud.size_body),
                            self._text(account.mac or "(无 MAC)", color=self.palette.text_muted,
                                       size=self.hud.size_micro, mono=True),
                            self._text(("已存密码" if account.has_password() else "无密码"),
                                       color=self.palette.text_muted, size=self.hud.size_micro),
                            ft.Container(expand=True),
                            self._button("切换", lambda e, a=account: self._activate_account(a),
                                         accent=self.palette.cyan),
                            self._button("删除", lambda e, a=account: self._delete_account(a),
                                         accent=self.palette.danger),
                        ],
                        spacing=12,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    bgcolor=self.palette.panel_alt if active else None,
                    border=ft.Border.all(1, self.palette.border),
                    border_radius=self.hud.radius,
                    padding=10,
                )
            )

        def add(_event) -> None:
            from ..config import Account

            new = Account(label=f"账号 {len(controller.config.accounts) + 1}")
            controller.add_account(new, make_active=True)
            controller.store.save()
            self._switch_view("账号")
            self._flash("已新增账号，请填写信息", True)

        return self._panel(
            ft.Column(
                rows or [self._text("暂无账号。", color=self.palette.text_muted)],
                spacing=8,
            ),
            title=f"账号列表（{len(controller.config.accounts)}）",
        )

    def _field(self, label: str, value: str, *, hint: str = "", password: bool = False) -> ft.TextField:
        return ft.TextField(
            label=label,
            value=value,
            hint_text=hint,
            password=password,
            can_reveal_password=password,
            dense=True,
            bgcolor=self.palette.panel_sunk,
            border_color=self.palette.border,
            focused_border_color=self.palette.cyan,
            color=self.palette.text,
            cursor_color=self.palette.cyan,
            label_style=ft.TextStyle(color=self.palette.text_muted, size=self.hud.size_label),
            text_style=ft.TextStyle(color=self.palette.text, size=self.hud.size_body, font_family=self.hud.font_mono),
            border_radius=self.hud.radius,
        )

    def _refresh_credentials_note(self) -> None:
        note = getattr(self, "credential_note", None)
        if note is None:
            return
        account = self.controller.config.active_account()
        if account is None:
            note.value = ""
            return
        backend = self.controller.store.password_backend_note(account.id)
        masked = self.controller.masked_credentials()
        note.value = (
            f"当前账号 {masked['account']} · {masked['mac']} · "
            f"密码存储：{backend or ('未设置' if not account.has_password() else 'DPAPI')}"
        )

    def _activate_account(self, account) -> None:
        self.controller.config.active_account_id = account.id
        self.controller.store.save()
        self._switch_view("账号")
        self._flash(f"已切换到 {account.display_name}", True)

    def _delete_account(self, account) -> None:
        if len(self.controller.config.accounts) <= 1:
            self._flash("至少保留一个账号", False)
            return
        self.controller.config.accounts = [
            a for a in self.controller.config.accounts if a.id != account.id
        ]
        if self.controller.config.active_account_id == account.id:
            self.controller.config.active_account_id = self.controller.config.accounts[0].id
        self.controller.store.clear_password_cache()
        self.controller.store.save()
        self._switch_view("账号")
        self._flash(f"已删除 {account.display_name}", True)

    # ------------------------------------------------------------------
    # view: 设置
    # ------------------------------------------------------------------
    def _view_settings(self) -> ft.Control:
        cfg = self.controller.config
        controller = self.controller

        def make_switch(label: str, value: bool, *, on_change=None, disabled: bool = False) -> ft.Switch:
            control = ft.Switch(
                label=label,
                value=value,
                active_color=self.palette.green,
                on_change=on_change,
                disabled=disabled,
            )
            return control

        def persist(_event=None) -> None:
            controller.store.save()

        # --- reconnect ---------------------------------------------------
        reconnect_enabled = make_switch("掉线后自动重连", cfg.reconnect.enabled, on_change=lambda e: (
            setattr(cfg.reconnect, "enabled", e.control.value), persist()))
        min_delay = self._field("最小重试间隔（秒）", str(cfg.reconnect.min_delay))
        max_delay = self._field("最大重试间隔（秒）", str(cfg.reconnect.max_delay))
        quiet_enabled = make_switch("启用夜间静默（重连间隔放宽）", cfg.reconnect.quiet_hours_enabled,
                                    on_change=lambda e: (setattr(cfg.reconnect, "quiet_hours_enabled", e.control.value), persist()))
        quiet_start = self._field("静默开始（HH:MM）", cfg.reconnect.quiet_hours_start)
        quiet_end = self._field("静默结束（HH:MM）", cfg.reconnect.quiet_hours_end)

        def save_reconnect(_event) -> None:
            try:
                cfg.reconnect.min_delay = max(1.0, float(min_delay.value or 5))
                cfg.reconnect.max_delay = max(cfg.reconnect.min_delay, float(max_delay.value or 300))
            except ValueError:
                self._flash("重试间隔必须是数字", False)
                return
            cfg.reconnect.quiet_hours_start = quiet_start.value or "01:00"
            cfg.reconnect.quiet_hours_end = quiet_end.value or "06:00"
            persist()
            self._flash("重连策略已保存", True)

        # --- startup ------------------------------------------------------
        autostart = make_switch(
            "开机自动启动" + ("" if tray_available() else "（需要 pystray 才能最小化到托盘）"),
            controller.autostart_enabled(),
            on_change=lambda e: self._on_autostart(e.control.value),
        )
        close_action_note = self._text(
            "", color=self.palette.text_muted, size=self.hud.size_micro
        )
        close_action = ft.SegmentedButton(
            segments=[
                ft.Segment(value="ask", label=ft.Text("每次询问")),
                ft.Segment(value="tray", label=ft.Text("最小化到托盘")),
                ft.Segment(value="quit", label=ft.Text("直接退出")),
            ],
            # NB: a *list*, not a set.  Flet 1.0 annotates `selected` as
            # list[str]; handing it a set makes msgpack raise
            # "TypeError: can not serialize 'set' object" when the patch is
            # sent, and because _safe_update swallows that, the whole view
            # silently failed to render -- the settings tab looked dead.
            selected=[cfg.ui.close_action if cfg.ui.close_action in CLOSE_ACTIONS else "ask"],
            allow_empty_selection=False,
            allow_multiple_selection=False,
            # Keep the tick: with three short labels the coloured underline
            # alone is easy to miss, and this is a setting the user has to be
            # able to read at a glance.
            show_selected_icon=True,
            selected_icon=ft.Icon(ft.Icons.CHECK, color=self.palette.green, size=16),
            style=ft.ButtonStyle(
                color=self.palette.text,                 # 17.7:1 on panel_sunk
                bgcolor=self.palette.panel_sunk,
                side=ft.BorderSide(1, self.palette.border),
            ),
            on_change=lambda e: self._on_close_action_change(e),
        )
        # Stored so the chooser can refresh it after "记住我的选择".
        self.close_action_control = close_action
        self.close_action_note = close_action_note
        self._sync_close_action_control()
        auto_login_launch = make_switch("启动后自动登录", cfg.ui.auto_login_on_launch,
                                        on_change=lambda e: (setattr(cfg.ui, "auto_login_on_launch", e.control.value), persist()))

        # --- HUD / accessibility -------------------------------------------
        reduce_motion = make_switch("减少动态效果（关闭扫描线扫过动画）", cfg.ui.reduce_motion,
                                    on_change=lambda e: self._set_ui_flag("reduce_motion", e.control.value))
        high_contrast = make_switch("高对比度模式（关闭发光、加亮文字）", cfg.ui.high_contrast,
                                    on_change=lambda e: self._set_ui_flag("high_contrast", e.control.value))
        decorations = make_switch("显示 HUD 装饰层（网格 / 扫描线）", cfg.ui.hud_decorations,
                                  on_change=lambda e: self._set_ui_flag("hud_decorations", e.control.value))
        scanline = make_switch("扫描线扫过动画（默认关，开了会更耗电）", cfg.ui.scanline_animation,
                               on_change=lambda e: self._set_ui_flag("scanline_animation", e.control.value),
                               disabled=cfg.ui.reduce_motion)

        contrast_note = self._text(
            "所有正文颜色与背景的对比度均已实测 ≥ 4.5:1（WCAG AA）。"
            "本程序不降低窗口整体不透明度——HUD 质感来自不透明深色底 + 半透明装饰层，"
            "这样文字不会被桌面内容冲淡。",
            color=self.palette.text_muted,
            size=self.hud.size_micro,
        )

        # --- notifications -------------------------------------------------
        notify_desktop = make_switch("桌面通知", cfg.notify.desktop,
                                     on_change=lambda e: (setattr(cfg.notify, "desktop", e.control.value), persist()))
        webhook = self._field("Webhook URL（可选）", cfg.notify.webhook, hint="POST JSON 到该地址")
        hook_command = self._field("外部命令（可选）", cfg.notify.command,
                                   hint='例如 notify.bat "{event}"')

        def save_notify(_event) -> None:
            cfg.notify.webhook = (webhook.value or "").strip()
            cfg.notify.command = (hook_command.value or "").strip()
            persist()
            self._flash("通知设置已保存", True)

        # --- local API -------------------------------------------------------
        api_enabled = make_switch("启用本地状态接口（HTTP）", cfg.api.enabled,
                                  on_change=lambda e: self._toggle_api(e.control.value))
        api_port = self._field("接口端口", str(cfg.api.port))
        status_file = make_switch("同时写状态文件", cfg.api.status_file,
                                  on_change=lambda e: (setattr(cfg.api, "status_file", e.control.value),
                                                       setattr(controller.status_writer, "enabled", e.control.value),
                                                       persist()))
        api_note = self._text(
            chr(10).join([
                "GET /status /health /metrics /stats /logs /diag 开放读取。",
                "POST /login /logout /reconnect /probe 需要 X-DrCOM-Token 头"
                "（否则你浏览的网页可以跨站调用本机接口）。",
                f"令牌写在 {controller.data_dir / 'api-token.txt'}，「关于」页也会显示。",
                f"状态文件：{controller.status_writer.path}",
            ]),
            color=self.palette.text_muted,
            size=self.hud.size_micro,
            selectable=True,
        )

        def save_api(_event) -> None:
            try:
                cfg.api.port = int(api_port.value or 8848)
            except ValueError:
                self._flash("端口必须是数字", False)
                return
            persist()
            self._flash("接口设置已保存（启用状态需重启生效或使用上方开关）", True)

        # --- probe / traffic --------------------------------------------------
        probe_enabled = make_switch("启用网络质量探测（延迟 / 丢包）", cfg.probe.enabled,
                                    on_change=lambda e: (setattr(cfg.probe, "enabled", e.control.value), persist()))
        probe_targets = self._field("探测目标（逗号分隔）", ", ".join(cfg.probe.targets))
        traffic_enabled = make_switch("启用流量统计", cfg.traffic.enabled,
                                      on_change=lambda e: (setattr(cfg.traffic, "enabled", e.control.value), persist()))

        def save_probe(_event) -> None:
            targets = [t.strip() for t in (probe_targets.value or "").split(",") if t.strip()]
            cfg.probe.targets = targets or ["10.100.61.3"]
            persist()
            self._flash("探测设置已保存", True)

        # --- advanced protocol ---------------------------------------------
        alt_port = make_switch(
            "允许备选源端口自愈（61440 被占用时改用邻近端口）",
            cfg.auth.allow_alternate_port,
            on_change=lambda e: (setattr(cfg.auth, "allow_alternate_port", e.control.value), persist()),
        )
        server_field = self._field("认证服务器", cfg.auth.server)
        port_field = self._field("认证端口", str(cfg.auth.port))
        timeout_field = self._field("收包超时（毫秒）", str(cfg.auth.timeout_ms))
        keepalive_field = self._field("保活间隔（秒）", str(int(cfg.auth.keepalive_interval)))

        def save_protocol(_event) -> None:
            cfg.auth.server = (server_field.value or "").strip() or "10.100.61.3"
            try:
                cfg.auth.port = int(port_field.value or 61440)
                cfg.auth.bind_port = cfg.auth.port
                cfg.auth.timeout_ms = int(timeout_field.value or 3000)
                cfg.auth.keepalive_interval = max(5.0, float(keepalive_field.value or 20))
            except ValueError:
                self._flash("端口/超时必须为数字", False)
                return
            persist()
            self._flash("协议参数已保存，重连后生效", True)

        return ft.Column(
            [
                self._panel(ft.Column([reconnect_enabled, ft.Row([min_delay, max_delay], spacing=10),
                                       quiet_enabled, ft.Row([quiet_start, quiet_end], spacing=10),
                                       self._button("保存重连策略", save_reconnect, accent=self.palette.cyan)],
                                      spacing=self.hud.gap), title="重连与节流"),
                self._panel(
                    ft.Column(
                        [
                            autostart,
                            auto_login_launch,
                            self._text("点击窗口右上角关闭按钮时：", color=self.palette.text_dim,
                                       size=self.hud.size_body),
                            close_action,
                            close_action_note,
                        ],
                        spacing=6,
                    ),
                    title="启动与窗口",
                ),
                self._panel(ft.Column([reduce_motion, high_contrast, decorations, scanline, contrast_note],
                                      spacing=4), title="界面与无障碍"),
                self._panel(ft.Column([notify_desktop, webhook, hook_command,
                                       self._button("保存通知设置", save_notify, accent=self.palette.cyan)],
                                      spacing=self.hud.gap), title="通知"),
                self._panel(ft.Column([api_enabled, api_port, status_file, api_note,
                                       self._button("保存接口设置", save_api, accent=self.palette.cyan)],
                                      spacing=self.hud.gap), title="本地状态接口"),
                self._panel(ft.Column([probe_enabled, probe_targets, traffic_enabled,
                                       self._button("保存探测设置", save_probe, accent=self.palette.cyan)],
                                      spacing=self.hud.gap), title="探测与流量"),
                self._panel(ft.Column([server_field, ft.Row([port_field, timeout_field, keepalive_field], spacing=10),
                                       alt_port,
                                       self._button("保存协议参数", save_protocol, accent=self.palette.amber)],
                                      spacing=self.hud.gap), title="协议参数（高级）"),
            ],
            spacing=self.hud.gap,
            scroll=ft.ScrollMode.AUTO,
            expand=True,
        )

    # ------------------------------------------------------------------
    # view: 关于
    # ------------------------------------------------------------------
    def _view_about(self) -> ft.Control:
        controller = self.controller
        from .. import __version__

        def open_dir(_event) -> None:
            from ..single_instance import open_in_file_manager

            open_in_file_manager(controller.data_dir)

        lines = [
            ("版本", __version__),
            ("认证服务器", f"{controller.config.auth.server}:{controller.config.auth.port}"),
            ("本地绑定端口", str(controller.config.auth.bind_port)),
            ("数据目录", str(controller.data_dir)),
            ("日志文件", str(controller.log.file_path or "")),
            ("状态文件", str(controller.status_writer.path)),
        ]
        if controller.config.api.enabled:
            lines.append(
                ("控制令牌", controller.api.control_token if controller.api.is_running
                 else "（接口未运行）")
            )
        rows = [
            ft.Row(
                [
                    self._text(f"{key}", color=self.palette.text_muted, size=self.hud.size_label, mono=True, expand=2),
                    self._text(str(value), color=self.palette.text, size=self.hud.size_body, mono=True,
                               selectable=True, expand=5),
                ],
                spacing=10,
            )
            for key, value in lines
        ]

        notes = [
            "协议实现按「挑战 → 登录 → keepalive1 → keepalive2」四段报文循环工作，"
            "报文构造已与可用实现的真实抓包逐字节比对一致。",
            "注销说明：本变体的 Dr.COM 没有客户端注销报文，「注销」等于停止保活并关闭套接字，"
            "服务端在超时后释放会话。",
            "密码使用 Windows DPAPI 加密后以 Base64 存储；DPAPI 不可用时改用 Fernet，"
            "再不可用则降级为可逆混淆并在界面明确告警。",
            "账号写入日志前会脱敏，密码任何时候都不写日志。",
        ]

        return ft.Column(
            [
                self._panel(ft.Column(rows, spacing=8), title="运行环境"),
                self._panel(
                    ft.Column([self._text(n, color=self.palette.text_dim, size=self.hud.size_body, selectable=True)
                               for n in notes], spacing=8),
                    title="实现说明",
                ),
                ft.Row(
                    [
                        self._button("打开数据目录", open_dir, accent=self.palette.cyan, icon=ft.Icons.FOLDER_OPEN),
                        self._button("导出日志", self._on_export_logs, accent=self.palette.green),
                        self._button("端口诊断", self._on_diagnose, accent=self.palette.violet),
                    ],
                    spacing=10,
                    wrap=True,
                ),
            ],
            spacing=self.hud.gap,
            scroll=ft.ScrollMode.AUTO,
            expand=True,
        )

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------
    def _on_login(self, _event) -> None:
        ok, message = self.controller.request_login()
        self._flash(message, ok)

    def _on_logout(self, _event) -> None:
        ok, message = self.controller.request_logout()
        self._flash(message, ok)

    def _on_reconnect(self, _event) -> None:
        ok, message = self.controller.request_reconnect()
        self._flash(message, ok)

    def _on_diagnose(self, _event) -> None:
        data = self.controller.diagnostics()
        port = data["bind_port"]
        if data["bind_free"]:
            self._show_dialog("端口诊断", f"端口 {port} 当前可用，可以直接登录。")
            return
        from ..binding import diagnose_bind_error

        diagnosis = diagnose_bind_error(port, data["bind_error_code"])
        self._show_dialog(diagnosis.headline, diagnosis.to_text())

    def _on_export_logs(self, _event) -> None:
        path = self.controller.export_logs()
        self._flash(f"日志已导出到 {path}", True)

    def _on_open_log_dir(self, _event) -> None:
        from ..single_instance import open_in_file_manager

        target = self.controller.log.file_path or self.controller.data_dir
        open_in_file_manager(Path(target))

    def _on_autostart(self, enabled: bool) -> None:
        ok, message = self.controller.set_autostart(enabled)
        self._flash(message, ok)

    def _toggle_api(self, enabled: bool) -> None:
        controller = self.controller
        controller.config.api.enabled = enabled
        controller.store.save()
        if enabled:
            ok, message = controller.api.start()
            self._flash(message, ok)
        else:
            controller.api.stop()
            self._flash("本地状态接口已停止", True)

    def _close_action_from_event(self, event) -> str:
        """Read the chosen value out of a SegmentedButton event.

        ``selected`` is a list in Flet 1.0, and the value may also arrive on the
        event itself, so accept every shape rather than trusting one.
        """
        control = getattr(event, "control", None)
        selected = getattr(control, "selected", None)
        if selected is None:
            value = getattr(event, "data", None) or getattr(event, "value", None)
            if isinstance(value, str):
                return value
            selected = value
        if isinstance(selected, str):
            return selected
        if selected:
            return next(iter(selected))
        return ""

    def _on_close_action_change(self, event) -> None:
        action = self._close_action_from_event(event)
        if action not in CLOSE_ACTIONS:
            return
        self.controller.config.ui.close_action = action
        self.controller.store.save()
        self._sync_close_action_control()
        self._flash(f"关闭窗口时：{CLOSE_ACTION_LABELS[action]}", True)

    def _sync_close_action_control(self) -> None:
        """Keep the settings selector and its hint in step with the config."""
        control = getattr(self, "close_action_control", None)
        if control is None:
            return
        action = self.controller.config.ui.close_action
        try:
            control.selected = [action]
        except Exception:
            pass
        note = getattr(self, "close_action_note", None)
        if note is not None:
            if action == "ask":
                note.value = "每次点关闭都会询问，并可选「记住我的选择」。"
            elif action == "tray":
                note.value = (
                    "关闭即隐藏到托盘，认证继续在后台运行；"
                    "要真正退出请用托盘菜单的「退出」，或在「设置」里改回。"
                )
            else:
                note.value = "关闭即退出程序，认证会中断。"
            note.color = self.palette.amber if action == "quit" else self.palette.text_muted
        self._safe_update()

    def _set_ui_flag(self, name: str, value: bool) -> None:
        setattr(self.controller.config.ui, name, value)
        self.controller.store.save()
        self.hud = self._make_hud()
        if self.view_host is not None:
            self.view_host.content = self._build_view(self.active_view)
        self._safe_update()
        self._flash("界面设置已应用", True)

    # ------------------------------------------------------------------
    # dialogs / flash
    # ------------------------------------------------------------------
    def _show_dialog(self, title: str, body: str, *, selectable: bool = True) -> None:
        if self.page is None:
            return
        dialog = ft.AlertDialog(
            modal=False,
            title=self._text(title, color=self.palette.cyan, size=self.hud.size_title, mono=True),
            content=ft.Container(
                content=ft.Column(
                    [
                        self._text(line, color=self.palette.text_dim, size=self.hud.size_body, selectable=selectable)
                        for line in body.splitlines() or [""]
                    ],
                    spacing=4,
                    scroll=ft.ScrollMode.AUTO,
                ),
                width=620,
                height=min(460, 40 + 22 * len(body.splitlines())),
            ),
            bgcolor=self.palette.panel,
            actions=[self._button("知道了", lambda e: self._close_dialog(), accent=self.palette.cyan)],
        )
        self.page.show_dialog(dialog)

    def _close_dialog(self) -> None:
        if self.page is not None:
            self.page.pop_dialog()

    def _flash(self, message: str, ok: bool = True) -> None:
        """Show a message in the dashboard banner and log it."""
        (self.controller.log.info if ok else self.controller.log.warning)("UI: %s", message)
        if self.message_text is None:
            return
        self.message_text.value = message
        self.message_text.style.color = self.palette.text_dim if ok else self.palette.amber
        self.message_text.style.shadow = self._glow(self.message_text.style.color, 6) if self.hud.glow else None
        if self.alert_panel is not None:
            self.alert_panel.visible = True
        self._safe_update()

    def _set_alert(self, message: str, advice: str = "", *, color: str | None = None) -> None:
        if self.message_text is None:
            return
        color = color or self.palette.text_dim
        self.message_text.value = message
        self.message_text.style.color = color
        self.advice_text.value = advice
        self.advice_text.style.color = self.palette.amber
        if self.alert_panel is not None:
            self.alert_panel.visible = True
            self.alert_panel.border = ft.Border.only(left=ft.BorderSide(3, color))

    # ------------------------------------------------------------------
    # tray / window
    # ------------------------------------------------------------------
    def _setup_tray(self) -> None:
        if not tray_available():
            self.controller.log.warning(
                "未安装 pystray，无法创建系统托盘图标；「关闭时隐藏」将改为最小化，"
                "以免窗口无法找回。安装：pip install pystray"
            )
            return
        self._tray = TrayIcon(
            tooltip="JLU DrCOM NG · 待机",
            on_show=lambda: self.events.put(ControllerEvent(kind="tray_show")),
            on_toggle=lambda: self.events.put(ControllerEvent(kind="tray_toggle")),
            on_reconnect=lambda: self.events.put(ControllerEvent(kind="tray_reconnect")),
            on_quit=lambda: self.events.put(ControllerEvent(kind="tray_quit")),
        )
        if not self._tray.start():
            self._tray = None

    def _hide_window(self) -> None:
        """Hide to the tray, or minimise if there is no tray to come back from."""
        if self.page is None:
            return
        if self._tray is not None:
            try:
                self.page.window.visible = False
                self.page.window.skip_task_bar = True
                self._safe_update()
                return
            except Exception:
                pass
        self._minimize()

    def _minimize(self) -> None:
        if self.page is None:
            return
        try:
            self.page.window.minimized = True
            self._safe_update()
        except Exception:
            pass

    def _show_window(self) -> None:
        if self.page is None:
            return
        try:
            self.page.window.skip_task_bar = False
            self.page.window.visible = True
            self.page.window.minimized = False
            self.page.window.to_front()
            self._safe_update()
        except Exception:
            pass

    def _on_window_event(self, event) -> None:
        """Decide what the X button does.

        Note the mechanism: Flet delivers the event, but the window only stays
        open because ``page.window.prevent_close`` is set once at startup.
        Flet 1.0's ``WindowEvent`` is a plain object with no ``prevent_default``
        field, so setting that attribute on the event (as this used to) is
        silently ignored -- the window closed and the app exited no matter what
        the setting said.
        """
        try:
            close_event = getattr(ft.WindowEventType, "CLOSE", None)
            if close_event is not None and event.type != close_event:
                return

            action = self.controller.config.ui.close_action
            if action == "tray":
                self._hide_window()
            elif action == "quit":
                self._quit()
            else:
                self._ask_close_action()
        except Exception as exc:  # pragma: no cover - never kill the app here
            self.controller.log.warning(f"处理关闭事件失败：{exc!r}")

    def _ask_close_action(self) -> None:
        """The first-time (and not-yet-remembered) chooser."""
        if self.page is None:
            return

        remember = ft.Checkbox(
            label="记住我的选择（之后不再询问，可在「设置」里改）",
            value=False,
            check_color=self.palette.panel,
            active_color=self.palette.green,
            label_style=ft.TextStyle(color=self.palette.text_dim, size=self.hud.size_body),
        )

        tray_ok = self._tray is not None
        hint = (
            "隐藏到托盘后，认证继续在后台运行，双击托盘图标可以再打开。"
            if tray_ok
            else "未安装 pystray，无法隐藏到托盘，将改为最小化到任务栏。安装：pip install pystray"
        )

        def choose(action: str) -> None:
            if remember.value:
                cfg = self.controller.config
                cfg.ui.close_action = action
                self.controller.store.save()
                self._sync_close_action_control()
            self._close_dialog()
            if action == "tray":
                self._hide_window()
                self._flash("已隐藏到托盘，认证继续在后台运行", True)
            else:
                self._quit()

        dialog = ft.AlertDialog(
            modal=True,
            title=self._text("关闭窗口", color=self.palette.cyan, size=self.hud.size_title, mono=True),
            content=ft.Container(
                content=ft.Column(
                    [
                        self._text(
                            "要让程序退到后台继续认证，还是彻底退出？",
                            color=self.palette.text, size=self.hud.size_body,
                        ),
                        self._text(hint, color=self.palette.text_muted, size=self.hud.size_micro),
                        remember,
                    ],
                    spacing=10,
                    tight=True,
                ),
                width=460,
            ),
            bgcolor=self.palette.panel,
            actions=[
                self._button("最小化到托盘", lambda e: choose("tray"), primary=True,
                             accent=self.palette.green, icon=ft.Icons.MINIMIZE),
                self._button("退出程序", lambda e: choose("quit"), accent=self.palette.amber,
                             icon=ft.Icons.POWER_SETTINGS_NEW),
                self._button("取消", lambda e: self._close_dialog(), accent=self.palette.cyan),
            ],
        )
        self.page.show_dialog(dialog)

    # ------------------------------------------------------------------
    # async loops
    # ------------------------------------------------------------------
    async def _ui_tick(self) -> None:
        """Drain controller events and refresh the dashboard."""
        ticks = 0
        while True:
            await asyncio.sleep(0.5)
            try:
                self._drain_events()
                self._drain_logs()
                ticks += 1
                if self.active_view == "状态" and ticks % 2 == 0:
                    self._refresh_dashboard()
                self._safe_update()
            except Exception as exc:  # pragma: no cover - never kill the loop
                self.controller.log.warning(f"界面刷新异常：{exc!r}")
                await asyncio.sleep(1.0)

    async def _clock_tick(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            if self.clock_text is None or self.page is None:
                continue
            try:
                self.clock_text.value = time.strftime("%H:%M:%S")
                self._update_phase()
            except Exception:
                pass

    async def _sweep_tick(self) -> None:
        """Animate the scanline band (opt-in; see UiConfig.scanline_animation).

        Small steps at a steady interval: 7 px every 80 ms looked like it was
        stuttering across the panel.
        """
        offset = 0
        while True:
            await asyncio.sleep(0.05)
            if self.sweep_band is None or self.page is None:
                continue
            offset = (offset + 3) % (int(self._canvas_size[1]) + 60)
            try:
                self.sweep_band.top = offset - 30
                self.sweep_band.update()
            except Exception:
                pass

    def _drain_events(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            self._handle_event(event)

    def _on_controller_event(self, event: ControllerEvent) -> None:
        # Called from worker threads — only enqueue.
        self.events.put(event)

    def _on_log_record(self, record: LogRecordView) -> None:
        self.log_queue.put(record)

    def _handle_event(self, event: ControllerEvent) -> None:
        if event.kind == "engine" and event.engine_event is not None:
            inner = event.engine_event
            if inner.kind == "online":
                self._set_alert(f"已上线 · {inner.ip}", "保活循环运行中。")
                self._notify_tray("已上线", f"IP {inner.ip}")
            elif inner.kind == "login_failed":
                self._set_alert(f"认证失败：{inner.message}", inner.advice, color=self.palette.danger)
                self._notify_tray("认证失败", inner.message)
            elif inner.kind == "fatal":
                self._set_alert(f"需要处理：{inner.message}", inner.advice, color=self.palette.danger)
                self._notify_tray("需要处理", inner.message)
            elif inner.kind == "bind_failed":
                self._set_alert(inner.message, inner.detail, color=self.palette.danger)
                self._notify_tray("端口冲突", inner.message)
            elif inner.kind in ("challenge_failed", "keepalive_failed", "gave_up"):
                self._set_alert(inner.message, inner.detail, color=self.palette.amber)
            elif inner.kind == "state" and inner.message:
                if inner.state not in (EngineState.ONLINE,):
                    self.message_text and self._set_alert(inner.message)
            elif inner.kind == "stopped":
                self._set_alert("已停止认证。", "")
        elif event.kind == "tray_show":
            self._show_window()
        elif event.kind == "tray_toggle":
            if self.controller.online:
                self._on_logout(None)
            else:
                self._on_login(None)
        elif event.kind == "tray_reconnect":
            self._on_reconnect(None)
        elif event.kind == "tray_quit":
            self._quit()

    def _notify_tray(self, title: str, message: str) -> None:
        if self._tray is not None:
            self._tray.notify(message, title)

    def _drain_logs(self) -> None:
        if self._log_paused:
            # Still count them so the UI can say how many were skipped.
            while True:
                try:
                    self.log_queue.get_nowait()
                    self._pending_log_count += 1
                except queue.Empty:
                    break
            return

        appended = 0
        while appended < 60:
            try:
                record = self.log_queue.get_nowait()
            except queue.Empty:
                break
            row = self._log_row(record)
            self._log_rows.append(row)
            if self.log_list is not None and self.active_view == "日志":
                self.log_list.controls.append(row)
            appended += 1

        if self.log_list is not None and appended and self.active_view == "日志":
            del self._log_rows[: max(0, len(self._log_rows) - 800)]
            controls = self.log_list.controls
            if len(controls) > 800:
                del controls[: len(controls) - 800]

    # ------------------------------------------------------------------
    # dashboard refresh
    # ------------------------------------------------------------------
    def _smooth(self, key: str, value: float | None, *, alpha: float | None = None) -> float | None:
        """Exponential moving average, so instruments do not twitch."""
        if value is None:
            return None
        alpha = self._ema_alpha if alpha is None else alpha
        previous = self._ema.get(key)
        smoothed = value if previous is None else previous + alpha * (value - previous)
        self._ema[key] = smoothed
        return smoothed

    def _build_telemetry(self) -> HudTelemetry:
        controller = self.controller
        engine = controller.engine
        state = engine.state if engine else EngineState.IDLE
        snapshot = controller.traffic.snapshot if controller.config.traffic.enabled else None

        quality = {}
        for item in controller.probe.history.all_summaries():
            quality = item
            break

        stats = controller.stats
        today = stats.today()

        interval = max(1.0, controller.config.auth.keepalive_interval)
        phase = 0.0
        if engine and engine.is_online:
            phase = (engine.uptime_seconds() % interval) / interval

        return HudTelemetry(
            online=bool(engine and engine.is_online),
            state_label=STATE_LABELS.get(state, state.value),
            state_color=self.palette.state_color(state.value),
            ip=(engine.ip if engine else "") or "",
            account=controller.account.masked_account if controller.account else "",
            uptime=controller.uptime,
            rx_rate=self._smooth("rx_rate", snapshot.rx_rate if snapshot else 0.0) or 0.0,
            tx_rate=self._smooth("tx_rate", snapshot.tx_rate if snapshot else 0.0) or 0.0,
            session_rx=snapshot.total_rx if snapshot else 0,
            session_tx=snapshot.total_tx if snapshot else 0,
            rtt_ms=self._smooth("rtt", quality.get("rtt_ms")),
            loss_percent=self._smooth("loss", quality.get("loss_percent"), alpha=0.4),
            jitter_ms=self._smooth("jitter", quality.get("jitter_ms")),
            keepalive_phase=phase,
            drops=today.drops,
            attempts=engine.consecutive_failures if engine else 0,
        )

    def _refresh_dashboard(self) -> None:
        controller = self.controller
        telemetry = self._build_telemetry()
        self._telemetry = telemetry

        # --- HUD canvas -------------------------------------------------
        # Only rebuild when something the HUD actually draws has moved.  The
        # scanline band is animated separately, so an idle HUD costs nothing.
        if self.instrument_canvas is not None:
            signature = (
                telemetry.online,
                telemetry.state_label,
                f"{telemetry.rx_rate:.0f}",
                f"{telemetry.uptime:.0f}",
                f"{telemetry.rtt_ms:.0f}" if telemetry.rtt_ms is not None else None,
                f"{telemetry.loss_percent:.0f}" if telemetry.loss_percent is not None else None,
                f"{telemetry.jitter_ms:.0f}" if telemetry.jitter_ms is not None else None,
                round(telemetry.keepalive_phase, 2),
                telemetry.session_rx // 4096,
                telemetry.session_tx // 4096,
                telemetry.drops,
                self._canvas_size,
            )
            if signature != self._hud_signature:
                self._hud_signature = signature
                painter = HudPainter(
                    self.hud, width=self._canvas_size[0], height=self._canvas_size[1]
                )
                self.instrument_canvas.shapes = painter.render(telemetry, phase=time.time() % 60)
                self.instrument_canvas.width = self._canvas_size[0]
                self.instrument_canvas.height = self._canvas_size[1]

        # --- hero readouts ----------------------------------------------
        if self.hero_status is not None:
            self.hero_status.value = telemetry.state_label
            self.hero_status.style.color = telemetry.state_color
            self.hero_status.style.shadow = self._glow(telemetry.state_color, 14) if self.hud.glow else None
        if self.hero_ip is not None:
            self.hero_ip.value = telemetry.ip or "—"
        if self.hero_uptime is not None:
            self.hero_uptime.value = _format_uptime(telemetry.uptime)
        if self.hero_account is not None:
            masked = controller.masked_credentials()
            self.hero_account.value = masked["account"]
            self.hero_account.tooltip = f"MAC {masked['mac']}"

        # --- state pill ---------------------------------------------------
        if self.state_pill_text is not None and self.state_pill is not None:
            self.state_pill_text.value = telemetry.state_label
            self.state_pill_text.style.color = telemetry.state_color
            self.state_pill_text.style.shadow = self._glow(telemetry.state_color, 6) if self.hud.glow else None
            self.state_pill.border = ft.Border.all(1, telemetry.state_color)
            row = self.state_pill.content
            if isinstance(row, ft.Row) and row.controls:
                row.controls[0].bgcolor = telemetry.state_color

        # --- stats --------------------------------------------------------
        stats = controller.stats
        today_seconds = stats.today().online_seconds + stats.session_seconds
        values = {
            "today": _format_uptime(today_seconds),
            "week": _format_uptime(stats.week_seconds()),
            "drops": str(stats.today().drops),
            "rtt": f"{telemetry.rtt_ms:.0f} ms" if telemetry.rtt_ms is not None else "—",
            "loss": f"{telemetry.loss_percent:.0f}%" if telemetry.loss_percent is not None else "—",
            "traffic": f"↓{format_bytes(telemetry.session_rx)} ↑{format_bytes(telemetry.session_tx)}",
        }
        for key, text in self.stat_cells.items():
            if key in values:
                text.value = values[key]

        self._update_phase()
        self._update_tooltip(telemetry)

    def _update_phase(self) -> None:
        if self.phase_bar is None:
            return
        engine = self.controller.engine
        if engine and engine.is_online:
            interval = max(1.0, self.controller.config.auth.keepalive_interval)
            self.phase_bar.value = (engine.uptime_seconds() % interval) / interval
        else:
            self.phase_bar.value = 0.0

    def _update_tooltip(self, telemetry: HudTelemetry) -> None:
        if self._tray is None:
            return
        if telemetry.online:
            text = f"JLU DrCOM NG · 在线 {telemetry.ip} · {_format_uptime(telemetry.uptime)}"
            color = self.palette.online
        else:
            text = f"JLU DrCOM NG · {telemetry.state_label}"
            color = self.palette.offline
        if text != self._last_tooltip:
            self._last_tooltip = text
            self._tray.update(tooltip=text, color=color)

    #: Ignore size changes smaller than this.  Sub-pixel and 1-2 px wobble is
    #: what turned a resize into an endless measure/relayout loop.
    RESIZE_HYSTERESIS = 10.0
    #: Vertical share of the window the HUD panel gets.
    HUD_HEIGHT_RATIO = 0.52
    HUD_MIN_HEIGHT = 320.0
    HUD_MAX_HEIGHT = 560.0

    def _desired_canvas_size(self) -> tuple[float, float]:
        """Canvas size: width from the real client area, height fixed.

        Only the width is dynamic.  Keeping the height constant means the
        container's height never depends on a measurement, which is what makes
        this impossible to turn into a measure/relayout loop.
        """
        page_width, _page_height = self._page_size
        if page_width <= 0:
            page_width = 1100.0  # first frame, before on_resize has fired
        # Page padding on both sides plus a little slack for the scrollbar.
        width = max(560.0, page_width - 2 * self.hud.pad - 34.0)
        return round(width), round(self._canvas_height)

    def _apply_canvas_size(self, *, force: bool = False) -> bool:
        """Resize the HUD canvases if the desired size moved enough."""
        width, height = self._desired_canvas_size()
        current_width, current_height = self._canvas_size
        if not force and (
            abs(width - current_width) < self.RESIZE_HYSTERESIS
            and abs(height - current_height) < self.RESIZE_HYSTERESIS
        ):
            return False

        self._canvas_size = (width, height)
        if self.decor_canvas is not None:
            self.decor_canvas.shapes = self._make_decor_shapes(width, height)
            self.decor_canvas.width = width
            self.decor_canvas.height = height
        if self.instrument_canvas is not None:
            self.instrument_canvas.width = width
            self.instrument_canvas.height = height
        if self.sweep_band is not None:
            self.sweep_band.width = width
        if self.canvas_host is not None:
            self.canvas_host.height = int(height)
        self.controller.log.debug(
            "HUD 画布尺寸调整为 %dx%d（页面 %s）", int(width), int(height), self._page_size
        )
        self._hud_signature = None  # force the next refresh to repaint
        return True

    async def _on_page_resize(self, event=None) -> None:
        """Window resize handler: record the real client size, then re-lay out."""
        try:
            width = float(getattr(event, "width", 0) or 0)
            height = float(getattr(event, "height", 0) or 0)
            if width > 0 and height > 0:
                self._page_size = (width, height)
            if self._apply_canvas_size():
                self._refresh_dashboard()
                self._safe_update()
        except Exception as exc:  # pragma: no cover - never break the UI loop
            self.controller.log.debug(f"处理窗口尺寸变化失败：{exc!r}")

    def _make_decor_shapes(self, width: float, height: float) -> list:
        painter = HudPainter(self.hud, width=width, height=height)
        painter.shapes = []
        painter.grid()
        painter.scanlines(phase=0.0)
        painter.frame_marks()
        return painter.shapes

    # ------------------------------------------------------------------
    # misc
    # ------------------------------------------------------------------
    def _safe_update(self) -> None:
        if self.page is None:
            return
        try:
            self.page.update()
        except Exception:
            pass

    def _quit(self) -> None:
        if self._tray is not None:
            self._tray.stop()
            self._tray = None
        try:
            if self.page is not None:
                self.page.window.visible = True
                self.page.window.skip_task_bar = False
                # Release the intercept installed at startup, so nothing can
                # turn this exit into another close request.
                self.page.window.prevent_close = False
        except Exception:
            pass
        self.controller.shutdown()

        # Never let a wedged client keep the process (and port 61440) alive.
        # The event lets a completed shutdown stand the watchdog down, so it
        # cannot fire later against a process that already exited cleanly.
        self._exit_now.clear()
        threading.Thread(
            target=self._force_exit_after,
            args=(self.quit_watchdog_seconds,),
            daemon=True,
        ).start()
        try:
            self.page.run_task(self._finish_quit)
        except Exception:
            self._finish_quit_now()

    def _finish_quit_now(self) -> None:
        self._exit_now.set()
        self._hard_exit()

    async def _finish_quit(self) -> None:
        """Ask the client to close, then exit.

        ``Window.destroy()`` is a coroutine in Flet 1.0.  The old code called it
        without awaiting, so the coroutine was created and thrown away: the
        client was never told to close, ``os._exit`` killed Python immediately
        after, and the native window was left on screen with no backend.  The
        window looked alive, so every click on it (a tab, a button) silently did
        nothing -- which is how "点设置没反应" got reported.
        """
        try:
            if self.page is not None:
                await self.page.window.destroy()
                self.page.update()
                # Give the client a moment to process the message before the
                # socket is torn down underneath it.
                await asyncio.sleep(0.2)
        except Exception:
            pass
        finally:
            self._finish_quit_now()

    def _force_exit_after(self, delay: float) -> None:
        """Watchdog: exit if the graceful path has not finished within *delay*."""
        if delay and not self._exit_now.wait(delay):
            self._hard_exit()

    @staticmethod
    def _hard_exit() -> None:
        """End the process for real.

        A separate method so tests can intercept it without patching ``os``
        globally -- which would also disarm the watchdog thread that exists
        precisely to guarantee this happens.
        """
        import os

        os._exit(0)


def _format_uptime(seconds: float) -> str:
    seconds = int(max(0, seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def run_gui(
    controller: AppController,
    *,
    minimized: bool = False,
    web: bool = False,
    port: int = 0,
    enable_tray: bool = True,
) -> None:
    """Start the Flet app (desktop window, or a local web view with ``web``)."""
    app = DrcomApp(controller, minimized=minimized, enable_tray=enable_tray)

    def target(page: ft.Page) -> None:
        page.run_task(app.main, page)

    if web:
        # Serve the same UI over HTTP on localhost.  Handy for remote/headless
        # machines, and it is how the UI can be inspected without a desktop.
        ft.run(
            target,
            view=ft.AppView.WEB_BROWSER,
            host="127.0.0.1",
            port=port or 8550,
            assets_dir=None,
        )
    else:
        ft.run(target, assets_dir=None)
