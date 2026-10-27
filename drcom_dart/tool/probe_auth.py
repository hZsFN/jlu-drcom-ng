#!/usr/bin/env python
"""认证服务器连通性探测（只读，不发登录包）。

用法：
    python dart/tool/probe_auth.py            # 只发挑战包
    python dart/tool/probe_auth.py --timeout 5

做的事只有一件：给认证服务器发一个 20 字节挑战包，看它回不回。
**不会登录、不会改变任何连接状态**——挑战包是登录流程的第一步，
服务端只会回一个 seed，不发任何凭据。

用途：
1. 确认本机到认证服务器的 UDP 通不通（换网/换宿舍/防火墙变了时最快的判据）
2. 给 Dart 版做联调前的"参考实现也活着"对照
"""
from __future__ import annotations

import argparse
import socket
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from drcom import protocol as P  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Dr.COM 认证服务器连通性探测")
    parser.add_argument("--timeout", type=float, default=3.0, help="等待响应的秒数（默认 3）")
    parser.add_argument("--server", default=P.AUTH_SERVER, help="认证服务器地址")
    parser.add_argument("--port", type=int, default=P.AUTH_PORT, help="认证服务器端口")
    args = parser.parse_args()

    print(f"目标 {args.server}:{args.port}（UDP）")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(args.timeout)
    packet = P.build_challenge_packet()
    print(f"发送挑战包 {len(packet)} 字节：{packet.hex()}")

    try:
        sock.sendto(packet, (args.server, args.port))
        data, addr = sock.recvfrom(2048)
    except socket.timeout:
        print(f"TIMEOUT：{args.timeout}s 内没有收到响应")
        print("可能原因：不在校园网内 / 认证服务器不通 / UDP 被挡")
        return 2
    except OSError as exc:
        print(f"ERROR：{exc}")
        return 3
    finally:
        sock.close()

    print(f"收到响应 {len(data)} 字节（来自 {addr[0]}:{addr[1]}）")
    print(f"  原始：{data.hex()}")
    try:
        parsed = P.parse_challenge_response(data)
    except P.ProtocolError as exc:
        print(f"  解析失败：{exc}")
        return 4
    print(f"  seed = {parsed.seed.hex()}")
    print(f"  服务端宣告的本机 IP = {parsed.ip}")
    print("OK：认证服务器在线且可达")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
