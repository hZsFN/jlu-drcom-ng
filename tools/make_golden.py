#!/usr/bin/env python3
"""Generate C golden vectors for the native core, straight from drcom/protocol.py.

The C implementation in ``native/`` must reproduce every byte the Python
reference produces.  This script is the referee: it imports the Python module,
builds the expected bytes for a fixed list of cases, and writes them out as a C
header (``native/tests/golden.h``) so the C test has zero parsing to do.

Run from the repository root::

    python tools/make_golden.py

Re-run whenever drcom/protocol.py changes; the C test will then fail until the
C side catches up.  That is the point.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import random
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from drcom import protocol as P  # noqa: E402
from drcom.md4 import md4  # noqa: E402

ACCOUNT = "202300000001"
MAC = "AA:BB:CC:DD:EE:FF"

CASES: list[dict] = []


def add(name: str, kind: str, **kw) -> None:
    kw["case"] = name
    kw["kind"] = kind
    CASES.append(kw)


# --- login packets: every password-length branch --------------------------
for pw in ("a", "abc12345", "abcdefgh", "abcdefghi", "abcdefghijkl", "abcdefghijklmnop"):
    for seed_hex in ("11223344", "DEADBEEF"):
        add(f"login_pw{len(pw)}_{seed_hex}", "login",
            account=ACCOUNT, password=pw, mac=MAC, seed_hex=seed_hex)

for mac in ("00:11:22:33:44:55", "aa-bb-cc-dd-ee-ff"):
    tag = mac.replace(":", "").replace("-", "")
    add(f"login_mac{tag}", "login",
        account=ACCOUNT, password="abc12345", mac=mac, seed_hex="11223344")

# --- challenge ------------------------------------------------------------
for rnd in ("0000", "ABCD", "FFFF"):
    add(f"challenge_{rnd}", "challenge", rng_hex=rnd)

# --- checksum1 ------------------------------------------------------------
add("checksum1_zero", "checksum1", data_hex="00" * 97)
add("checksum1_seq", "checksum1", data_hex="".join(f"{(i * 7 + 3) & 0xFF:02X}" for i in range(97)))

# --- keepalive-1 packet 2 (all four CRC types) ----------------------------
for seed_hex in ("00000000", "FC112233", "01AABBCC", "02DDEEFF", "03FFFFFF"):
    add(f"ka1p2_{seed_hex}", "ka1p2", seed_hex=seed_hex,
        data_hex="00112233445566778899AABBCCDDEEFF", rng_hex="5A5A")

# --- keepalive-2 ----------------------------------------------------------
add("ka2_type1", "ka2", counter=7, pkt_type=1, file_packet=0, tail_hex="")
add("ka2_type1_file", "ka2", counter=3, pkt_type=1, file_packet=1, tail_hex="")
add("ka2_type3", "ka2", counter=9, pkt_type=3, file_packet=0, tail_hex="DEADBEEF")

# --- gen_crc --------------------------------------------------------------
for et in range(4):
    add(f"gencrc_et{et}", "gencrc", seed_hex="11AABBCC", encrypt_type=et)

# --- hash primitives ------------------------------------------------------
for pw in ("abc12345", "abcdefghijklmnop"):
    add(f"md5a_{pw}", "md5a", password=pw, seed_hex="11223344")
    add(f"md5b_{pw}", "md5b", password=pw, seed_hex="11223344")

for text in (b"", b"a", b"abc", b"message digest", b"abcdefghijklmnopqrstuvwxyz"):
    tag = text.hex() or "empty"
    add(f"md4_{tag}", "md4", data_hex=text.hex())
    add(f"md5_{tag}", "md5", data_hex=text.hex())

# --- response parsing -----------------------------------------------------
challenge_resp = bytes([0x02]) + bytes(3) + bytes([0x11, 0x22, 0x33, 0x44]) \
    + bytes(12) + bytes([10, 100, 61, 99]) + bytes(52)
add("parse_challenge_ok", "parse_challenge", data_hex=challenge_resp.hex())

ka1_resp = bytes([0x07]) + bytes(7) + bytes([0xFC, 0x11, 0x22, 0x33])
add("parse_ka1_ok", "parse_ka1", data_hex=ka1_resp.hex())

login_ok = bytearray(100)
login_ok[0] = 0x04
login_ok[23:39] = bytes(range(16))
add("parse_login_ok", "parse_login", data_hex=bytes(login_ok).hex())

login_bad = bytearray(100)
login_bad[0] = 0x05
login_bad[4] = 0x0B
add("parse_login_mac_mismatch", "parse_login", data_hex=bytes(login_bad).hex())

ka2_resp = bytearray(40)
ka2_resp[0] = 0x07
ka2_resp[2] = 0x28
ka2_resp[16:20] = bytes([0xC0, 0xFF, 0xEE, 0x01])
add("parse_ka2_ok", "parse_ka2", data_hex=bytes(ka2_resp).hex())

ka2_file = bytearray(40)
ka2_file[0] = 0x07
ka2_file[2] = 0x10
add("parse_ka2_file", "parse_ka2_file", data_hex=bytes(ka2_file).hex())


def hexb(text: str) -> bytes:
    return bytes.fromhex(text)


def expected(case: dict) -> str:
    kind = case["kind"]
    if kind == "login":
        return P.build_login_packet(
            case["account"], case["password"], case["mac"], hexb(case["seed_hex"])).hex()
    if kind == "challenge":
        out = bytearray(P.build_challenge_packet(random.Random(0)))
        out[2] = int(case["rng_hex"][0:2], 16)
        out[3] = int(case["rng_hex"][2:4], 16)
        return bytes(out).hex()
    if kind == "checksum1":
        return P.compute_checksum1(hexb(case["data_hex"])).hex()
    if kind == "ka1p2":
        out = bytearray(P.build_keepalive1_packet2(
            hexb(case["seed_hex"]), hexb(case["data_hex"]), random.Random(0)))
        out[36] = int(case["rng_hex"][0:2], 16)
        out[37] = int(case["rng_hex"][2:4], 16)
        return bytes(out).hex()
    if kind == "ka2":
        return P.build_keepalive2_packet(
            case["counter"], file_packet=bool(case["file_packet"]),
            pkt_type=case["pkt_type"],
            tail=hexb(case["tail_hex"]) if case["tail_hex"] else b"").hex()
    if kind == "gencrc":
        return P.gen_crc(hexb(case["seed_hex"]), case["encrypt_type"]).hex()
    if kind == "md5a":
        return P.md5a(case["password"].encode(), hexb(case["seed_hex"])).hex()
    if kind == "md5b":
        return P.md5b(case["password"].encode(), hexb(case["seed_hex"])).hex()
    if kind == "md4":
        return md4(hexb(case["data_hex"])).hex()
    if kind == "md5":
        return hashlib.md5(hexb(case["data_hex"])).hexdigest()
    return ""  # parsers are asserted structurally in the C test


C_FIELDS = (
    ("account", "s"), ("password", "s"), ("mac", "s"), ("seed_hex", "s"),
    ("data_hex", "s"), ("tail_hex", "s"), ("rng_hex", "s"),
    ("counter", "i"), ("pkt_type", "i"), ("file_packet", "i"),
    ("encrypt_type", "i"), ("expect_hex", "s"),
)


def c_str(value) -> str:
    if value is None:
        return '""'
    if isinstance(value, int):
        return str(value)
    return '"' + str(value) + '"'


def main() -> int:
    for case in CASES:
        case["expect_hex"] = expected(case)
    for case in CASES:
        case.setdefault("counter", 0)
        case.setdefault("pkt_type", 0)
        case.setdefault("file_packet", 0)
        case.setdefault("encrypt_type", 0)

    out = pathlib.Path(ROOT / "native" / "tests")
    out.mkdir(parents=True, exist_ok=True)

    lines = [
        "/* Generated by tools/make_golden.py - do not edit by hand. */",
        "#ifndef DRCOMD_GOLDEN_H",
        "#define DRCOMD_GOLDEN_H",
        "",
        "typedef struct {",
        "    const char *name;",
        "    const char *kind;",
    ]
    for field, typ in C_FIELDS:
        lines.append(f"    {'const char *' if typ == 's' else 'int'} {field};")
    lines += ["} golden_case;", "", "static const golden_case GOLDEN[] = {"]
    for case in CASES:
        values = [c_str(case["case"]), c_str(case["kind"])]
        for field, typ in C_FIELDS:
            values.append(c_str(case.get(field, 0)))
        lines.append("    { " + ", ".join(values) + " },")
    lines += [
        "};",
        f"#define GOLDEN_COUNT {len(CASES)}",
        "",
        "#endif",
        "",
    ]
    (out / "golden.h").write_text("\n".join(lines), encoding="utf-8")
    (out / "golden_cases.json").write_text(
        json.dumps(CASES, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(CASES)} cases -> native/tests/golden.h")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
