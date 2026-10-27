#!/usr/bin/env python
"""生成协议黄金向量：Python 参考实现 → JSON，供 Dart 版逐字节对拍。

用法（在仓库根执行）：
    python dart/tool/gen_golden.py

输出：``dart/test/golden/protocol_vectors.json``

为什么要有这个文件
------------------
协议实现最容易出的错不是"跑不通"，而是"跑通了但某一字节错位，服务端
静默拒绝"。真去连校园网验证既慢又不可复现，所以这里把参考实现的输出
钉死成向量，Dart 侧逐字节断言。改协议后必须重跑本脚本，否则 Dart 测试
会拿旧向量自查——那时错误的是测试，不是实现。
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from drcom import protocol as P  # noqa: E402

#: 覆盖 ≤8 字符（ror 分支）与 >8 字符（jlu padding 分支），含 16 字符上界。
LOGIN_CASES = [
    ("20230001", "abcd1234", "AA:BB:CC:DD:EE:FF", "01020304"),
    ("20230002", "short", "aa-bb-cc-dd-ee-01", "a1b2c3d4"),
    ("20230003", "abcdefghijklmnop", "00:11:22:33:44:55", "ffffffff"),
    ("20230004", "123456789", "de:ad:be:ef:00:01", "00000000"),
    ("20230005", "a", "01:02:03:04:05:06", "deadbeef"),
]


def hx(data: bytes) -> str:
    return data.hex()


class _Rng:
    """按给定字节序列回放的假随机源。

    用途：`build_keepalive1_packet2` 的 `rng` 参数只需要 `randrange(256)`，
    用这个把尾部两个字节钉死，Dart 侧传同样的 randBytes 就能对拍。
    """

    def __init__(self, values: list[int]) -> None:
        self._values = list(values)
        self._index = 0

    def randrange(self, stop: int) -> int:
        value = self._values[self._index % len(self._values)]
        self._index += 1
        return value % stop


def main() -> int:
    out: dict = {
        "_note": "由 dart/tool/gen_golden.py 从 Python 参考实现生成，勿手改",
        "constants": {
            "LOGIN_BASE_LEN": P.LOGIN_BASE_LEN,
            "PW_SECTION_OFFSET": P._PW_SECTION_OFFSET,
            "KEEPALIVE1_PACKET1": hx(P.KEEPALIVE1_PACKET1),
            "KEEPALIVE1_PACKET2_LEN": P.KEEPALIVE1_PACKET2_LEN,
            "KEEPALIVE2_LEN": P.KEEPALIVE2_LEN,
            "SERVICE_PACK": hx(P._SERVICE_PACK),
            "HOSTNAME_BLOB": hx(P._HOSTNAME_BLOB),
            "KEEPALIVE_VERSION_FILE": hx(P._KEEPALIVE_VERSION_FILE),
            "KEEPALIVE_VERSION_NORMAL": hx(P._KEEPALIVE_VERSION_NORMAL),
            "REFERENCE_MAX_PASSWORD_LEN": P.REFERENCE_MAX_PASSWORD_LEN,
        },
        "challenge": [],
        "logins": [],
        "padding": [],
        "keepalive1_p2": [],
        "keepalive2": [],
        "responses": {},
        "login_errors": {},
    }

    # ---- 挑战包：固定 rng 保证可复现 --------------------------------
    # 注意：Dart 的 Random(seed) 与 Python 的 random.Random(seed) 算法不同，
    # 所以不能指望两边"同一个种子抽同一对数"。这里把抽出的两个字节也存进
    # 向量，Dart 侧直接按字节构造，绕开随机数实现差异。
    for seed in (0, 42, 12345):
        rng = random.Random(seed)
        packet = P.build_challenge_packet(rng)
        probe = random.Random(seed)
        rand_bytes = [probe.randrange(256), probe.randrange(256)]
        out["challenge"].append(
            {"rng_seed": seed, "rand_bytes": rand_bytes, "packet": hx(packet)}
        )

    # ---- 登录包 ------------------------------------------------------
    for account, password, mac, seed_hex in LOGIN_CASES:
        seed = bytes.fromhex(seed_hex)
        pw = password.encode()
        login = P.build_login_packet(account, password, mac, seed)
        out["logins"].append(
            {
                "account": account,
                "password": password,
                "mac": mac,
                "seed": seed_hex,
                "mac_bytes": hx(P.mac_to_bytes(mac)),
                "md5a": hx(P.md5a(pw, seed)),
                "md5b": hx(P.md5b(pw, seed)),
                "checksum1": hx(P.compute_checksum1(login)),
                "login_len": len(login),
                "login_packet": hx(login),
            }
        )

    # ---- 密码长度分支表 ---------------------------------------------
    for n in range(1, 18):
        extra, jlu = P._password_padding(n)
        out["padding"].append(
            {"len": n, "extra": extra, "jlu_padding": jlu, "supported": P.password_length_supported(n)}
        )

    # ---- keepalive CRC（gen_crc 的 4 种 encrypt_type）----------------
    seed = bytes.fromhex("11223344")
    out["gen_crc"] = {str(t): hx(P.gen_crc(seed, t)) for t in range(4)}

    # ---- keepalive-1 第二个包（42 字节，四种 encrypt_type 各一）------
    # seed[0] & 3 决定 encrypt_type，所以 seed 首字节分别取 00/01/02/03。
    auth_info = bytes.fromhex("aabbccddeeff00112233445566778899")
    for first in range(4):
        ka1_seed = bytes([first, 0x11, 0x22, 0x33])
        out["keepalive1_p2"].append(
            {
                "seed": hx(ka1_seed),
                "auth_information": hx(auth_info),
                "encrypt_type": first,
                "rand_bytes": [0xA5, 0x5A],
                "packet": hx(P.build_keepalive1_packet2(ka1_seed, auth_info, _Rng([0xA5, 0x5A]))),
            }
        )

    # ---- keepalive-2 包（file/普通 × A/C 包 × counter 边界）----------
    ka2_cases = [
        {"counter": 0, "file_packet": True, "pkt_type": 1, "tail": ""},
        {"counter": 5, "file_packet": False, "pkt_type": 1, "tail": ""},
        {"counter": 9, "file_packet": False, "pkt_type": 3, "tail": "11223344"},
        {"counter": 255, "file_packet": False, "pkt_type": 3, "tail": ""},
        {"counter": 256, "file_packet": False, "pkt_type": 1, "tail": ""},
    ]
    for case in ka2_cases:
        tail = bytes.fromhex(case["tail"]) if case["tail"] else b""
        packet = P.build_keepalive2_packet(
            case["counter"],
            file_packet=case["file_packet"],
            pkt_type=case["pkt_type"],
            tail=tail,
        )
        out["keepalive2"].append({**case, "packet": hx(packet)})

    # ---- 响应解析（输入 hex + 期望解析出的字段）----------------------
    challenge_reply = bytearray(76)
    challenge_reply[0] = 0x02
    challenge_reply[4:8] = bytes.fromhex("cafebabe")
    challenge_reply[20:24] = bytes((10, 100, 61, 3))
    cresp = P.parse_challenge_response(bytes(challenge_reply))
    out["responses"]["challenge_ok"] = {
        "input": hx(bytes(challenge_reply)),
        "seed": hx(cresp.seed),
        "ip": cresp.ip,
    }

    login_ok = bytearray(100)
    login_ok[0] = 0x04
    login_ok[23:39] = bytes(range(16))
    lresp = P.parse_login_response(bytes(login_ok))
    out["responses"]["login_ok"] = {
        "input": hx(bytes(login_ok)),
        "success": lresp.success,
        "auth_information": hx(lresp.auth_information),
    }

    login_fail = bytearray(88)
    login_fail[0] = 0x05
    login_fail[4] = 0x03
    fresp = P.parse_login_response(bytes(login_fail))
    out["responses"]["login_fail"] = {
        "input": hx(bytes(login_fail)),
        "success": fresp.success,
        "error_code": fresp.error_code,
        "message": fresp.message,
        "advice": fresp.advice,
    }

    ka1_reply = bytearray(20)
    ka1_reply[0] = 0x07
    ka1_reply[8:12] = bytes.fromhex("0a0b0c0d")
    k1resp = P.parse_keepalive1_response(bytes(ka1_reply))
    out["responses"]["keepalive1_ok"] = {
        "input": hx(bytes(ka1_reply)),
        "seed": hx(k1resp.seed),
        "encrypt_type": k1resp.encrypt_type,
    }

    ka2_reply = bytearray(40)
    ka2_reply[0] = 0x07
    ka2_reply[2] = 0x28
    ka2_reply[16:20] = bytes.fromhex("99887766")
    k2resp = P.parse_keepalive2_response(bytes(ka2_reply))
    out["responses"]["keepalive2_ok"] = {
        "input": hx(bytes(ka2_reply)),
        "ok": k2resp.ok,
        "tail": hx(k2resp.tail),
    }

    ka2_file = bytearray(40)
    ka2_file[0] = 0x07
    ka2_file[2] = 0x10
    k2f = P.parse_keepalive2_response(bytes(ka2_file), file_packet=True)
    out["responses"]["keepalive2_file_ok"] = {
        "input": hx(bytes(ka2_file)),
        "ok": k2f.ok,
        "detail": k2f.detail,
    }

    ka2_bad = bytearray(40)
    ka2_bad[0] = 0x07
    ka2_bad[2] = 0x30
    k2b = P.parse_keepalive2_response(bytes(ka2_bad))
    out["responses"]["keepalive2_bad"] = {
        "input": hx(bytes(ka2_bad)),
        "ok": k2b.ok,
        "detail": k2b.detail,
    }

    notice = bytearray(32)
    notice[0] = 0x4D
    out["responses"]["notification"] = {
        "input": hx(bytes(notice)),
        "is_notification": P.looks_like_notification(bytes(notice)),
    }

    # ---- 登录错误码表（文案 + 是否致命）------------------------------
    for code in sorted(P._LOGIN_ERROR_TEXT):
        text, advice = P._LOGIN_ERROR_TEXT[code]
        out["login_errors"][str(int(code))] = {
            "text": text,
            "advice": advice,
            "fatal": P.is_fatal_login_error(int(code)),
        }

    dest = REPO / "dart" / "test" / "golden" / "protocol_vectors.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"写入 {dest}（{dest.stat().st_size} 字节）")
    print(f"  登录用例 {len(out['logins'])} 组，挑战包 {len(out['challenge'])} 组")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
