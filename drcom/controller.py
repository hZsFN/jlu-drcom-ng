"""Application controller — the single seam shared by the GUI and the CLI.

Owns every long-lived component (config, log bus, stats, engine, notifier,
probe, traffic meter, status API) and exposes a small, thread-safe command
surface: :meth:`request_login`, :meth:`request_logout`, :meth:`request_reconnect`.

Both front-ends subscribe to :meth:`add_listener <Controller.add_listener>` to
receive :class:`~drcom.engine.EngineEvent` objects plus periodic "tick" events,
so neither has to poll the engine directly.
"""

from __future__ import annotations

import platform
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import __version__
from .config import (
    Account,
    AppConfig,
    ConfigStore,
    default_data_dir,
    migrate_legacy_data_dir,
)
from .engine import AuthEngine, EngineEvent, EngineState, OfflineReason
from .logbus import LogBus
from .netprobe import NetworkProbe
from .netiface import get_default_route_ip
from .notify import NotificationEvent, Notifier
from . import autostart as task_autostart
from .single_instance import (
    autostart_command,
    disable_autostart,
    enable_autostart,
    is_autostart_enabled,
)
from .stats import StatsStore
from .statusapi import StatusServer, StatusWriter, build_status_payload
from .traffic import TrafficMeter

__all__ = ["AppController", "ControllerEvent"]

#: How often the controller ticks (traffic sample, stats checkpoint, status file).
TICK_INTERVAL = 1.0

#: Lines taken from the tail of each on-disk log when exporting.  Six rotated
#: files at 8 MB would make a 50 MB attachment; the tail is what diagnoses.
EXPORT_TAIL_LINES = 4000


def _tail_lines(path: Path, limit: int) -> list[str]:
    """Last *limit* lines of *path*, tolerating a file another thread is writing."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return [line.rstrip("\n") for line in deque(handle, maxlen=limit)]
    except OSError as exc:
        return [f"（无法读取 {path.name}：{exc}）"]


@dataclass
class ControllerEvent:
    """Either an engine event or a periodic tick."""

    kind: str
    engine_event: EngineEvent | None = None
    payload: dict = field(default_factory=dict)


Listener = Callable[[ControllerEvent], None]


class AppController:
    """Owns the components and mediates between them."""

    version = __version__

    def __init__(
        self,
        *,
        data_dir: Path | None = None,
        log_level: str | None = None,
        log_to_file: bool = True,
    ) -> None:
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # Pick up a pre-rename data directory the first time we run, so an
        # existing install keeps its account and encrypted password.
        self.migration_note = migrate_legacy_data_dir(self.data_dir)

        self.store = ConfigStore(self.data_dir)
        self.config: AppConfig = self.store.load()
        if log_level:
            self.config.logging.level = log_level

        self.log = LogBus(
            log_dir=self.data_dir / "logs",
            level=self.config.logging.level,
            mask_accounts=self.config.logging.mask_accounts,
            protocol_hex=self.config.logging.protocol_hex,
            protocol_hex_unredacted=self.config.logging.protocol_hex_unredacted,
            keep_days=self.config.logging.keep_days,
            max_file_mb=self.config.logging.max_file_mb,
            file_logging=log_to_file,
        )
        if self.migration_note:
            self.log.info(self.migration_note)
        self.stats = StatsStore(self.data_dir / "stats.json")
        self.notifier = Notifier(self.config.notify, self.log)
        self.traffic = TrafficMeter(
            server=self.config.auth.server, port=self.config.auth.port, log=self.log
        )
        self.probe = NetworkProbe(self.config.probe, self.log, on_result=self._on_probe_result)
        self.probe.set_online_predicate(lambda: bool(self.engine and self.engine.is_online))
        self.status_writer = StatusWriter(
            path=Path(self.config.api.status_file_path)
            if self.config.api.status_file_path
            else self.data_dir / "status.json",
            enabled=self.config.api.status_file,
        )
        self.api = StatusServer(
            self, host=self.config.api.host, port=self.config.api.port, log=self.log
        )

        self.engine: AuthEngine | None = None
        #: Whether a notification-area icon is wanted.  Only the native core
        #: reads this (it owns the tray); the Python engine's tray lives in the
        #: UI, which gets its own flag.  ``main.py`` sets it from ``--no-tray``.
        self.enable_tray = True
        self.account: Account | None = None
        self.local_ip = ""
        self.last_error = ""
        self.last_advice = ""
        self.last_diagnosis_text = ""
        self.last_diagnosis_kind = ""

        self._listeners: list[Listener] = []
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._tick_thread: threading.Thread | None = None
        self._session_started_at: float | None = None
        self._last_schedule_check = ""
        self._shutdown_done = False
        self._session_begun = False

        self._last_traffic_at: float = 0.0
        self._last_snapshot = None
        self._last_route_check_at: float = 0.0
        self._last_route_key: tuple[str, int, str] = ("", 0, "")
        self._last_status_write_at: float = 0.0
        self._last_status_key: tuple = ()

        self.available_update = None
        self.update_error: str = ""
        self.update_checking: bool = False
        self._update_check_started_once: bool = False
        self._update_lock = threading.Lock()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def attach_running_daemon(self) -> bool:
        """Attach to a resident ``drcomd`` if it is already running an active session."""
        if not self.config.auth.native_core:
            return False
        from .remote_engine import DEFAULT_API_PORT, RemoteEngine, daemon_status

        st = daemon_status(DEFAULT_API_PORT, timeout=0.35)
        if not isinstance(st, dict):
            return False
        if st.get("state") not in (
            "online", "binding", "challenging", "authenticating", "retry_wait"
        ):
            return False
        account = self.config.active_account()
        if account is None:
            return False
        password = self.store.get_password(account.id)
        with self._lock:
            if self.engine and self.engine.is_running:
                return True
            self.account = account
            self.engine = RemoteEngine(
                config=self.config,
                account=account,
                password=password,
                log=self.log,
                stats=self.stats,
                config_path=self.data_dir / "config.json",
                enable_tray=self.enable_tray,
            )
            self.engine.subscribe(self._on_engine_event)
            self.engine.start(attach_only=True)
            if self.engine.online_since is not None:
                self._session_started_at = self.engine.online_since
        self.log.info("已接管正在运行的 drcomd 原生核心会话（不中断在线状态）")
        return True

    def begin_session(self) -> None:
        """Start the background ticker and, if configured, authenticate now.

        Owned here rather than in the UI, and called from the entry point
        *before* the window is created.  The Flet client takes about a second
        to put a window on screen on a warm machine and considerably longer on
        a cold boot, and the campus network should not have to wait for a
        window to finish drawing.

        Idempotent: once the session has begun this does nothing, so the launch
        path and the UI can both ask without authenticating twice.
        """
        if self._session_begun:
            return
        self._session_begun = True
        self.start_background()

        if self.attach_running_daemon():
            return

        if not getattr(self.config.ui, "auto_login_on_launch", True):
            return
        account = self.config.active_account()
        if account is None or not account.auto_login or not account.has_password():
            return
        ok, message = self.request_login()
        if ok:
            self.log.info("已按「启动时自动登录」开始认证")
        else:
            self.log.warning("自动登录未能开始：%s", message)

    def start_background(self) -> None:
        """Start the traffic/status ticker, probe and (optionally) the API."""
        if self._tick_thread and self._tick_thread.is_alive():
            return
        self._stop.clear()
        self._tick_thread = threading.Thread(target=self._tick_loop, name="drcom-tick", daemon=True)
        self._tick_thread.start()
        if self.config.probe.enabled:
            self.probe.start()
        if self.config.api.enabled:
            ok, message = self.api.start()
            (self.log.info if ok else self.log.warning)(message)

    def shutdown(self) -> None:
        """Stop everything cleanly; safe to call more than once.

        Deliberately never raises: this runs on the exit path, where an
        exception would leave the socket bound (and the port "busy" for the
        next launch) — exactly the failure mode spec 6.2 warns about.

        Idempotent: a signal handler and the ``finally`` block both call it.
        """
        if self._shutdown_done:
            return
        self._shutdown_done = True
        try:
            self.log.info("正在退出…")
        except Exception:
            pass
        self._stop.set()

        engine = self.engine
        if engine is not None and getattr(engine, "is_running", False):
            try:
                engine.stop()
                engine.join(3.0)
            except Exception as exc:  # pragma: no cover - defensive
                self._safe_log(f"停止认证线程失败：{exc!r}")

        for name, closer in (
            ("探测线程", self.probe.stop),
            ("状态接口", self.api.stop),
        ):
            try:
                closer()
            except Exception as exc:  # pragma: no cover - defensive
                self._safe_log(f"停止{name}失败：{exc!r}")

        try:
            if self.stats:
                self.stats.close_session(drop=False)
        except Exception:
            pass
        try:
            self._write_status()
        except Exception:
            pass
        try:
            self.store.save()
        except Exception as exc:  # pragma: no cover - defensive
            self._safe_log(f"保存配置失败：{exc!r}")
        self._safe_log("已退出")
        try:
            self.log.close()
        except Exception:
            pass

    def _safe_log(self, message: str) -> None:
        try:
            self.log.info("%s", message)
        except Exception:  # pragma: no cover
            pass

    # ------------------------------------------------------------------
    # listeners
    # ------------------------------------------------------------------
    def add_listener(self, listener: Listener) -> None:
        with self._lock:
            self._listeners.append(listener)

    def remove_listener(self, listener: Listener) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def _publish(self, event: ControllerEvent) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception as exc:  # pragma: no cover
                self.log.warning(f"控制器监听器异常：{exc!r}")

    # ------------------------------------------------------------------
    # auth commands
    # ------------------------------------------------------------------
    def request_login(self) -> tuple[bool, str]:
        """Start (or restart) authentication. Returns ``(ok, message)``."""
        with self._lock:
            account = self.config.active_account()
            if account is None or not account.account:
                return False, "还没有配置账号，请先在「账号」页填写。"
            if not account.mac:
                return False, "还没有填写 MAC 地址，请先在「账号」页填写。"

            password = self.store.get_password(account.id)
            if not password:
                return False, "还没有设置密码，请先在「账号」页填写并保存。"
            try:
                from .protocol import mac_to_bytes

                mac_to_bytes(account.mac)
            except ValueError as exc:
                return False, str(exc)

            if self.engine and self.engine.is_running:
                self.engine.stop()
                self.engine.join(2.0)

            self.account = account
            self.last_error = ""
            self.last_advice = ""
            native = self.config.auth.native_core
            if native:
                from .remote_engine import daemon_path

                if daemon_path() is None:
                    # Installed build is missing the core, or a source checkout
                    # never ran native/build.bat.  Degrade instead of failing.
                    self.log.warning("找不到 drcomd.exe，回退到内置 Python 核心")
                    native = False

            if native:
                # Native core: the window may close, the session stays up.
                from .remote_engine import RemoteEngine

                self.engine = RemoteEngine(
                    config=self.config,
                    account=account,
                    password=password,
                    log=self.log,
                    stats=self.stats,
                    config_path=self.data_dir / "config.json",
                    enable_tray=self.enable_tray,
                )
            else:
                self.engine = AuthEngine(
                    config=self.config,
                    account=account,
                    password=password,
                    log=self.log,
                    stats=self.stats,
                )
            self.engine.subscribe(self._on_engine_event)
            self.engine.start()
        self.log.info("用户请求登录")
        return True, "已开始认证"

    def request_logout(self) -> tuple[bool, str]:
        """Stop authenticating.

        Dr.COM (this variant) has no client-side logout packet; the session is
        released by the server once keepalives stop.  We make that explicit in
        the UI instead of pretending a real logout happened.
        """
        with self._lock:
            if not (self.engine and self.engine.is_running):
                return False, "当前没有在认证。"
            self.engine.stop(reason=OfflineReason.USER_LOGOUT)
            self._session_started_at = None
        self.log.info("用户请求注销：已停止保活，服务端将在超时后释放会话")
        self._publish(ControllerEvent(kind="logout_requested", payload={}))
        return True, "已注销（保活已停止）"

    def request_reconnect(self) -> tuple[bool, str]:
        """Tear the session down and start over — picks up a fresh IP."""
        self.store.save()
        with self._lock:
            engine = self.engine
            reconnect_fn = getattr(engine, "reconnect", None)
            if (
                engine is not None
                and callable(reconnect_fn)
                and engine.is_running
                and self.config.auth.native_core
            ):
                self.account = self.config.active_account()
                engine.config = self.config
                if self.account is not None:
                    engine.account = self.account
                ok = reconnect_fn()
                self.log.info("用户请求重连（原生核心重载配置）")
                return (
                    bool(ok),
                    "已通知原生核心重载配置并重连" if ok else "重连请求未能送达原生核心",
                )
            if engine and engine.is_running:
                engine.stop(reason=OfflineReason.USER_LOGOUT)
                engine.join(2.5)
        self.log.info("用户请求重连")
        time.sleep(0.2)
        return self.request_login()

    # ------------------------------------------------------------------
    # hot-reload configuration apply helpers
    # ------------------------------------------------------------------
    def apply_api_settings(self) -> tuple[bool, str]:
        """Save and apply local status API & file settings immediately."""
        self.store.save()
        self.status_writer.enabled = self.config.api.status_file
        self.status_writer.path = (
            Path(self.config.api.status_file_path)
            if self.config.api.status_file_path
            else self.data_dir / "status.json"
        )
        try:
            self.api.stop()
        except Exception:
            pass
        self.api = StatusServer(
            self, host=self.config.api.host, port=self.config.api.port, log=self.log
        )
        if self.config.api.enabled:
            ok, msg = self.api.start()
            (self.log.info if ok else self.log.warning)(msg)
            return ok, msg
        self.log.info("本地状态接口设置已热更新并关闭")
        return True, "状态接口设置已即时生效"

    def apply_probe_settings(self) -> tuple[bool, str]:
        """Save and apply latency-probe targets/interval immediately."""
        self.store.save()
        self.probe.config = self.config.probe
        self.probe.history.retain_targets(self.config.probe.targets)
        if self.config.probe.enabled:
            self.probe.start()
            self.log.info("延迟探测配置已热更新并生效")
            return True, "延迟探测设置已即时生效"
        self.probe.stop()
        self.log.info("延迟探测已停止并即时生效")
        return True, "延迟探测已关闭"

    def apply_reconnect_settings(self) -> tuple[bool, str]:
        """Save and apply reconnect/backoff/schedule settings without dropping online session."""
        self.store.save()
        engine = self.engine
        if engine is not None and getattr(engine, "is_running", False):
            engine.config = self.config
            reloader = getattr(engine, "reload_config", None)
            if callable(reloader):
                reloader()
        self.log.info("断线重连策略已热更新并即时生效")
        return True, "断线重连策略已即时生效"

    def apply_logging_settings(self) -> tuple[bool, str]:
        """Save and apply logging level/masking/hex switches live."""
        self.store.save()
        self.log.set_level(self.config.logging.level)
        self.log.mask_accounts = self.config.logging.mask_accounts
        self.log.protocol_hex = self.config.logging.protocol_hex
        self.log.protocol_hex_unredacted = self.config.logging.protocol_hex_unredacted
        engine = self.engine
        if engine is not None and getattr(engine, "is_running", False):
            reloader = getattr(engine, "reload_config", None)
            if callable(reloader):
                reloader()
        return True, "日志设置已即时生效"

    def apply_auth_settings(self, *, reconnect_if_needed: bool = True) -> tuple[bool, str]:
        """Save and apply auth/protocol/account changes live without restarting GUI."""
        self.store.save()
        self.traffic.server = self.config.auth.server
        self.traffic.port = self.config.auth.port

        from .remote_engine import RemoteEngine

        engine = self.engine
        was_running = bool(engine is not None and getattr(engine, "is_running", False))
        was_native = isinstance(engine, RemoteEngine)
        want_native = bool(self.config.auth.native_core)

        if not was_running:
            self.account = self.config.active_account()
            return True, "认证与协议设置已保存，将在下次登录时生效"

        if was_native != want_native:
            if was_native and hasattr(engine, "shutdown_daemon"):
                try:
                    engine.shutdown_daemon()
                except Exception:
                    pass
            try:
                engine.stop(reason=OfflineReason.USER_LOGOUT)
                engine.join(2.5)
            except Exception:
                pass
            if reconnect_if_needed:
                return self.request_login()
            return True, "核心模式已切换，下次登录生效"

        if reconnect_if_needed:
            return self.request_reconnect()

        engine.config = self.config
        reloader = getattr(engine, "reload_config", None)
        if callable(reloader):
            reloader()
        return True, "设置已保存并热更新"

    def probe_now(self, target: str | None = None):
        return self.probe.probe_now(target)

    # ------------------------------------------------------------------
    # engine events
    # ------------------------------------------------------------------
    def _bind_traffic_to_session(self) -> None:
        """Follow the interface the engine actually authenticated from."""
        address = ""
        info = getattr(self.engine, "socket_info", None)
        if info is not None:
            address = getattr(info, "bind_address", "") or ""
        if not address:
            address = getattr(self.engine, "ip", "") or ""
        self.traffic.set_source_address(address)
        self.probe.set_source_address(address)

    def _log_engine_event(self, event: EngineEvent) -> None:
        """Mirror engine events into the log file.

        The UI reacts to these events, so until now nothing wrote them down and
        a failure left only ``last_error`` in memory.  A user reporting "it does
        not work" could export a log that said "已开始认证" and stopped there --
        the reason was nowhere on disk.  Failures are recorded at WARNING/ERROR
        so they survive any log level the user picks.
        """
        kind = event.kind
        detail = f"（{event.detail}）" if event.detail else ""
        text = event.message or kind
        if kind == "login_failed":
            code = f" 错误码 0x{event.error_code:02X}" if event.error_code else ""
            self.log.warning("认证失败：%s%s%s", text, code, detail)
        elif kind in ("challenge_failed", "keepalive_failed", "login_timeout", "error"):
            self.log.warning("%s%s", text, detail)
        elif kind in ("bind_failed", "fatal", "core_gone"):
            self.log.error("%s%s", text, detail)
        elif kind in ("gave_up", "reconnect_disabled"):
            self.log.warning("%s%s", text, detail)
        elif kind in ("online", "healed"):
            ip_suffix = f" IP {event.ip}" if (event.ip and event.ip not in text) else ""
            self.log.info("%s%s", text, ip_suffix)
        elif kind == "retry_scheduled":
            self.log.info("%s%s", text, detail)
        elif kind == "stopped":
            self.log.info("认证已停止（%s）", event.message or "用户或核心主动停止")
        elif kind == "ip":
            self.log.debug("本机 IP：%s", event.ip or text)
        elif kind in ("state", "started"):
            self.log.debug("%s", text)
        # keepalive_ok fires every 20 s; writing it would bury everything else.

    def _sync_traffic_baseline(self, payload: dict | None) -> None:
        if not isinstance(payload, dict):
            return
        if_index = int(payload.get("traffic_if_index") or 0)
        rx_base = payload.get("traffic_rx_base")
        tx_base = payload.get("traffic_tx_base")
        sess_rx = payload.get("session_rx")
        sess_tx = payload.get("session_tx")
        if if_index > 0 or (sess_rx is not None and sess_tx is not None):
            self.traffic.sync_baseline(
                if_index=if_index,
                rx_base=rx_base,
                tx_base=tx_base,
                session_rx=sess_rx,
                session_tx=sess_tx,
            )

    def _on_engine_event(self, event: EngineEvent) -> None:
        if event.kind == "traffic_sync":
            self._sync_traffic_baseline(event.payload)
            return
        self._log_engine_event(event)
        if event.kind in ("ip", "online", "started"):
            self._bind_traffic_to_session()
        if event.kind == "login_failed":
            self.last_error = event.message
            self.last_advice = event.advice
            self.stats.note_login_failure()
            self.notifier.notify(
                NotificationEvent(
                    kind="login_failed",
                    title="校园网认证失败",
                    body=f"{event.message}",
                    detail=event.advice,
                    error_code=event.error_code,
                )
            )
        elif event.kind == "online":
            self.last_error = ""
            self.last_advice = ""
            self._sync_traffic_baseline(event.payload)
            self._session_started_at = getattr(self.engine, "online_since", None) or time.time()
            attached = bool((event.payload or {}).get("attached"))
            if not attached:
                self.notifier.notify(
                    NotificationEvent(
                        kind="online",
                        title="校园网已连接",
                        body=f"IP {event.ip}",
                        ip=event.ip,
                    )
                )
            self.schedule_update_check()
        elif event.kind in ("bind_failed",):
            self.last_error = event.message
            self.last_diagnosis_text = event.detail
            self.last_diagnosis_kind = str(event.payload.get("kind", ""))
            self.notifier.notify(
                NotificationEvent(
                    kind="bind_failed",
                    title="端口被占用/保留",
                    body=event.message,
                    detail=event.detail,
                )
            )
        elif event.kind in ("fatal", "core_gone"):
            self.last_error = event.message
            self.last_advice = event.advice
            # A tray-menu quit is the user getting rid of the program, not a
            # failure to report back to them: the window is already closing.
            deliberate = event.kind == "core_gone" and event.payload.get("exit_code") == 0
            if not deliberate:
                self.notifier.notify(
                    NotificationEvent(
                        kind="error",
                        title="原生核心已退出" if event.kind == "core_gone" else "认证被中止，需要处理",
                        body=event.message,
                        detail=event.detail or event.advice,
                        error_code=event.error_code,
                    )
                )
        elif event.kind in ("challenge_failed", "keepalive_failed", "gave_up"):
            self.last_error = event.message
            if event.detail:
                self.last_diagnosis_text = event.detail
            if event.kind == "gave_up":
                self.notifier.notify(
                    NotificationEvent(
                        kind="error",
                        title="已暂停自动重连",
                        body=event.message,
                        detail=event.detail,
                    )
                )
        elif event.kind == "stopped":
            self._session_started_at = None

        self._write_status()
        self._publish(ControllerEvent(kind="engine", engine_event=event))

    def _on_probe_result(self, result) -> None:
        self._publish(
            ControllerEvent(kind="probe", payload={"target": result.target, "ok": result.ok, "rtt": result.rtt_ms})
        )

    # ------------------------------------------------------------------
    # ticker
    # ------------------------------------------------------------------
    def _tick_loop(self) -> None:
        while not self._stop.wait(TICK_INTERVAL):
            try:
                self._tick()
            except Exception as exc:  # pragma: no cover - keep the ticker alive
                self.log.warning(f"定时任务异常：{exc!r}")

    def _tick(self) -> None:
        now = time.monotonic()
        snapshot = self._last_snapshot
        if self.config.traffic.enabled:
            interval = max(0.5, float(getattr(self.config.traffic, "interval", 1.0) or 1.0))
            if self._last_traffic_at == 0.0 or (now - self._last_traffic_at) >= interval:
                snapshot = self.traffic.sample()
                self._last_snapshot = snapshot
                self._last_traffic_at = now
        else:
            snapshot = None
            self._last_snapshot = None

        if self.engine and self.engine.is_online:
            self.stats.checkpoint()

        self._check_schedule()

        state_val = self.engine.state.value if self.engine else "idle"
        attempt_val = getattr(self.engine, "attempt", 0) if self.engine else 0
        ip_val = getattr(self.engine, "ip", "") if self.engine else ""
        route_key = (state_val, attempt_val, ip_val)
        if (
            not self.local_ip
            or route_key != self._last_route_key
            or (now - self._last_route_check_at) >= 15.0
        ):
            self.local_ip = get_default_route_ip(self.config.auth.server, self.config.auth.port)
            self._last_route_key = route_key
            self._last_route_check_at = now

        status_key = (
            state_val,
            ip_val,
            self.last_error,
            getattr(self.account, "id", ""),
        )
        if status_key != self._last_status_key or (now - self._last_status_write_at) >= 5.0:
            self._write_status(snapshot=snapshot)
            self._last_status_key = status_key
            self._last_status_write_at = now

        self._publish(
            ControllerEvent(
                kind="tick",
                payload={
                    "traffic": snapshot,
                    "uptime": self.engine.uptime_seconds() if self.engine else 0.0,
                    "state": state_val,
                },
            )
        )

    def _check_schedule(self) -> None:
        """Reconnect at configured clock times, or after a max session length."""
        cfg = self.config.schedule
        if not cfg.enabled:
            return
        now = time.localtime()
        stamp = time.strftime("%Y-%m-%d %H:%M", now)

        if cfg.max_session_minutes > 0 and self.engine and self.engine.is_online:
            if self.engine.uptime_seconds() > cfg.max_session_minutes * 60:
                self.log.info("会话已超过 %d 分钟，按计划重连以更换 IP", cfg.max_session_minutes)
                self.request_reconnect()
                return

        if stamp == self._last_schedule_check:
            return
        current = time.strftime("%H:%M", now)
        if current in (cfg.daily_times or []):
            self._last_schedule_check = stamp
            self.log.info("到达计划重连时间 %s", current)
            self.request_reconnect()

    def _write_status(self, *, snapshot=None) -> None:
        payload = build_status_payload(self)
        self.status_writer.write(payload)
        return payload

    # ------------------------------------------------------------------
    # diagnostics / UI helpers
    # ------------------------------------------------------------------
    def diagnostics(self) -> dict:
        """Everything needed to explain "why is it not working"."""
        from .binding import (
            detect_conflict_suspects,
            find_free_nearby,
            probe_bind,
            read_excluded_port_ranges,
            scan_port_window,
        )
        from .netiface import get_default_route_ip as get_local_ip
        from .netiface import list_interfaces

        port = self.config.auth.bind_port

        # If *we* are holding the port right now, do not probe it: on Windows a
        # second SO_REUSEADDR bind takes over delivery for that address, so a
        # diagnostic scan would silently swallow the next keepalive reply and
        # cause a phantom reconnect.  Report it as ours instead.
        ours = getattr(self.engine, "socket_info", None)
        held_native = False
        if ours is None and getattr(self.config.auth, "native_core", False):
            from .remote_engine import DEFAULT_API_PORT, daemon_status

            st = daemon_status(getattr(self.engine, "api_port", None) or DEFAULT_API_PORT, timeout=0.3)
            if isinstance(st, dict) and st.get("state") in (
                "online", "binding", "challenging", "authenticating", "retry_wait"
            ):
                held_native = True
        if (ours is not None and getattr(ours, "local_port", None) == port) or held_native:
            excluded_now = read_excluded_port_ranges()
            return {
                "bind_port": port,
                "bind_free": True,
                "bind_error_code": None,
                "held_by_this_process": True,
                "blocked_range": None,
                "blocked_ports_nearby": [],
                "excluded_ranges": [list(r) for r in excluded_now],
                "excluded_covers_port": any(lo <= port <= hi for lo, hi in excluded_now),
                "suspects": [],
                "free_alternatives": [],
                "local_ip": get_local_ip(self.config.auth.server, self.config.auth.port),
                "interfaces": [],
                "data_dir": str(self.data_dir),
                "log_file": str(self.log.file_path) if self.log.file_path else "",
                "note": "端口由本程序自己的会话占用，已跳过探测以免影响保活。",
                "version": self.version,
            }

        probe_code = probe_bind(port, self.config.auth.bind_address)
        scan = scan_port_window(port, radius=200) if probe_code is not None else None
        excluded = read_excluded_port_ranges()

        return {
            "bind_port": port,
            "bind_free": probe_code is None,
            "bind_error_code": probe_code,
            "held_by_this_process": False,
            "blocked_range": scan.range_containing(port) if scan else None,
            "blocked_ports_nearby": sorted(p for p in (scan.blocked if scan else {}) if abs(p - port) <= 64),
            "excluded_ranges": [list(r) for r in excluded],
            "excluded_covers_port": any(lo <= port <= hi for lo, hi in excluded),
            "suspects": detect_conflict_suspects(),
            "free_alternatives": find_free_nearby(port, limit=8) if probe_code is not None else [],
            "local_ip": get_default_route_ip(self.config.auth.server, self.config.auth.port),
            "interfaces": [
                {
                    "name": i.label,
                    "kind": i.kind,
                    "up": i.is_up,
                    "mac": i.mac,
                    "bytes_in": i.bytes_in,
                    "bytes_out": i.bytes_out,
                }
                for i in list_interfaces()
                if i.if_type != 24 and (i.is_up or i.mac)
            ][:24],
            "data_dir": str(self.data_dir),
            "log_file": str(self.log.file_path) if self.log.file_path else "",
            "single_instance_only": True,
            "version": self.version,
        }

    def masked_credentials(self) -> dict:
        acc = self.config.active_account()
        if acc is None:
            return {"account": "(未设置)", "mac": "(未设置)", "label": "", "has_password": False}
        return {
            "account": acc.masked_account,
            "mac": acc.mac or "(未设置)",
            "label": acc.label,
            "has_password": acc.has_password(),
        }

    # -- settings helpers used by the UI --------------------------------
    def save_password(self, password: str) -> tuple[bool, str]:
        acc = self.config.active_account()
        if acc is None:
            return False, "请先填写账号"
        degraded = self.store.set_password(acc.id, password)
        self.store.save()
        note = self.store.password_backend_note(acc.id)
        if degraded and note:
            return True, note
        return True, f"密码已加密保存（{self.store.password_backend_note(acc.id) or 'DPAPI'}）"

    def set_autostart(self, enabled: bool) -> tuple[bool, str]:
        """Turn boot startup on or off, preferring native OS scheduler mechanisms."""
        if sys.platform != "win32":
            if enabled:
                ok = False
                message = ""
                if task_autostart.systemd_available():
                    ok, message = task_autostart.install_systemd_service(minimized=True)
                    if ok and is_autostart_enabled():
                        disable_autostart()
                        message = f"{message}（已移除旧的 XDG autostart，避免重复启动）"
                if not ok:
                    fallback_ok, fallback_message = enable_autostart(minimized=True)
                    if fallback_ok:
                        ok = True
                        message = fallback_message if not message else f"{fallback_message}（systemd 不可用：{message}）"
                    else:
                        message = fallback_message
            else:
                task_autostart.remove_systemd_service()
                ok, message = disable_autostart()
            if ok:
                self.config.ui.autostart = enabled
                self.store.save()
            return ok, message

        if enabled:
            ok, message = task_autostart.install_current(minimized=True)
            if ok:
                if is_autostart_enabled():
                    disable_autostart()
                    message = f"{message}（已移除旧的注册表自启，避免开机拉起两份）"
            else:
                fallback_ok, fallback_message = enable_autostart(minimized=True)
                if fallback_ok:
                    ok, message = True, f"{fallback_message}（计划任务注册失败：{message}）"
        else:
            task_autostart.remove_task()
            ok, message = disable_autostart()
        if ok:
            self.config.ui.autostart = enabled
            self.store.save()
        return ok, message

    def autostart_enabled(self) -> bool:
        try:
            if sys.platform != "win32":
                return bool(task_autostart.systemd_service_exists() or is_autostart_enabled())
            return bool(task_autostart.task_exists() or is_autostart_enabled())
        except Exception:
            return False

    @property
    def autostart_cmd(self) -> str:
        try:
            if sys.platform != "win32":
                if task_autostart.systemd_service_exists():
                    return task_autostart.systemd_service_command()
            elif task_autostart.task_exists():
                return task_autostart.task_command()
        except Exception:
            pass
        return autostart_command()

    # ------------------------------------------------------------------
    # GitHub Releases auto-update
    # ------------------------------------------------------------------
    def schedule_update_check(self) -> None:
        """Run a background update check once per launch after auth goes online."""
        cfg = getattr(self.config, "update", None)
        if cfg is None or not cfg.auto_check or self._update_check_started_once:
            return
        interval_secs = max(1, int(cfg.check_interval_hours or 24)) * 3600
        if cfg.last_check_at and (time.time() - cfg.last_check_at) < interval_secs:
            return
        self._update_check_started_once = True
        self.check_for_updates(manual=False, background=True)

    def check_for_updates(
        self,
        *,
        manual: bool = False,
        background: bool = True,
    ) -> tuple[bool, object | None, str]:
        """Check GitHub Releases for a newer version."""
        from . import updater

        def _worker() -> tuple[bool, object | None, str]:
            with self._update_lock:
                self.update_checking = True
            has_update = False
            info = None
            err = ""
            try:
                has_update, info = updater.check_latest_release(
                    current_version=self.version,
                    mirror_prefix=self.config.update.mirror_prefix,
                )
                self.config.update.last_check_at = time.time()
                self.store.save()
                if has_update and (manual or info.version != self.config.update.skipped_version):
                    self.available_update = info
                    self.update_error = ""
                    self._publish(
                        ControllerEvent(
                            kind="update_available",
                            payload={"info": info, "manual": manual},
                        )
                    )
                else:
                    if not has_update:
                        self.available_update = None
                    self.update_error = ""
                    if manual:
                        self._publish(
                            ControllerEvent(
                                kind="update_checked",
                                payload={"has_update": has_update, "info": info, "error": ""},
                            )
                        )
            except Exception as exc:
                err = str(exc) or repr(exc)
                self.update_error = err
                self.log.debug("检查更新失败：%s", err)
                if manual:
                    self._publish(
                        ControllerEvent(
                            kind="update_checked",
                            payload={"has_update": False, "info": None, "error": err},
                        )
                    )
            finally:
                with self._update_lock:
                    self.update_checking = False
            return has_update, info, err

        if background:
            thread = threading.Thread(
                target=_worker, name="drcom-update-check", daemon=True
            )
            thread.start()
            return False, None, ""
        return _worker()

    def skip_update_version(self, version: str) -> None:
        self.config.update.skipped_version = version
        self.available_update = None
        self.store.save()

    def download_and_apply_update(
        self,
        info,
        *,
        progress_cb=None,
        restart: bool = True,
    ) -> dict:
        """Download matching release asset, verify SHA-256, and apply update."""
        from . import updater

        asset = updater.select_asset(info)
        if asset is None:
            raise ValueError("当前系统架构在最新 Release 中没有匹配的安装包")
        updates_dir = self.data_dir / "updates"
        pkg = updater.download_and_verify(
            asset,
            updates_dir,
            mirror_prefix=self.config.update.mirror_prefix,
            progress_cb=progress_cb,
        )
        result = updater.apply_update(pkg, restart=restart)
        if result.get("should_exit"):
            native_quit = getattr(self.engine, "shutdown_daemon", None)
            if callable(native_quit):
                try:
                    native_quit()
                except Exception:
                    pass
        return result

    def export_logs(self, destination: Path | None = None) -> Path:
        """Write one shareable report: header, in-memory buffer, every log file.

        The buffer alone was not enough.  It holds the last 2000 lines, so a
        failure from an hour earlier -- or anything the native core printed
        before the window opened -- was missing from the file users attach to a
        bug report, and the report read like nothing had happened at all.
        """
        destination = Path(destination) if destination else (self.data_dir / "logs" / "export.log")
        destination.parent.mkdir(parents=True, exist_ok=True)
        log_dir = self.data_dir / "logs"

        lines: list[str] = [
            "# JLU DrCOM NG 日志导出",
            f"# 版本：{__version__}",
            f"# 导出时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"# 系统：{platform.platform()}",
            f"# Python：{platform.python_version()}",
            f"# 当前状态：{self.state.value}  在线：{'是' if self.online else '否'}",
            f"# 最后错误：{self.last_error or '（无）'}",
            "# 账号已脱敏，密码从不记录；此文件可直接发给维护者。",
            "",
        ]

        buffer = self.log.snapshot()
        lines.append(f"===== 内存缓冲（最近 {len(buffer)} 行）=====")
        for record in buffer:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.timestamp))
            lines.append(f"{stamp} {record.level:<7} {record.message}")

        for path in sorted(log_dir.glob("drcom-*.log*")):
            if path.resolve() == destination.resolve():
                continue
            lines.append("")
            lines.append(f"===== {path.name}（文件尾部 {EXPORT_TAIL_LINES} 行）=====")
            lines.extend(_tail_lines(path, EXPORT_TAIL_LINES))

        core_log = log_dir / "drcomd-core.log"
        if core_log.exists():
            lines.append("")
            lines.append(f"===== {core_log.name}（原生核心自身输出）=====")
            lines.extend(_tail_lines(core_log, EXPORT_TAIL_LINES))

        destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return destination

    @property
    def uptime(self) -> float:
        return self.engine.uptime_seconds() if self.engine else 0.0

    @property
    def online(self) -> bool:
        return bool(self.engine and self.engine.is_online)

    @property
    def state(self) -> EngineState:
        return self.engine.state if self.engine else EngineState.IDLE
