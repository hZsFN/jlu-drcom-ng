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

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

__all__ = [
    "SYSTEMD_UNIT",
    "TASK_NAME",
    "build_systemd_unit",
    "build_task_xml",
    "install_current",
    "install_systemd_service",
    "install_task",
    "launch_target",
    "remove_systemd_service",
    "remove_task",
    "systemd_available",
    "systemd_service_command",
    "systemd_service_exists",
    "systemd_unit_path",
    "task_exists",
    "task_command",
]

IS_WINDOWS = sys.platform == "win32"

TASK_NAME = "JLU-DrCOM-NG"
SYSTEMD_UNIT = "jlu-drcom-ng.service"

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


def launch_target(*, minimized: bool = True, with_watchdog: bool = False) -> tuple[str, str, str]:
    """(command, arguments, working_dir) for the logon entry.

    From a frozen build the command is the app exe.  From source it is python.exe
    with main.py as its *first argument* -- pointing the task at sys.executable
    alone would start an interpreter and exit immediately.
    """
    args: list[str] = []
    if getattr(sys, "frozen", False):
        command = str(Path(sys.executable).resolve())
        workdir = str(Path(command).parent)
    else:
        entry = Path(__file__).resolve().parent.parent / "main.py"
        command = str(Path(sys.executable).resolve())
        args.append(str(entry))
        workdir = str(entry.parent)
    if with_watchdog:
        args.append("--watchdog")
    args.append("--autostart")
    if minimized:
        args.append("--minimized")
    joined_args = subprocess.list2cmdline(args) if IS_WINDOWS else shlex.join(args)
    return command, joined_args, workdir


def install_current(*, minimized: bool = True, with_watchdog: bool = False) -> tuple[bool, str]:
    """Register the logon task for the running executable."""
    command, arguments, workdir = launch_target(minimized=minimized, with_watchdog=with_watchdog)
    return install_task(command, arguments, working_dir=workdir)


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


# --------------------------------------------------------------------------
# Linux: systemd --user unit
# --------------------------------------------------------------------------
def systemd_unit_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "systemd" / "user" / SYSTEMD_UNIT


def systemd_available() -> bool:
    if IS_WINDOWS:
        return False
    if not shutil.which("systemctl"):
        return False
    res = _run(["systemctl", "--user", "show-environment"])
    return res.returncode == 0


def build_systemd_unit(*, command: str, arguments: str, working_dir: str = "") -> str:
    exec_cmd = f"{shlex.quote(command)} {arguments}".strip() if arguments else shlex.quote(command)
    workdir_line = f"WorkingDirectory={working_dir}\n" if working_dir else ""
    return (
        "[Unit]\n"
        "Description=JLU DrCOM NG Campus Network Client\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"{workdir_line}"
        f"ExecStart={exec_cmd}\n"
        "Restart=on-failure\n"
        "RestartSec=5s\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def install_systemd_service(
    *,
    minimized: bool = True,
    with_watchdog: bool = False,
    command: str = "",
    arguments: str = "",
    working_dir: str = "",
    unit_path: Path | None = None,
) -> tuple[bool, str]:
    if IS_WINDOWS:
        return False, "systemd --user 仅支持 Linux"
    if not command:
        command, arguments, workdir = launch_target(minimized=minimized, with_watchdog=with_watchdog)
        working_dir = working_dir or workdir
    unit_file = unit_path or systemd_unit_path()
    try:
        unit_file.parent.mkdir(parents=True, exist_ok=True)
        unit_file.write_text(
            build_systemd_unit(command=command, arguments=arguments, working_dir=working_dir),
            encoding="utf-8",
        )
    except OSError as exc:
        return False, f"写入 systemd unit 失败：{exc}"

    if shutil.which("systemctl"):
        _run(["systemctl", "--user", "daemon-reload"])
        res = _run(["systemctl", "--user", "enable", SYSTEMD_UNIT])
        if res.returncode != 0:
            detail = (res.stderr or res.stdout or "").strip()
            return False, f"启用 systemd 用户服务失败：{detail}"
    return True, f"已启用 systemd 用户服务「{SYSTEMD_UNIT}」"


def remove_systemd_service(unit_path: Path | None = None) -> tuple[bool, str]:
    if IS_WINDOWS:
        return False, "systemd --user 仅支持 Linux"
    unit_file = unit_path or systemd_unit_path()
    if shutil.which("systemctl"):
        _run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT])
    try:
        unit_file.unlink(missing_ok=True)
    except OSError as exc:
        return False, f"删除 systemd unit 失败：{exc}"
    if shutil.which("systemctl"):
        _run(["systemctl", "--user", "daemon-reload"])
    return True, f"已移除 systemd 用户服务「{SYSTEMD_UNIT}」"


def systemd_service_exists(unit_path: Path | None = None) -> bool:
    if IS_WINDOWS:
        return False
    unit_file = unit_path or systemd_unit_path()
    if not unit_file.exists():
        return False
    if shutil.which("systemctl"):
        res = _run(["systemctl", "--user", "is-enabled", SYSTEMD_UNIT])
        if res.returncode == 0 or res.stdout.strip() == "enabled":
            return True
    return True


def systemd_service_command(unit_path: Path | None = None) -> str:
    unit_file = unit_path or systemd_unit_path()
    try:
        for line in unit_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("ExecStart="):
                return line[len("ExecStart="):].strip()
    except OSError:
        pass
    return ""
