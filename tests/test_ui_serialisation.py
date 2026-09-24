"""Every view must survive Flet's own msgpack encoding.

This exists because of a real, silent failure.  A `ft.SegmentedButton` was
given `selected={"ask"}` -- a set -- but Flet 1.0 annotates that field as
``list[str]``.  Building the control worked, `_switch_view` worked, the handler
ran; the failure only happened when Flet packed the patch to send to the client:

    TypeError: can not serialize 'set' object

`_safe_update()` wraps `page.update()` in a bare ``except Exception: pass``, so
the exception vanished and the settings tab simply never rendered.  Every
existing test passed, because none of them ever serialised anything -- they only
walked the Python control tree.

So: pack every view the way the client would.  It is cheap and it is the only
thing that catches a control property Flet cannot encode.
"""

from __future__ import annotations

import pytest

flet = pytest.importorskip("flet")

from tests.ui_fakes import FakePage  # noqa: E402

VIEWS = ["状态", "日志", "账号", "设置", "关于"]


def _packer():
    from flet.controls.base_control import BaseControl
    from flet.messaging.flet_socket_server import configure_encode_object_for_msgpack

    return configure_encode_object_for_msgpack(BaseControl)


def serialise(control) -> None:
    """Raise exactly what Flet would raise when sending this control."""
    import msgpack

    msgpack.packb(["patch", control], default=_packer())


@pytest.fixture
def app(controller, monkeypatch):
    import drcom.ui.app as ui_app

    monkeypatch.setattr(flet.Control, "update", lambda self, *a, **k: None)
    monkeypatch.setattr(type(controller), "start_background", lambda self: None)

    instance = ui_app.DrcomApp(controller, enable_tray=False)
    instance.page = FakePage()
    return instance


@pytest.mark.parametrize("view", VIEWS)
def test_view_serialises(app, view: str) -> None:
    app.view_host = None
    control = app._build_view(view)
    serialise(control)


def test_shell_serialises(app) -> None:
    serialise(app._build_shell())


def test_close_action_selector_serialises_for_every_choice(app) -> None:
    """The exact regression: a set in `selected` blows up at send time."""
    for action in ("ask", "tray", "quit"):
        app.controller.config.ui.close_action = action
        app.view_host = None
        control = app._build_view("设置")
        serialise(control)


def test_syncing_the_selector_keeps_it_serialisable(app) -> None:
    """The chooser rewrites `selected` at runtime; that path must encode too."""
    app.view_host = None
    app._build_view("设置")
    for action in ("ask", "tray", "quit"):
        app.controller.config.ui.close_action = action
        app._sync_close_action_control()
        serialise(app.close_action_control)


def test_a_set_would_have_failed(app) -> None:
    """Pin the reasoning, so nobody 'simplifies' this back to a set."""
    app.view_host = None
    app._build_view("设置")
    app.close_action_control.selected = {"ask"}     # the old, broken value
    with pytest.raises(TypeError, match="set"):
        serialise(app.close_action_control)
