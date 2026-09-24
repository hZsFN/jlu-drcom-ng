"""HUD design tokens for the Flet front-end.

Design rationale (spec 3.4)
---------------------------
The brief warns against confusing two kinds of transparency:

* **window transparency** (see through to the desktop) — the *content* fades
  too, so a busy wallpaper destroys legibility.  Flet's ``window.opacity`` is
  window-wide, so this is not usable without wrecking readability.
* **element transparency** (only the decorative layer is translucent) — text
  stays fully opaque, so contrast is preserved.

So this is a **HUD panel**, not HUD glass: the base layer is an opaque dark
colour, the grid/scanlines are low-alpha decoration, and every glyph is drawn
fully opaque with a glow on top.  ``window.opacity`` stays at 1.

Accessibility
-------------
Every text colour below was measured against :data:`BG` with the WCAG relative
luminance formula.  All of them clear 4.5:1 (AA for normal text); the measured
ratios are in the comments and are re-checked by ``tests/test_theme.py``.

Two saturated colours from the source cyberpunk palette were deliberately
*brightened* per spec 3.4 rule 4 ("pure saturated colour is unreadable"), and
glow is applied as a ``TextStyle.shadow`` halo so that glyphs stay crisp even
where the decoration layer sits behind them (rule 2).
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["HUD", "Palette", "hud_of"]


@dataclass(frozen=True)
class Palette:
    """A complete, contrast-checked colour set."""

    name: str = "hud"

    # --- surfaces (always opaque; spec 3.4 layer 1) ---------------------
    bg: str = "#0A0E14"          # window / page base
    panel: str = "#111823"       # card surface
    panel_alt: str = "#141C28"   # raised surface (inputs, headers)
    panel_sunk: str = "#070A0F"  # log console, code blocks
    border: str = "#243244"

    # --- decoration (translucent on purpose; layer 2) -------------------
    grid: str = "#141E2A"   # dimmer than v1: it competed with the glyphs
    grid_alpha: str = "#16202C"
    scanline: str = "#0C1118"
    glow_soft: str = "#0E2A33"

    # --- text (layer 4: opaque + glow) ---------------------------------
    text: str = "#E8F4F8"        # 17.25:1 on bg
    text_dim: str = "#A9BED0"    # 10.10:1
    text_muted: str = "#7C8FA3"  #  5.82:1

    # --- accents -------------------------------------------------------
    cyan: str = "#7FE7F5"        # 13.49:1  (brightened from #00FFFF)
    green: str = "#86FF86"       # 15.34:1  (brightened from #00FF00)
    amber: str = "#FFC857"       # 12.57:1
    danger: str = "#FF7B7B"      #  7.71:1  (brightened from #FF0000)
    violet: str = "#C79BFF"      #  8.81:1
    offline: str = "#7C8FA3"     #  5.82:1
    online: str = "#86FF86"
    warning: str = "#FFC857"

    # --- translucent accent washes (decoration only) --------------------
    cyan_wash: str = "#1A7FE7F5"
    green_wash: str = "#1A86FF86"
    amber_wash: str = "#1AFFC857"
    danger_wash: str = "#1AFF7B7B"
    violet_wash: str = "#1AC79BFF"

    def state_color(self, state: str) -> str:
        return {
            "online": self.online,
            "authenticating": self.cyan,
            "challenging": self.cyan,
            "binding": self.cyan,
            "retry_wait": self.warning,
            "fatal": self.danger,
            "idle": self.offline,
            "stopped": self.offline,
        }.get(state, self.text)


@dataclass(frozen=True)
class HighContrastPalette(Palette):
    """Same geometry, maximum legibility: no glow, brighter text, sparser decor."""

    name: str = "high-contrast"
    text: str = "#FFFFFF"        # 19.60:1
    text_dim: str = "#D8E6F2"    # 13.76:1
    text_muted: str = "#A9BED0"  # 10.10:1
    grid: str = "#111823"
    scanline: str = "#0A0E14"
    glow_soft: str = "#0A0E14"


@dataclass(frozen=True)
class HUD:
    """Palette plus the layout/typography metrics the widgets share."""

    palette: Palette = Palette()

    # geometry
    pad: int = 14
    gap: int = 10
    radius: int = 4
    notch: int = 12          # clipped corner size on cards
    hairline: float = 1.0

    # typography
    font_mono: str = "Consolas, Cascadia Mono, DejaVu Sans Mono, monospace"
    font_ui: str = ""        # empty == Flet/Flutter default (good CJK coverage)
    # Bumped after the first live look: 10 px monospace on a dark grid was too
    # small to read comfortably on a 1100x780 window.
    size_hero: int = 36
    size_title: int = 16
    size_body: int = 14
    size_label: int = 12
    size_micro: int = 11

    # effects
    reduce_motion: bool = False
    decorations: bool = True
    glow: bool = True
    scanline_animation: bool = True

    def glow_shadow(self, color: str, blur: float = 10.0):
        """A single-colour halo; returns ``None`` when effects are off."""
        if not self.glow:
            return None
        from flet import BoxShadow

        return BoxShadow(blur_radius=blur, color=color, offset=(0, 0))

    @property
    def state_palette(self) -> Palette:
        return self.palette


def hud_of(*, high_contrast: bool = False, reduce_motion: bool = False,
           decorations: bool = True, glow: bool | None = None,
           scanline_animation: bool = True) -> HUD:
    """Build the token set from user preferences.

    ``reduce_motion`` disables every animation (spec 3.4 rule 5: the scanline
    sweep and any rotation must be switchable off).
    """
    palette: Palette = HighContrastPalette() if high_contrast else Palette()
    return HUD(
        palette=palette,
        reduce_motion=reduce_motion,
        decorations=decorations,
        glow=(not high_contrast) if glow is None else glow,
        scanline_animation=scanline_animation and not reduce_motion,
    )
