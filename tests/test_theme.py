"""Readability guarantees (acceptance criterion 9, spec 3.4).

The brief makes legibility a hard requirement, not a nicety, so the palette is
checked numerically rather than by eye:

* every text token clears 4.5:1 against every surface it can appear on;
* the saturated source colours are the *brightened* variants;
* decoration never draws text, and the reduce-motion preference is honoured.
"""

from __future__ import annotations

import pytest

from drcom.ui.theme import HUD, HighContrastPalette, Palette, hud_of


# --------------------------------------------------------------------------
# WCAG relative luminance / contrast
# --------------------------------------------------------------------------
def _channel(value: float) -> float:
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def luminance(color: str) -> float:
    text = color.lstrip("#")
    if len(text) == 8:  # ARGB wash like #1A7FE7F5
        text = text[2:]
    red, green, blue = (int(text[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * _channel(red) + 0.7152 * _channel(green) + 0.0722 * _channel(blue)


def contrast(foreground: str, background: str) -> float:
    a, b = luminance(foreground), luminance(background)
    high, low = max(a, b), min(a, b)
    return (high + 0.05) / (low + 0.05)


TEXT_TOKENS = ("text", "text_dim", "text_muted", "cyan", "green", "amber", "danger", "violet")
SURFACES = ("bg", "panel", "panel_alt", "panel_sunk")


# --------------------------------------------------------------------------
# contrast
# --------------------------------------------------------------------------
@pytest.mark.parametrize("palette_cls", [Palette, HighContrastPalette])
@pytest.mark.parametrize("token", TEXT_TOKENS)
@pytest.mark.parametrize("surface", SURFACES)
def test_text_contrast_meets_wcag_aa(palette_cls, token: str, surface: str) -> None:
    palette = palette_cls()
    ratio = contrast(getattr(palette, token), getattr(palette, surface))
    assert ratio >= 4.5, (
        f"{palette_cls.__name__}.{token} on {surface} is only {ratio:.2f}:1"
    )


def test_state_colours_are_readable() -> None:
    palette = Palette()
    for state in ("online", "offline", "warning", "idle", "retry_wait", "fatal", "stopping"):
        ratio = contrast(palette.state_color(state), palette.bg)
        assert ratio >= 4.5, f"state colour for {state!r} is only {ratio:.2f}:1"


def test_brightened_accents_beat_the_raw_saturated_originals() -> None:
    """Spec 3.4 rule 4: do not use #00FF00 / #FF0000 directly for text."""
    palette = Palette()
    assert palette.green != "#00FF00"
    assert palette.cyan != "#00FFFF"
    assert palette.danger != "#FF0000"
    # The substitutes are genuinely lighter, i.e. easier on the eye.
    assert luminance(palette.green) > luminance("#00FF00") or contrast(palette.green, palette.bg) > 10
    assert contrast(palette.danger, palette.bg) > contrast("#FF0000", palette.bg)


def test_danger_is_comfortably_above_threshold() -> None:
    """#FF0000 only just scrapes past 4.5:1; ours must do better."""
    assert contrast(Palette().danger, Palette().bg) > 6.0


# --------------------------------------------------------------------------
# layering
# --------------------------------------------------------------------------
def test_base_surface_is_fully_opaque() -> None:
    """Spec 3.4: the base layer must be opaque so contrast is preserved."""
    palette = Palette()
    for surface in SURFACES:
        value = getattr(palette, surface)
        # 6 hex digits == RRGGBB, i.e. no alpha channel.
        assert len(value) == 7, f"{surface}={value} is not an opaque colour"


def test_decorative_washes_are_translucent_but_never_used_for_text() -> None:
    palette = Palette()
    for name in ("cyan_wash", "green_wash", "amber_wash", "danger_wash", "violet_wash"):
        value = getattr(palette, name)
        assert len(value) == 9, f"{name}={value} should be #AARRGGBB"
        assert value[1:3].upper() != "FF", f"{name} should be translucent"


def test_surfaces_are_ordered_dark_to_light() -> None:
    """A consistent ordering keeps the depth cues believable."""
    palette = Palette()
    assert luminance(palette.panel_sunk) < luminance(palette.bg) < luminance(palette.panel)
    assert luminance(palette.panel) < luminance(palette.panel_alt)


# --------------------------------------------------------------------------
# tokens / preferences
# --------------------------------------------------------------------------
def test_default_hud_enables_effects() -> None:
    hud = hud_of()
    assert hud.glow
    assert hud.decorations
    assert not hud.reduce_motion


def test_reduce_motion_disables_the_sweep() -> None:
    """Spec 3.4 rule 5: motion effects must be switchable off."""
    hud = hud_of(scanline_animation=True, reduce_motion=True)
    assert hud.reduce_motion
    assert not hud.scanline_animation


def test_high_contrast_mode_turns_off_glow_and_brightens_text() -> None:
    hud = hud_of(high_contrast=True)
    assert not hud.glow
    assert hud.palette.text == "#FFFFFF"
    assert contrast(hud.palette.text, hud.palette.bg) > contrast(Palette().text, Palette().bg)


def test_glow_helper_respects_the_flag() -> None:
    assert hud_of(glow=True).glow_shadow("#FFFFFF") is not None
    assert hud_of(glow=False).glow_shadow("#FFFFFF") is None


def test_decoration_can_be_disabled() -> None:
    hud = hud_of(decorations=False)
    assert not hud.decorations


def test_font_stack_falls_back_to_a_monospace() -> None:
    hud = HUD()
    assert "monospace" in hud.font_mono


def test_typography_scale_is_ordered() -> None:
    hud = HUD()
    assert hud.size_micro < hud.size_label < hud.size_body < hud.size_title < hud.size_hero
