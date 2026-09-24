"""UDP socket creation, bind-failure classification and self-healing.

Motivation (spec section 6.1): on this machine the client once died with

    bind failed. Error code: 10013

``10013`` is ``WSAEACCES`` — *permission denied* — not ``10048``
(``WSAEADDRINUSE``, "port already in use").  The cause was Clash Verge's TUN
mode grabbing a random block of *system-reserved* ports (61064–63040) that
happened to cover 61440.  Those exclusions do **not** show up in
``netsh int ipv4 show excludedportrange``, and running as administrator does
not help.

So this module:

* separates 10013 from 10048 and says what each actually means;
* finds the extent of the blocked block empirically, by probing binds;
* names the usual suspects that are actually running;
* tries to heal: exclusive-address mode, retry, alternate bind address, and
  (opt-in) an alternate source port;
* returns a concrete, clickable instruction list for the user.
"""

from __future__ import annotations

import errno
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "BindDiagnosis",
    "BindKind",
    "PortScan",
    "bind_udp_socket",
    "diagnose_bind_error",
    "detect_conflict_suspects",
    "find_port_holders",
    "scan_port_window",
]

IS_WINDOWS = sys.platform == "win32"

# Windows Winsock error codes we care about.
WSAEACCES = 10013
WSAEADDRINUSE = 10048


class BindKind:
    """Classification of a bind failure."""

    RESERVED = "reserved"
    IN_USE = "in_use"
    PERMISSION = "permission"
    ADDRESS = "address"
    UNKNOWN = "unknown"


#: Processes that historically reserve port ranges on this machine via TUN
#: adapters, WinNAT, hypervisors or emulator network stacks (spec 6.1).
_KNOWN_SUSPECTS: tuple[tuple[str, str], ...] = (
    ("clash-verge", "Clash Verge（TUN 模式会随机圈占大段保留端口）"),
    ("clash", "Clash 内核"),
    ("verge-mihomo", "Clash Verge 的 mihomo 内核（TUN 模式）"),
    ("mihomo", "mihomo 内核（TUN 模式）"),
    ("PgyVisitor", "蒲公英 VPN"),
    ("winnat", "Windows 网络地址转换服务（Hyper-V/WSL/Docker 会触发）"),
    ("wsl", "WSL"),
    ("vmmem", "WSL/Hyper-V 虚拟机进程"),
    ("ldremote", "雷电模拟器"),
    ("dnplayer", "雷电模拟器"),
    ("Ld9BoxHeadless", "雷电模拟器"),
    ("vmware", "VMware"),
    ("vbox", "VirtualBox"),
    ("openvpn", "OpenVPN"),
    ("wireguard", "WireGuard"),
    ("tailscale", "Tailscale"),
    ("EasyConnect", "深信服 EasyConnect VPN"),
    ("SangForPromote", "深信服 VPN 服务"),
    ("AnyConnect", "Cisco AnyConnect"),
    ("Surge", "Surge"),
    ("sing-box", "sing-box（TUN 模式）"),
    ("v2ray", "v2ray"),
    ("xray", "Xray"),
)


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------
@dataclass
class BindDiagnosis:
    """Everything we can say about a failed bind."""

    port: int
    errno: int
    kind: str
    headline: str
    explanation: str
    advice: list[str] = field(default_factory=list)
    holders: list[str] = field(default_factory=list)
    suspects: list[str] = field(default_factory=list)
    blocked_range: tuple[int, int] | None = None
    free_alternatives: list[int] = field(default_factory=list)

    def to_text(self) -> str:
        lines = [self.headline, "", self.explanation]
        if self.blocked_range:
            lo, hi = self.blocked_range
            lines.append(
                f"\n实测：本机 {self.port} 落在被占用的连续区间 [{lo}, {hi}]"
                f"（共 {hi - lo + 1} 个端口）内。"
            )
        if self.holders:
            lines.append("\n占用该端口的进程：")
            lines.extend(f"  - {h}" for h in self.holders)
        if self.suspects:
            lines.append("\n检测到以下可能做端口保留的程序正在运行：")
            lines.extend(f"  - {s}" for s in self.suspects)
        if self.advice:
            lines.append("\n处置建议：")
            lines.extend(f"  {i}. {a}" for i, a in enumerate(self.advice, 1))
        return "\n".join(lines)


@dataclass
class PortScan:
    """Result of probing a window of ports."""

    start: int
    end: int
    blocked: dict[int, int]  # port -> errno
    free: list[int]

    @property
    def blocked_ranges(self) -> list[tuple[int, int]]:
        """Contiguous runs of blocked ports."""
        out: list[tuple[int, int]] = []
        run_start = None
        previous = None
        for port in sorted(self.blocked):
            if run_start is None:
                run_start = previous = port
                continue
            if port == previous + 1:
                previous = port
                continue
            out.append((run_start, previous))
            run_start = previous = port
        if run_start is not None:
            out.append((run_start, previous))
        return out

    def range_containing(self, port: int) -> tuple[int, int] | None:
        for lo, hi in self.blocked_ranges:
            if lo <= port <= hi:
                return (lo, hi)
        return None


# --------------------------------------------------------------------------
# Low-level probing
# --------------------------------------------------------------------------
def _error_code(exc: OSError) -> int:
    """Normalise an OSError to the platform's meaningful code."""
    if IS_WINDOWS:
        winerr = getattr(exc, "winerror", None)
        if winerr:
            return int(winerr)
    return int(exc.errno or 0)


def probe_bind(port: int, address: str = "0.0.0.0", *, reuse: bool = True) -> int | None:
    """Try to bind *port*; return ``None`` on success or the error code.

    ``SO_REUSEADDR`` is set **before** ``bind()``, as it must be (spec 6.2).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if reuse:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((address, port))
            return None
        except OSError as exc:
            return _error_code(exc)
    finally:
        sock.close()


def probe_bind_exclusive(port: int, address: str = "0.0.0.0") -> int | None:
    """Probe with ``SO_EXCLUSIVEADDRUSE`` — rejects even reuse-stolen ports."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if IS_WINDOWS and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                pass
        try:
            sock.bind((address, port))
            return None
        except OSError as exc:
            return _error_code(exc)
    finally:
        sock.close()


def scan_port_window(
    center: int,
    *,
    radius: int = 400,
    address: str = "0.0.0.0",
    max_seconds: float = 8.0,
) -> PortScan:
    """Probe ``[center-radius, center+radius]`` and report blocked ports.

    A bind probe is a few microseconds, so a few hundred ports is cheap and
    gives a *much* more accurate picture than ``netsh``, which (per spec 6.1)
    does not even list the exclusions that TUN adapters create.
    """
    start = max(1, center - radius)
    end = min(65535, center + radius)
    blocked: dict[int, int] = {}
    free: list[int] = []
    deadline = time.monotonic() + max_seconds
    for port in range(start, end + 1):
        code = probe_bind(port, address)
        if code is None:
            free.append(port)
        else:
            blocked[port] = code
        if time.monotonic() > deadline:
            end = port
            break
    return PortScan(start=start, end=end, blocked=blocked, free=free)


def find_free_nearby(center: int, *, radius: int = 64, limit: int = 12) -> list[int]:
    """Free ports just outside *center*, closest first."""
    found: list[int] = []
    for offset in range(1, radius + 1):
        for candidate in (center + offset, center - offset):
            if not 1 <= candidate <= 65535:
                continue
            if probe_bind(candidate) is None:
                found.append(candidate)
                if len(found) >= limit:
                    return found
    return found


# --------------------------------------------------------------------------
# External inspection
# --------------------------------------------------------------------------
def _run_hidden(cmd: list[str], timeout: float = 6.0) -> str:
    """Run a helper command without flashing a console window."""
    creationflags = 0x08000000 if IS_WINDOWS else 0  # CREATE_NO_WINDOW
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=creationflags,
            errors="replace",
        )
        return proc.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


def find_port_holders(port: int) -> list[str]:
    """Best-effort: which process is holding *port* (Windows)."""
    if not IS_WINDOWS:
        return []
    output = _run_hidden(["netstat", "-ano", "-p", "udp"])
    pids: set[str] = set()
    needle = f":{port}"
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0].upper().startswith("UDP") and parts[1].endswith(needle):
            pids.add(parts[-1])

    holders: list[str] = []
    for pid in pids:
        if pid in ("0", "4"):
            holders.append(f"PID {pid}（系统进程：WinNAT / 端口保留，通常看不到具体程序名）")
            continue
        out = _run_hidden(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"])
        name = out.strip().split(",")[0].strip('"') if out.strip() else "?"
        holders.append(f"PID {pid} — {name}" if name and name != "?" else f"PID {pid}")
    return holders


def detect_conflict_suspects() -> list[str]:
    """List running processes known to reserve port ranges."""
    if not IS_WINDOWS:
        return []
    output = _run_hidden(["tasklist", "/FO", "CSV", "/NH"])
    if not output:
        return []
    blob = output.lower()
    found: list[str] = []
    for needle, description in _KNOWN_SUSPECTS:
        if needle.lower() in blob and description not in found:
            found.append(description)
    return found


def read_excluded_port_ranges() -> list[tuple[int, int]]:
    """Parse ``netsh int ipv4 show excludedportrange``.

    Note: this is advisory only.  Per spec 6.1 the ranges created by TUN
    adapters do *not* appear here, which is exactly why we also probe.
    """
    if not IS_WINDOWS:
        return []
    ranges: list[tuple[int, int]] = []
    for proto in ("udp", "tcp"):
        out = _run_hidden(
            ["netsh", "int", "ipv4", "show", "excludedportrange", f"protocol={proto}"]
        )
        for line in out.splitlines():
            match = re.match(r"\s*(\d+)\s+(\d+)\s*$", line)
            if match:
                lo, hi = int(match.group(1)), int(match.group(2))
                if 0 < lo <= hi <= 65535:
                    ranges.append((lo, hi))
    return ranges


# --------------------------------------------------------------------------
# Classification + advice
# --------------------------------------------------------------------------
def _classify(code: int) -> tuple[str, str, str]:
    """Return ``(kind, headline, explanation)`` for an error code."""
    if code == WSAEACCES:
        return (
            BindKind.RESERVED,
            f"端口被独占（错误码 {WSAEACCES} = WSAEACCES，权限拒绝）",
            "注意：这不是「端口已被占用」（那是 10048）。在 Windows 上 10013 有两种成因，"
            "本程序会在下面实测区分：\n"
            "  (a) 端口落在 Windows 的**排除端口区间（excluded port range）** 里 —— "
            "代理/VPN 的 TUN 模式、WSL、Hyper-V、Docker、安卓模拟器启动时会随机圈占大段端口；\n"
            "  (b) **另一个程序以独占方式绑定了这个端口** —— Windows 的独占绑定会让后续 "
            "SO_REUSEADDR 绑定返回 10013 而不是 10048，最常见的就是原版 Dr.COM 客户端还在后台运行。",
        )
    if code == WSAEADDRINUSE:
        return (
            BindKind.IN_USE,
            f"端口已被其它程序占用（错误码 {WSAEADDRINUSE} = WSAEADDRINUSE）",
            "确实有另一个程序绑定着 61440。最常见的原因是**本程序已经在运行**"
            "（重复启动会抢同一个端口），其次是原版 Dr.COM 客户端没有退出干净。",
        )
    if code in (errno.EACCES, errno.EPERM, 13, 1):
        return (
            BindKind.PERMISSION,
            f"没有权限绑定该端口（错误码 {code}）",
            "低于 1024 的端口在类 Unix 系统上需要特权；61440 本身不需要，"
            "因此更可能是安全软件/策略拦截。",
        )
    if code in (errno.EADDRNOTAVAIL, 10049):
        return (
            BindKind.ADDRESS,
            f"绑定地址不可用（错误码 {code}）",
            "指定的本地地址不属于本机网卡。请检查「绑定地址」设置，或改回 0.0.0.0。",
        )
    return (
        BindKind.UNKNOWN,
        f"绑定失败（错误码 {code}）",
        "没有归类的绑定错误，请查看日志中的完整信息。",
    )


def diagnose_bind_error(
    port: int,
    code: int,
    *,
    address: str = "0.0.0.0",
    deep: bool = True,
) -> BindDiagnosis:
    """Turn a raw bind error code into a human-actionable diagnosis."""
    kind, headline, explanation = _classify(code)
    diagnosis = BindDiagnosis(port=port, errno=code, kind=kind, headline=headline, explanation=explanation)

    if deep and kind in (BindKind.RESERVED, BindKind.IN_USE, BindKind.UNKNOWN):
        scan = scan_port_window(port, radius=400, address=address)
        diagnosis.blocked_range = scan.range_containing(port)
        if diagnosis.blocked_range is None and scan.blocked:
            # Not contiguous, but the neighbours are telling.
            near = [p for p in scan.blocked if abs(p - port) <= 32]
            if near:
                diagnosis.blocked_range = (min(near), max(near))
        if not diagnosis.holders:
            diagnosis.holders = find_port_holders(port)
        diagnosis.suspects = detect_conflict_suspects()
        diagnosis.free_alternatives = find_free_nearby(port, limit=8)

    # --- advice ------------------------------------------------------
    # Which of the two 10013 causes is it?  A wide contiguous blocked block
    # means a TUN-created exclusion range; one or two blocked ports with a
    # named holder means somebody bound it exclusively.
    range_span = (diagnosis.blocked_range[1] - diagnosis.blocked_range[0] + 1) if diagnosis.blocked_range else 0
    held_exclusively = bool(diagnosis.holders) and range_span <= 3

    advice: list[str] = []
    if kind == BindKind.RESERVED and held_exclusively:
        diagnosis.headline = f"端口被另一个程序独占（错误码 {code}，不是 10048）"
        advice.append(
            "实测只有这一个端口不可用，并且已经查到占用它的进程（见上），"
            "所以这是「独占绑定」而不是大段端口保留。"
        )
        advice.append(
            "最常见的原因：原版 Dr.COM 客户端还在后台运行。请在任务管理器里结束它"
            "（或在托盘图标上右键退出），然后点「重试」。"
        )
        advice.append(
            "如果占用者就是本程序的另一个实例：本程序只允许单实例运行，检查系统托盘是否已有窗口。"
        )
        advice.append(
            "提示：Windows 上独占绑定导致的失败会返回 10013 而不是 10048，"
            "所以错误码本身不足以区分原因 —— 本程序用实测端口扫描来区分。"
        )
    elif kind == BindKind.RESERVED:
        if diagnosis.blocked_range:
            lo, hi = diagnosis.blocked_range
            advice.append(
                f"61440 落在实测被阻断的区间 [{lo}, {hi}]（{range_span} 个端口）；"
                "这种大段连续保留通常由 TUN/VPN 适配器创建。"
            )
        advice.append(
            "最有效的办法：先完全退出 Clash Verge / mihomo（不是只关代理开关，要退出程序），"
            "再启动本程序完成认证，最后重新打开 Clash —— 后启动的一方会自动避开已被占用的端口。"
        )
        advice.append(
            "如果必须保持 Clash 常驻：在 Clash Verge 的 TUN 设置里把 TUN 关闭，"
            "改用系统代理模式；或在 mihomo 配置里用 tun.exclude-ports / "
            "「保留端口」设置把 61440 排除。"
        )
        advice.append(
            "其它同类嫌疑程序（蒲公英 VPN、WSL/Hyper-V、Docker Desktop、Android 模拟器）"
            "也可能做同样的保留，可逐个退出后点「重试」。"
        )
        advice.append(
            "本程序还支持「备选源端口」自愈：勾选后会在 61440 被占用时改用邻近空闲端口尝试认证"
            "（多数 Dr.COM 服务器按报文源端口回包，因此通常可用；若失败请关闭此选项）。"
        )
        advice.append(
            "「用管理员身份运行」和「net stop winnat」对本问题无效 —— 请不要再浪费时间尝试。"
        )
    elif kind == BindKind.IN_USE:
        if diagnosis.holders:
            advice.append("先结束上面的进程，或用任务管理器按 PID 结束。")
        advice.append("本程序同一时间只能运行一个实例；如果你没看到窗口，检查一下系统托盘。")
        advice.append("也可以确认一下「原版 Dr.COM 客户端」是否还在后台运行，退出后重试。")
    elif kind == BindKind.ADDRESS:
        advice.append("把「绑定地址」改回 0.0.0.0（默认值）。")
    else:
        advice.append("重启本程序后再试；若持续失败请导出日志反馈。")

    diagnosis.advice = advice
    return diagnosis


# --------------------------------------------------------------------------
# The socket we actually use
# --------------------------------------------------------------------------
@dataclass
class BoundSocket:
    """A ready-to-use UDP socket plus metadata about how it was obtained."""

    sock: socket.socket
    local_port: int
    bind_address: str
    #: True when we had to deviate from the spec'd 61440
    used_fallback_port: bool = False
    heal_notes: list[str] = field(default_factory=list)


class BindFailure(Exception):
    """Raised when the socket could not be bound; carries the diagnosis."""

    def __init__(self, diagnosis: BindDiagnosis) -> None:
        self.diagnosis = diagnosis
        super().__init__(diagnosis.headline)


def _try_bind(
    port: int,
    address: str,
    *,
    exclusive: bool,
    allow_reuse: bool,
) -> tuple[socket.socket | None, int | None]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if allow_reuse:
            # MUST come before bind() — spec 6.2.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if exclusive and IS_WINDOWS and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                pass
        sock.bind((address, port))
        return sock, None
    except OSError as exc:
        code = _error_code(exc)
        sock.close()
        return None, code


def bind_udp_socket(
    port: int,
    *,
    address: str = "0.0.0.0",
    timeout_ms: int = 3000,
    allow_alternate_port: bool = False,
    alternate_candidates: int = 12,
    self_heal: bool = True,
) -> BoundSocket:
    """Bind the auth socket, self-healing where possible.

    Escalation ladder:

    1. ``SO_REUSEADDR`` + plain bind — the spec'd path.
    2. Retry a few times with a short delay (transient conflicts happen when a
       TUN adapter is starting or stopping).
    3. ``SO_EXCLUSIVEADDRUSE`` — wins the port back from a stale
       reuse-bound socket belonging to a previous run of *this* program.
    4. Bind to this machine's own interface address instead of 0.0.0.0.
    5. *Opt-in*: bind an alternate source port near 61440.

    :raises BindFailure: with a full :class:`BindDiagnosis` when nothing worked.
    """
    notes: list[str] = []
    last_code: int | None = None

    # --- 1 & 2: the spec'd path, with a couple of retries ----------------
    for attempt in range(3):
        sock, code = _try_bind(port, address, exclusive=False, allow_reuse=True)
        if sock is not None:
            _finish_socket(sock, timeout_ms)
            return BoundSocket(sock, port, address)
        last_code = code
        if attempt < 2:
            time.sleep(0.4 * (attempt + 1))

    if self_heal:
        # --- 3: take the port back from a stale reuse-bound socket --------
        sock, code = _try_bind(port, address, exclusive=True, allow_reuse=False)
        if sock is not None:
            notes.append("已用 SO_EXCLUSIVEADDRUSE 模式夺回端口（此前有残留的复用绑定）。")
            _finish_socket(sock, timeout_ms)
            return BoundSocket(sock, port, address, heal_notes=notes)
        last_code = last_code or code

        # --- 4: bind the concrete interface address -----------------------
        for local_ip in _local_ipv4_addresses():
            sock, code = _try_bind(port, local_ip, exclusive=False, allow_reuse=True)
            if sock is not None:
                notes.append(f"已改为绑定具体网卡地址 {local_ip}（0.0.0.0 绑定被拒绝）。")
                _finish_socket(sock, timeout_ms)
                return BoundSocket(sock, port, local_ip, heal_notes=notes)

    # --- 5: alternate source port (opt-in, deviates from the spec) --------
    if allow_alternate_port:
        for candidate in find_free_nearby(port, limit=alternate_candidates):
            sock, _ = _try_bind(candidate, address, exclusive=False, allow_reuse=True)
            if sock is not None:
                notes.append(
                    f"注意：已改用备选源端口 {candidate}（规范要求 {port}）。"
                    "Dr.COM 服务器一般按报文源端口回包，因此通常可用；若认证失败请关闭此自愈选项。"
                )
                _finish_socket(sock, timeout_ms)
                return BoundSocket(
                    sock, candidate, address, used_fallback_port=True, heal_notes=notes
                )

    diagnosis = diagnose_bind_error(port, last_code or 0, address=address)
    if notes:
        diagnosis.advice.insert(0, "；".join(notes))
    raise BindFailure(diagnosis)


def _finish_socket(sock: socket.socket, timeout_ms: int) -> None:
    """Apply the receive timeout, then widen the buffers a little."""
    sock.settimeout(max(0.05, timeout_ms / 1000.0))
    for option in ("SO_RCVBUF", "SO_SNDBUF"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, getattr(socket, option), 64 * 1024)
        except OSError:
            pass


def _local_ipv4_addresses() -> list[str]:
    """Non-loopback IPv4 addresses of this machine, most likely first."""
    found: list[str] = []
    try:
        import socket as _socket

        for info in _socket.getaddrinfo(_socket.gethostname(), None, _socket.AF_INET):
            ip = info[4][0]
            if ip not in found and not ip.startswith("127."):
                found.append(ip)
    except OSError:
        pass
    return found


def netsh_hint() -> str:
    """Shortcut text for the "copy this command" button."""
    if IS_WINDOWS:
        return "netsh int ipv4 show excludedportrange protocol=udp"
    return "ss -ulpn | grep 61440"


def script_path() -> Path | None:
    """Where the shipped diagnostic script lives, if it was packaged."""
    candidate = Path(__file__).resolve().parent.parent / "tools" / "port_diag.py"
    return candidate if candidate.exists() else None
