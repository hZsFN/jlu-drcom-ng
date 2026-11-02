#!/usr/bin/env python3
"""A fake JLU Dr.COM server, so the native engine can be exercised offline.

Speaks just enough of the wire protocol to drive drcomd through a full
challenge -> login -> keepalive cycle.  Packet layouts come from
drcom/protocol.py, which is the reference the native side is diffed against.

Usage::

    python native/tests/mock_server.py --port 51440 [--duration 20]

Every exchange is logged to stdout as one line so a failing test can be
diagnosed without a packet capture.
"""

from __future__ import annotations

import argparse
import socket
import sys
import time

SEED = bytes((0x11, 0x22, 0x33, 0x44))
KA1_SEED = bytes((0x00, 0x00, 0x00, 0x00))     # encrypt type 0 -> constant CRC
AUTH_INFO = bytes(range(0x80, 0x90))
TAIL = bytes((0xC0, 0xFF, 0xEE, 0x01))


def challenge_reply() -> bytes:
    out = bytearray(76)
    out[0] = 0x02
    out[4:8] = SEED
    out[20:24] = bytes((10, 100, 61, 99))
    return bytes(out)


def login_reply() -> bytes:
    out = bytearray(100)
    out[0] = 0x04
    out[23:39] = AUTH_INFO
    return bytes(out)


def login_failure(code: int) -> bytes:
    out = bytearray(100)
    out[0] = 0x05
    out[4] = code
    return bytes(out)


def keepalive2_reply(is_file: bool) -> bytes:
    out = bytearray(40)
    out[0] = 0x07
    out[2] = 0x10 if is_file else 0x28
    out[16:20] = TAIL
    return bytes(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=51440)
    ap.add_argument("--duration", type=float, default=30.0)
    ap.add_argument("--fail-code", type=lambda v: int(v, 0), default=0,
                    help="non-zero: answer the login with this failure code")
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", args.port))
    sock.settimeout(0.5)
    deadline = time.time() + args.duration
    print(f"mock server listening on 127.0.0.1:{args.port}", flush=True)

    counts: dict[str, int] = {}
    while time.time() < deadline:
        try:
            data, peer = sock.recvfrom(2048)
        except socket.timeout:
            continue
        if not data:
            continue
        head = data[0]

        if head == 0x01:                      # challenge
            counts["challenge"] = counts.get("challenge", 0) + 1
            sock.sendto(challenge_reply(), peer)
            print(f"  challenge #{counts['challenge']} ({len(data)}B) -> 76B", flush=True)

        elif head == 0x03:                    # login
            counts["login"] = counts.get("login", 0) + 1
            if args.fail_code:
                sock.sendto(login_failure(args.fail_code), peer)
                print(f"  login #{counts['login']} -> failure 0x{args.fail_code:02X}", flush=True)
            else:
                sock.sendto(login_reply(), peer)
                print(f"  login #{counts['login']} ({len(data)}B) -> success", flush=True)

        elif head == 0x07 and len(data) == 8:  # keepalive-1 probe
            counts["ka1p1"] = counts.get("ka1p1", 0) + 1
            out = bytearray(24)
            out[0] = 0x07
            out[8:12] = KA1_SEED
            sock.sendto(bytes(out), peer)
            print(f"  keepalive1 probe #{counts['ka1p1']} -> seed answer", flush=True)

        elif head == 0xFF and len(data) == 42:  # keepalive-1 second packet
            counts["ka1p2"] = counts.get("ka1p2", 0) + 1
            out = bytearray(24)
            out[0] = 0x07
            sock.sendto(bytes(out), peer)
            print(f"  keepalive1 answer #{counts['ka1p2']}", flush=True)

        elif head == 0x07 and len(data) == 40:  # keepalive-2
            is_file = data[6:8] == bytes((0x0F, 0x27))
            ptype = "file" if is_file else ("A" if data[5] == 1 else f"C({data[5]})")
            counts[f"ka2_{ptype}"] = counts.get(f"ka2_{ptype}", 0) + 1
            sock.sendto(keepalive2_reply(is_file), peer)
            print(f"  keepalive2 {ptype} #{counts[f'ka2_{ptype}']} (counter={data[1]})", flush=True)

        else:
            print(f"  unexpected {len(data)}B head=0x{head:02X}", flush=True)

    print("totals: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
