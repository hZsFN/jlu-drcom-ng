"""A client for the native core (``drcomd.exe`` / ``drcomd``).

Why this exists
---------------
The Python build used to do the network work in-process, which meant the UI
process had to stay alive to keep the session up — and the UI drags in Flet,
so "closed the window" still cost ~100-190 MB of private memory.

``drcomd`` is a tiny native binary that does nothing but authenticate and keep
the session alive (about 1 MB of private memory).  This module speaks its local
HTTP API, so the GUI can be closed, restarted or killed without touching the
session, and the resident footprint is the daemon's.

The class is a drop-in for :class:`drcom.engine.AuthEngine`: same attribute
names, same event objects, so the controller and the UI do not care which one
they are talking to.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import Account, AppConfig
from .engine import EngineEvent, EngineState, Listener, OfflineReason
from .logbus import LogBus
from .stats import StatsStore

__all__ = [
    "RemoteEngine",
    "DEFAULT_API_PORT",
    "daemon_path",
    "daemon_is_up",
    "daemon_status",
    "gui_path",
]

#: Loopback port the native core listens on.  Fixed rather than dynamic so the
#: GUI can find a daemon it did not start (autostart, previous run, watchdog).
DEFAULT_API_PORT = 61441

#: Poll cadence for the status snapshot.  One request per second against a
#: loopback socket is free, and it keeps the UI feeling live without the daemon
#: having to push events.
POLL_INTERVAL = 1.0

#: How long to wait for a freshly spawned daemon to answer its first request.
START_TIMEOUT = 6.0

REASON_TEXT = {
    "user_logout": "你点了注销",
    "bind_failed": "UDP 端口绑定失败",
    "challenge_failed": "没有拿到挑战码",
    "login_failed": "服务器拒绝了登录",
    "timeout": "等待服务器回应超时",
    "keepalive_failed": "保活失败，会话已断开",
    "stopped": "已经停止",
}

REASON_ADVICE = {
    "bind_failed": "端口被别的程序占了，或者落在 Windows/系统的保留区间：点「端口诊断」看是谁占用，"
                   "关掉它，或在设置里换一个绑定端口。",
    "challenge_failed": "服务器没回应：确认连着校园网、没有 TUN/VPN 代理抢路由，再试一次。",
    "login_failed": "核对账号密码；仍不行就把日志里的错误码发出来（0x03 密码错 / 0x0B MAC 不符 / "
                    "0x07 IP 不符 / 0x17 需要 DHCP）。",
    "timeout": "校园网这边丢包严重或被劫持：换有线/无线再试，先关掉代理软件。",
    "keepalive_failed": "会话已经掉了：点「重新连接」。",
}

_CORE_FAILURE_TOKENS = ("error", "fail", "fatal", "失败", "错误", "超时", "拒绝")


def daemon_path() -> Path | None:
    """Locate drcomd.exe (Windows) or drcomd (POSIX)."""
    exe_name = "drcomd.exe" if sys.platform == "win32" else "drcomd"
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir / exe_name)
        candidates.append(exe_dir / "_internal" / exe_name)
    root = Path(__file__).resolve().parent.parent
    candidates.append(root / "native" / exe_name)
    if sys.platform == "win32":
        candidates.append(root / "native" / "bin" / "Release" / exe_name)
    for path in candidates:
        if path.is_file():
            return path
    return None


def _default_token_path(config_path: Path | None = None) -> Path:
    if config_path is not None:
        return Path(config_path).parent / "core-token.txt"
    from .config import default_data_dir

    return default_data_dir() / "core-token.txt"


def _read_core_token(config_path: Path | None = None) -> str:
    try:
        return _default_token_path(config_path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _request(
    port: int,
    path: str,
    method: str = "GET",
    timeout: float = 1.5,
    *,
    token: str = "",
    config_path: Path | None = None,
) -> dict | None:
    """One HTTP call against the daemon.  Returns None when it is not there."""
    url = f"http://127.0.0.1:{port}{path}"
    need_token = method == "POST" or path.startswith("/logs")
    headers: dict[str, str] = {}
    if need_token:
        tok = token or _read_core_token(config_path)
        if tok:
            headers["X-DrCOM-Token"] = tok
    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read()
        return json.loads(body.decode("utf-8")) if body else {}
    except urllib.error.HTTPError as exc:
        # Daemon may have rotated core-token.txt after a restart; retry once.
        if exc.code == 403 and need_token:
            fresh = _read_core_token(config_path)
            if fresh and fresh != headers.get("X-DrCOM-Token"):
                retry_req = urllib.request.Request(
                    url, headers={"X-DrCOM-Token": fresh}, method=method
                )
                try:
                    with urllib.request.urlopen(retry_req, timeout=timeout) as response:
                        body = response.read()
                    return json.loads(body.decode("utf-8")) if body else {}
                except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
                    return None
        return None
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None


def daemon_status(port: int = DEFAULT_API_PORT, timeout: float = 0.6) -> dict | None:
    """Return the running daemon's /status snapshot, or None when unreachable."""
    return _request(port, "/status", timeout=timeout)


def daemon_is_up(port: int = DEFAULT_API_PORT) -> bool:
    """Is a daemon already answering on this port?"""
    return _request(port, "/health", timeout=0.6) is not None


def gui_path() -> str | None:
    """Path of the window binary, for the tray menu to reopen it."""
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
        self.enable_tray = enable_tray
        self.config_path = Path(config_path) if config_path else None

        self.state = EngineState.IDLE
        self.reason = OfflineReason.STOPPED
        self.ip = ""
        self.online_since: float | None = None
        self.attempt = 0
        self.consecutive_failures = 0
        self.last_reason = ""
        self.last_diagnosis = None
        self.socket_info = None
        self.session_id = 0
        self.traffic_baseline: tuple[int, int, int] | None = None
        self.core_session_rx = 0
        self.core_session_tx = 0

        self._process: subprocess.Popen | None = None
        self._daemon_log = None
        self._listeners: list[Listener] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_log_count = 0
        self._log_cursor = 0
        self._announced_reason = ""
        self._fresh_login_requested = False
        self._seen_first_status = False

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

    def _close_daemon_log(self) -> None:
        handle = getattr(self, "_daemon_log", None)
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
            self._daemon_log = None

    # -- lifecycle -------------------------------------------------------
    def start(self, *, attach_only: bool = False, force_reconnect: bool = False) -> None:
        """Ensure a daemon is running (or attach to an existing one) and start polling."""
        if self.is_running:
            if force_reconnect:
                self.reconnect()
            return
        self._stop.clear()
        was_up = False
        if attach_only:
            if not daemon_is_up(self.api_port):
                return
            was_up = True
        else:
            was_up = daemon_is_up(self.api_port)
            if not self._ensure_daemon():
                self.state = EngineState.FATAL
                self.reason = OfflineReason.BIND_FAILED
                self._emit(
                    "fatal",
                    "找不到或无法启动原生核心 drcomd",
                    detail="请确认安装目录里有 drcomd.exe（Windows）或 drcomd（Linux）。",
                )
                return
            if force_reconnect:
                _request(self.api_port, "/reconnect", method="POST", config_path=self.config_path)
            elif was_up:
                # /login on drcomd is idempotent when already running, and reloads config when stopped.
                _request(self.api_port, "/login", method="POST", config_path=self.config_path)

        # Seed initial snapshot immediately so callers don't wait 1s.
        initial = daemon_status(self.api_port)
        initial_online = bool(initial and initial.get("online"))
        self._fresh_login_requested = (not attach_only) and (
            force_reconnect or not was_up or not initial_online
        )
        if initial:
            is_initial_attach = bool(
                attach_only or (was_up and not force_reconnect and initial_online)
            )
            self._apply(initial, initial_attach=is_initial_attach)
        self._thread = threading.Thread(
            target=self._poll_loop, name="drcom-remote-engine", daemon=True
        )
        self._thread.start()

    def reconnect(self) -> bool:
        """Ask the running daemon to reload config.json and reconnect cleanly."""
        self._fresh_login_requested = True
        if not daemon_is_up(self.api_port):
            self.start()
            return True
        payload = _request(
            self.api_port, "/reconnect", method="POST", timeout=6.0, config_path=self.config_path
        )
        return bool(payload and payload.get("ok"))

    def stop(self, *, reason: OfflineReason = OfflineReason.USER_LOGOUT) -> None:
        """Stop the session but leave the daemon resident."""
        self._stop.set()
        _request(
            self.api_port, "/logout", method="POST", timeout=6.0, config_path=self.config_path
        )
        self.reason = reason
        if self.state == EngineState.ONLINE:
            self.state = EngineState.STOPPED
            self.online_since = None
            stats = getattr(self, "stats", None)
            if stats is not None:
                stats.close_session(drop=(reason is not OfflineReason.USER_LOGOUT))
            self._emit("stopped", "已停止保活，服务端将在超时后释放会话")
        self._close_daemon_log()

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
            self.log.error("找不到原生核心 drcomd，无法启动")
            return False

        config_file = self.config_path
        if config_file is None:
            from .config import default_data_dir

            config_file = default_data_dir() / "config.json"
        token_file = _default_token_path(config_file)
        args = [
            str(exe),
            "--config", str(config_file),
            "--token-file", str(token_file),
            "--api-port", str(self.api_port),
        ]
        if sys.platform == "win32" and self.enable_tray:
            args.append("--tray")
            window = gui_path()
            if window:
                args += ["--gui", window]

        popen_kwargs: dict = {"close_fds": True}
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = (
                subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            popen_kwargs["start_new_session"] = True

        self._close_daemon_log()
        try:
            log_dir = config_file.parent / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            core_log = log_dir / "drcomd-core.log"
            try:
                if core_log.exists() and core_log.stat().st_size > 2 * 1024 * 1024:
                    rotated = log_dir / "drcomd-core.log.1"
                    core_log.replace(rotated)
            except OSError:
                pass
            self._daemon_log = open(core_log, "ab", buffering=0)
        except OSError:
            self._daemon_log = None

        try:
            self._process = subprocess.Popen(
                args,
                stdout=self._daemon_log or subprocess.DEVNULL,
                stderr=subprocess.STDOUT if self._daemon_log else subprocess.DEVNULL,
                **popen_kwargs,
            )
        except OSError as exc:
            self._close_daemon_log()
            self.log.error("启动 drcomd 失败：%r", exc)
            return False

        deadline = time.time() + START_TIMEOUT
        while time.time() < deadline:
            if daemon_is_up(self.api_port):
                self.log.info("原生核心已启动：%s（pid %d）", exe.name, self._process.pid)
                return True
            if self._process.poll() is not None:
                self._close_daemon_log()
                self.log.error("drcomd 启动后立即退出（退出码 %s）", self._process.returncode)
                return False
            time.sleep(0.15)
        self._close_daemon_log()
        self.log.error("drcomd 在 %.0f 秒内没有响应", START_TIMEOUT)
        return False

    def restart_daemon(self) -> None:
        """Restart the daemon process."""
        _request(self.api_port, "/shutdown", method="POST", config_path=self.config_path)
        time.sleep(0.4)
        if self._process is not None:
            try:
                self._process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
        self._close_daemon_log()
        self._log_cursor = 0
        self._last_log_count = 0
        self.start()

    def shutdown_daemon(self) -> None:
        """Ask the resident core to exit."""
        _request(
            self.api_port, "/shutdown", method="POST", timeout=2.0, config_path=self.config_path
        )
        self._close_daemon_log()

    # -- polling ---------------------------------------------------------
    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            payload = _request(self.api_port, "/status")
            if payload is None:
                exit_code = self._daemon_exit_code()
                if self.is_online:
                    stats = getattr(self, "stats", None)
                    if stats is not None:
                        stats.close_session(drop=(exit_code != 0))
                self.online_since = None
                self.state = EngineState.FATAL
                self._emit(
                    "core_gone",
                    "原生核心已退出",
                    detail=(
                        "drcomd 从托盘菜单退出。" if exit_code == 0
                        else f"drcomd 异常退出（退出码 {exit_code}）。" if exit_code is not None
                        else "drcomd 已退出。"
                    ),
                    payload={"exit_code": exit_code},
                )
                return
            self._apply(payload)
            self._drain_logs()
            self._stop.wait(POLL_INTERVAL)

    def _daemon_exit_code(self) -> int | None:
        process = self._process
        if process is None:
            return None
        try:
            return process.poll()
        except Exception:
            return None

    def _apply(self, payload: dict, *, initial_attach: bool = False) -> None:
        """Fold one /status document into our local state, emitting on change."""
        try:
            state = EngineState(payload.get("state", "idle"))
        except ValueError:
            state = EngineState.IDLE
        online = bool(payload.get("online"))
        ip = str(payload.get("ip") or "")
        previous_state = self.state
        was_online = self.is_online
        first_status = not getattr(self, "_seen_first_status", False)
        self._seen_first_status = True

        self.state = state
        self.ip = ip
        self.last_reason = str(payload.get("last_reason") or "")
        self.consecutive_failures = int(payload.get("consecutive_failures") or 0)

        def _safe_int(val, default: int = 0) -> int:
            try:
                return int(val) if val is not None else default
            except (TypeError, ValueError):
                return default

        def _safe_float(val, default: float = 0.0) -> float:
            try:
                return float(val) if val is not None else default
            except (TypeError, ValueError):
                return default

        session_id = _safe_int(payload.get("session_id"), 0)
        online_since_epoch = _safe_float(payload.get("online_since"), 0.0)
        if_index = _safe_int(payload.get("traffic_if_index"), 0)
        rx_base = _safe_int(payload.get("traffic_rx_base"), -1)
        tx_base = _safe_int(payload.get("traffic_tx_base"), -1)
        sess_rx = _safe_int(payload.get("session_rx"), -1)
        sess_tx = _safe_int(payload.get("session_tx"), -1)

        prev_session_id = getattr(self, "session_id", 0)
        session_changed = bool(
            online and session_id > 0 and prev_session_id > 0 and session_id != prev_session_id
        )

        stats = getattr(self, "stats", None)
        account = getattr(self, "account", None)

        if online:
            uptime_val = _safe_float(payload.get("uptime_seconds"), 0.0)
            if sess_rx >= 0:
                self.core_session_rx = sess_rx
            if sess_tx >= 0:
                self.core_session_tx = sess_tx
            baseline_payload: dict = {}
            if if_index > 0 and rx_base >= 0 and tx_base >= 0:
                baseline_payload.update(
                    {
                        "traffic_if_index": if_index,
                        "traffic_rx_base": rx_base,
                        "traffic_tx_base": tx_base,
                    }
                )
            if sess_rx >= 0 and sess_tx >= 0:
                baseline_payload["session_rx"] = sess_rx
                baseline_payload["session_tx"] = sess_tx

            if not was_online or session_changed:
                if was_online and session_changed and stats is not None:
                    stats.close_session(drop=False)
                effective_since = (
                    online_since_epoch
                    if online_since_epoch > 0.0
                    else (time.time() - max(0.0, uptime_val))
                )
                self.online_since = effective_since
                if session_id > 0:
                    self.session_id = session_id
                self.traffic_baseline = (
                    (if_index, rx_base, tx_base)
                    if (if_index > 0 and rx_base >= 0 and tx_base >= 0)
                    else None
                )
                if stats is not None:
                    masked = account.masked_account if account is not None else ""
                    if session_id > 0 and hasattr(stats, "sync_native_session"):
                        stats.sync_native_session(
                            session_id=session_id,
                            online_since=effective_since,
                            ip=ip,
                            account=masked,
                        )
                    else:
                        stats.open_session(ip=ip, account=masked)
                attached = bool(
                    initial_attach
                    or (
                        first_status
                        and not getattr(self, "_fresh_login_requested", False)
                        and uptime_val >= 3.0
                    )
                )
                self._fresh_login_requested = False
                ev_payload = {"attached": attached, "session_id": session_id, **baseline_payload}
                self._emit("online", f"已上线，IP {ip}" if ip else "已上线", payload=ev_payload)
                return

            if session_id > 0:
                self.session_id = session_id
            if (
                if_index > 0
                and rx_base >= 0
                and tx_base >= 0
                and getattr(self, "traffic_baseline", None) is None
            ):
                self.traffic_baseline = (if_index, rx_base, tx_base)
                self._emit("traffic_sync", "", payload=baseline_payload)
        else:
            if was_online and stats is not None:
                drop = self.last_reason not in ("user_logout", "stopped")
                stats.close_session(drop=drop)
            self.online_since = None
            self.traffic_baseline = None
            self.core_session_rx = 0
            self.core_session_tx = 0

        reason_changed = state == EngineState.FATAL and self.last_reason != self._announced_reason
        if state != previous_state or reason_changed:
            if state == EngineState.FATAL:
                reason = self.last_reason
                self._emit(
                    "fatal",
                    REASON_TEXT.get(reason) or "核心已经停下来，但没有说明原因",
                    advice=REASON_ADVICE.get(reason, ""),
                    detail=f"core last_reason={reason or 'unknown'}",
                )
                self._announced_reason = reason
            elif state != previous_state:
                self._emit("state", self._state_text(state))

    def _drain_logs(self) -> None:
        """Forward new entries from the daemon's ring buffer into our log bus."""
        cursor = getattr(self, "_log_cursor", 0)
        query = f"/logs?since={cursor}" if cursor > 0 else "/logs"
        payload = _request(self.api_port, query, config_path=self.config_path)
        if not payload:
            return
        lines = payload.get("lines") or []
        next_seq = payload.get("next_seq")
        if next_seq is not None:
            try:
                next_val = int(next_seq)
            except (TypeError, ValueError):
                next_val = cursor
            if next_val < cursor:
                # Daemon restarted; re-sync from scratch on next poll.
                self._log_cursor = 0
                lines = []
            else:
                self._log_cursor = next_val
            new_lines = lines
        else:
            # Compatibility with older daemon response without next_seq.
            if len(lines) < self._last_log_count:
                self._last_log_count = 0
            new_lines = lines[self._last_log_count:]
            self._last_log_count = len(lines)

        for line in new_lines:
            stripped = line.lstrip()
            if stripped.startswith("packet "):
                self.log.packet_line(stripped[len("packet "):].strip())
                continue
            lowered = line.lower()
            if any(token in lowered for token in _CORE_FAILURE_TOKENS):
                self.log.warning("core: %s", line)
            else:
                self.log.debug("core: %s", line)

    def reload_config(self) -> bool:
        """Tell the daemon to hot-reload live policy switches (protocol_hex, reconnect)."""
        payload = _request(
            self.api_port, "/reload", method="POST", timeout=2.0, config_path=self.config_path
        )
        return bool(payload and payload.get("ok"))

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
    def _emit(
        self,
        kind: str,
        message: str = "",
        *,
        detail: str = "",
        advice: str = "",
        ip: str = "",
        payload: dict | None = None,
    ) -> None:
        event = EngineEvent(
            kind=kind,
            state=self.state,
            message=message,
            ip=ip or self.ip,
            advice=advice,
            detail=detail,
            payload=dict(payload or {}),
        )
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                self.log.debug("引擎事件监听器抛错", exc_info=True)
