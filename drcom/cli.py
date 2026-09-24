"""Headless / CLI mode (P2).

Enough surface to drive the client from a script, a scheduled task or another
program:

    python main.py --cli login          # authenticate and stay in the foreground
    python main.py --cli status         # one-line status, exit code reflects state
    python main.py --cli status --json  # machine-readable
    python main.py --cli diag           # port-conflict diagnosis
    python main.py --cli set --account 2023xxxx --mac AA:BB:.. --password ***
    python main.py --cli probe          # ping the probe targets
    python main.py --cli export-logs    # dump the buffered log

Exit codes: 0 = online, 1 = offline/failure, 2 = usage error, 3 = fatal.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path

from .config import Account
from .controller import AppController
from .engine import EngineState
from .stats import StatsStore
from .statusapi import build_status_payload

__all__ = ["build_parser", "run_cli"]

_EXIT_ONLINE = 0
_EXIT_OFFLINE = 1
_EXIT_USAGE = 2
_EXIT_FATAL = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="drcom-jlu",
        description="吉林大学 Dr.COM 校园网认证客户端",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  drcom-jlu --cli login            前台登录并保持在线\n"
            "  drcom-jlu --cli status --json    输出 JSON 状态\n"
            "  drcom-jlu --cli diag             诊断 61440 端口被谁占了\n"
            "  drcom-jlu --cli set --account 2023xxxx --mac AA:BB:CC:DD:EE:FF\n"
        ),
    )
    parser.add_argument("--data-dir", type=Path, help="配置文件/日志目录")
    parser.add_argument("--log-level", default=None, help="DEBUG / INFO / WARNING")
    parser.add_argument("--cli", metavar="COMMAND", help="以命令行模式运行（见下方命令）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出（status/diag/probe）")
    parser.add_argument("--verbose", action="store_true", help="打印协议交互日志")

    # `set` subcommand options
    parser.add_argument("--account", help="学号/账号")
    parser.add_argument("--mac", help="网卡 MAC，形如 AA:BB:CC:DD:EE:FF")
    parser.add_argument("--password", help="密码（写入时立即加密，不会明文落盘）")
    parser.add_argument("--label", help="账号备注名")
    parser.add_argument("--auto-login", action="store_true", help="启动时自动登录该账号")

    parser.add_argument("--apply", action="store_true", help="应用并保存配置（ai 自动设置时使用）")
    parser.add_argument("--no-save", action="store_true", help="不写回配置文件")
    return parser


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def _cmd_status(controller: AppController, args) -> int:
    payload = build_status_payload(controller)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        state = payload["state"]
        if payload["online"]:
            uptime = payload["uptime_seconds"]
            print(
                f"在线 | IP {payload['ip']} | 已持续 {_human(uptime)} | "
                f"账号 {payload['account']}"
            )
        else:
            suffix = f" | {payload['last_error']}" if payload.get("last_error") else ""
            print(f"离线（{state}）{suffix}")
    return _EXIT_ONLINE if payload["online"] else _EXIT_OFFLINE


def _cmd_diag(controller: AppController, args) -> int:
    data = controller.diagnostics()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return _EXIT_ONLINE if data["bind_free"] else _EXIT_OFFLINE

    port = data["bind_port"]
    print(f"目标端口        : {port}")
    print(f"能否绑定        : {'可以' if data['bind_free'] else '不可以'}")
    if data["bind_error_code"]:
        from .binding import diagnose_bind_error

        diagnosis = diagnose_bind_error(port, data["bind_error_code"])
        print()
        print(diagnosis.to_text())
    else:
        print("端口一切正常，可以直接登录。")
    if data["blocked_ports_nearby"]:
        print(f"\n附近被阻断的端口: {data['blocked_ports_nearby'][:24]}")
    print(f"\n本机路由 IP     : {data['local_ip']}")
    print(f"数据目录        : {data['data_dir']}")
    print(f"当前日志        : {data['log_file']}")
    return _EXIT_ONLINE if data["bind_free"] else _EXIT_OFFLINE


def _cmd_probe(controller: AppController, args) -> int:
    results = controller.probe_now()
    if args.json:
        print(json.dumps([r.__dict__ for r in results], ensure_ascii=False, indent=2))
    else:
        for r in results:
            if r.ok:
                print(f"{r.target:<18} RTT {r.rtt_ms:.1f} ms   丢包 {r.loss_percent:.0f}%")
            else:
                print(f"{r.target:<18} 无响应  丢包 {r.loss_percent:.0f}%")
    return _EXIT_ONLINE if any(r.ok for r in results) else _EXIT_OFFLINE


def _cmd_set(controller: AppController, args) -> int:
    account = controller.config.active_account()
    if account is None:
        account = Account()
        controller.config.accounts.append(account)
        controller.config.active_account_id = account.id

    changed = []
    if args.account:
        account.account = args.account.strip()
        changed.append("账号")
    if args.mac:
        account.mac = args.mac.strip().upper()
        changed.append("MAC")
    if args.label:
        account.label = args.label
        changed.append("备注")
    if args.auto_login:
        account.auto_login = True
        changed.append("自动登录")

    if args.password:
        degraded = controller.store.set_password(account.id, args.password)
        changed.append("密码（已加密）")
        if degraded:
            print(
                "警告：当前系统没有 DPAPI/Fernet 可用，密码只做了可逆混淆。",
                file=sys.stderr,
            )

    if not changed:
        print("没有需要修改的内容。", file=sys.stderr)
        return _EXIT_USAGE

    if args.no_save:
        print("（--no-save：未写回配置文件）")
    else:
        controller.store.save()
    print("已更新：" + "、".join(changed))
    print(f"当前账号：{account.masked_account}  MAC: {account.mac or '(未设置)'}")
    return _EXIT_ONLINE


def _cmd_export_logs(controller: AppController, args) -> int:
    destination = controller.export_logs()
    print(f"已导出到 {destination}")
    return _EXIT_ONLINE


def _cmd_login(controller: AppController, args) -> int:
    ok, message = controller.request_login()
    if not ok:
        print(f"无法登录：{message}", file=sys.stderr)
        return _EXIT_USAGE
    print(message)

    final = {"code": _EXIT_OFFLINE}

    def on_event(event) -> None:
        if event.kind != "engine" or event.engine_event is None:
            return
        inner = event.engine_event
        if inner.kind == "online":
            print(f"[+] 已上线 | IP {inner.ip}", flush=True)
            final["code"] = _EXIT_ONLINE
        elif inner.kind == "state":
            print(f"[.] {inner.state.value}: {inner.message}", flush=True)
        elif inner.kind in ("login_failed", "fatal"):
            print(f"[!] {inner.message}", flush=True)
            if inner.advice:
                print(f"    {inner.advice}", flush=True)
            if inner.kind == "fatal":
                final["code"] = _EXIT_FATAL
        elif inner.kind in ("bind_failed", "challenge_failed", "keepalive_failed", "gave_up"):
            print(f"[!] {inner.message}", flush=True)
            if inner.detail:
                print(f"    {inner.detail}", flush=True)
        elif inner.kind == "retry_scheduled":
            print(f"[…] {inner.message}", flush=True)

    controller.add_listener(on_event)
    controller.start_background()

    stopping = False

    def handle_signal(_signum, _frame) -> None:
        nonlocal stopping
        if stopping:
            return
        stopping = True
        print("\n收到中断信号，正在退出…", flush=True)
        controller.request_logout()
        controller.shutdown()

    signals = [signal.SIGINT, signal.SIGTERM]
    # On Windows a console Ctrl+Break delivers SIGBREAK, not SIGINT — without
    # this the process dies by STATUS_CONTROL_C_EXIT and skips the clean
    # shutdown path below (socket close, stats flush, config save).
    if hasattr(signal, "SIGBREAK"):
        signals.append(signal.SIGBREAK)
    for sig in signals:
        try:
            signal.signal(sig, handle_signal)
        except (ValueError, OSError, RuntimeError):  # pragma: no cover - not on the main thread
            pass

    # Stay in the foreground; the engine keeps the session alive.
    watch = ForegroundWatch()
    try:
        while not stopping:
            time.sleep(0.5)
            verdict = watch.poll(controller.engine)
            if verdict == "restarted":
                continue
            if verdict == "fatal":
                return _EXIT_FATAL
            if verdict == "finished":
                break
    except KeyboardInterrupt:
        handle_signal(None, None)
    return final["code"]


_COMMANDS = {
    "login": _cmd_login,
    "status": _cmd_status,
    "diag": _cmd_diag,
    "probe": _cmd_probe,
    "set": _cmd_set,
    "export-logs": _cmd_export_logs,
}



class ForegroundWatch:
    """Decides whether a foreground CLI session is really over.

    Why this exists: a *user-requested* reconnect (or a scheduled one, or the
    HTTP API, or the tray) stops the old engine and constructs a new one.  If
    the foreground loop watches ``engine.is_running`` alone it reads that
    transient moment as "the engine gave up" and exits the program — which is
    exactly what happened on the first long soak run, minutes in, right after a
    ``POST /reconnect``.

    So we only conclude "finished" when the *same* engine object stays stopped
    across several consecutive polls, which gives any restart in flight time to
    swap the engine out.
    """

    #: ~3 seconds of grace at a 0.5 s poll interval.
    GRACE_POLLS = 6

    def __init__(self) -> None:
        self._seen = None
        self._stopped_polls = 0

    def poll(self, engine) -> str:
        """Return ``"running"``, ``"restarted"``, ``"fatal"`` or ``"finished"``."""
        if engine is not self._seen:
            # First look, or the controller swapped in a new engine.
            restarted = self._seen is not None
            self._seen = engine
            self._stopped_polls = 0
            return "restarted" if restarted else "running"

        if engine is None or engine.is_running:
            self._stopped_polls = 0
            return "running"

        self._stopped_polls += 1
        if self._stopped_polls < self.GRACE_POLLS:
            return "running"
        if engine.state is EngineState.FATAL:
            return "fatal"
        return "finished"


def _human(seconds: float) -> str:
    seconds = int(max(0, seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


def run_cli(argv: list[str]) -> int:
    """Entry point for ``--cli``; returns a process exit code."""
    args = build_parser().parse_args(argv)
    command = args.cli
    if command not in _COMMANDS:
        print(f"未知命令：{command!r}，可用：{', '.join(_COMMANDS)}", file=sys.stderr)
        return _EXIT_USAGE

    controller = AppController(data_dir=args.data_dir, log_level=args.log_level)
    if args.verbose:
        controller.log.protocol_hex = True

    try:
        return _COMMANDS[command](controller, args)
    finally:
        controller.shutdown()
