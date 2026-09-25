"""Launch-time behaviour: autostart must authenticate, and must not wait on the UI.

Two reported problems, one root cause each:

1. "自动登录貌似没有生效" -- the autostart entry runs the exe with
   ``--autostart --minimized``, and ``DrcomApp.main()`` decided whether to hide
   the window and whether to authenticate in one if/elif.  Launching minimised
   took the hide branch, so the login branch never ran -- and neither did
   ``start_background()``, leaving a tray icon with no ticker and no session.

2. "开机自启太慢" -- authentication used to happen at the very end of
   ``DrcomApp.main()``, i.e. after the Flet client had connected and the whole
   control tree had been built.  On a cold boot that is the slowest part of the
   start, and the network was waiting behind it.  It now happens in the entry
   point, before a window exists.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from drcom.config import Account


@pytest.fixture
def controller(temp_data_dir, monkeypatch):
    from drcom.controller import AppController

    monkeypatch.setattr(AppController, "start_background", lambda self: None)
    instance = AppController(data_dir=temp_data_dir, log_level="WARNING")
    yield instance
    instance.shutdown()


def make_account(controller, *, auto_login: bool = True) -> Account:
    account = Account(account="2023000000", mac="AA:BB:CC:DD:EE:FF")
    account.auto_login = auto_login
    controller.config.accounts = [account]
    controller.config.active_account_id = account.id
    controller.config.ui.auto_login_on_launch = True
    controller.store.set_password(account.id, "secret-password")
    return account


# --------------------------------------------------------------------------
# the autostart path
# --------------------------------------------------------------------------
def test_autostart_logs_in_even_though_it_starts_minimised(controller, monkeypatch) -> None:
    """The reported bug: --autostart --minimized used to skip authentication."""
    make_account(controller)
    calls: list = []
    monkeypatch.setattr(type(controller), "request_login", lambda self: (calls.append(True), (True, ""))[1])

    controller.begin_session()
    assert calls == [True], "launching minimised must not skip the login"


def test_autostart_hides_the_window_and_still_logs_in(controller, monkeypatch) -> None:
    """Both decisions must happen; neither may shadow the other."""
    import flet as ft

    import drcom.ui.app as ui_app
    from tests.ui_fakes import FakePage

    make_account(controller)
    monkeypatch.setattr(ft.Control, "update", lambda self, *a, **k: None)
    logged_in: list = []
    monkeypatch.setattr(type(controller), "request_login", lambda self: (logged_in.append(True), (True, ""))[1])

    app = ui_app.DrcomApp(controller, minimized=True, enable_tray=False)
    app.page = FakePage()
    app.view_host = None
    app._build_shell()
    app.controller.config.ui.minimize_to_tray = True

    import asyncio

    asyncio.run(app.main(app.page))

    assert app.page.window.visible is False or app.page.window.minimized, "the window should start hidden"
    assert logged_in == [True], "hiding the window swallowed the login"


# --------------------------------------------------------------------------
# ordering: the network must not wait for the window
# --------------------------------------------------------------------------
def test_begin_session_is_idempotent(controller, monkeypatch) -> None:
    make_account(controller)
    calls: list = []
    monkeypatch.setattr(type(controller), "request_login", lambda self: (calls.append(True), (True, ""))[1])

    controller.begin_session()
    controller.begin_session()
    controller.begin_session()
    assert calls == [True], "begin_session logged in more than once"


def test_begin_session_starts_the_ticker(controller, monkeypatch) -> None:
    started: list = []
    monkeypatch.setattr(type(controller), "start_background", lambda self: started.append(True))
    controller.begin_session()
    assert started == [True]


def test_begin_session_skips_login_when_not_configured(controller, monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(type(controller), "request_login", lambda self: (calls.append(True), (True, ""))[1])
    # No account at all.
    controller.begin_session()
    assert calls == [], "there is nothing to log in with"


def test_begin_session_respects_the_per_account_switch(controller, monkeypatch) -> None:
    make_account(controller, auto_login=False)
    calls: list = []
    monkeypatch.setattr(type(controller), "request_login", lambda self: (calls.append(True), (True, ""))[1])
    controller.begin_session()
    assert calls == [], "the account opted out of auto-login"


def test_entry_point_begins_the_session_before_running_the_gui() -> None:
    """Pin the ordering in main.py: login must not sit behind run_gui()."""
    import main as entry

    source = inspect.getsource(entry.main)
    assert "begin_session()" in source, "the entry point no longer starts the session"
    assert source.index("begin_session()") < source.index("run_gui("), (
        "begin_session must be called before run_gui, otherwise the network "
        "waits for the Flet window to finish drawing"
    )


# --------------------------------------------------------------------------
# priority
# --------------------------------------------------------------------------
def test_priority_bump_is_best_effort() -> None:
    """It must never be fatal on a machine that refuses the request."""
    import main as entry

    entry._raise_priority()  # must not raise here either


def test_priority_class_is_actually_applied() -> None:
    """Ask the kernel, not the source text.

    The first version of this shipped doing nothing: it called
    SetPriorityClass without argtypes, so ctypes truncated the 64-bit process
    handle to 32 bits, the call returned 0, and no exception was raised to
    catch.  A test that greps the source for "0x00008000" passes happily in
    that state -- so measure the real thing in a subprocess.
    """
    import os
    import subprocess
    import sys
    import textwrap

    if os.name != "nt":
        pytest.skip("Windows-only scheduling class")

    probe = textwrap.dedent(
        """
        import ctypes, sys
        sys.path.insert(0, r"%s")
        import main
        applied = main._raise_priority()
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.GetCurrentProcess.restype = ctypes.c_void_p
        k.GetPriorityClass.argtypes = [ctypes.c_void_p]
        k.GetPriorityClass.restype = ctypes.c_uint
        print(applied, k.GetPriorityClass(k.GetCurrentProcess()))
        """
    ) % str(Path(__file__).resolve().parent.parent)

    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    applied, actual = result.stdout.split()
    assert applied == "True", "SetPriorityClass reported failure"
    assert int(actual) == 0x00008000, (
        f"priority class is 0x{int(actual):X}, expected 0x8000 (ABOVE_NORMAL)"
    )


def test_priority_helper_reports_failure_instead_of_lying() -> None:
    """A refused call returns False rather than a silent no-op."""
    import main as entry

    assert isinstance(entry._raise_priority(), bool)
