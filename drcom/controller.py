"""Application controller — the single seam shared by the GUI and the CLI.

Owns every long-lived component (config, log bus, stats, engine, notifier,
probe, traffic meter, status API) and exposes a small, thread-safe command
surface: :meth:`request_login`, :meth:`request_logout`, :meth:`request_reconnect`.

Both front-ends subscribe to :meth:`add_listener <Controller.add_listener>` to
receive :class:`~drcom.engine.EngineEvent` objects plus periodic "tick" events,
so neither has to poll the engine directly.
"""

from __future__ import annotations

import threading
import time
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

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
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
        with self._lock:
            if self.engine and self.engine.is_running:
                self.engine.stop(reason=OfflineReason.USER_LOGOUT)
                self.engine.join(2.5)
        self.log.info("用户请求重连")
        time.sleep(0.2)
        return self.request_login()

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

    def _on_engine_event(self, event: EngineEvent) -> None:
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
            self._session_started_at = time.time()
            self.notifier.notify(
                NotificationEvent(
                    kind="online",
                    title="校园网已连接",
                    body=f"IP {event.ip}",
                    ip=event.ip,
                )
            )
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
        elif event.kind == "fatal":
            self.last_error = event.message
            self.last_advice = event.advice
            self.notifier.notify(
                NotificationEvent(
                    kind="error",
                    title="认证被中止，需要处理",
                    body=event.message,
                    detail=event.advice,
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
        snapshot = None
        if self.config.traffic.enabled:
            snapshot = self.traffic.sample()

        if self.engine and self.engine.is_online:
            self.stats.checkpoint()

        self._check_schedule()

        self.local_ip = get_default_route_ip(self.config.auth.server, self.config.auth.port)
        self._write_status(snapshot=snapshot)
        self._publish(
            ControllerEvent(
                kind="tick",
                payload={
                    "traffic": snapshot,
                    "uptime": self.engine.uptime_seconds() if self.engine else 0.0,
                    "state": self.engine.state.value if self.engine else "idle",
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
        if ours is not None and getattr(ours, "local_port", None) == port:
            return {
                "bind_port": port,
                "bind_free": True,
                "bind_error_code": None,
                "held_by_this_process": True,
                "blocked_range": None,
                "blocked_ports_nearby": [],
                "excluded_ranges": [list(r) for r in read_excluded_port_ranges()],
                "excluded_covers_port": any(lo <= port <= hi for lo, hi in read_excluded_port_ranges()),
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
        ok, message = enable_autostart() if enabled else disable_autostart()
        if ok:
            self.config.ui.autostart = enabled
            self.store.save()
        return ok, message

    def autostart_enabled(self) -> bool:
        try:
            return is_autostart_enabled()
        except Exception:
            return False

    @property
    def autostart_cmd(self) -> str:
        return autostart_command()

    def export_logs(self, destination: Path | None = None) -> Path:
        destination = destination or (self.data_dir / "logs" / "export.log")
        return self.log.export(Path(destination))

    @property
    def uptime(self) -> float:
        return self.engine.uptime_seconds() if self.engine else 0.0

    @property
    def online(self) -> bool:
        return bool(self.engine and self.engine.is_online)

    @property
    def state(self) -> EngineState:
        return self.engine.state if self.engine else EngineState.IDLE
