"""The authentication engine.

Runs the whole Dr.COM exchange on its own thread so the UI never blocks, and
communicates outward through a small publish/subscribe bus of typed events.

Threading model
---------------
* one worker thread owns the UDP socket and the protocol state machine;
* sleeps go through :meth:`_sleep`, which is interruptible, so "注销" and
  "program exit" take effect immediately instead of after a 20 s keepalive
  wait;
* UI code subscribes to events and marshals them onto its own event loop.

Reconnect policy
----------------
Errors are split into *transient* (server busy, timeouts, socket hiccups) and
*fatal* (wrong password, MAC mismatch, must-use-DHCP, ...).  Transient errors
back off exponentially with jitter so the server is never hammered; fatal ones
stop the loop and wait for the user, because retrying identical credentials
forever is exactly the "疯狂重试" behaviour the brief asks us to avoid.
"""

from __future__ import annotations

import enum
import random
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from . import protocol
from .binding import BindFailure, BoundSocket, bind_udp_socket
from .config import AppConfig, Account
from .logbus import LogBus
from .stats import StatsStore

__all__ = ["AuthEngine", "EngineEvent", "EngineState", "OfflineReason"]


class EngineState(str, enum.Enum):
    """States the UI displays."""

    IDLE = "idle"
    BINDING = "binding"
    CHALLENGING = "challenging"
    AUTHENTICATING = "authenticating"
    ONLINE = "online"
    RETRY_WAIT = "retry_wait"
    FATAL = "fatal"  # needs user action; not auto-retrying
    STOPPED = "stopped"


class OfflineReason(str, enum.Enum):
    """Why we are not online."""

    USER_LOGOUT = "user_logout"
    BIND_FAILED = "bind_failed"
    CHALLENGE_FAILED = "challenge_failed"
    LOGIN_FAILED = "login_failed"
    TIMEOUT = "timeout"
    KEEPALIVE_FAILED = "keepalive_failed"
    STOPPED = "stopped"


@dataclass
class EngineEvent:
    """Everything the engine tells the outside world."""

    kind: str
    state: EngineState
    message: str = ""
    ip: str = ""
    error_code: int | None = None
    advice: str = ""
    detail: str = ""
    payload: dict = field(default_factory=dict)


Listener = Callable[[EngineEvent], None]


class AuthEngine:
    """Drives challenge → login → keepalive until stopped."""

    def __init__(
        self,
        *,
        config: AppConfig,
        account: Account,
        password: str,
        log: LogBus,
        stats: StatsStore | None = None,
    ) -> None:
        self.config = config
        self.account = account
        self._password = password
        self.log = log
        self.stats = stats

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._listeners: list[Listener] = []
        self._lock = threading.Lock()

        self.state = EngineState.IDLE
        self.reason = OfflineReason.STOPPED
        self.ip = ""
        self.auth_information = b""
        self.online_since: float | None = None
        self.attempt = 0
        self.consecutive_failures = 0
        self.last_diagnosis = None  # BindDiagnosis when a bind failed
        self.socket_info: BoundSocket | None = None

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def subscribe(self, listener: Listener) -> None:
        with self._lock:
            self._listeners.append(listener)

    def unsubscribe(self, listener: Listener) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._wake.clear()
        self._thread = threading.Thread(target=self._run_guarded, name="drcom-engine", daemon=True)
        self._thread.start()

    def stop(self, *, reason: OfflineReason = OfflineReason.USER_LOGOUT) -> None:
        """Ask the engine to finish; returns immediately."""
        self.reason = reason
        self._stop.set()
        self._wake.set()

    def join(self, timeout: float | None = 5.0) -> bool:
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def is_online(self) -> bool:
        return self.state is EngineState.ONLINE

    def uptime_seconds(self) -> float:
        return time.time() - self.online_since if self.online_since else 0.0

    # ------------------------------------------------------------------
    # event plumbing
    # ------------------------------------------------------------------
    def _emit(self, kind: str, **kwargs) -> None:
        event = EngineEvent(kind=kind, state=self.state, **kwargs)
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception as exc:  # pragma: no cover - listener bugs must not kill auth
                self.log.warning(f"事件监听器异常：{exc!r}")

    def _set_state(self, state: EngineState, message: str = "") -> None:
        self.state = state
        self._emit("state", message=message)

    # ------------------------------------------------------------------
    # sleeps
    # ------------------------------------------------------------------
    def _sleep(self, seconds: float) -> bool:
        """Interruptible sleep. Returns ``False`` when a stop was requested."""
        if self._stop.is_set():
            return False
        # _wake lets a caller force an early wake (currently unused, but keeps
        # the primitive honest for future "reconnect now" buttons).
        self._wake.wait(timeout=seconds)
        wake_requested = self._wake.is_set()
        self._wake.clear()
        return not (self._stop.is_set() or self._stop.is_set() and not wake_requested)

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------
    def _run_guarded(self) -> None:
        try:
            self._run()
        except Exception as exc:  # pragma: no cover - last-resort safety net
            self.log.error(f"认证线程异常终止：{exc!r}", exc_info=True)
            self.reason = OfflineReason.STOPPED
            self._set_state(EngineState.FATAL, f"内部错误：{exc!r}")
            self._emit("error", message=f"认证线程内部错误：{exc!r}")
        finally:
            if self.online_since and self.stats:
                self.stats.close_session()
            self.online_since = None
            if self.state not in (EngineState.FATAL,):
                self._set_state(EngineState.STOPPED, "已停止")

    def _run(self) -> None:
        self.log.info(
            "启动认证：账号 %s，服务器 %s:%d，绑定 %s:%d",
            self.account.masked_account,
            self.config.auth.server,
            self.config.auth.port,
            self.config.auth.bind_address,
            self.config.auth.bind_port,
        )
        self._emit("started", message="认证线程已启动")

        while not self._stop.is_set():
            ok = self._session()
            if self._stop.is_set():
                break
            if ok == "fatal":
                self._set_state(EngineState.FATAL, "需要人工处理")
                return
            if not self._reconnect_allowed():
                self._set_state(EngineState.FATAL, "自动重连已关闭")
                return

            delay = self._next_backoff()
            self._set_state(
                EngineState.RETRY_WAIT,
                f"{delay:.0f} 秒后重试（第 {self.consecutive_failures} 次失败）",
            )
            self._emit("retry_scheduled", message=f"{delay:.0f} 秒后重试", payload={"delay": delay})
            if not self._sleep(delay):
                break

        self.reason = self.reason or OfflineReason.USER_LOGOUT
        self._emit("stopped", message="认证已停止")

    # -- bind-address fallback -------------------------------------------
    def _bind_candidates(self) -> list[str]:
        """Bind addresses to try, in order.

        When several adapters are up, binding ``0.0.0.0`` normally *succeeds* but
        the kernel then picks the route by metric.  If a VPN, a hotspot or a
        Wi-Fi link has a lower metric than the campus Ethernet, the challenge
        packet leaves through the wrong interface and the server simply never
        answers.  That looks like "the server is down" but is really "I asked
        from the wrong address", so we retry bound to each concrete local
        address before giving up.
        """
        configured = (self.config.auth.bind_address or "").strip()
        if configured and configured not in ("0.0.0.0", "::", "*"):
            return [configured]
        if not getattr(self.config.auth, "auto_interface_fallback", True):
            return [configured or "0.0.0.0"]

        candidates = [configured or "0.0.0.0"]
        try:
            from .netiface import _interface_addresses, list_interfaces, pick_relevant_interface

            preferred = pick_relevant_interface(
                server=self.config.auth.server, port=self.config.auth.port
            )
            addresses = _interface_addresses()
            ordered: list[str] = []
            if preferred is not None:
                ordered.extend(addresses.get(preferred.index, ()))
            for iface in list_interfaces():
                if iface.if_type == 24:  # loopback
                    continue
                ordered.extend(addresses.get(iface.index, ()))
            for address in ordered:
                if address in candidates or address.startswith(("127.", "169.254.")):
                    continue
                candidates.append(address)
        except Exception as exc:  # pragma: no cover - never block authentication
            self.log.debug(f"枚举本机地址失败，跳过接口回退：{exc!r}")
        return candidates

    # -- one full session attempt ---------------------------------------
    def _session(self) -> str:
        """Run one bind/challenge/login/keepalive session.

        Returns ``"ok"``, ``"transient"`` or ``"fatal"``.
        """
        candidates = self._bind_candidates()
        bind_failure: BindFailure | None = None

        for index, address in enumerate(candidates):
            if self._stop.is_set():
                return "transient"
            if index:
                self.log.warning(
                    "改用绑定地址 %s 重试（上一次在 %s 上收不到认证服务器响应）",
                    address,
                    candidates[index - 1],
                )

            self._set_state(EngineState.BINDING, f"正在绑定 {address}")
            try:
                bound = bind_udp_socket(
                    self.config.auth.bind_port,
                    address=address,
                    timeout_ms=self.config.auth.timeout_ms,
                    allow_alternate_port=getattr(self.config.auth, "allow_alternate_port", False),
                )
            except BindFailure as exc:
                bind_failure = exc
                if index == 0:
                    break  # a genuine port conflict: report it, do not mask it
                self.log.warning(f"绑定 {address} 失败：{exc.diagnosis.headline}")
                continue

            self.socket_info = bound
            self.last_diagnosis = None
            for note in bound.heal_notes:
                self.log.warning(note)
            if bound.used_fallback_port:
                self._emit("healed", message="已改用备选源端口",
                           detail=bound.heal_notes[-1])
            self.log.info(
                "绑定成功：%s:%d（本地端口 %d）",
                bound.bind_address,
                bound.local_port,
                bound.local_port,
            )

            try:
                challenge = self._obtain_challenge(bound)
                if challenge is None:
                    more = index + 1 < len(candidates)
                    detail = "服务器没有在超时时间内回应，或响应格式不对。"
                    if more:
                        detail += (
                            "\n正在改用其它本机地址重试"
                            "（可能有多块网卡或 VPN 抢了默认路由）。"
                        )
                    else:
                        detail += (
                            "\n常见原因：网线未连接 / 未接入校园网 / "
                            "服务器暂时不可用 / 上联交换机端口未开放。"
                        )
                    self._emit("challenge_failed", message="获取挑战码失败", detail=detail)
                    if more:
                        continue
                    self.reason = OfflineReason.CHALLENGE_FAILED
                    self._set_state(EngineState.RETRY_WAIT, "挑战失败")
                    self.consecutive_failures += 1
                    return "transient"

                self.ip = challenge.ip
                self.log.note(f"获得挑战码，本机 IP：{challenge.ip}")
                self._emit("ip", ip=challenge.ip, message=f"本机 IP：{challenge.ip}")
                if index and bound.bind_address not in ("0.0.0.0", ""):
                    self.log.info(
                        "提示：本次是用绑定地址 %s 认证成功的。"
                        "若你常在有线/无线之间切换，"
                        "可在设置里把「绑定地址」固定为此地址以加快认证。",
                        bound.bind_address,
                    )
                return self._authenticate(bound, challenge)
            finally:
                try:
                    bound.sock.close()
                except OSError:
                    pass
                self.socket_info = None

        # Nothing could be bound at all.
        if bind_failure is not None:
            diagnosis = bind_failure.diagnosis
            self.last_diagnosis = diagnosis
            self.log.error(f"绑定端口失败：{diagnosis.headline}")
            for line in diagnosis.explanation.splitlines():
                if line.strip():
                    self.log.error(f"  {line.strip()}")
            self._set_state(EngineState.RETRY_WAIT, diagnosis.headline)
            self._emit(
                "bind_failed",
                message=diagnosis.headline,
                detail=diagnosis.to_text(),
                payload={"kind": diagnosis.kind},
            )
        self.consecutive_failures += 1
        return "transient"

    def _obtain_challenge(self, bound: BoundSocket):
        """Challenge handshake with retries; ``None`` when it never answered."""
        self._set_state(EngineState.CHALLENGING, "正在获取挑战码")
        for attempt in range(1, max(1, self.config.auth.challenge_retries) + 1):
            if self._stop.is_set():
                return None
            try:
                return self._do_challenge(bound.sock)
            except protocol.ProtocolError as exc:
                self.log.warning(f"challenge 第 {attempt} 次失败：{exc}")
            except socket.timeout:
                self.log.warning(f"challenge 第 {attempt} 次超时（服务器未响应）")
            except OSError as exc:
                self.log.warning(f"challenge 第 {attempt} 次网络错误：{exc}")
            if attempt < self.config.auth.challenge_retries and not self._sleep(0.5):
                return None
        return None

    def _authenticate(self, bound: BoundSocket, challenge) -> str:
        """Submit the login, then run the keepalive loop until something breaks."""
        sock = bound.sock

        if not self._sleep(self.config.auth.post_login_delay):
            return "transient"

        # --- login -------------------------------------------------------
        self._set_state(EngineState.AUTHENTICATING, "正在提交认证")
        try:
            packet = protocol.build_login_packet(
                self.account.account, self._password, self.account.mac, challenge.seed
            )
        except ValueError as exc:
            self.reason = OfflineReason.LOGIN_FAILED
            self._set_state(EngineState.FATAL, "配置不完整")
            self._emit("error", message=f"配置错误：{exc}")
            return "fatal"

        # Blank the account string and everything derived from the password
        # before this reaches the log file (spec 9: mask the account).
        account_end = 20 + len(self.account.account.encode("utf-8"))
        password_length = packet[313]
        password_section_end = 312 + 2 + password_length + 16
        self.log.packet(
            "tx",
            "[Login sent]",
            packet,
            redact=(
                (4, 20),                       # MD5A (unsalted digest of password)
                (20, account_end),             # account string
                (58, 80),                      # MAC-XOR + MD5B
                (314, min(password_section_end, len(packet))),  # password section
            ),
        )
        try:
            self._send(sock, packet)
            reply = self._recv_until(
                sock, interesting=lambda d: d and d[0] in (0x04, 0x05), label="[Login recv]"
            )
        except socket.timeout:
            self.log.warning("登录响应超时")
            self.reason = OfflineReason.TIMEOUT
            self._set_state(EngineState.RETRY_WAIT, "登录响应超时")
            self._emit("login_timeout", message="登录响应超时", detail="服务器未在 3 秒内回复登录请求。")
            self.consecutive_failures += 1
            return "transient"
        except OSError as exc:
            self.log.warning(f"发送登录包失败：{exc}")
            self.consecutive_failures += 1
            return "transient"

        outcome = protocol.parse_login_response(reply)
        if not outcome.success:
            code = outcome.error_code
            message = outcome.message
            advice = outcome.advice
            self.reason = OfflineReason.LOGIN_FAILED
            self.log.error(f"认证失败：{message}（错误码 0x{code:02X}）" if code else f"认证失败：{message}")
            self._emit(
                "login_failed",
                message=message,
                advice=advice,
                error_code=code,
                detail=f"服务端返回错误码 0x{code:02X}" if code else "服务端未返回错误码",
            )
            if code is not None and protocol.is_fatal_login_error(code):
                self._set_state(EngineState.FATAL, message)
                self._emit(
                    "fatal",
                    message=message,
                    advice=advice,
                    error_code=code,
                    detail="这类错误重试无用，请按提示修正配置后手动重试。",
                )
                return "fatal"
            self.consecutive_failures += 1
            self._set_state(EngineState.RETRY_WAIT, message)
            return "transient"

        # --- online ------------------------------------------------------
        self.auth_information = outcome.auth_information
        self.online_since = time.time()
        self.consecutive_failures = 0
        self.attempt += 1
        if self.stats:
            self.stats.open_session(ip=self.ip, account=self.account.masked_account)
        self.log.note(f"认证成功，已上线（IP {self.ip}）")
        self._set_state(EngineState.ONLINE, f"在线 · {self.ip}")
        self._emit(
            "online",
            ip=self.ip,
            message=f"已上线 · {self.ip}",
            payload={"auth_information": self.auth_information.hex()},
        )

        reason = self._keepalive_loop(sock, bound)
        if self.online_since:
            if self.stats:
                self.stats.close_session()
            self.online_since = None
        self.reason = reason
        return "transient"

    # -- keepalive --------------------------------------------------------
    def _keepalive_loop(self, sock: socket.socket, bound: BoundSocket) -> OfflineReason:
        """Section 4.9. Mirrors the reference control flow exactly.

        * keepalive-1 fails            → offline, reconnect
        * keepalive-1 ok, ka2 fails    → retry the pair immediately
        * keepalive-1 ok, ka2 ok       → sleep ``keepalive_interval``, repeat
        """
        keepalive_counter = 0
        first = True
        consecutive_ka2_failures = 0
        interval = self.config.auth.keepalive_interval

        while not self._stop.is_set():
            # ---- keepalive 1 ------------------------------------------
            try:
                ka1 = self._keepalive_1(sock)
            except (socket.timeout, OSError, protocol.ProtocolError) as exc:
                self.log.error(f"keepalive1 失败：{exc}")
                self._emit(
                    "keepalive_failed",
                    message="保活失败（keepalive1）",
                    detail=str(exc),
                )
                return OfflineReason.TIMEOUT

            if not self._sleep(self.config.auth.post_login_delay):
                return OfflineReason.USER_LOGOUT

            # ---- keepalive 2 ------------------------------------------
            try:
                keepalive_counter, ok = self._keepalive_2(
                    sock, keepalive_counter, first=first, ka1=ka1
                )
            except (socket.timeout, OSError, protocol.ProtocolError) as exc:
                self.log.error(f"keepalive2 失败：{exc}")
                ok = False

            first = False

            if not ok:
                consecutive_ka2_failures += 1
                self.log.warning(
                    f"keepalive2 未通过（连续 {consecutive_ka2_failures} 次），立即重试"
                )
                if consecutive_ka2_failures >= 5:
                    self._emit(
                        "keepalive_failed",
                        message="保活失败（keepalive2 连续失败）",
                        detail=f"连续 {consecutive_ka2_failures} 次 keepalive2 未通过",
                    )
                    return OfflineReason.KEEPALIVE_FAILED
                # Small guard so a pathological server cannot make us spin.
                if not self._sleep(0.5):
                    return OfflineReason.USER_LOGOUT
                continue

            consecutive_ka2_failures = 0
            self.log.debug("keepalive 正常，%ss 后进入下一轮", f"{interval:g}")
            self._emit("keepalive_ok", message="保活正常", payload={"uptime": self.uptime_seconds()})
            if not self._sleep(interval):
                return OfflineReason.USER_LOGOUT

        return OfflineReason.USER_LOGOUT

    def _keepalive_1(self, sock: socket.socket) -> protocol.Keepalive1Result:
        """Sections 4.8: 8-byte probe, then the 42-byte reply."""
        self.log.packet("tx", "[Keepalive1 sent]", protocol.KEEPALIVE1_PACKET1)
        self._send(sock, protocol.KEEPALIVE1_PACKET1)

        # Server notices (opcode 0x4D) may arrive first; ignore and keep waiting.
        raw = self._recv_until(
            sock,
            interesting=lambda d: d and d[0] != 0x4D,
            label="[Keepalive1 challenge_recv]",
            on_skip=lambda d: self.log.debug("收到服务端通知包（0x4D），忽略并继续等待"),
        )
        result = protocol.parse_keepalive1_response(raw)

        packet2 = protocol.build_keepalive1_packet2(result.seed, self.auth_information)
        self.log.packet("tx", "[Keepalive1 answer sent]", packet2)
        self._send(sock, packet2)

        reply = self._recv_until(sock, label="[Keepalive1 recv]")
        if not reply or reply[0] != 0x07:
            raise protocol.ProtocolError(
                f"keepalive1 应答首字节应为 0x07，实际 0x{reply[0]:02X}" if reply else "keepalive1 应答为空"
            )
        return result

    def _keepalive_2(
        self,
        sock: socket.socket,
        counter: int,
        *,
        first: bool,
        ka1: protocol.Keepalive1Result,
    ) -> tuple[int, bool]:
        """Sections 4.9: optional file packet, then the A → C exchange."""
        detail = ""
        if first:
            packet = protocol.build_keepalive2_packet(counter, file_packet=True)
            self.log.packet("tx", "[Keepalive2_file sent]", packet)
            self._send(sock, packet)
            counter += 1
            reply = self._recv_until(sock, label="[Keepalive2_file recv]")
            result = protocol.parse_keepalive2_response(reply, file_packet=True)
            if not result.ok:
                self.log.warning(f"file packet 响应异常：{result.detail}")
                # Not fatal: the reference tolerates a 0x28 here.
                if not reply or reply[0] != 0x07:
                    return counter, False
            else:
                self.log.debug("file packet 已确认")

        # --- A packet -----------------------------------------------------
        packet = protocol.build_keepalive2_packet(counter, pkt_type=1)
        self.log.packet("tx", "[Keepalive2_A sent]", packet)
        self._send(sock, packet)
        counter += 1
        reply = self._recv_until(sock, label="[Keepalive2_A recv]")
        result = protocol.parse_keepalive2_response(reply)
        if not result.ok:
            self.log.warning(f"keepalive2 A 响应异常：{result.detail}")
            return counter, False
        tail = result.tail

        # --- C packet -----------------------------------------------------
        packet = protocol.build_keepalive2_packet(counter, pkt_type=3, tail=tail)
        self.log.packet("tx", "[Keepalive2_C sent]", packet)
        self._send(sock, packet)
        counter += 1
        reply = self._recv_until(sock, label="[Keepalive2_D recv]")
        result = protocol.parse_keepalive2_response(reply)
        if not result.ok:
            self.log.warning(f"keepalive2 C 响应异常：{result.detail}")
            return counter, False

        detail = "ok"
        self.log.debug("keepalive2 往返完成（tail=%s）", tail.hex())
        del detail
        return counter, True

    # -- single exchange helpers -------------------------------------------
    def _do_challenge(self, sock: socket.socket) -> protocol.ChallengeResponse:
        packet = protocol.build_challenge_packet()
        self.log.packet("tx", "[Challenge sent]", packet)
        self._send(sock, packet)
        reply = self._recv_until(sock, label="[Challenge recv]")
        return protocol.parse_challenge_response(reply)

    def _send(self, sock: socket.socket, data: bytes) -> None:
        sock.sendto(data, (self.config.auth.server, self.config.auth.port))

    def _recv_until(
        self,
        sock: socket.socket,
        *,
        interesting: Callable[[bytes], bool] | None = None,
        label: str = "[recv]",
        on_skip: Callable[[bytes], None] | None = None,
        max_packets: int = 32,
    ) -> bytes:
        """Receive one datagram, skipping server notices if asked.

        The socket timeout (3 s by default) is enforced by the OS; each
        datagram restarts the wait, so a chatty server cannot starve us of
        responses but a silent one still fails fast.

        :raises socket.timeout: when nothing usable arrives in time.
        """
        for _ in range(max_packets):
            if self._stop.is_set():
                raise socket.timeout("已被用户停止")
            data, peer = sock.recvfrom(2048)
            self.log.packet("rx", label, data)
            if peer[0] != self.config.auth.server:
                self.log.warning(f"收到来自意外地址 {peer[0]}:{peer[1]} 的报文，已忽略")
                continue
            if interesting is None or interesting(data):
                return data
            if on_skip is not None:
                on_skip(data)
        raise socket.timeout("连续收到无法处理的报文")

    # -- reconnect policy --------------------------------------------------
    def _reconnect_allowed(self) -> bool:
        if not self.config.reconnect.enabled:
            self._emit("reconnect_disabled", message="自动重连已关闭，请手动登录")
            return False
        if self.consecutive_failures >= self.config.reconnect.give_up_after:
            self._emit(
                "gave_up",
                message=f"连续失败 {self.consecutive_failures} 次，已暂停自动重连",
                detail="问题多半不在网络抖动，而在配置或环境。请检查上面的提示后手动点「登录」。",
            )
            return False
        return True

    def _next_backoff(self) -> float:
        cfg = self.config.reconnect
        exponent = max(0, self.consecutive_failures - 1)
        delay = min(cfg.max_delay, cfg.min_delay * (cfg.factor**exponent))
        if cfg.jitter:
            delay *= 1.0 + random.uniform(-cfg.jitter, cfg.jitter)
        delay = max(1.0, delay)

        if cfg.quiet_hours_enabled and _in_quiet_hours(cfg.quiet_hours_start, cfg.quiet_hours_end):
            delay = max(delay, cfg.quiet_hours_delay)
            self.log.info("处于静默时段，重连间隔放宽到 %.0f 秒", delay)
        return delay


def _in_quiet_hours(start: str, end: str, *, now: time.struct_time | None = None) -> bool:
    """Whether the current local time falls in ``[start, end)``."""
    now = now or time.localtime()

    def parse(value: str) -> int | None:
        try:
            hours, minutes = value.split(":")
            return int(hours) * 60 + int(minutes)
        except (ValueError, AttributeError):
            return None

    begin, finish = parse(start), parse(end)
    if begin is None or finish is None:
        return False
    current = now.tm_hour * 60 + now.tm_min
    if begin == finish:
        return False
    if begin < finish:
        return begin <= current < finish
    # window wraps past midnight
    return current >= begin or current < finish
