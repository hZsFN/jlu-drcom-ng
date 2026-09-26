"""Autostart mechanism: the logon task must beat the Run key, and must not be
killed by defaults nobody asked for.

The Run key is what this replaces.  Explorer launches those after the shell is
up, one at a time, behind every other startup entry; a logon-triggered scheduled
task is started by the Task Scheduler service the moment the session exists, in
parallel with the shell.

The XML settings are the part worth pinning, because Task Scheduler's defaults
are actively hostile to a long-running GUI app.
"""

from __future__ import annotations

from pathlib import Path
from xml.dom import minidom

from drcom.autostart import TASK_NAME, build_task_xml


def _task(**overrides) -> str:
    kwargs = dict(
        command=r"C:\Program Files\JLU-DrCOM-NG\JLU-DrCOM-NG.exe",
        arguments="--watchdog --autostart --minimized",
        user_id="PC\\user",
        working_dir=r"C:\Program Files\JLU-DrCOM-NG",
    )
    kwargs.update(overrides)
    return build_task_xml(**kwargs)


def _text(xml: str, tag: str) -> str:
    return xml.split(f"<{tag}>")[1].split(f"</{tag}>")[0]


def test_the_task_xml_is_well_formed() -> None:
    minidom.parseString(_task())


def test_it_triggers_at_logon_with_no_delay() -> None:
    xml = _task()
    assert "<LogonTrigger>" in xml
    assert _text(xml, "Delay") == "PT0S", "a delay would defeat the whole point"


def test_there_is_no_execution_time_limit() -> None:
    """Task Scheduler's default is three days, after which it kills the task."""
    assert _text(_task(), "ExecutionTimeLimit") == "PT0S"


def test_priority_is_above_normal_not_the_default() -> None:
    """The default (7) is *below* normal -- the opposite of the point."""
    assert _text(_task(), "Priority") == "3"


def test_a_crash_is_restarted_but_a_clean_exit_is_not() -> None:
    """RestartOnFailure keys off the exit code, which is exactly our rule."""
    xml = _task()
    assert "<RestartOnFailure>" in xml
    assert _text(xml, "Interval") == "PT1M"
    assert _text(xml, "Count") == "3"


def test_it_keeps_running_on_battery_and_when_idle() -> None:
    xml = _task()
    assert _text(xml, "DisallowStartIfOnBatteries") == "false"
    assert _text(xml, "StopIfGoingOnBatteries") == "false"
    assert _text(xml, "StopOnIdleEnd") == "false"


def test_it_runs_interactively_as_the_user() -> None:
    """It is a GUI app in the user's session, not a service."""
    xml = _task()
    assert _text(xml, "LogonType") == "InteractiveToken"
    assert _text(xml, "RunLevel") == "LeastPrivilege"


def test_the_command_reaches_the_action() -> None:
    xml = _task(command=r"C:\Program Files\x\app.exe")
    assert _text(xml, "Command") == r"C:\Program Files\x\app.exe"
    assert _text(xml, "Arguments") == "--watchdog --autostart --minimized"


def test_special_characters_are_escaped() -> None:
    built = _task(arguments="--data-dir C:\\a & b\\<x>")
    assert "&amp;" in built and "&lt;x&gt;" in built
    minidom.parseString(built)  # still valid


def test_the_task_name_is_stable() -> None:
    assert TASK_NAME == "JLU-DrCOM-NG"


def test_install_reports_a_failure_instead_of_raising(monkeypatch) -> None:
    from drcom import autostart

    monkeypatch.setattr(
        autostart, "_run",
        lambda args: type("R", (), {"returncode": 1, "stdout": "", "stderr": "Access is denied."})(),
    )
    ok, message = autostart.install_task("cmd", "args", user_id="PC\\user")
    assert not ok and "Access is denied" in message


def test_install_hands_the_tool_a_readable_definition(monkeypatch) -> None:
    from drcom import autostart

    captured: dict = {}

    def fake_run(args):
        captured["args"] = args
        captured["xml"] = Path(args[args.index("/XML") + 1]).read_text(encoding="utf-16")
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(autostart, "_run", fake_run)
    ok, _ = autostart.install_task(
        r"C:\app.exe", "--watchdog", user_id="PC\\user", working_dir=r"C:\app"
    )
    assert ok
    assert captured["args"][:4] == ["schtasks", "/Create", "/TN", TASK_NAME]
    assert "--watchdog" in captured["xml"]


def test_a_missing_task_is_not_an_error_when_removing(monkeypatch) -> None:
    from drcom import autostart

    monkeypatch.setattr(
        autostart, "_run",
        lambda args: type("R", (), {"returncode": 1, "stdout": "", "stderr": "ERROR: cannot find"})(),
    )
    ok, message = autostart.remove_task()
    assert ok, message
