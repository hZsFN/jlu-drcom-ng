"""UI construction smoke tests.

Flet needs a live session to *render*, but almost all of the risk in this app is
in the build path: constructing controls, binding handlers, reacting to engine
events.  Those are exercised here against a stub page, which catches API misuse
(wrong constructor arguments, missing attributes) without a display.

The HUD painter is checked for the element inventory the brief asks for, and for
the accessibility switch (reduce-motion must remove the sweep).
"""

from __future__ import annotations

import pytest

flet = pytest.importorskip("flet")


# --------------------------------------------------------------------------
# stub page
# --------------------------------------------------------------------------
class FakeWindow:
    def __init__(self) -> None:
        self.width = 1080
        self.height = 720
        self.min_width = 860
        self.min_height = 600
        self.opacity = 1.0
        self.bgcolor = None
        self.visible = True
        self.minimized = False
        self.skip_task_bar = False

    def destroy(self) -> None:
        pass

    def to_front(self) -> None:
        pass


class FakePage:
    """Just enough of ``ft.Page`` for the app to build against."""

    def __init__(self) -> None:
        self.controls: list = []
        self.window = FakeWindow()
        self.bgcolor = None
        self.padding = None
        self.spacing = None
        self.theme_mode = None
        self.theme = None
        self.title = ""
        self.tasks: list = []
        self.dialogs: list = []
        self.updates = 0

    def add(self, *controls) -> None:
        self.controls.extend(controls)

    def update(self) -> None:
        self.updates += 1

    def run_task(self, handler, *args) -> None:
        self.tasks.append((handler, args))

    def show_dialog(self, dialog) -> None:
        self.dialogs.append(dialog)

    def pop_dialog(self) -> None:
        if self.dialogs:
            self.dialogs.pop()


@pytest.fixture
def app(controller):
    from drcom.ui.app import DrcomApp

    instance = DrcomApp(controller, enable_tray=False)
    instance.page = FakePage()
    return instance


# --------------------------------------------------------------------------
# construction
# --------------------------------------------------------------------------
def test_shell_builds(app) -> None:
    shell = app._build_shell()
    assert shell is not None
    assert app.nav_buttons, "navigation was not built"
    assert app.hero_status is not None
    assert app.instrument_canvas is not None


@pytest.mark.parametrize("view", ["状态", "日志", "账号", "设置", "关于"])
def test_every_view_builds(app, view: str) -> None:
    app.view_host = None
    app.active_view = view
    control = app._build_view(view)
    assert control is not None


def test_view_switching(app) -> None:
    app._build_shell()
    for view in ("日志", "账号", "设置", "关于", "状态"):
        app._switch_view(view)
        assert app.active_view == view
    assert app.page.updates > 0


def test_dashboard_has_login_and_logout_controls(app) -> None:
    """Acceptance criterion 8: the actions must exist."""
    texts: list[str] = []

    def walk(control) -> None:
        if control is None:
            return
        value = getattr(control, "value", None)
        if isinstance(value, str):
            texts.append(value)
        content = getattr(control, "content", None)
        if content is not None:
            walk(content)
        for attr in ("controls", "actions"):
            children = getattr(control, attr, None)
            if isinstance(children, list):
                for child in children:
                    walk(child)

    walk(app._build_shell())
    joined = " ".join(texts)
    for expected in ("登录", "注销", "重新连接", "端口诊断", "导出日志"):
        assert expected in joined, f"dashboard is missing the {expected!r} control"
    for label in ("状态", "本机 IP", "在线时长", "账号"):
        assert label in joined, f"dashboard is missing the {label!r} readout"


# --------------------------------------------------------------------------
# HUD painter
# --------------------------------------------------------------------------
def test_hud_painter_produces_the_expected_elements() -> None:
    from flet import canvas as cv

    from drcom.ui.hud import HudPainter, HudTelemetry
    from drcom.ui.theme import hud_of

    painter = HudPainter(hud_of(), width=900, height=380)
    shapes = painter.render(
        HudTelemetry(
            online=True, state_label="在线", ip="172.18.1.2", account="20****01",
            uptime=3600, rx_rate=1_500_000, tx_rate=200_000,
            session_rx=10**8, session_tx=10**7, rtt_ms=18, loss_percent=0,
            jitter_ms=2.5, keepalive_phase=0.5, drops=2,
        )
    )
    kinds = {type(shape).__name__ for shape in shapes}
    assert kinds == {"Line", "Circle", "Arc", "Rect", "Text"}
    assert len(shapes) > 120, "the HUD looks suspiciously empty"
    # Dashed strokes are how the dive half of the pitch ladder is drawn.
    dashed = [
        shape for shape in shapes
        if isinstance(shape, cv.Line) and getattr(shape.paint, "stroke_dash_pattern", None)
    ]
    assert dashed, "pitch ladder has no dashed (dive) rungs"


def test_hud_labels_are_always_opaque() -> None:
    """Rule: decoration may be translucent, glyphs may not."""
    from drcom.ui.hud import HudPainter, HudTelemetry
    from drcom.ui.theme import hud_of

    painter = HudPainter(hud_of(), width=900, height=380)
    shapes = painter.render(HudTelemetry(online=True, uptime=60, rx_rate=1000))
    from flet import canvas as cv

    for shape in shapes:
        if isinstance(shape, cv.Text):
            color = shape.style.color
            assert isinstance(color, str) and len(color) == 7, f"text colour {color!r} is translucent"


def test_hud_handles_offline_state() -> None:
    from drcom.ui.hud import HudPainter, HudTelemetry
    from drcom.ui.theme import hud_of

    painter = HudPainter(hud_of(), width=700, height=300)
    shapes = painter.render(HudTelemetry(online=False, state_label="离线"))
    assert shapes


def test_hud_survives_a_tiny_canvas() -> None:
    from drcom.ui.hud import HudPainter, HudTelemetry
    from drcom.ui.theme import hud_of

    painter = HudPainter(hud_of(), width=10, height=10)
    assert painter.render(HudTelemetry()) is not None


def test_pitch_ladder_dashes_appear_only_below_the_horizon() -> None:
    """Climb solid, dive dashed — the classic HUD convention."""
    from flet import canvas as cv

    from drcom.ui.hud import HudPainter, HudTelemetry
    from drcom.ui.theme import hud_of

    painter = HudPainter(hud_of(), width=900, height=400)
    shapes = painter.render(HudTelemetry(online=True))
    centre_y = 200
    lines = [s for s in shapes if isinstance(s, cv.Line)]
    dashed = [s for s in lines if getattr(s.paint, "stroke_dash_pattern", None)]
    assert dashed
    # Every dashed rung sits below the vertical centre (dive half).
    assert all(s.y1 > centre_y for s in dashed), "a climb rung was drawn dashed"


# --------------------------------------------------------------------------
# behaviour
# --------------------------------------------------------------------------
def test_reduce_motion_removes_the_sweep_band(controller) -> None:
    controller.config.ui.scanline_animation = True
    controller.config.ui.reduce_motion = True
    from drcom.ui.app import DrcomApp

    app = DrcomApp(controller, enable_tray=False)
    app.page = FakePage()
    assert not app.hud.scanline_animation
    app._build_shell()
    assert app.sweep_band is None


def test_engine_events_update_the_dashboard(app) -> None:
    from drcom.controller import ControllerEvent
    from drcom.engine import EngineEvent, EngineState

    app._build_shell()
    app.engine_test_events = True
    app.controller.engine = None

    app._handle_event(
        ControllerEvent(
            kind="engine",
            engine_event=EngineEvent(
                kind="login_failed",
                state=EngineState.FATAL,
                message="密码错误",
                advice="请检查校园网密码",
                error_code=3,
            ),
        )
    )
    assert app.message_text.value.startswith("认证失败")
    assert "密码错误" in app.message_text.value
    assert "密码" in app.advice_text.value


def test_flash_updates_the_banner(app) -> None:
    app._build_shell()
    app._flash("测试消息", True)
    assert app.message_text.value == "测试消息"
    app._flash("出错了", False)
    assert app.message_text.value == "出错了"
    assert app.message_text.style.color == app.palette.amber


def test_diagnostics_dialog_renders(app) -> None:
    app._build_shell()
    app._show_dialog("标题", "第一行\n第二行")
    assert app.page.dialogs


def test_telemetry_reflects_engine_state(app) -> None:
    from drcom.engine import EngineState

    app.controller.engine = None
    telemetry = app._build_telemetry()
    assert telemetry.online is False
    assert telemetry.state_label == "待机"

    class FakeEngine:
        state = EngineState.ONLINE
        ip = "192.0.2.10"
        is_online = True
        is_running = True
        online_since = None
        consecutive_failures = 0
        reason = None

        def uptime_seconds(self) -> float:
            return 123.0

        def stop(self, **kwargs) -> None:
            pass

        def join(self, timeout=None) -> bool:
            return True

    app.controller.engine = FakeEngine()
    app.controller.account = app.controller.config.active_account()
    telemetry = app._build_telemetry()
    assert telemetry.online is True
    assert telemetry.ip == "192.0.2.10"
    assert telemetry.uptime == 123.0


def test_log_rows_are_rendered_and_masked(app) -> None:
    from drcom.logbus import LogRecordView
    import time

    app._build_shell()
    record = LogRecordView(
        seq=1, timestamp=time.time(), level="INFO", message="账号 20****01 已上线"
    )
    row = app._log_row(record)
    assert row is not None
    app.log_queue.put(record)
    app.active_view = "日志"
    app._view_logs()
    app._drain_logs()
    assert app.log_list is not None
    assert len(app.log_list.controls) >= 1


def test_settings_view_exposes_accessibility_switches(app) -> None:
    app.view_host = None
    control = app._view_settings()
    labels: list[str] = []

    def walk(item) -> None:
        if item is None:
            return
        label = getattr(item, "label", None)
        if isinstance(label, str):
            labels.append(label)
        content = getattr(item, "content", None)
        if content is not None:
            walk(content)
        children = getattr(item, "controls", None)
        if isinstance(children, list):
            for child in children:
                walk(child)

    walk(control)
    joined = " ".join(labels)
    assert "减少动态效果" in joined
    assert "高对比度模式" in joined
    assert "掉线后自动重连" in joined


# --------------------------------------------------------------------------
# regressions from the first live look at the UI
# --------------------------------------------------------------------------
def test_speed_tape_follows_the_traffic_snapshot(app) -> None:
    """The left tape must actually move with the rate.

    It looked dead because ``build_status_payload`` called ``traffic.sample()``
    itself: sample() derives the rate from the delta since the previous call, so
    the HTTP thread kept consuming the ticker's window and the rendered rate
    alternated between the real value and zero.
    """
    from drcom.traffic import TrafficSnapshot

    app.controller.traffic._snapshot = TrafficSnapshot(
        interface="test0", rx_rate=512_000.0, tx_rate=8_000.0, timestamp=1.0
    )
    for _ in range(6):
        app._ema.pop("rx_rate", None)
        telemetry = app._build_telemetry()
        if abs(telemetry.rx_rate - 512_000.0) < 1024:
            break
    assert telemetry.rx_rate > 100_000, f"tape is not following the rate: {telemetry.rx_rate}"
    assert "512" in _hud_rate_text(telemetry.rx_rate) or "500" in _hud_rate_text(telemetry.rx_rate)


def _hud_rate_text(rate: float) -> str:
    from drcom.ui.hud import _rate_label

    return _rate_label(rate)


def test_status_payload_does_not_resample_traffic(controller, monkeypatch) -> None:
    """The status payload reads the cache; only the ticker samples."""
    from drcom import statusapi

    calls = {"n": 0}
    original = controller.traffic.sample

    def counting_sample():
        calls["n"] += 1
        return original()

    monkeypatch.setattr(controller.traffic, "sample", counting_sample)
    statusapi.build_status_payload(controller)
    assert calls["n"] == 0, "the HTTP path must not sample traffic again"


def test_telemetry_values_are_smoothed(app) -> None:
    """A single spiky sample must not slam the instruments."""
    from drcom.traffic import TrafficSnapshot

    for _ in range(10):
        app.controller.traffic._snapshot = TrafficSnapshot(rx_rate=1000.0, timestamp=1.0)
        app._build_telemetry()
    baseline = app._build_telemetry().rx_rate

    app.controller.traffic._snapshot = TrafficSnapshot(rx_rate=1_000_000.0, timestamp=2.0)
    after_spike = app._build_telemetry().rx_rate

    assert baseline == pytest.approx(1000.0, rel=0.2)
    assert after_spike < 400_000, "a single spike jumped most of the way to the new value"
    assert after_spike > baseline


def test_canvas_size_comes_from_the_reported_client_area(app) -> None:
    """Width tracks the *real* client area reported by ``page.on_resize``.

    Not ``page.window.width``: that echoes back the value we assigned, and
    trusting it drew the canvas wider than its container so the right-hand third
    of the HUD was clipped away.
    """
    app._page_size = (1100.0, 750.0)
    width, height = app._desired_canvas_size()
    assert 560 <= width < 1100
    assert app.HUD_MIN_HEIGHT <= height <= app.HUD_MAX_HEIGHT

    app._page_size = (1600.0, 900.0)
    wider, same_height = app._desired_canvas_size()
    assert wider > width
    # Height is decided once, so a resize must not change it.
    assert same_height == height


def test_canvas_never_exceeds_the_client_area(app) -> None:
    """Regression: the canvas must fit inside the page it is drawn in."""
    for page_width in (900.0, 1100.0, 1440.0, 1920.0):
        app._page_size = (page_width, 800.0)
        width, _ = app._desired_canvas_size()
        assert width <= page_width, f"canvas {width} wider than page {page_width}"


def test_small_resizes_are_ignored(app) -> None:
    """Sub-hysteresis wobble must not trigger a relayout.

    Feeding a measurement back into the layout is what made the HUD twitch:
    measure -> write -> relayout -> measure again.
    """
    app._page_size = (1100.0, 750.0)
    assert app._apply_canvas_size(force=True) is True
    settled = app._canvas_size

    app._page_size = (1100.0 + app.RESIZE_HYSTERESIS - 4, 750.0)
    assert app._apply_canvas_size() is False
    assert app._canvas_size == settled

    app._page_size = (1100.0 + app.RESIZE_HYSTERESIS + 40, 750.0)
    assert app._apply_canvas_size() is True
    assert app._canvas_size != settled


def test_hud_repaint_is_skipped_when_nothing_changed(app) -> None:
    """An idle HUD must not rebuild ~280 shapes every tick."""
    from drcom.traffic import TrafficSnapshot

    app.controller.traffic._snapshot = TrafficSnapshot(rx_rate=5000.0, timestamp=1.0)
    app.controller.probe.history.results.clear()
    app._build_shell()

    app._refresh_dashboard()
    first = app._hud_signature
    app._refresh_dashboard()
    assert app._hud_signature == first

    app.controller.traffic._snapshot = TrafficSnapshot(rx_rate=900_000.0, timestamp=2.0)
    app._build_telemetry()
    for _ in range(8):
        app._refresh_dashboard()
    assert app._hud_signature != first, "a large rate change must repaint the HUD"


def test_hud_height_leaves_room_for_the_panels_below(app) -> None:
    """The HUD must not squeeze the hero readouts and buttons out of view."""
    app.page.window.width = 1100
    app.page.window.height = 780
    height = app._initial_canvas_height()
    assert app.HUD_MIN_HEIGHT <= height <= app.HUD_MAX_HEIGHT
    assert height <= app.page.window.height * 0.6
