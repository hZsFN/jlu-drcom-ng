"""Single-instance guard and login autostart helpers.

Repeated launches are a real failure mode here: the second instance cannot bind
61440 and reports a confusing error (spec 6.2).  So we take a named lock first
and, when another instance is already running, surface *that* fact rather than
a port error.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

__all__ = [
    "SingleInstance",
    "autostart_command",
    "disable_autostart",
    "enable_autostart",
    "is_autostart_enabled",
]

IS_WINDOWS = sys.platform == "win32"

_MUTEX_NAME = "Global\\DrCOM-JLU-Py-SingleInstance"
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE = "DrCOM-JLU"
_DESKTOP_FILE = "drcom-jlu.desktop"


class SingleInstance:
    """A cross-platform "only one of me" lock.

    On Windows this is a named mutex (released automatically if the process
    dies), elsewhere an ``O_EXCL`` lock file whose PID is checked for liveness.
    """

    def __init__(self, name: str = _MUTEX_NAME) -> None:
        self.name = name
        self._handle = None
        self._lock_path: Path | None = None
        self.already_running = False

    def acquire(self) -> bool:
        """Return ``True`` if we are the first instance."""
        if IS_WINDOWS:
            return self._acquire_windows()
        return self._acquire_posix()

    def _acquire_windows(self) -> bool:
        import ctypes

        ERROR_ALREADY_EXISTS = 183
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p

        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, 0, self.name)
        last_error = ctypes.get_last_error()
        if not handle:
            # Mutex creation failed for an unrelated reason — do not block the app.
            return True
        self._handle = handle
        self.already_running = last_error == ERROR_ALREADY_EXISTS
        return not self.already_running

    def _acquire_posix(self) -> bool:
        path = Path(tempfile.gettempdir()) / "drcom-jlu.lock"
        self._lock_path = path
        if path.exists():
            try:
                pid = int(path.read_text(encoding="utf-8").strip() or "0")
            except (OSError, ValueError):
                pid = 0
            if pid and _pid_alive(pid):
                self.already_running = True
                return False
            # Stale lock from a crashed run.
            try:
                path.unlink()
            except OSError:
                pass
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
        except FileExistsError:
            self.already_running = True
            return False
        return True

    def release(self) -> None:
        if IS_WINDOWS and self._handle:
            import ctypes

            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(ctypes.c_void_p(self._handle))
            self._handle = None
        elif self._lock_path and self._lock_path.exists():
            try:
                self._lock_path.unlink()
            except OSError:
                pass

    def __enter__(self) -> "SingleInstance":
        self.acquire()
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except ProcessLookupError:
        return False
    return True


# --------------------------------------------------------------------------
# Autostart
# --------------------------------------------------------------------------
def _launch_command(*, minimized: bool = True) -> str:
    """The command line to register for autostart."""
    if getattr(sys, "frozen", False):
        exe = f'"{Path(sys.executable).resolve()}"'
    else:
        entry = Path(__file__).resolve().parent.parent / "main.py"
        exe = f'"{Path(sys.executable).resolve()}" "{entry}"'
    return f"{exe} --autostart" + (" --minimized" if minimized else "")


def autostart_command(*, minimized: bool = True) -> str:
    return _launch_command(minimized=minimized)


def is_autostart_enabled() -> bool:
    if IS_WINDOWS:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
                winreg.QueryValueEx(key, _RUN_VALUE)
            return True
        except OSError:
            return False
    return (Path.home() / ".config" / "autostart" / _DESKTOP_FILE).exists()


def enable_autostart(*, minimized: bool = True) -> tuple[bool, str]:
    command = _launch_command(minimized=minimized)
    if IS_WINDOWS:
        import winreg

        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
                winreg.SetValueEx(key, _RUN_VALUE, 0, winreg.REG_SZ, command)
            return True, f"已写入开机自启：HKCU\\{_RUN_KEY}\\{_RUN_VALUE}"
        except OSError as exc:
            return False, f"写入注册表失败：{exc}"

    path = Path.home() / ".config" / "autostart" / _DESKTOP_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=DrCOM JLU\n"
            "Comment=吉林大学校园网认证客户端\n"
            f"Exec={command}\n"
            "X-GNOME-Autostart-enabled=true\n"
            "Terminal=false\n",
            encoding="utf-8",
        )
        return True, f"已写入 {path}"
    except OSError as exc:
        return False, f"写入 {path} 失败：{exc}"


def disable_autostart() -> tuple[bool, str]:
    if IS_WINDOWS:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, _RUN_VALUE)
            return True, "已取消开机自启"
        except FileNotFoundError:
            return True, "本来就没有设置开机自启"
        except OSError as exc:
            return False, f"删除注册表值失败：{exc}"

    path = Path.home() / ".config" / "autostart" / _DESKTOP_FILE
    try:
        path.unlink(missing_ok=True)
        return True, "已取消开机自启"
    except OSError as exc:
        return False, f"删除 {path} 失败：{exc}"


def open_in_file_manager(path: Path) -> None:
    """Reveal *path* in Explorer/Finder/Nautilus."""
    path = Path(path)
    try:
        if IS_WINDOWS:
            subprocess.Popen(["explorer", "/select,", str(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])
    except OSError:
        pass
