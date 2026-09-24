"""Fakes shared by the UI tests.

Kept in one module rather than copied per file: the window-event and callback
tests have to agree on what a "page" is, and two drifting copies of that would
quietly stop testing the same thing.
"""

from __future__ import annotations

__all__ = [
    "FakeEvent",
    "FakePage",
    "FakeWindow",
    "iter_actionable",
    "walk",
]


class FakeEvent:
    """Enough of a Flet event for the handlers, which mostly use ``.control``."""

    def __init__(self, control=None, value=None, data=None, type=None) -> None:
        self.control = control
        self.value = value
        self.data = data
        self.type = type
        self.page = None

    def prevent_default(self, *_args) -> None:  # pragma: no cover - not needed here
        pass


def walk(control, seen=None):
    """Yield every control in the tree, actionable or not.

    ``iter_actionable`` deliberately skips inert controls, so it cannot be used
    to find a plain ``TextField``.
    """
    if seen is None:
        seen = set()
    if control is None or id(control) in seen:
        return
    seen.add(id(control))
    yield control

    for attr in ("controls", "actions", "content"):
        child = getattr(control, attr, None)
        if isinstance(child, list):
            for item in child:
                yield from walk(item, seen)
        elif child is not None:
            yield from walk(child, seen)


def iter_actionable(control, seen=None):
    """Yield ``(control, attribute)`` for every control carrying a callback."""
    if seen is None:
        seen = set()
    if control is None or id(control) in seen:
        return
    seen.add(id(control))

    for attr in ("on_click", "on_change", "on_submit", "on_select"):
        if callable(getattr(control, attr, None)):
            yield control, attr

    for attr in ("controls", "actions", "content"):
        child = getattr(control, attr, None)
        if isinstance(child, list):
            for item in child:
                yield from iter_actionable(item, seen)
        elif child is not None:
            yield from iter_actionable(child, seen)


class FakeWindow:
    """Records the window mutations the app performs."""

    def __init__(self) -> None:
        self.width = 1100
        self.height = 780
        self.min_width = 900
        self.min_height = 660
        self.opacity = 1.0
        self.bgcolor = None
        self.visible = True
        self.minimized = False
        self.skip_task_bar = False
        self.prevent_close = False
        self.destroyed = False
        self.on_event = None

    def destroy(self) -> None:
        self.destroyed = True

    def to_front(self) -> None:
        pass


class FakePage:
    """Minimal stand-in for ``ft.Page`` (see tests/test_ui_smoke.py)."""

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
