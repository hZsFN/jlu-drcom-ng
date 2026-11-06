"""One pass that answers "why is it not working" without a debugger.

The port diagnostic already explains a bind failure in detail, but it starts
from the assumption that the port *is* the problem.  The failures that actually
reach the issue tracker are wider than that: an account with no password, a
laptop whose packets leave through a phone hotspot, a leftover Run key racing
the logon task, a firewall rule someone added, a core that is not running.  Each
of those has a cheap, local, non-destructive test.

Two rules shape this module:

* Nothing here needs elevation.  A client cannot ask to be an administrator just
  to be diagnosed, so every check runs as the user who reported the problem --
  and the checks that genuinely need elevation (the firewall profile state)
  report ``skip`` with the reason instead of guessing.  A wrong answer is worse
  than a missing one.
* Nothing here touches the live session.  A second ``SO_REUSEADDR`` bind on
  Windows steals delivery for that address, so probing the port we are already
  holding would silently swallow the next keepalive reply; the bind check asks
  the controller first and reports "ours" instead of probing.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "Check",
    "Report",
    "run_self_check",
    "is_elevated",
    "OK",
    "WARN",
    "FAIL",
    "SKIP",
]

IS_WINDOWS = sys.platform == "win32"

#: Outcomes, ordered by how much they want the user's attention.
OK = "ok"
WARN = "warn"
FAIL = "fail"
SKIP = "skip"

_ORDER = {FAIL: 0, WARN: 1, SKIP: 2, OK: 3}
_LABEL = {OK: "正常", WARN: "注意", FAIL: "异常", SKIP: "跳过"}


@dataclass(frozen=True)
class Check:
    """One answered question about this machine."""

    key: str
    title: str
    status: str
    detail: str
    advice: str = ""

    @property
    def label(self) -> str:
        return _LABEL.get(self.status, self.status)

    def to_line(self) -> str:
        head = f"[{self.label}] {self.title}：{self.detail}"
        return f"{head}\n          → {self.advice}" if self.advice else head


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    context: dict = field(default_factory=dict)

    def add(self, check: Check) -> None:
        self.checks.append(check)

    def of(self, key: str) -> Check | None:
        for check in self.checks:
            if check.key == key:
                return check
        return None

    def worst(self) -> str:
        if not self.checks:
            return OK
        return min((c.status for c in self.checks), key=lambda s: _ORDER.get(s, 9))

    def counts(self) -> dict[str, int]:
        out = {OK: 0, WARN: 0, FAIL: 0, SKIP: 0}
        for check in self.checks:
            out[check.status] = out.get(check.status, 0) + 1
        return out

    def headline(self) -> str:
        counts = self.counts()
        if counts[FAIL]:
            return f"发现 {counts[FAIL]} 项异常" + (f"，另有 {counts[WARN]} 项需要注意" if counts[WARN] else "")
        if counts[WARN]:
            return f"没有发现异常，但有 {counts[WARN]} 项需要注意"
        return "各项检查通过"

    def to_text(self) -> str:
        lines = [self.headline(), ""]
        lines.extend(check.to_line() for check in self.checks)
        if self.context:
            lines.append("")
            lines.extend(f"{key}：{value}" for key, value in self.context.items())
        return "\n".join(lines)


# --------------------------------------------------------------------------
# environment
# --------------------------------------------------------------------------
def is_elevated() -> bool:
    """Whether this process can already do administrator-only things."""
    try:
        if IS_WINDOWS:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        return os.geteuid() == 0
    except Exception:
        return False


def _decode(raw: bytes) -> str:
    """Decode console output without ever raising.

    ``text=True`` would decode with the locale encoding inside a reader thread,
    and ``netsh`` on a Chinese Windows emits GBK -- a single mis-decoded byte
    then kills that thread with ``UnicodeDecodeError`` and the check silently
    answers "".  Asking for bytes and decoding here keeps the failure local.
    """
    for encoding in ("utf-8", "cp936", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _run(cmd: list[str], timeout: float = 8.0) -> str:
    """Run a command hidden and return its stdout, or "" on any failure."""
    creation = 0
    if IS_WINDOWS:
        creation = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            creationflags=creation,
        )
        return _decode(result.stdout or b"")
    except (OSError, subprocess.SubprocessError):
        return ""


def _default_routes() -> list[tuple[str, str, str]]:
    """``(destination, interface address, metric)`` for every default route.

    More than one means the machine is multi-homed, and Windows sends the
    packets wherever the *lowest metric* points -- which on a laptop with a
    phone hotspot and a docked cable is frequently the hotspot.  That is not a
    hypothetical: it is the exact shape of the "获取挑战码一直转圈" report that
    started this module.
    """
    if not IS_WINDOWS:
        return []
    out = _run([
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        "Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | "
        "ForEach-Object { \"$($_.InterfaceIndex)`t$($_.NextHop)`t$($_.RouteMetric)\" }",
    ])
    routes = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split("\t")]
        if len(parts) >= 3 and parts[0].isdigit():
            routes.append((parts[0], parts[1], parts[2]))
    return routes


def _firewall_state() -> dict[str, str]:
    """Per-profile firewall state, empty when it cannot be read.

    Read through PowerShell's object model rather than ``netsh`` text: the
    profile names and the on/off words are localised, so parsing the console
    output would need a table of translations and would still be wrong on the
    next locale.  ``Get-NetFirewallProfile`` answers ``Domain/Private/Public``
    with a boolean, which no translation can break.

    A standard user gets nothing back, which is why the caller treats an empty
    answer as "skip" rather than "off".
    """
    out = _run([
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        "Get-NetFirewallProfile -ErrorAction SilentlyContinue | "
        "ForEach-Object { \"$($_.Name)`t$($_.Enabled)\" }",
    ])
    state: dict[str, str] = {}
    for line in out.splitlines():
        parts = [p.strip() for p in line.split("\t")]
        if len(parts) < 2 or not parts[0]:
            continue
        name = parts[0].lower()
        value = parts[1].strip().lower()
        if value in ("true", "false"):
            state[name] = "ON" if value == "true" else "OFF"
    return state


# --------------------------------------------------------------------------
# individual checks
# --------------------------------------------------------------------------
def _check_elevation(report: Report) -> None:
    elevated = is_elevated()
    report.context["运行权限"] = "管理员" if elevated else "标准用户"
    report.add(Check(
        "elevation",
        "运行权限",
        OK,
        "以管理员身份运行" if elevated else "以标准用户身份运行",
        "" if elevated else "标准用户下部分检查（防火墙状态）无法读取，会标为「跳过」；"
                            "需要修改路由、防火墙或计划任务时，请右键以管理员身份运行。",
    ))


def _check_account(report: Report, controller) -> None:
    account = controller.config.active_account()
    if account is None:
        report.add(Check("account", "账号配置", FAIL, "还没有配置账号", "在「账号」页填写学号、密码与 MAC 地址。"))
        return
    problems = []
    if not account.account:
        problems.append("账号为空")
    if not account.has_password():
        problems.append("没有保存密码")
    if not account.mac:
        problems.append("没有填写 MAC 地址")
    masked = account.masked_account
    if problems:
        report.add(Check("account", "账号配置", FAIL, f"{masked}：" + "、".join(problems),
                         "在「账号」页补齐后重新登录；MAC 必须是本机网卡的物理地址。"))
    else:
        report.add(Check("account", "账号配置", OK, f"{masked}，密码与 MAC 已保存",
                         "密码用 DPAPI 加密存放，日志里不会出现明文。"))


def _check_bind(report: Report, controller) -> None:
    from .binding import diagnose_bind_error, find_free_nearby, probe_bind

    auth = controller.config.auth
    port = auth.bind_port
    ours = getattr(controller.engine, "socket_info", None)
    if ours is not None and getattr(ours, "local_port", None) == port:
        report.add(Check("bind", "绑定端口", OK, f"UDP {port} 由本程序自己的会话占用",
                         "已在运行，跳过探测以免抢走保活回包。"))
        return

    code = probe_bind(port, auth.bind_address)
    if code is None:
        report.add(Check("bind", "绑定端口", OK, f"UDP {port} 可用（地址 {auth.bind_address or '0.0.0.0'}）",
                         ""))
        return

    diagnosis = diagnose_bind_error(port, code)
    alternatives = find_free_nearby(port, limit=6)
    advice = diagnosis.to_text()
    if alternatives:
        advice += "\n附近可用端口：" + "、".join(str(p) for p in alternatives)
    report.add(Check("bind", "绑定端口", FAIL, f"UDP {port} 绑定失败（错误码 {code}）", advice))


def _check_reserved(report: Report, controller) -> None:
    from .binding import read_excluded_port_ranges

    port = controller.config.auth.bind_port
    ranges = read_excluded_port_ranges()
    if not ranges:
        report.add(Check("reserved", "系统保留端口", SKIP,
                         "读不到系统的保留端口区间（需要管理员权限读取 HKLM）",
                         "以管理员身份运行可补上这一项。"))
        return
    covering = [r for r in ranges if r[0] <= port <= r[1]]
    if covering:
        report.add(Check("reserved", "系统保留端口", FAIL,
                         f"UDP {port} 落在系统保留区间 {covering[0][0]}-{covering[0][1]}",
                         "用管理员 PowerShell 执行：\n"
                         f"netsh int ipv4 set dynamicport udp start={port} num=1\n"
                         "或把「设置」里的绑定端口换到保留区间之外。"))
        return
    report.add(Check("reserved", "系统保留端口", OK,
                     f"UDP {port} 不在保留区间内（共 {len(ranges)} 段）", ""))


def _check_routes(report: Report, controller) -> None:
    from .netiface import get_default_route_ip

    auth = controller.config.auth
    local_ip = get_default_route_ip(auth.server, auth.port)
    routes = _default_routes()
    report.context["本机出口地址"] = local_ip or "（无法确定）"
    if not routes:
        report.add(Check("route", "出口网卡", SKIP, "读不到默认路由",
                         "以管理员身份运行可补上这一项。"))
        return
    if len(routes) == 1:
        report.add(Check("route", "出口网卡", OK,
                         f"只有一条默认路由（接口 {routes[0][0]}，网关 {routes[0][1]}，metric {routes[0][2]}），"
                         f"到 {auth.server} 的出口是 {local_ip or '未知'}",
                         ""))
        return
    ordered = sorted(routes, key=lambda r: int(r[2]) if r[2].isdigit() else 9999)
    listing = "\n".join(f"  接口 {idx} → 网关 {hop}（metric {metric}）" for idx, hop, metric in ordered)
    top = ordered[0]
    tunnel = _looks_like_tunnel(top[1])
    report.add(Check("route", "出口网卡", WARN,
                     f"有 {len(routes)} 条默认路由，优先走接口 {top[0]}（网关 {top[1]}，metric {top[2]}）；"
                     f"到 {auth.server} 的出口地址是 {local_ip or '未知'}",
                     "校园网认证只认有线宿舍网：手机热点、VPN、代理软件或无线网卡优先级更高时，"
                     "挑战码根本发不到服务器。"
                     + ("当前最高优先级那条看起来是代理/VPN 的虚拟网卡（网关 198.18.x.x 是典型特征）："
                        "关掉它再试，或把校园网段设为直连。"
                        if tunnel else "断开热点/VPN 后重试，或临时调高有线网卡的优先级。")
                     + f"\n{listing}"))


def _looks_like_tunnel(next_hop: str) -> bool:
    """Whether a gateway looks like a proxy/VPN virtual adapter.

    ``198.18.0.0/15`` is the benchmarking range, and that is precisely what
    Clash/mihomo TUN mode assigns to its own interface -- a gateway there means
    the traffic is being handed to a tunnel, not to the campus switch.
    """
    try:
        parts = next_hop.split(".")
        if len(parts) != 4:
            return False
        return int(parts[0]) == 198 and 18 <= int(parts[1]) <= 19
    except ValueError:
        return False


def _check_server(report: Report, controller) -> None:
    from .netiface import candidate_bind_addresses

    auth = controller.config.auth
    candidates = candidate_bind_addresses()
    report.context["绑定地址候选"] = "、".join(candidates) if candidates else "（无）"
    report.add(Check(
        "candidates", "可用绑定地址",
        OK if candidates else WARN,
        f"可用于绑定 {auth.bind_port} 的本机地址：{len(candidates)} 个",
        "" if candidates else "没有可用的 IPv4 地址：网线没插好，或者网卡被禁用。",
    ))


def _check_core(report: Report, controller) -> None:
    from .remote_engine import daemon_is_up

    engine = controller.engine
    native = getattr(engine, "api_port", None)
    if native is None:
        report.add(Check("core", "原生核心", OK, "当前使用内置引擎，无需外部核心", ""))
        return
    up = daemon_is_up(native)
    report.add(Check(
        "core", "原生核心",
        OK if up else WARN,
        f"drcomd 状态接口 {native} " + ("有响应" if up else "无响应"),
        "" if up else "核心没在跑：重新打开客户端即可拉起；若反复失败，导出日志查看 drcomd-core.log。",
    ))


def _check_autostart(report: Report, controller) -> None:
    from . import autostart as task_autostart
    from .single_instance import is_autostart_enabled

    has_task = task_autostart.task_exists()
    has_run = is_autostart_enabled()
    command = task_autostart.task_command() if has_task else ""
    report.context["计划任务"] = command or "（未注册）"

    if has_task and has_run:
        report.add(Check("autostart", "开机自启", WARN,
                         "计划任务与注册表 Run 值同时存在",
                         "两项都会在登录时拉起客户端，第二份会被单实例互斥体挡掉，"
                         "表现为开机时偶发弹窗或端口被占。在「设置」页关掉再打开一次开机自启即可，"
                         "程序会保留计划任务并清掉旧的 Run 值。"))
        return
    if has_task:
        report.add(Check("autostart", "开机自启", OK, "由计划任务拉起（比 Run 键更早）", command))
        return
    if has_run:
        report.add(Check("autostart", "开机自启", WARN, "只有注册表 Run 值（计划任务注册失败时的退路）",
                         "如果希望开机更早完成认证，在「设置」页重新打开开机自启。"))
        return
    report.add(Check("autostart", "开机自启", SKIP, "没有设置开机自启", "需要时在「设置」页打开。"))


def _check_firewall(report: Report) -> None:
    state = _firewall_state()
    if not state:
        report.add(Check("firewall", "防火墙", SKIP, "读取防火墙状态需要管理员权限",
                         "以管理员身份运行可以补上这一项；程序只需要出站 UDP，默认不受拦截。"))
        return
    off = [name for name, value in state.items() if value not in ("ON", "开启")]
    summary = "、".join(f"{name}={value}" for name, value in state.items())
    if off:
        report.add(Check("firewall", "防火墙", WARN, f"以下配置的防火墙未开启：{'、'.join(off)}", summary))
    else:
        report.add(Check("firewall", "防火墙", OK, "各配置的防火墙均开启", summary))


def _check_files(report: Report, controller) -> None:
    from .remote_engine import daemon_path

    data_dir = controller.data_dir
    writable = True
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".selfcheck-write"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        writable = False
        detail = f"数据目录不可写：{exc}"
    if writable:
        detail = f"数据目录可写：{data_dir}"
    advice = "" if writable else "换一个数据目录，或用管理员身份运行。"
    report.add(Check("files", "数据目录", OK if writable else FAIL, detail, advice))

    exe = daemon_path()
    if exe is None:
        report.add(Check("corefile", "核心文件", WARN, "找不到 drcomd.exe",
                         "安装目录可能不完整：重新安装，或确认 drcomd.exe 与主程序在同一目录。"))
    else:
        report.add(Check("corefile", "核心文件", OK, f"{exe.name}（{exe.stat().st_size:,} 字节）", ""))


def _check_log(report: Report, controller) -> None:
    path = controller.log.file_path
    report.context["日志文件"] = str(path) if path else "（未启用文件日志）"
    if not path:
        report.add(Check("log", "日志", WARN, "没有启用文件日志", "在「设置」页打开文件日志，出问题时才能导出。"))
        return
    try:
        size = Path(path).stat().st_size
    except OSError:
        report.add(Check("log", "日志", WARN, f"日志文件还不存在：{path}", "登录一次后会生成。"))
        return
    report.add(Check("log", "日志", OK, f"{path}（{size:,} 字节）", ""))


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------
def run_self_check(controller) -> Report:
    """Answer every cheap question about this machine, worst first."""
    report = Report()
    report.context["版本"] = controller.version
    report.context["认证服务器"] = f"{controller.config.auth.server}:{controller.config.auth.port}"

    _check_elevation(report)
    # The key travels with the step so a crash can still say *which* question
    # went unanswered -- a fallback entry named after the function would be
    # unfindable by `report.of("bind")` and invisible to the caller that cares.
    steps = (
        ("account", "账号配置", _check_account, True),
        ("bind", "绑定端口", _check_bind, True),
        ("reserved", "系统保留端口", _check_reserved, True),
        ("route", "出口网卡", _check_routes, True),
        ("candidates", "可用绑定地址", _check_server, True),
        ("core", "原生核心", _check_core, True),
        ("autostart", "开机自启", _check_autostart, True),
        ("firewall", "防火墙", _check_firewall, False),
        ("files", "数据目录", _check_files, True),
        ("log", "日志", _check_log, True),
    )
    for key, title, step, needs_controller in steps:
        try:
            step(report, controller) if needs_controller else step(report)
        except Exception as exc:                      # one broken probe must not hide the rest
            report.add(Check(key, title, SKIP, f"检查未能完成：{exc!r}",
                             "这一项读不到原因，请导出日志反馈。"))

    report.checks.sort(key=lambda c: _ORDER.get(c.status, 9))
    return report
