"""Flet front-end package."""

from __future__ import annotations

__all__ = ["DrcomApp", "run_gui"]


def __getattr__(name: str):
    # Import lazily so that `--cli` never pays for the Flet import.
    if name in ("DrcomApp", "run_gui"):
        from . import app as _app

        return getattr(_app, name)
    raise AttributeError(name)
