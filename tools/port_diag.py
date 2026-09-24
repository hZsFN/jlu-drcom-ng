#!/usr/bin/env python3
"""Standalone port-conflict diagnostic — no GUI, no dependencies.

Answers one question: **why can't I bind 61440?**  and what should I do about it.

    python tools/port_diag.py            # inspect the default port
    python tools/port_diag.py 61440      # or an explicit one
    python tools/port_diag.py --json     # machine-readable

Kept dependency-free and standalone on purpose: it must still work when the
main program cannot start at all.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drcom.binding import (  # noqa: E402
    WSAEACCES,
    WSAEADDRINUSE,
    detect_conflict_suspects,
    diagnose_bind_error,
    find_free_nearby,
    netsh_hint,
    probe_bind,
    read_excluded_port_ranges,
    scan_port_window,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("port", nargs="?", type=int, default=61440)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--radius", type=int, default=400, help="扫描半径（默认 400）")
    options = parser.parse_args()

    port = options.port
    code = probe_bind(port)

    if options.json:
        payload = {
            "port": port,
            "bindable": code is None,
            "error_code": code,
            "excluded_ranges": [list(r) for r in read_excluded_port_ranges()],
            "suspects": detect_conflict_suspects(),
        }
        if code is not None:
            diagnosis = diagnose_bind_error(port, code, deep=True)
            payload["diagnosis"] = {
                "kind": diagnosis.kind,
                "headline": diagnosis.headline,
                "explanation": diagnosis.explanation,
                "advice": diagnosis.advice,
                "holders": diagnosis.holders,
                "blocked_range": list(diagnosis.blocked_range) if diagnosis.blocked_range else None,
                "free_alternatives": diagnosis.free_alternatives,
            }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if code is None else 1

    print(f"目标端口        : {port}")
    print(f"本机路由地址    : ", end="")
    from drcom.netiface import get_default_route_ip

    print(get_default_route_ip() or "(未知)")

    if code is None:
        print("绑定测试        : 通过 [OK]  端口可用。")
        print("\n如果程序仍然连不上，问题不在端口，而在网络或账号：")
        print("  - 网线 / 接入交换机端口是否通")
        print("  - 账号密码、MAC 是否与绑定信息一致")
        return 0

    print(f"绑定测试        : 失败 [X]  原始错误码 {code}")
    if code == WSAEACCES:
        print("                  （10013 = WSAEACCES，权限拒绝 -- 注意这**不是** 10048 端口占用）")
    elif code == WSAEADDRINUSE:
        print("                  （10048 = WSAEADDRINUSE，确实被占用了）")

    diagnostic = diagnose_bind_error(port, code, deep=True)
    print()
    print(diagnostic.to_text())

    print("\n（补充）原始错误码对照：")
    print(f"  10013 WSAEACCES    权限拒绝：端口被系统保留，或被别的程序独占绑定")
    print(f"  10048 WSAEADDRINUSE 端口已被占用")

    excluded = read_excluded_port_ranges()
    covered = any(lo <= port <= hi for lo, hi in excluded)
    print(f"\n系统报告的排除端口区间（可能不完整，TUN 创建的常常不在这里）：")
    if excluded:
        for low, high in excluded:
            mark = "  <-- 包含目标端口" if low <= port <= high else ""
            print(f"  [{low}, {high}]{mark}")
    else:
        print("  （读不到。本机没有管理员权限时 netsh 也可能返回空。）")
    print(f"目标端口是否落在其中：{'是' if covered else '否'}")

    free = find_free_nearby(port, limit=8)
    if free:
        print(f"邻近可用端口    : {free}")

    print(f"\n可复现的命令    : {netsh_hint()}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
