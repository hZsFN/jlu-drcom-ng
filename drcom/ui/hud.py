"""HUD instrument painters for Flet's canvas API.

Geometry follows the Typhoon / Gripen HUD layout used by the reference demo:
a central flight-path vector, a pitch ladder (climb solid, dive dashed), speed
and altitude tapes with value bugs, a heading tape, a bank scale with a pointer,
and corner readouts.

Every instrument is bound to real telemetry (see :class:`HudTelemetry`), so this
is an instrument panel rather than wallpaper.

Design notes that matter for the two problems this file was rewritten to fix
---------------------------------------------------------------------------

**Legibility.**  The first version packed in too many small labels at 10 px with
dense ticks, and the grid competed with the glyphs.  Now: a smaller tick count,
larger type, labels only where they carry information, a dimmer/sparser
decoration layer, and a solid scrim behind every value box so text never sits
directly on grid lines (spec 3.4: "put body text on a dark backing").

**Stability.**  Nothing here reads the wall clock, and the caller is expected to
feed *smoothed* telemetry.  The scale of each tape is chosen with hysteresis so
it does not flip between two tick steps, which was one of the causes of the
"twitching" the first version showed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from flet import canvas as cv

from .theme import HUD

# Flet 1.0 keeps the paint primitives in ``flet.controls.painting`` while the
# shapes live in ``flet.canvas``.  Import defensively so a reshuffle degrades to
# a plain colour instead of taking the whole UI down.
try:  # pragma: no cover - import shim
    from flet.controls.painting import Paint as _Paint
    from flet.controls.painting import PaintingStyle as _PaintingStyle
except Exception:  # pragma: no cover
    try:
        from flet import Paint as _Paint  # type: ignore
        from flet import PaintingStyle as _PaintingStyle  # type: ignore
    except Exception:
        _Paint = None
        _PaintingStyle = None


def _paint(color: str, *, width: float = 1.0, dash: list[float] | None = None, stroke: bool = False):
    kwargs: dict = {"color": color, "stroke_width": width}
    if dash:
        kwargs["stroke_dash_pattern"] = dash
    if stroke and _PaintingStyle is not None:
        kwargs["style"] = _PaintingStyle.STROKE
    if _Paint is None:  # pragma: no cover - very old/odd Flet
        return color
    try:
        return _Paint(**kwargs)
    except TypeError:  # pragma: no cover - tolerate a different Flet build
        kwargs.pop("style", None)
        kwargs.pop("stroke_dash_pattern", None)
        return _Paint(**kwargs)


def _stroke(color: str, *, width: float = 1.0, dash: list[float] | None = None):
    return _paint(color, width=width, dash=dash, stroke=True)


def _text_style(hud: HUD, color: str, size: int, *, bold: bool = False):
    from flet import FontWeight, TextStyle

    shadow = None
    if hud.glow:
        from flet import BoxShadow

        shadow = BoxShadow(blur_radius=4, color=color, offset=(0, 0))
    return TextStyle(
        color=color,
        size=size,
        font_family=hud.font_mono,
        weight=FontWeight.W_600 if bold else None,
        shadow=shadow,
    )


# --------------------------------------------------------------------------
# Telemetry
# --------------------------------------------------------------------------
@dataclass
class HudTelemetry:
    """Everything the HUD draws.

    Feed *smoothed* values: the caller is responsible for filtering, because a
    raw RTT or rate reading jitters by its very nature and a HUD that twitches is
    worse than one that lags slightly.
    """

    online: bool = False
    state_label: str = "离线"
    state_color: str = "#7C8FA3"
    ip: str = ""
    account: str = ""
    uptime: float = 0.0
    rx_rate: float = 0.0
    tx_rate: float = 0.0
    session_rx: int = 0
    session_tx: int = 0
    rtt_ms: float | None = None
    loss_percent: float | None = None
    jitter_ms: float | None = None
    keepalive_phase: float = 0.0  # 0..1 through the keepalive cycle
    drops: int = 0
    attempts: int = 0  # consecutive reconnect failures, for the status line
    detail: str = ""
    extra: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _nice_step(span: float, target_divisions: int = 5) -> float:
    """A 1/2/5x10^n tick step giving roughly *target_divisions* ticks."""
    if span <= 0:
        return 1.0
    raw = span / max(1, target_divisions)
    magnitude = 10 ** math.floor(math.log10(raw))
    for multiplier in (1, 2, 5, 10):
        if raw <= magnitude * multiplier:
            return magnitude * multiplier
    return magnitude * 10


class _TapeScale:
    """Tick step with hysteresis, so the tape stops flipping between steps.

    Recomputing the step from the raw value every frame made the whole tape jump
    whenever the value crossed a 1/2/5 boundary — a large part of the twitching.
    A step is only replaced once the value has moved well outside the band the
    current step can comfortably show.
    """

    def __init__(self, minimum: float) -> None:
        self.minimum = minimum
        self.step = _nice_step(minimum, 5)

    def update(self, value: float, *, divisions: int = 6) -> float:
        """Pick a tick step that keeps the current value mid-scale.

        The band is deliberately wide: re-deriving the step on every sample made
        the whole tape jump whenever the value crossed a 1/2/5 boundary, which
        was a large part of the twitching.
        """
        span = max(abs(value), self.minimum) * 2.6
        if span > self.step * divisions * 1.6 or span < self.step * divisions * 0.45:
            self.step = _nice_step(span, divisions)
        return self.step


#: Text advance as a fraction of the font size.  Measured off a real render:
#: a CJK glyph fills its em (1.00), a Latin glyph or digit takes about 0.60.
#: A single ratio for both is what made the "账号" label sit on top of the value
#: beside it -- "账号" is 24 px at size 12, not the 14.9 px a flat 0.62
#: predicted, so a right-aligned label reached ~9 px further than intended.
_WIDE_ADVANCE = 1.00
_NARROW_ADVANCE = 0.60

#: Gap between a readout's label and its value.
_LABEL_GAP = 8.0


def _is_wide(char: str) -> bool:
    """Whether *char* occupies a full em (CJK and friends)."""
    code = ord(char)
    return (
        0x1100 <= code <= 0x115F        # Hangul Jamo
        or 0x2E80 <= code <= 0xA4CF     # CJK radicals through Yi
        or 0xAC00 <= code <= 0xD7A3     # Hangul syllables
        or 0xF900 <= code <= 0xFAFF     # CJK compatibility ideographs
        or 0xFE30 <= code <= 0xFE6F     # CJK compatibility forms
        or 0xFF00 <= code <= 0xFF60     # Fullwidth forms
        or 0xFFE0 <= code <= 0xFFE6
        or 0x20000 <= code <= 0x3FFFD   # CJK extensions B and beyond
    )


def _advance(text: str, size: float) -> float:
    """Estimated width of *text* at *size*, in pixels."""
    total = 0.0
    for char in text or "":
        total += size * (_WIDE_ADVANCE if _is_wide(char) else _NARROW_ADVANCE)
    return total


class HudPainter:
    """Builds the shape list for one frame."""

    # Reserved bands, so instruments never collide with the corner readouts.
    TOP_BAND = 78.0
    BOTTOM_BAND = 62.0
    TAPE_INSET = 104.0

    def __init__(self, hud: HUD, *, width: float, height: float) -> None:
        self.hud = hud
        self.w = max(200.0, float(width))
        self.h = max(140.0, float(height))
        self.shapes: list = []
        self._speed_scale = _TapeScale(512.0)
        self._alt_scale = _TapeScale(60.0)

    # -- primitive helpers ------------------------------------------------
    def line(self, x1, y1, x2, y2, color, *, width=1.0, dash=None) -> None:
        self.shapes.append(cv.Line(x1, y1, x2, y2, paint=_stroke(color, width=width, dash=dash)))

    def outline_rect(self, x, y, w, h, color, *, width=1.0, dash=None) -> None:
        self.shapes.append(cv.Rect(x, y, w, h, paint=_stroke(color, width=width, dash=dash)))

    def filled_rect(self, x, y, w, h, color) -> None:
        self.shapes.append(cv.Rect(x, y, w, h, paint=_paint(color)))

    def circle(self, x, y, r, color, *, width=1.0, fill=False) -> None:
        self.shapes.append(cv.Circle(x, y, r, paint=_paint(color) if fill else _stroke(color, width=width)))

    def arc(self, x, y, w, h, start_deg, sweep_deg, color, *, width=1.5, use_center=False) -> None:
        """Arc in **degrees** (Flet's ``Arc`` takes radians, so we convert).

        Getting this wrong is dramatic: a 3-degree tick handed over as 3
        *radians* sweeps ~172 degrees, and seven of them turn the roll scale
        into a full circle.
        """
        self.shapes.append(
            cv.Arc(
                x, y, w, h,
                math.radians(start_deg),
                math.radians(sweep_deg),
                use_center=use_center,
                paint=_stroke(color, width=width),
            )
        )

    def label(self, x, y, text, color, *, size=None, align="left", bold=False) -> None:
        size = size if size is not None else self.hud.size_micro
        approx_width = _advance(text, size)
        if align == "center":
            x -= approx_width / 2
        elif align == "right":
            x -= approx_width
        self.shapes.append(cv.Text(x, y, text, style=_text_style(self.hud, color, size, bold=bold)))

    def value_box(self, x, y, w, h, text, *, accent: str, size: int, align="left") -> None:
        """A value in a solid scrim + frame.

        The opaque backing is the point: it keeps the number readable no matter
        what grid lines or ladder rungs pass behind it.
        """
        self.filled_rect(x, y, w, h, self.hud.palette.panel_sunk)
        self.outline_rect(x, y, w, h, accent, width=1.5)
        padding = 7.0
        if align == "left":
            self.label(x + padding, y + (h - size) / 2 - 1.5, text, accent, size=size, bold=True)
        elif align == "right":
            self.label(x + w - padding, y + (h - size) / 2 - 1.5, text, accent, size=size,
                       align="right", bold=True)
        else:
            self.label(x + w / 2, y + (h - size) / 2 - 1.5, text, accent, size=size,
                       align="center", bold=True)

    # -- decoration layer --------------------------------------------------
    def grid(self) -> None:
        if not self.hud.decorations:
            return
        color = self.hud.palette.grid
        spacing = 64.0  # sparser than v1: the grid was competing with the text
        y = spacing
        while y < self.h:
            self.line(0, y, self.w, y, color, width=1.0)
            y += spacing
        x = spacing
        while x < self.w:
            self.line(x, 0, x, self.h, color, width=1.0)
            x += spacing

    def scanlines(self, phase: float = 0.0) -> None:
        if not self.hud.decorations:
            return
        color = self.hud.palette.scanline
        # 4 px spacing over a ~400 px panel produced a visible "combed" moire
        # that fought the glyphs; sparser and dimmer reads as texture instead.
        spacing = 7.0
        y = phase % spacing
        while y < self.h:
            self.line(0, y, self.w, y, color, width=1.0)
            y += spacing
        if self.hud.scanline_animation:
            band = (phase * 20.0) % (self.h + 160.0) - 80.0
            self.filled_rect(0, band, self.w, 2.0, self.hud.palette.glow_soft)

    def frame_marks(self) -> None:
        if not self.hud.decorations:
            return
        color = self.hud.palette.border
        length, inset = 26.0, 10.0
        for (x, y, dx, dy) in (
            (inset, inset, 1, 1),
            (self.w - inset, inset, -1, 1),
            (inset, self.h - inset, 1, -1),
            (self.w - inset, self.h - inset, -1, -1),
        ):
            self.line(x, y, x + dx * length, y, color, width=2.0)
            self.line(x, y, x, y + dy * length, color, width=2.0)

    # -- instruments -------------------------------------------------------
    def fpv(self, cx: float, cy: float, telemetry: HudTelemetry) -> None:
        color = self.hud.palette.green if telemetry.online else self.hud.palette.offline
        radius = 12.0
        if telemetry.online:
            self.circle(cx, cy, radius, color, width=2.4)
            self.circle(cx, cy, 2.5, color, fill=True)
        else:
            for start in (30, 150, 270):
                self.arc(cx - radius, cy - radius, radius * 2, radius * 2, start, 80, color, width=2.0)
            self.line(cx - 15, cy, cx - 5, cy, color, width=2.0)
            self.line(cx + 5, cy, cx + 15, cy, color, width=2.0)
        # wings
        self.line(cx - 44, cy + 8, cx - 15, cy + 8, color, width=2.6)
        self.line(cx + 15, cy + 8, cx + 44, cy + 8, color, width=2.6)
        self.line(cx - 44, cy + 8, cx - 44, cy + 2, color, width=2.6)
        self.line(cx + 44, cy + 8, cx + 44, cy + 2, color, width=2.6)
        self.line(cx, cy - 32, cx, cy - 18, color, width=1.8)

    def pitch_ladder(self, cx: float, cy: float, telemetry: HudTelemetry) -> None:
        """5-degree rungs; climb solid and above, dive dashed and below.

        Only every other rung is labelled — labelling all nine was a big part of
        the clutter in the first version.
        """
        color = self.hud.palette.cyan
        pixels_per_degree = max(3.0, (self.h - self.TOP_BAND - self.BOTTOM_BAND) / 40.0)
        pitch = 0.0
        if telemetry.rtt_ms is not None:
            pitch = max(-15.0, min(15.0, (telemetry.rtt_ms - 25.0) / 5.0))

        for degrees in range(-20, 25, 5):
            if degrees == 0:
                continue
            y = cy - (degrees - pitch) * pixels_per_degree
            if y < self.TOP_BAND or y > self.h - self.BOTTOM_BAND:
                continue
            major = degrees % 10 == 0
            half = 96.0 if major else 70.0
            dash = [6, 5] if degrees < 0 else None
            self.line(cx - half, y, cx - 34, y, color, width=1.6 if major else 1.2, dash=dash)
            self.line(cx + 34, y, cx + half, y, color, width=1.6 if major else 1.2, dash=dash)
            tip = -7.0 if degrees > 0 else 7.0
            self.line(cx - half, y, cx - half, y + tip, color, width=1.6, dash=dash)
            self.line(cx + half, y, cx + half, y + tip, color, width=1.6, dash=dash)
            if major:
                self.label(cx - half - 30, y - 7, f"{degrees:+d}", color,
                           size=self.hud.size_micro + 1, bold=True)
                self.label(cx + half + 10, y - 7, f"{degrees:+d}", color,
                           size=self.hud.size_micro + 1, bold=True)

    def speed_tape(self, x: float, telemetry: HudTelemetry) -> None:
        """Left tape: downlink rate, with the current value in a scrimmed box."""
        color = self.hud.palette.cyan
        top, bottom = self.TOP_BAND, max(self.TOP_BAND + 80.0, self.h - self.BOTTOM_BAND)
        self.line(x, top, x, bottom, color, width=1.4)
        self.line(x + 9, top, x + 9, bottom, color, width=1.4)
        self.label(x - 10, top - 22, "下行速率", color, size=self.hud.size_micro + 1,
                   align="right", bold=True)

        value = max(0.0, telemetry.rx_rate)
        step = self._speed_scale.update(value)
        center = (top + bottom) / 2
        span = bottom - top
        scale = span / max(step * 6, 1.0)

        start_tick = math.floor((value - step * 3) / step) * step
        for index in range(7):
            tick_value = start_tick + index * step
            if tick_value < 0:
                continue
            y = center + (value - tick_value) * scale
            if not (top - 4 <= y <= bottom + 4):
                continue
            if index % 2:
                self.line(x + 9, y, x + 14, y, color, width=1.0)
            else:
                self.line(x + 9, y, x + 18, y, color, width=1.2)
                self.label(x + 26, y - 7, _rate_label(tick_value), color,
                           size=self.hud.size_micro + 1)

        self.value_box(x - 74, center - 13, 78, 26, _rate_label(value),
                       accent=self.hud.palette.green, size=self.hud.size_body, align="left")

    def altitude_tape(self, x: float, telemetry: HudTelemetry) -> None:
        """Right tape: session uptime, mirrored so labels stay inside the frame."""
        color = self.hud.palette.cyan
        top, bottom = self.TOP_BAND, max(self.TOP_BAND + 80.0, self.h - self.BOTTOM_BAND)
        self.line(x, top, x, bottom, color, width=1.4)
        self.line(x - 9, top, x - 9, bottom, color, width=1.4)
        self.label(x + 10, top - 22, "在线时长", color, size=self.hud.size_micro + 1, bold=True)

        value = max(0.0, telemetry.uptime)
        step = self._alt_scale.update(value)
        center = (top + bottom) / 2
        span = bottom - top
        scale = span / max(step * 6, 1.0)

        start_tick = math.floor((value - step * 3) / step) * step
        for index in range(7):
            tick_value = max(0.0, start_tick + index * step)
            y = center + (value - tick_value) * scale
            if not (top - 4 <= y <= bottom + 4):
                continue
            if index % 2:
                self.line(x - 9 - 5, y, x - 9, y, color, width=1.0)
            else:
                self.line(x - 9 - 9, y, x - 9, y, color, width=1.2)
                self.label(x - 26, y - 7, _duration_label(tick_value), color,
                           size=self.hud.size_micro + 1, align="right")

        self.value_box(x - 4, center - 13, 82, 26, _duration_label(value),
                       accent=self.hud.palette.green, size=self.hud.size_body, align="right")

    def heading_tape(self, telemetry: HudTelemetry) -> None:
        """Top-centre tape for the keepalive cycle phase.

        Labels are quantised to 5-degree steps so the text stops flickering: at
        the refresh rate a continuously-varying label changed on nearly every
        frame, which read as noise rather than motion.
        """
        color = self.hud.palette.cyan
        y = 48.0
        left, right = 268.0, self.w - 268.0
        if right - left < 170:
            left, right = self.w * 0.24, self.w * 0.76
        if right - left < 90:
            return
        self.line(left, y, right, y, color, width=1.4)

        degrees = (telemetry.keepalive_phase % 1.0) * 360.0
        reference = round(degrees / 5.0) * 5.0  # quantised: stable labels
        pixels_per_degree = (right - left) / 120.0
        mid = (left + right) / 2
        for tick in range(-60, 61, 10):
            x = mid + tick * pixels_per_degree
            if not (left - 1 <= x <= right + 1):
                continue
            major = tick % 30 == 0
            self.line(x, y, x, y - (10.0 if major else 6.0), color,
                      width=1.4 if major else 1.0)
            if major:
                self.label(x, y - 26, f"{(int(reference) + tick) % 360:03d}", color,
                           size=self.hud.size_micro + 1, align="center", bold=True)

        self.line(mid, y, mid, y - 15, self.hud.palette.green, width=2.4)
        bug_x = mid + (reference if reference <= 180 else reference - 360) * pixels_per_degree
        bug_x = max(left, min(right, bug_x))
        self.filled_rect(bug_x - 3, y + 3, 6, 6, self.hud.palette.green)
        self.label(left - 12, y - 8, "保活", color, size=self.hud.size_micro + 1,
                   align="right", bold=True)

    def bank_arc(self, cx: float, cy: float, radius: float, telemetry: HudTelemetry) -> None:
        """Roll scale with a pointer driven by packet loss.

        ``use_center`` must be False: with it True Flutter draws a *pie wedge*,
        and a 2-degree wedge from the centre looks like a radial spoke, turning
        the scale into a starburst.
        """
        color = self.hud.palette.cyan
        loss = telemetry.loss_percent or 0.0
        bank = max(-45.0, min(45.0, loss * 4.5 - 22.5))

        box = (cx - radius, cy - radius, radius * 2, radius * 2)
        for mark_angle in (-45, -30, -15, 0, 15, 30, 45):
            major = mark_angle % 30 == 0
            sweep = 3.5 if major else 2.0
            self.arc(*box, -90 + mark_angle - sweep / 2, sweep, color,
                     width=3.0 if major else 1.8)

        angle_rad = math.radians(-90 + bank)
        inner, outer = radius - 20, radius - 7
        self.line(
            cx + inner * math.cos(angle_rad),
            cy + inner * math.sin(angle_rad),
            cx + outer * math.cos(angle_rad),
            cy + outer * math.sin(angle_rad),
            self.hud.palette.green,
            width=3.0,
        )
        self.label(cx, cy - radius - 16, "滚转 / 丢包", color,
                   size=self.hud.size_micro + 1, align="center", bold=True)

    def corner_readout(self, corner: str, lines: list[tuple[str, str]]) -> None:
        """Labelled readouts in the corners, each on its own scrim.

        The label column is derived from the widest value instead of being a
        fixed offset.  It used to right-align the value at the box edge and the
        label 92 px to its left, which silently assumed every value was
        narrower than 92 px -- "172.18.123.63" is about 109 px at this size, so
        the address was drawn straight through its own label.
        """
        line_height = 20.0
        right = corner in ("tr", "br")
        bottom = corner in ("bl", "br")

        size_value = self.hud.size_body
        size_key = self.hud.size_micro + 1

        widest_value = max((_advance(v, size_value) for _, v in lines), default=0.0)
        widest_key = max((_advance(k, size_key) for k, _ in lines), default=0.0)

        total = len(lines) * line_height
        base_y = (self.h - 16.0 - total) if bottom else 12.0
        base_x = (self.w - 18.0) if right else 18.0

        # Left corners keep their original breathing room; the offset only grows
        # if a label would otherwise run into its own value.
        key_offset = max(62.0, widest_key + _LABEL_GAP)
        content_w = widest_value + _LABEL_GAP + widest_key

        box_w = max(190.0, content_w + 26.0)
        box_h = total + 8.0
        box_x = base_x - box_w + 8.0 if right else base_x - 8.0
        self.filled_rect(box_x, base_y - 4.0, box_w, box_h, self.hud.palette.panel_sunk)
        self.outline_rect(box_x, base_y - 4.0, box_w, box_h, self.hud.palette.border, width=1.0)

        for index, (key, value) in enumerate(lines):
            y = base_y + index * line_height
            if right:
                self.label(base_x, y, value, self.hud.palette.text,
                           size=size_value, align="right", bold=True)
                # Sit the label immediately left of the widest value on this
                # corner, so no line can reach across it.
                self.label(base_x - widest_value - _LABEL_GAP, y, key,
                           self.hud.palette.text_muted, size=size_key, align="right")
            else:
                self.label(base_x, y, key, self.hud.palette.text_muted, size=size_key)
                self.label(base_x + key_offset, y, value, self.hud.palette.text,
                           size=size_value, bold=True)

    def mode_bar(self, telemetry: HudTelemetry) -> None:
        """Bottom mode strip, centred so it clears the corner blocks."""
        y = self.h - 18
        left = self.w * 0.30
        right = self.w * 0.70
        self.line(left, y - 10, right, y - 10, self.hud.palette.border, width=1.0)
        self.label(left, y - 6, "状态", self.hud.palette.text_muted,
                   size=self.hud.size_micro + 1)
        self.label(left + 40, y - 7, telemetry.state_label, telemetry.state_color,
                   size=self.hud.size_body, bold=True)
        if telemetry.detail:
            self.label(right, y - 6, telemetry.detail[:24], self.hud.palette.text_dim,
                       size=self.hud.size_micro, align="right")

    # -- whole frame -------------------------------------------------------
    def render(self, telemetry: HudTelemetry, *, phase: float = 0.0) -> list:
        self.shapes = []
        palette = self.hud.palette
        cx, cy = self.w / 2, self.h / 2

        self.grid()
        self.frame_marks()

        self.heading_tape(telemetry)
        self.bank_arc(cx, cy, min(self.h * 0.25, 104.0), telemetry)
        self.pitch_ladder(cx, cy, telemetry)
        self.fpv(cx, cy, telemetry)
        self.speed_tape(self.TAPE_INSET, telemetry)
        self.altitude_tape(self.w - self.TAPE_INSET, telemetry)

        # Two corner blocks only: the other numbers already live in the widgets
        # under the canvas, and repeating them just adds clutter.
        self.corner_readout("tl", [
            ("延迟", _ms(telemetry.rtt_ms)),
            ("抖动", _ms(telemetry.jitter_ms)),
        ])
        self.corner_readout("tr", [
            ("丢包", f"{telemetry.loss_percent:.0f}%" if telemetry.loss_percent is not None else "--"),
            ("掉线", str(telemetry.drops)),
        ])
        self.corner_readout("bl", [
            ("接收", _bytes(telemetry.session_rx)),
            ("发送", _bytes(telemetry.session_tx)),
        ])
        self.corner_readout("br", [
            ("账号", telemetry.account or "--------"),
            ("IP", telemetry.ip or "--"),
        ])

        self.mode_bar(telemetry)
        self.scanlines(phase=phase)
        self.outline_rect(0.5, 0.5, self.w - 1, self.h - 1, palette.border, width=1.0)
        del palette
        return self.shapes


# --------------------------------------------------------------------------
# formatting helpers
# --------------------------------------------------------------------------
def _ms(value: float | None) -> str:
    return f"{value:.0f}ms" if value is not None else "--"


def _bytes(value: float) -> str:
    value = max(0.0, float(value))
    for unit in ("B", "K", "M", "G"):
        if abs(value) < 1024 or unit == "G":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024.0
    return f"{value:.1f}G"


def _rate_label(value: float) -> str:
    return _bytes(value) + "/s"


def _duration_label(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 3600:
        return f"{seconds // 60:d}:{seconds % 60:02d}"
    return f"{seconds // 3600:d}:{(seconds % 3600) // 60:02d}"
