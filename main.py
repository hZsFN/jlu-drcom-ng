#!/usr/bin/env python3
"""Dr.COM (吉林大学) 校园网认证客户端 —— 入口。

用法::

    python main.py                     # 启动图形界面
    python main.py --minimized         # 启动并最小化（配合开机自启）
    python main.py --cli login         # 无界面模式，前台保持在线
    python main.py --cli status --json # 查询状态
    python main.py --cli diag          # 诊断 61440 端口冲突
    python main.py --selftest          # 协议自检，不联网

Windows 上可直接双击，或打包为单文件 exe（见 build.py）。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Make `python main.py` work from any working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from drcom import __version__  # noqa: E402


def _raise_priority() -> bool:
    """Ask the OS for a slightly higher scheduling class.

    Deliberately ABOVE_NORMAL and not HIGH: this is a background utility, and
    HIGH_PRIORITY_CLASS would let it starve the interactive programs it shares
    the machine with.  What it buys is the boot case -- a dozen startup entries
    and a cold disk -- where being scheduled promptly is most of the wait.

    Child processes do NOT inherit this.  Windows only hands the creating
    process's class to a child when that class is IDLE or BELOW_NORMAL, so an
    ABOVE_NORMAL parent gets a NORMAL child.  Measured, not assumed: after this
    runs, the app process reads 0x8000 while the Flet client reads 0x20.  Only
    the Python process is raised; raising the client too would mean taking over
    a spawn that Flet owns.

    Never fatal: on a platform or policy that refuses the request, we simply
    run at normal priority.

    The argtypes matter.  Without them ctypes marshals the process HANDLE as a
    32-bit int, which truncates on 64-bit Windows, so the call is handed a
    garbage handle -- it returns 0 and fails *silently*, with no exception to
    catch.  That is exactly how the first version of this shipped doing
    nothing at all.

    Returns whether the class was actually applied.
    """
    try:
        import os

        if os.name != "nt":
            return False
        import ctypes

        ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        kernel32.SetPriorityClass.restype = ctypes.c_int
        return bool(
            kernel32.SetPriorityClass(
                kernel32.GetCurrentProcess(), ABOVE_NORMAL_PRIORITY_CLASS
            )
        )
    except Exception:
        return False


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jlu-drcom-ng",
        description="吉林大学 Dr.COM 校园网认证客户端（Python + Flet）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="无界面模式请用 --cli，例如：python main.py --cli diag",
    )
    parser.add_argument("--version", action="version", version=f"jlu-drcom-ng {__version__}")
    parser.add_argument("--data-dir", type=Path, help="配置文件与日志目录")
    parser.add_argument("--log-level", default=None, help="DEBUG / INFO / WARNING / ERROR")
    parser.add_argument("--minimized", action="store_true", help="启动后最小化到托盘/任务栏")
    parser.add_argument("--autostart", action="store_true", help="表示由开机自启拉起（等同于 --minimized）")
    parser.add_argument("--no-tray", action="store_true", help="不创建系统托盘图标")
    parser.add_argument("--web", action="store_true", help="改用浏览器界面（本机网页）")
    parser.add_argument("--port", type=int, default=0, help="--web 模式监听端口（0=自动）")
    parser.add_argument("--no-single-instance", action="store_true", help="允许多开（仅供调试）")
    parser.add_argument("--selftest", action="store_true", help="运行协议自检并退出（不联网）")
    parser.add_argument("--doctor", action="store_true", help="打印环境与端口诊断后退出")
    parser.add_argument(
        "--watchdog",
        action="store_true",
        help="作为守护进程运行：拉起客户端，崩溃后自动重启（正常退出则一起结束）",
    )
    return parser


def _selftest() -> int:
    """Offline checks: MD4 vectors, packet geometry, password backends."""
    from drcom.md4 import md4_hex
    from drcom import protocol

    failures: list[str] = []

    vectors = {
        b"": "31d6cfe0d16ae931b73c59d7e0c089c0",
        b"a": "bde52cb31de33e46245e05fbdbd6fb24",
        b"abc": "a448017aaf21d8525fc10ae87aa6729d",
        b"message digest": "d9130a8164549fe818874806e1c7014b",
        b"abcdefghijklmnopqrstuvwxyz": "d79e1c308aa5bbcdeea8ed63df412da9",
        b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789":
            "043f8582f241db351ce627e153e7f0e4",
        b"1234567890" * 8: "e33b4ddc9c38f2199c3e7b164fcc0536",
    }
    print("[1] MD4 (RFC 1320) 测试向量")
    for data, expected in vectors.items():
        got = md4_hex(data)
        status = "ok" if got == expected else "FAIL"
        if got != expected:
            failures.append(f"MD4({data[:20]!r})")
        print(f"    [{status:4s}] {data[:28]!r:<32} {got}")

    print("\n[2] 登录包长度 / padding 规则")
    seed = bytes((0x21, 0x79, 0x04, 0x0F))
    mac = "AA:BB:CC:DD:EE:FF"
    for length in range(1, 25):
        password = "p" * length
        packet = protocol.build_login_packet("2023000001", password, mac, seed)
        extra, jlu = protocol._password_padding(length)
        expected_len = protocol.LOGIN_BASE_LEN + extra
        counter = 312 + 2 + length
        ror = (8 - length) if length <= 8 else jlu
        ok = (
            len(packet) == expected_len
            and packet[313] == length
            and packet[counter] == 0x02
            and packet[counter + 1] == 0x0C
            and packet[counter + ror + 14 : counter + ror + 16] == bytes((0x60, 0xA2))
            and packet[97:105] == protocol.compute_checksum1(packet)
            and packet[counter + 2 : counter + 6]
            == protocol.compute_checksum2(packet[: counter + 2], protocol.mac_to_bytes(mac))
        )
        if not ok:
            failures.append(f"login packet for password length {length}")
        flag = "" if protocol.password_length_supported(length) else "  <- 超出参考实现范围（使用本程序扩展算法）"
        print(
            f"    [{'ok' if ok else 'FAIL':4s}] 密码长度 {length:2d} -> 包长 {len(packet):3d} "
            f"(338+{extra}), ror={ror}{flag}"
        )
    print(f"    （参考实现验证过的长度：1–{protocol.REFERENCE_MAX_PASSWORD_LEN}）")

    print("\n[3] 保活包形状")
    checks = [
        ("keepalive1 p1 == 8 字节", len(protocol.KEEPALIVE1_PACKET1) == 8),
        (
            "keepalive1 p2 == 42 字节",
            len(protocol.build_keepalive1_packet2(seed, bytes(16))) == 42,
        ),
        (
            "keepalive2 == 40 字节",
            len(protocol.build_keepalive2_packet(0)) == 40,
        ),
    ]
    for name, ok in checks:
        if not ok:
            failures.append(name)
        print(f"    [{'ok' if ok else 'FAIL':4s}] {name}")

    print("\n[4] 密码加密后端")
    import tempfile

    from drcom.secrets_store import protect, unprotect

    with tempfile.TemporaryDirectory() as tmp:
        result = protect("hunter2-测试", key_path=Path(tmp) / "key.bin")
        round_trip = unprotect(result.value, key_path=Path(tmp) / "key.bin").value
        ok = round_trip == "hunter2-测试"
        if not ok:
            failures.append("password round-trip")
        print(f"    [{'ok' if ok else 'FAIL':4s}] 后端 {result.backend}，往返一致：{ok}")
        if result.degraded:
            print("    警告：当前环境只能做可逆混淆，请勿在此环境下保存重要密码")

    print("\n" + "=" * 64)
    if failures:
        print(f"自检失败：{len(failures)} 项 -- {failures}")
        return 1
    print("自检全部通过。")
    return 0


def _doctor(data_dir: Path | None) -> int:
    from drcom.controller import AppController

    controller = AppController(data_dir=data_dir, log_level="WARNING")
    data = controller.diagnostics()
    print(f"版本            : {data['version']}")
    print(f"数据目录        : {data['data_dir']}")
    print(f"认证服务器      : {controller.config.auth.server}:{controller.config.auth.port}")
    print(f"绑定端口        : {data['bind_port']}  ({'可用' if data['bind_free'] else '不可用'})")
    print(f"本机路由 IP     : {data['local_ip']}")
    if data["bind_error_code"]:
        from drcom.binding import diagnose_bind_error

        print()
        print(diagnose_bind_error(data["bind_port"], data["bind_error_code"]).to_text())
    print(f"\n排除端口区间（netsh）: {data['excluded_ranges'] or '（无 / 读不到）'}")
    print(f"端口是否落在其中      : {data['excluded_covers_port']}")
    controller.shutdown()
    return 0 if data["bind_free"] else 1


def _force_utf8_console() -> None:
    """Make console output UTF-8 regardless of the shell / code page.

    Without this, Chinese text comes out as mojibake whenever the output is
    piped or the console is on a non-UTF-8 code page (the classic GBK-vs-UTF-8
    mismatch).  Best-effort: any failure here is not worth aborting a launch.
    """
    try:
        if sys.platform == "win32":
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    for stream in ("stdout", "stderr"):
        handle = getattr(sys, stream, None)
        reconfigure = getattr(handle, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass



def _watchdog_logger(data_dir: Path):
    """A line logger for the supervisor: to the console and to its own file."""
    log_path = Path(data_dir) / "logs" / f"watchdog-{time.strftime('%Y%m%d')}.log"

    def log(message: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"{stamp} {message}"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + chr(10))
        except OSError:
            pass
        try:
            print(line, flush=True)
        except Exception:
            pass

    return log


def _run_watchdog(argv: list[str], args) -> int:
    """Run the supervisor in the foreground (usually from the autostart entry)."""
    from drcom.config import default_data_dir
    from drcom.watchdog import run_watchdog

    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir()
    log = _watchdog_logger(data_dir)
    log("守护进程启动")
    try:
        return run_watchdog(argv, data_dir=data_dir, log=log)
    except KeyboardInterrupt:
        log("守护进程被中断")
        return 0


def main(argv: list[str] | None = None) -> int:
    _raise_priority()
    _force_utf8_console()
    argv = list(sys.argv[1:] if argv is None else argv)

    # `--cli` is handled by the CLI module, which owns its own parser.
    if "--cli" in argv:
        from drcom.cli import run_cli

        return run_cli(argv)

    args = _build_parser().parse_args(argv)

    if args.selftest:
        return _selftest()
    if args.doctor:
        return _doctor(args.data_dir)

    # --- watchdog --------------------------------------------------------
    if args.watchdog:
        return _run_watchdog(argv, args)

    # --- single instance -------------------------------------------------
    guard = None
    if not args.no_single_instance:
        from drcom.single_instance import SingleInstance

        guard = SingleInstance()
        if not guard.acquire():
            message = (
                "DrCOM 客户端已经在运行了。\n\n"
                "重复启动会争抢本地 61440 端口，第二个实例必然绑定失败，\n"
                "所以这里直接退出，避免出现看不懂的端口错误。\n\n"
                "请检查系统托盘 / 任务栏里已有的窗口。"
            )
            print(message, file=sys.stderr)
            try:
                import tkinter.messagebox

                root = tkinter.Tk()
                root.withdraw()
                tkinter.messagebox.showinfo("DrCOM · 已在运行", message)
                root.destroy()
            except Exception:
                pass
            return 3

    try:
        from drcom.controller import AppController
        from drcom.ui import run_gui

        controller = AppController(data_dir=args.data_dir, log_level=args.log_level)
        # Authenticate before the window exists.  The GUI is the slowest part
        # of a cold start, and there is no reason for the network to wait on it.
        controller.begin_session()
        run_gui(
            controller,
            minimized=args.minimized or args.autostart,
            web=args.web,
            port=args.port,
            enable_tray=not args.no_tray,
        )
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # pragma: no cover - top-level guard
        import traceback

        traceback.print_exc()
        try:
            import tkinter.messagebox

            root = tkinter.Tk()
            root.withdraw()
            tkinter.messagebox.showerror("DrCOM 启动失败", f"{exc}\n\n详情见控制台输出。")
            root.destroy()
        except Exception:
            pass
        return 1
    finally:
        if guard is not None:
            guard.release()


if __name__ == "__main__":
    raise SystemExit(main())
