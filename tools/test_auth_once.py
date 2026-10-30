# -*- coding: utf-8 -*-
"""jlu-drcom-ng 认证冒烟测试 / Auth smoke test.

只做一件事：用当前配置里的账号，走一遍
    绑定 -> 挑战码 -> 登录包 -> 读回执
然后打印结果。不保活、不改配置、不写统计、不起 UI。

会自动枚举本机网卡地址逐个试，所以切到 jlupc 后不用改「绑定地址」。

用法（在仓库根目录）：
    python tools/test_auth_once.py

退出码：0 = 认证通过；1 = 认证被拒（有错误码）；2 = 环境/配置问题；3 = 服务器没回应。
"""

from __future__ import annotations

import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drcom import protocol                                  # noqa: E402
from drcom.binding import BindFailure, bind_udp_socket      # noqa: E402
from drcom.config import ConfigStore, default_data_dir      # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

RECV_TIMEOUT = 3.0


def candidate_addresses(cfg) -> list[str]:
    """候选绑定地址，和引擎的 _bind_candidates 保持同一套逻辑。

    配置里填了具体地址就用它；留空（或 0.0.0.0）则 0.0.0.0 优先、本机具体地址兜底。
    """
    configured = (cfg.auth.bind_address or "").strip()
    if configured and configured not in ("0.0.0.0", "::", "*"):
        return [configured]

    try:
        from drcom.netiface import candidate_bind_addresses

        found = candidate_bind_addresses(server=cfg.auth.server, port=cfg.auth.port)
    except Exception as exc:                  # 枚举失败不该阻断测试
        print(f"[warn] 枚举网卡地址失败：{exc!r}")
        found = []
    return ["0.0.0.0", *found]


def recv_reply(sock: socket.socket, server: str, *, want: tuple[int, ...],
               timeout: float = RECV_TIMEOUT, max_packets: int = 32) -> bytes:
    """等一个首字节落在 want 里的报文，跳过服务器通知包。"""
    deadline = time.time() + timeout
    skipped = 0
    for _ in range(max_packets):
        left = deadline - time.time()
        if left <= 0:
            raise socket.timeout("等待响应超时")
        sock.settimeout(left)
        data, peer = sock.recvfrom(2048)
        if peer[0] != server:
            skipped += 1
            continue
        if data and data[0] in want:
            return data
        skipped += 1
    raise socket.timeout(f"连续收到 {skipped} 个无关报文")


def main() -> int:
    data_dir = default_data_dir()
    store = ConfigStore(data_dir)
    cfg = store.load()
    acc = cfg.active_account()
    if acc is None:
        print("[FAIL] 配置里没有账号")
        return 2

    password = store.get_password(acc.id)
    if not password:
        print(f"[FAIL] 账号 {acc.masked_account} 没有可用密码（未保存或解不开）")
        return 2

    server, port = cfg.auth.server, cfg.auth.port
    print(f"[config ] {data_dir}")
    print(f"[account] {acc.masked_account}  mac={acc.mac}  password_len={len(password)}")
    print(f"[server ] {server}:{port}")

    candidates = candidate_addresses(cfg)
    print(f"[nic    ] 待试地址 {candidates}")
    print()

    last_reason = ""
    for addr in candidates:
        try:
            bound = bind_udp_socket(
                cfg.auth.bind_port,
                address=addr,
                timeout_ms=cfg.auth.timeout_ms,
                allow_alternate_port=True,
            )
        except BindFailure as exc:
            print(f"[skip  ] {addr:<16} 绑定失败：{exc.diagnosis.headline}")
            last_reason = "bind"
            continue

        sock = bound.sock
        used = f"{bound.bind_address}:{bound.local_port}"
        try:
            # --- 挑战码 ------------------------------------------------
            try:
                sock.sendto(protocol.build_challenge_packet(), (server, port))
            except OSError as exc:
                print(f"[skip  ] {used:<22} 发不出去：{exc}")
                last_reason = "unreachable"
                continue
            try:
                raw = recv_reply(sock, server, want=(0x02,))
            except socket.timeout:
                print(f"[skip  ] {used:<22} 挑战码无回应")
                last_reason = "challenge"
                continue

            challenge = protocol.parse_challenge_response(raw)
            print(f"[chall ] {used:<22} 本机 IP={challenge.ip}")

            # --- 登录 --------------------------------------------------
            time.sleep(cfg.auth.post_login_delay)
            packet = protocol.build_login_packet(acc.account, password, acc.mac, challenge.seed)
            try:
                sock.sendto(packet, (server, port))
            except OSError as exc:
                print(f"[FAIL ] 登录包发不出去：{exc}")
                return 3
            print(f"[login ] {used:<22} 已发送 {len(packet)} 字节")

            try:
                reply = recv_reply(sock, server, want=(0x04, 0x05))
            except socket.timeout:
                print("[FAIL ] 登录回执超时，服务器没回")
                return 3

            outcome = protocol.parse_login_response(reply)
            if outcome.success:
                print()
                print("[OK] 认证通过 ✅  已上线")
                print(f"     auth_information={outcome.auth_information.hex()}")
                print("     注意：本脚本不做保活，连接很快会被服务器判离线。")
                return 0

            code = outcome.error_code
            print()
            if code is None:
                print("[FAIL] 登录被拒，服务端没给错误码")
                print(f"     原始报文：{reply.hex()}")
                return 1
            print(f"[FAIL] 认证失败：{outcome.message}（错误码 0x{code:02X}）")
            print(f"     建议：{outcome.advice}")
            print(f"     重试有用吗：{'没用，属致命错误' if protocol.is_fatal_login_error(code) else '可能有用，属临时故障'}")
            return 1
        finally:
            try:
                sock.close()
            except OSError:
                pass

    print()
    if last_reason == "bind":
        print("[FAIL] 所有地址都绑定失败 —— 61440 可能被正在运行的客户端占着，先退出它")
    elif last_reason == "challenge":
        print("[FAIL] 所有地址都拿不到挑战码")
        print("       服务器 10.100.61.3:61440 完全没回应。可能原因：")
        print("       - 本机不在校园网（WiFi 没连上 jlupc / 有线没插）")
        print("       - 默认路由被 VPN（如 OrayBoxVpnEnt）或其它网卡抢走")
        print("       - 认证服务器临时不可用")
    else:
        print("[FAIL] 没有可用的本机地址")
    return 3


if __name__ == "__main__":
    sys.exit(main())
