"""A supervisor that keeps the client running.

The point is the crash class, not the ordinary case: a fault inside a native
library (a use-after-free surfaces as 0xC0000005 in ``_ctypes.pyd``) takes the
process down without a traceback and without a chance to log anything, and the
campus session goes with it.  Something outside the process has to notice.

Design decisions worth stating:

* **A deliberate quit must not be undone.**  The client writes a stop marker as
  it shuts down; the supervisor reads it after the child exits and, if it is
  fresh, stops too.  Without that, "退出" in the tray menu would be answered by
  the app coming straight back -- a fight the user cannot win.
* **Crashes back off.**  A client that dies on start-up would otherwise be
  restarted forever at full speed.  Restarts get further apart as they repeat.
* **Orphans are cleaned up.**  A killed client can leave its Flet window behind
  -- still on screen, owned by nothing, so every click on it does nothing.  That
  is more confusing than the crash itself, so the supervisor kills any client
  window left over before starting a new one.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

__all__ = ["StopMarker", "run_watchdog", "iter_processes", "reap_leftover_windows"]

IS_WINDOWS = sys.platform == "win32"

#: Marker file the client touches when it shuts down on purpose.
STOP_MARKER_NAME = "stopped-intentionally.flag"

#: How recently the marker must have been written for the supervisor to read the
#: exit as deliberate.  Generous: the child writes it just before exiting, and
#: the supervisor may be busy for a moment.
STOP_MARKER_GRACE = 30.0

#: First restart delay, and the ceiling it backs off to.
RESTART_DELAY = 5.0
MAX_RESTART_DELAY = 120.0

#: A child that stays up at least this long is considered healthy, and the
#: backoff resets.  Without it, a client that runs for hours and then crashes
#: would keep the delay from some ancient crash loop.
HEALTHY_UPTIME = 120.0

#: How long to wait before looking again while another client holds the
#: single-instance lock.  Something *is* running, which is what we wanted.
ALREADY_RUNNING_RECHECK = 60.0

_TH32CS_SNAPPROCESS = 0x00000002
_PROCESS_TERMINATE = 0x0001
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class StopMarker:
    """Records that the client was asked to stop, so the supervisor stands down."""

    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / STOP_MARKER_NAME

    def write(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(str(time.time()), encoding="ascii")
        except OSError:
            # Losing the marker costs an unwanted restart, never the shutdown.
            pass

    def clear(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass

    def is_fresh(self, *, within: float = STOP_MARKER_GRACE) -> bool:
        try:
            written = float(self.path.read_text(encoding="ascii").strip() or 0)
        except (OSError, ValueError):
            return False
        return (time.time() - written) <= within


# --------------------------------------------------------------------------
# process listing (Windows)
# --------------------------------------------------------------------------
class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


@dataclass(frozen=True)
class ProcessEntry:
    pid: int
    parent_pid: int
    name: str


def _kernel32():
    """kernel32 with the prototypes this module relies on.

    Every prototype is declared.  Without argtypes ctypes marshals a 64-bit
    handle as a 32-bit int and truncates it, which fails silently -- the same
    trap that once made the priority bump in this project do nothing at all.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = ctypes.c_int
    kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel32.TerminateProcess.restype = ctypes.c_int
    return kernel32


def iter_processes() -> list[ProcessEntry]:
    """Every running process as ``(pid, parent pid, exe name)``."""
    if not IS_WINDOWS:
        return []
    kernel32 = _kernel32()
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == _INVALID_HANDLE_VALUE:
        return []

    out: list[ProcessEntry] = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        guard = 0
        while ok and guard < 20000:
            guard += 1
            out.append(
                ProcessEntry(
                    pid=int(entry.th32ProcessID),
                    parent_pid=int(entry.th32ParentProcessID),
                    name=str(entry.szExeFile or ""),
                )
            )
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return out


def _terminate(pid: int) -> bool:
    if not IS_WINDOWS or pid <= 0:
        return False
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_PROCESS_TERMINATE, 0, pid)
    if not handle:
        return False
    try:
        return bool(kernel32.TerminateProcess(handle, 1))
    finally:
        kernel32.CloseHandle(handle)


def reap_leftover_windows(*, log=None, names: tuple[str, ...] = ("flet.exe",)) -> list[int]:
    """Kill client windows whose parent process is gone.

    Returns the pids it killed.  Only windows whose *parent* has exited are
    touched: a live client's window has a live parent, so a running instance is
    never disturbed.
    """
    killed: list[int] = []
    try:
        processes = iter_processes()
    except Exception:  # pragma: no cover - enumeration is best effort
        return killed
    if not processes:
        return killed

    alive = {p.pid for p in processes}
    for proc in processes:
        if proc.name.lower() not in names:
            continue
        if proc.parent_pid in alive:
            continue  # somebody still owns it
        if _terminate(proc.pid):
            killed.append(proc.pid)
            if log:
                log(f"清理孤儿窗口 pid={proc.pid}（父进程 {proc.parent_pid} 已退出）")
    return killed


# --------------------------------------------------------------------------
# the supervisor loop
# --------------------------------------------------------------------------
def _client_command(argv: list[str], *, data_dir: Path) -> list[str]:
    """How to start one client, mirroring how this process was started."""
    if getattr(sys, "frozen", False):
        base = [sys.executable]
    else:
        base = [sys.executable, str(Path(__file__).resolve().parent.parent / "main.py")]
    args = [a for a in argv if a not in ("--watchdog",)]
    if "--data-dir" not in args:
        args += ["--data-dir", str(data_dir)]
    return base + args



def _single_instance_free(say) -> bool:
    """Whether no other client holds the single-instance lock.

    Acquires the lock only to look, then releases it: holding it would stop the
    child we are about to start from starting.
    """
    from .single_instance import SingleInstance

    probe = SingleInstance()
    try:
        if probe.acquire():
            return True
        say("已有另一个客户端在运行，等它退出后再接管")
        return False
    finally:
        try:
            probe.release()
        except Exception:
            pass


def run_watchdog(
    argv: list[str],
    *,
    data_dir: Path,
    log=None,
    restart_delay: float = RESTART_DELAY,
    max_restart_delay: float = MAX_RESTART_DELAY,
    max_restarts: int = 0,
    command_factory=None,
) -> int:
    """Keep a client running.  Returns when the client stops on purpose.

    *max_restarts* of 0 means "no limit"; a positive value stops after that many
    restarts, which is what makes this testable without an endless loop.
    *command_factory* exists so a test can supervise a stand-in script instead
    of the real client, without the production path knowing about tests.
    """
    build_command = command_factory or (lambda: _client_command(argv, data_dir=data_dir))
    def say(message: str) -> None:
        if log:
            log(message)

    marker = StopMarker(data_dir)
    marker.clear()

    delay = restart_delay
    restarts = 0
    while True:
        # Ask whether a client is already running *before* starting one.  The
        # client answers a second instance with a message box, so spawning and
        # letting it die would put a popup on screen every minute for as long as
        # the user keeps their own copy open.
        if not _single_instance_free(say):
            time.sleep(ALREADY_RUNNING_RECHECK)
            continue

        reap_leftover_windows(log=say)
        command = build_command()
        say(f"启动客户端：{' '.join(command[1:])}")
        started = time.monotonic()
        try:
            # Deliberately *not* the executable's directory.  Starting a child
            # with its cwd inside the install folder makes Windows hold a lock
            # on that folder, and the next build then fails to replace it with
            # "another process is using this file" -- which is exactly what
            # happened while developing this.  The data directory is neutral and
            # always writable.
            proc = subprocess.Popen(command, cwd=str(data_dir))
        except OSError as exc:
            say(f"无法启动客户端：{exc!r}")
            return 1

        try:
            code = proc.wait()
        except KeyboardInterrupt:
            say("守护进程收到中断，一并停止客户端")
            try:
                proc.terminate()
            except Exception:
                pass
            return 0

        uptime = time.monotonic() - started

        if marker.is_fresh():
            say(f"客户端主动退出（退出码 {code}），守护进程随之结束")
            marker.clear()
            return 0

        restarts += 1
        if max_restarts and restarts > max_restarts:
            say(f"已达到重启上限（{max_restarts} 次），停止守护")
            return 1

        if uptime >= HEALTHY_UPTIME:
            # It ran fine for a long while; treat this as a fresh failure.
            delay = restart_delay
        say(f"客户端异常退出（退出码 {code:#x}，运行 {uptime:.0f}s），{delay:.0f}s 后重启")
        time.sleep(delay)
        delay = min(max_restart_delay, delay * 2)
