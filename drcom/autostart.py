"""Autostart via Task Scheduler, which starts earlier than a Run key.

Why not the ``HKCU\\...\\Run`` key: Explorer launches those *after* the shell is
up, and it walks them one at a time alongside every other startup entry, so the
app ends up queued behind whatever else the user has.  A logon-triggered
scheduled task is started by the Task Scheduler service the moment the session
exists, in parallel with the shell, and it is covered by Windows' boot
prefetching -- which is most of the difference on a cold start.

The task is registered from XML rather than ``schtasks`` switches because the
defaults matter and would otherwise bite:

* ``ExecutionTimeLimit`` defaults to three days, after which Task Scheduler
  kills the task.  Set to ``PT0S`` (no limit) -- this program is meant to run for
  weeks.
* ``Priority`` defaults to 7, i.e. *below normal*, which is the opposite of what
  a program asked to start early should get.
* ``RestartOnFailure`` means a crash is recovered without our watchdog process
  having to be in the startup chain: a crash exits non-zero and gets restarted,
  while a deliberate quit exits 0 and is left alone.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

__all__ = [
    "TASK_NAME",
    "build_task_xml",
    "install_task",
    "remove_task",
    "task_exists",
    "task_command",
]

IS_WINDOWS = sys.platform == "win32"

TASK_NAME = "JLU-DrCOM-NG"

#: Above normal.  Task Scheduler's own scale runs 0 (highest) to 10; the default
#: is 7, which is below normal.
TASK_PRIORITY = 3

#: Give up after this long if the task somehow never exits.
_NO_LIMIT = "PT0S"


def _xml_escape(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def build_task_xml(*, command: str, arguments: str, user_id: str, working_dir: str = "") -> str:
    """The task definition, as the UTF-16 XML ``schtasks /XML`` expects."""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>JLU DrCOM NG campus-network client, started at logon.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{_xml_escape(user_id)}</UserId>
      <Delay>PT0S</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{_xml_escape(user_id)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>{_NO_LIMIT}</ExecutionTimeLimit>
    <Priority>{TASK_PRIORITY}</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{_xml_escape(command)}</Command>
      <Arguments>{_xml_escape(arguments)}</Arguments>
      <WorkingDirectory>{_xml_escape(working_dir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=0x08000000 if IS_WINDOWS else 0,
    )


def _current_user() -> str:
    import getpass
    import os

    domain = os.environ.get("USERDOMAIN", "")
    name = os.environ.get("USERNAME") or getpass.getuser()
    return f"{domain}\\{name}" if domain else name


def install_task(
    command: str,
    arguments: str,
    *,
    working_dir: str = "",
    user_id: str = "",
    xml_writer=None,
) -> tuple[bool, str]:
    """Register or replace the logon task.  Returns ``(ok, message)``."""
    if not IS_WINDOWS:
        return False, "计划任务方式仅支持 Windows"

    xml = build_task_xml(
        command=command,
        arguments=arguments,
        user_id=user_id or _current_user(),
        working_dir=working_dir,
    )

    write = xml_writer or _write_temp_xml
    try:
        path = write(xml)
    except OSError as exc:
        return False, f"无法写入任务定义：{exc}"

    result = _run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(path), "/F"])
    try:
        Path(path).unlink()
    except OSError:
        pass

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return False, f"注册计划任务失败：{detail}"
    return True, f"已注册计划任务「{TASK_NAME}」（登录时启动，无延迟）"


def _write_temp_xml(xml: str) -> Path:
    """schtasks wants the definition as UTF-16; UTF-8 is rejected."""
    import tempfile

    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".xml", delete=False, encoding="utf-16"
    )
    with handle:
        handle.write(xml)
    return Path(handle.name)


def remove_task() -> tuple[bool, str]:
    if not IS_WINDOWS:
        return False, "计划任务方式仅支持 Windows"
    result = _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if "cannot find" in detail.lower() or "找不到" in detail:
            return True, "计划任务本来就不存在"
        return False, f"删除计划任务失败：{detail}"
    return True, f"已删除计划任务「{TASK_NAME}」"


def task_exists() -> bool:
    if not IS_WINDOWS:
        return False
    return _run(["schtasks", "/Query", "/TN", TASK_NAME]).returncode == 0


def task_command() -> str:
    """The command line the task would run, or "" when there is no task."""
    if not IS_WINDOWS or not task_exists():
        return ""

    result = _run(["powershell", "-NoProfile", "-Command",
                   f"(Get-ScheduledTask -TaskName '{TASK_NAME}').Actions | "
                   "ForEach-Object { $_.Execute + ' ' + $_.Arguments }"])
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()
