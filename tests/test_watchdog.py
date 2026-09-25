"""The watchdog: restart a crash, but never undo a deliberate quit.

Written because the app died three times in a day with an access violation
inside ctypes -- a native fault that prints nothing and cannot be caught.  A
supervisor outside the process is the only thing that can notice.

The design decision that needs pinning is the second one: a supervisor that
restarts *everything* makes "退出" impossible to use, because the app comes
straight back.  The client writes a stop marker as it shuts down and the
supervisor stands down when it sees a fresh one.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from drcom.watchdog import StopMarker, iter_processes, run_watchdog


# --------------------------------------------------------------------------
# the stop marker
# --------------------------------------------------------------------------
def test_a_fresh_marker_means_the_exit_was_deliberate(tmp_path: Path) -> None:
    marker = StopMarker(tmp_path)
    assert not marker.is_fresh()
    marker.write()
    assert marker.is_fresh()


def test_a_stale_marker_does_not_stop_the_supervisor(tmp_path: Path) -> None:
    """Otherwise one deliberate quit would disarm the watchdog forever."""
    marker = StopMarker(tmp_path)
    marker.write()
    marker.path.write_text(str(time.time() - 3600), encoding="ascii")
    assert not marker.is_fresh()

    # And a marker from a previous session must not mask a crash today.
    marker.path.write_text(str(time.time() - 5), encoding="ascii")
    assert marker.is_fresh(within=30)


def test_clearing_the_marker(tmp_path: Path) -> None:
    marker = StopMarker(tmp_path)
    marker.write()
    marker.clear()
    assert not marker.path.exists()
    marker.clear()  # idempotent


def test_a_corrupt_marker_is_treated_as_absent(tmp_path: Path) -> None:
    marker = StopMarker(tmp_path)
    marker.path.write_text("not a number", encoding="ascii")
    assert not marker.is_fresh()


# --------------------------------------------------------------------------
# the supervisor loop
# --------------------------------------------------------------------------
def _fake_client(tmp_path: Path, *, exit_code: int, marker: bool, uptime: float = 0.0) -> str:
    """A stand-in "client" script: optionally writes the stop marker, then exits."""
    script = tmp_path / "fake_client.py"
    script.write_text(
        "import sys, time\n"
        f"time.sleep({uptime})\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})\n"
        "from drcom.watchdog import StopMarker\n"
        "import pathlib\n"
        f"if {marker!r}:\n"
        "    StopMarker(pathlib.Path(sys.argv[1])).write()\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    return str(script)


def _runner(script: str, tmp_path: Path):
    """Supervise a stand-in script instead of the real client."""
    return lambda: [sys.executable, script, str(tmp_path)]


def test_a_crash_is_restarted(tmp_path: Path, monkeypatch) -> None:
    """The whole point: an abnormal exit must come back."""
    script = _fake_client(tmp_path, exit_code=3, marker=False)
    monkeypatch.setattr("drcom.watchdog.reap_leftover_windows", lambda **kw: [])

    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    code = run_watchdog(
        [],
        data_dir=tmp_path,
        restart_delay=5.0,
        max_restarts=1,
        command_factory=_runner(script, tmp_path),
    )
    # It restarts once, then hits the cap rather than looping forever.
    assert code == 1
    assert slept, "the supervisor did not wait before restarting"


def test_a_deliberate_quit_ends_the_supervisor(tmp_path: Path, monkeypatch) -> None:
    """The client says it meant to stop; the supervisor must agree."""
    script = _fake_client(tmp_path, exit_code=0, marker=True)
    monkeypatch.setattr("drcom.watchdog.reap_leftover_windows", lambda **kw: [])

    code = run_watchdog(
        [],
        data_dir=tmp_path,
        max_restarts=5,
        command_factory=_runner(script, tmp_path),
    )
    assert code == 0, "a deliberate quit was treated as a crash"


def test_the_marker_is_cleared_when_the_supervisor_stops(tmp_path: Path, monkeypatch) -> None:
    script = _fake_client(tmp_path, exit_code=0, marker=True)
    monkeypatch.setattr("drcom.watchdog.reap_leftover_windows", lambda **kw: [])
    run_watchdog(
        [], data_dir=tmp_path, max_restarts=3, command_factory=_runner(script, tmp_path)
    )
    assert not StopMarker(tmp_path).path.exists(), "a stale marker would disarm the next run"


def test_the_supervisor_clears_a_leftover_marker_before_starting(tmp_path: Path, monkeypatch) -> None:
    """A marker from last time must not make the very first crash look deliberate."""
    StopMarker(tmp_path).write()
    script = _fake_client(tmp_path, exit_code=1, marker=False)
    monkeypatch.setattr("drcom.watchdog.reap_leftover_windows", lambda **kw: [])
    monkeypatch.setattr(time, "sleep", lambda s: None)

    code = run_watchdog(
        [], data_dir=tmp_path, max_restarts=1, command_factory=_runner(script, tmp_path)
    )
    assert code == 1, "it read a stale marker as a deliberate quit and gave up"


def test_backoff_grows_but_is_capped(tmp_path: Path, monkeypatch) -> None:
    script = _fake_client(tmp_path, exit_code=9, marker=False)
    monkeypatch.setattr("drcom.watchdog.reap_leftover_windows", lambda **kw: [])
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    run_watchdog(
        [],
        data_dir=tmp_path,
        restart_delay=5.0,
        max_restart_delay=20.0,
        max_restarts=4,
        command_factory=_runner(script, tmp_path),
    )
    assert slept[:4] == [5.0, 10.0, 20.0, 20.0], slept


def test_the_watchdog_flag_is_not_passed_to_the_client() -> None:
    from drcom.watchdog import _client_command

    command = _client_command(["--watchdog", "--autostart", "--minimized"], data_dir=Path("."))
    assert "--watchdog" not in command
    assert "--autostart" in command and "--minimized" in command


def test_the_data_dir_is_passed_through() -> None:
    from drcom.watchdog import _client_command

    command = _client_command(["--watchdog"], data_dir=Path("/tmp/x"))
    assert "--data-dir" in command


# --------------------------------------------------------------------------
# process enumeration / orphan cleanup
# --------------------------------------------------------------------------
@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only process listing")
def test_process_listing_finds_us_and_our_parent() -> None:
    import os

    processes = iter_processes()
    assert processes, "process enumeration returned nothing"
    mine = [p for p in processes if p.pid == os.getpid()]
    assert mine, "our own process is missing from the listing"
    assert mine[0].parent_pid > 0
    assert mine[0].name.lower().startswith("python")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only process listing")
def test_orphan_reaping_leaves_live_processes_alone() -> None:
    """A window whose parent is alive must not be touched."""
    from drcom.watchdog import reap_leftover_windows

    import os

    # 'python.exe' children of a live parent: nothing here is an orphan, and the
    # reaper only looks at the names it is asked about.
    killed = reap_leftover_windows(names=("definitely-not-a-real-process.exe",))
    assert killed == []

    processes = iter_processes()
    alive = {p.pid for p in processes}
    assert os.getpid() in alive


def test_stop_marker_lives_in_the_data_dir(tmp_path: Path) -> None:
    assert StopMarker(tmp_path).path.parent == tmp_path


# --------------------------------------------------------------------------
# the enable/disable entry point
# --------------------------------------------------------------------------
def test_watchdog_action_is_lifted_out_of_the_command_line(monkeypatch) -> None:
    """`--cli watchdog on` must not be read as a stray positional argument."""
    from drcom import cli

    seen: dict = {}

    def fake_command(controller, args):
        seen["action"] = getattr(args, "watchdog_action", None)
        return 0

    monkeypatch.setattr(cli, "_COMMANDS", {**cli._COMMANDS, "watchdog": fake_command})
    assert cli.run_cli(["--cli", "watchdog", "on"]) == 0
    assert seen["action"] == "on"


def test_watchdog_status_needs_no_action(monkeypatch) -> None:
    from drcom import cli

    seen: dict = {}
    monkeypatch.setattr(
        cli, "_COMMANDS",
        {**cli._COMMANDS, "watchdog": lambda c, a: seen.setdefault("action", getattr(a, "watchdog_action", "")) or 0},
    )
    cli.run_cli(["--cli", "watchdog"])
    assert seen["action"] in ("", None)


def test_autostart_command_can_carry_the_watchdog() -> None:
    from drcom.single_instance import _launch_command

    plain = _launch_command(minimized=True, with_watchdog=False)
    guarded = _launch_command(minimized=True, with_watchdog=True)
    assert "--watchdog" not in plain
    assert "--watchdog" in guarded
    assert "--autostart" in plain and "--autostart" in guarded and "--minimized" in guarded
