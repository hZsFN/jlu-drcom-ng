"""A client for the native core (``drcomd.exe``).

Why this exists
---------------
The Python build used to do the network work in-process, which meant the UI
process had to stay alive to keep the session up — and the UI drags in Flet,
so "closed the window" still cost ~90 MB of private memory.

``drcomd`` is a 220 KB native binary that does nothing but authenticate and
keep the session alive (about 1 MB of private memory).  This module speaks its
local HTTP API, so the GUI can be closed, restarted or killed without touching
the session, and the resident footprint is the daemon's.

The class is a drop-in for :class:`drcom.engine.AuthEngine`: same attribute
names, same event objects, so the controller and the UI do not care which one
they are talking to.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from .config import Account, AppConfig
from .engine import EngineEvent, EngineState, Listener, OfflineReason
from .logbus import LogBus
from .stats import StatsStore

__all__ = ["RemoteEngine", "DEFAULT_API_PORT", "daemon_path", "daemon_is_up", "gui_path"]

#: Loopback port the native core listens on.  Fixed rather than dynamic so the
#: GUI can find a daemon it did not start (autostart, previous run, watchdog).
DEFAULT_API_PORT = 61441

#: Poll cadence for the status snapshot.  One request per second against a
#: loopback socket is free, and it keeps the UI feeling live without the daemon
#: having to push events.
POLL_INTERVAL = 1.0

#: How long to wait for a freshly spawned daemon to answer its first request.
START_TIMEOUT = 6.0


def daemon_path() -> Path | None:
    """Locate drcomd.exe: next to the frozen exe, else inside the source tree."""
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir / "drcomd.exe")
        candidates.append(exe_dir / "_internal" / "drcomd.exe")
    root = Path(__file__).resolve().parent.parent
    candidates.append(root / "native" / "drcomd.exe")
    candidates.append(root / "native" / "bin" / "Release" / "drcomd.exe")
    for path in candidates:
        if path.is_file():
            return path
    return None


def _request(port: int, path: str, method: str = "GET", timeout: float = 1.5) -> dict | None:
    """One HTTP call against the daemon.  Returns None when it is not there."""
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read()
        return json.loads(body.decode("utf-8")) if body else {}
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None


def daemon_is_up(port: int = DEFAULT_API_PORT) -> bool:
    """Is a daemon already answering on this port?"""
    return _request(port, "/health", timeout=0.6) is not None


def gui_path() -> str | None:
    """Path of the window binary, for the tray menu to reopen it.

    Only meaningful in a frozen build: from source there is no single file to
    launch (the window is ``python main.py``), so we say nothing and let the
    daemon try the layout it knows about.
    """
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).resolve())
    return None


class RemoteEngine:
    """Talks to drcomd instead of doing the protocol work itself."""

    def __init__(
        self,
        *,
        config: AppConfig,
        account: Account,
        password: str,
        log: LogBus,
        stats: StatsStore | None = None,
        api_port: int = DEFAULT_API_PORT,
        config_path: Path | None = None,
        enable_tray: bool = True,
    ) -> None:
        self.config = config
        self.account = account
        self._password = password          # kept for interface parity; the daemon reads config.json
        self.log = log
        self.stats = stats
        self.api_port = api_port
        #: Whether the daemon should put up a notification-area icon.  False in
        #: web mode and with ``--no-tray``, where nothing would ever click it.
        self.enable_tray = enable_tray
        #: The daemon reads the very same config.json, so it must be told where
        #: that file is (portable installs and tests both move it).
        self.config_path = Path(config_path) if config_path else None

        self.state = EngineState.IDLE
        self.reason = OfflineReason.STOPPED
        self.ip = ""
        self.online_since: float | None = None
        self.attempt = 0
        self.consecutive_failures = 0
        self.last_diagnosis = None
        self.socket_info = None

        self._process: subprocess.Popen | None = None
        self._daemon_log = None
        self._listeners: list[Listener] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_log_count = 0

    # -- interface parity with AuthEngine --------------------------------
    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def is_online(self) -> bool:
        return self.state == EngineState.ONLINE

    def uptime_seconds(self) -> float:
        if self.online_since is None:
            return 0.0
        return max(0.0, time.time() - self.online_since)

    def subscribe(self, listener: Listener) -> None:
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def unsubscribe(self, listener: Listener) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        """Make sure a daemon is running, ask it to log in, start polling."""
        if self.is_running:
            return
        self._stop.clear()
        if not self._ensure_daemon():
            self.state = EngineState.FATAL
            self.reason = OfflineReason.BIND_FAILED
            self._emit("fatal", "找不到或无法启动原生核心 drcomd.exe",
                       detail="请确认安装目录里有 drcomd.exe。")
            return

        _request(self.api_port, "/login", method="POST")
        self._thread = threading.Thread(
            target=self._poll_loop, name="drcom-remote-engine", daemon=True
        )
        self._thread.start()

    def stop(self, *, reason: OfflineReason = OfflineReason.USER_LOGOUT) -> None:
        """Stop the session but leave the daemon resident."""
        self._stop.set()
        _request(self.api_port, "/logout", method="POST")
        self.reason = reason
        if self.state == EngineState.ONLINE:
            self.state = EngineState.STOPPED
            self.online_since = None
            self._emit("stopped", "已停止保活，服务端将在超时后释放会话")

    def join(self, timeout: float | None = 5.0) -> bool:
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    # -- process handling ------------------------------------------------
    def _ensure_daemon(self) -> bool:
        if daemon_is_up(self.api_port):
            self.log.info("原生核心已在运行（端口 %d）", self.api_port)
            return True

        exe = daemon_path()
        if exe is None:
            self.log.error("找不到 drcomd.exe，无法启动原生核心")
            return False

        config_file = self.config_path
        if config_file is None:
            from .config import default_data_dir

            config_file = default_data_dir() / "config.json"
        args = [str(exe), "--config", str(config_file), "--api-port", str(self.api_port)]
        # The icon belongs to the core, not to this process: that is what lets
        # the window close without taking the tray (or the session) with it.
        if self.enable_tray:
            args.append("--tray")
            window = gui_path()
            if window:
                args += ["--gui", window]
        creation = 0
        if sys.platform == "win32":
            # Detach: the daemon must outlive the window, so no console and no
            # shared Ctrl+C group.
            creation = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            # Keep the daemon's own output: when it cannot bind a port or fails to
            # decrypt the password, its console text is the only clue, and losing
            # it turns a one-line diagnosis into an afternoon.
            log_dir = config_file.parent / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            self._daemon_log = open(log_dir / "drcomd-core.log", "ab", buffering=0)
        except OSError:
            self._daemon_log = None

        try:
            self._process = subprocess.Popen(
                args,
                stdout=self._daemon_log or subprocess.DEVNULL,
                stderr=subprocess.STDOUT if self._daemon_log else subprocess.DEVNULL,
                creationflags=creation,
                close_fds=True,
            )
        except OSError as exc:
            self.log.error("启动 drcomd 失败：%r", exc)
            return False

        deadline = time.time() + START_TIMEOUT
        while time.time() < deadline:
            if daemon_is_up(self.api_port):
                self.log.info("原生核心已启动：%s（pid %d）", exe.name, self._process.pid)
                return True
            if self._process.poll() is not None:
                self.log.error("drcomd 启动后立即退出（退出码 %s）", self._process.returncode)
                return False
            time.sleep(0.15)
        self.log.error("drcomd 在 %.0f 秒内没有响应", START_TIMEOUT)
        return False

    def restart_daemon(self) -> None:
        """Restart the daemon, e.g. after the account or MAC changed."""
        _request(self.api_port, "/shutdown", method="POST")
        time.sleep(0.4)
        if self._process is not None:
            try:
                self._process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
        self.start()

    def shutdown_daemon(self) -> None:
        """Ask the resident core to exit.

        Closing the window must *not* do this -- the whole point of the split is
        that the session outlives the window.  This is for the explicit "quit"
        path, where the user means the program, tray icon included.  When the
        tray menu is used instead, the daemon stops itself from C and this is
        never called.
        """
        _request(self.api_port, "/shutdown", method="POST", timeout=2.0)

    # -- polling ---------------------------------------------------------
    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            payload = _request(self.api_port, "/status")
            if payload is None:
                # The daemon died under us: that is fatal, not a retry.
                self.state = EngineState.FATAL
                self._emit("fatal", "原生核心没有响应", detail="drcomd 可能已退出。")
                return
            self._apply(payload)
            self._drain_logs()
            self._stop.wait(POLL_INTERVAL)

    def _apply(self, payload: dict) -> None:
        """Fold one /status document into our local state, emitting on change."""
        try:
            state = EngineState(payload.get("state", "idle"))
        except ValueError:
            state = EngineState.IDLE
        online = bool(payload.get("online"))
        ip = str(payload.get("ip") or "")
        previous_state = self.state
        was_online = self.is_online

        self.state = state
        self.ip = ip
        self.consecutive_failures = int(payload.get("consecutive_failures") or 0)
        if online:
            if not was_online:
                self.online_since = time.time()
        else:
            self.online_since = None

        if online and not was_online:
            self._emit("online", f"已上线，IP {ip}" if ip else "已上线")
            return
        if state != previous_state:
            self._emit("state", self._state_text(state))

    def _drain_logs(self) -> None:
        """Forward anything new from the daemon's ring buffer into our log bus."""
        payload = _request(self.api_port, "/logs")
        if not payload:
            return
        lines = payload.get("lines") or []
        if len(lines) < self._last_log_count:
            self._last_log_count = 0          # ring buffer wrapped
        for line in lines[self._last_log_count:]:
            self.log.debug("core: %s", line)
        self._last_log_count = len(lines)

    @staticmethod
    def _state_text(state: EngineState) -> str:
        return {
            EngineState.IDLE: "空闲",
            EngineState.BINDING: "正在绑定端口",
            EngineState.CHALLENGING: "正在获取挑战码",
            EngineState.AUTHENTICATING: "正在提交认证",
            EngineState.ONLINE: "已上线",
            EngineState.RETRY_WAIT: "等待重试",
            EngineState.FATAL: "需要人工处理",
            EngineState.STOPPED: "已停止",
        }.get(state, state.value)

    # -- events ----------------------------------------------------------
    def _emit(self, kind: str, message: str = "", *, detail: str = "", ip: str = "") -> None:
        event = EngineEvent(
            kind=kind,
            state=self.state,
            message=message,
            ip=ip or self.ip,
            detail=detail,
        )
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception:                  # a bad listener must not kill polling
                self.log.debug("引擎事件监听器抛错", exc_info=True)
