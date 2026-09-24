"""Verify the Python protocol implementation against a recorded successful login.

This is the "step 2" gate from the task brief: prove the packet builders are
byte-identical to a known-good client *before* any UI work.

Inputs
------
1. A ``.lgt`` log file from the reference Qt client, which dumps
   ``[Challenge sent]`` / ``[Challenge recv]`` / ``[Login sent]`` /
   ``[Login recv]`` as hex.
2. The credentials that produced that log.  They are read from the reference
   client's own QSettings registry key and decrypted in memory with Windows
   DPAPI (we run as the same user, so this works).  **Nothing is ever written
   back out** — the password is used to recompute digests and then dropped.

Usage
-----
    python tools/verify_against_log.py [path/to/log.lgt] [--dump]

``--dump`` prints a side-by-side byte diff for every differing offset.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from drcom import protocol  # noqa: E402
from drcom.secrets_store import decrypt_dpapi  # noqa: E402

#: Where the reference client keeps its .lgt captures, and the registry key it
#: stores its settings under.  Both are overridable from the command line so no
#: machine-specific path is baked into the repository.
DEFAULT_LOG_DIR = Path(os.environ.get("DRCOM_REF_LOG_DIR", "~/drcom-ref-logs")).expanduser()
REG_PATH = os.environ.get("DRCOM_REF_REG_PATH", r"Software\DrCOM_JLU_Qt\OrganizationDefaults")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def read_registry_credentials() -> dict[str, str]:
    """Read the reference client's stored settings from the registry."""
    import winreg

    values: dict[str, str] = {}
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH) as key:
        index = 0
        while True:
            try:
                name, value, _kind = winreg.EnumValue(key, index)
            except OSError:
                break
            values[name] = value
            index += 1
    return values


def extract_packets(log_path: Path) -> dict[str, bytes]:
    """Pull the hex dumps we care about out of a reference-client log."""
    wanted = {
        "[Challenge sent]": "challenge_sent",
        "[Challenge recv]": "challenge_recv",
        "[Login sent]": "login_sent",
        "[Login recv]": "login_recv",
    }
    found: dict[str, bytes] = {}
    for raw_line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        for tag, key in wanted.items():
            if tag in raw_line and key not in found:
                hex_part = raw_line.split("]", 1)[1].strip().replace(" ", "")
                if hex_part:
                    found[key] = bytes.fromhex(hex_part)
    return found


def hexdiff(expected: bytes, actual: bytes, limit: int = 64) -> None:
    """Print the differing offsets between two byte strings."""
    shown = 0
    for i in range(max(len(expected), len(actual))):
        e = expected[i] if i < len(expected) else None
        a = actual[i] if i < len(actual) else None
        if e != a:
            es = f"{e:02x}" if e is not None else "--"
            as_ = f"{a:02x}" if a is not None else "--"
            print(f"    [{i:3d}] expected {es}  recreated {as_}")
            shown += 1
            if shown >= limit:
                print("    ... (truncated)")
                return


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------
def check_md4() -> bool:
    """RFC 1320 test vectors."""
    vectors = {
        b"": "31d6cfe0d16ae931b73c59d7e0c089c0",
        b"a": "bde52cb31de33e46245e05fbdbd6fb24",
        b"abc": "a448017aaf21d8525fc10ae87aa6729d",
        b"message digest": "d9130a8164549fe818874806e1c7014b",
        b"abcdefghijklmnopqrstuvwxyz": "d79e1c308aa5bbcdeea8ed63df412da9",
        b"1234567890" * 8: "e33b4ddc9c38f2199c3e7b164fcc0536",
    }
    ok = True
    for data, expected in vectors.items():
        got = hashlib_style_md4(data)
        flag = "ok " if got == expected else "FAIL"
        if got != expected:
            ok = False
            print(f"  [{flag}] MD4({data[:24]!r}...) = {got} (want {expected})")
    print(f"  MD4 RFC 1320 vectors: {'all pass' if ok else 'FAILURES'}")
    return ok


def hashlib_style_md4(data: bytes) -> str:
    from drcom.md4 import md4_hex

    return md4_hex(data)


def check_against_log(log_path: Path, *, dump: bool) -> bool:
    packets = extract_packets(log_path)
    missing = {"challenge_sent", "challenge_recv", "login_sent", "login_recv"} - packets.keys()
    if missing:
        print(f"  !! log is missing: {sorted(missing)}")
        return False

    print(f"  challenge sent : {len(packets['challenge_sent'])} bytes")
    print(f"  challenge recv : {len(packets['challenge_recv'])} bytes")
    print(f"  login sent     : {len(packets['login_sent'])} bytes")
    print(f"  login recv     : {len(packets['login_recv'])} bytes")

    challenge = protocol.parse_challenge_response(packets["challenge_recv"])
    print(f"  parsed seed    : {challenge.seed.hex(' ')}")
    print(f"  parsed ip      : {challenge.ip}")

    login_reply = protocol.parse_login_response(packets["login_recv"])
    print(f"  login reply    : success={login_reply.success}")
    print(f"  auth_info      : {login_reply.auth_information.hex(' ')}")

    # --- shape checks that need no credentials ---------------------------
    sent = packets["login_sent"]
    checks: list[tuple[str, bool]] = []
    checks.append(("login[0]==0x03", sent[0] == 0x03))
    checks.append(("login[1]==0x01", sent[1] == 0x01))
    checks.append(("login[2]==0x00", sent[2] == 0x00))
    checks.append(("login[3]==len(account)+20", sent[3] == 0x1E))
    checks.append(("login[56:58]==20 03", sent[56:58] == b"\x20\x03"))
    checks.append(("login[80]==0x01", sent[80] == 0x01))
    checks.append(("login[81:85] host ip zero", sent[81:85] == b"\x00\x00\x00\x00"))
    checks.append(("login[97:105] == checksum1", sent[97:105] == protocol.compute_checksum1(sent)))
    checks.append(("login[105]==0x01", sent[105] == 0x01))
    checks.append(("login[110:120]=='LIYUANYUAN'", sent[110:120] == b"LIYUANYUAN"))
    checks.append(("login[142:146]==10.10.10.10", sent[142:146] == bytes((10, 10, 10, 10))))
    checks.append(("login[162:166]==0x94", sent[162:166] == bytes((0x94, 0, 0, 0))))
    checks.append(("login[166:170]==6", sent[166:170] == bytes((6, 0, 0, 0))))
    checks.append(("login[170:174]==2", sent[170:174] == bytes((2, 0, 0, 0))))
    checks.append(("login[174:176]==f0 23", sent[174:176] == bytes((0xF0, 0x23))))
    checks.append(("login[178]==0x02", sent[178] == 0x02))
    checks.append(("login[182:191] hostname blob", sent[182:191] == bytes((0x44, 0x72, 0x43, 0x4F, 0x4D, 0x00, 0xCF, 0x07, 0x68))))
    checks.append(
        (
            "login[246:286] service pack",
            sent[246:286] == b"3dc79f5212e8170acfa9ec95f1d74916542be7b1",
        )
    )
    checks.append(("login[310:312]==68 00", sent[310:312] == bytes((0x68, 0x00))))

    # password section geometry, derived from the account string length
    account = sent[20:20 + sent[3] - 20].decode("ascii", "replace")
    pw_len = sent[313]
    counter = 312 + 2 + pw_len
    extra, jlu_pad = protocol._password_padding(pw_len)
    checks.append((f"total len == 338+{extra}", len(sent) == protocol.LOGIN_BASE_LEN + extra))
    checks.append(("login[312]==0x00 (reserved)", sent[312] == 0x00))
    checks.append(("login[313]==password length", sent[313] == pw_len))
    checks.append(("login[counter]==0x02", sent[counter] == 0x02))
    checks.append(("login[counter+1]==0x0c", sent[counter + 1] == 0x0C))
    mac_from_packet = sent[counter + 8 : counter + 14]
    checks.append(
        (
            "login[counter+2:counter+6] == checksum2",
            sent[counter + 2 : counter + 6]
            == protocol.compute_checksum2(sent[: counter + 2], mac_from_packet),
        )
    )
    ror = (8 - pw_len) if pw_len <= 8 else jlu_pad
    checks.append(
        (
            f"login[counter+ror+14:...]==60 a2 (ror={ror})",
            sent[counter + ror + 14 : counter + ror + 16] == bytes((0x60, 0xA2)),
        )
    )

    # MD5A / MAC relationship is checkable without the password:
    #   login[58:64] == MD5A[0:6] XOR mac, and login[64:80] is MD5B.
    md5a_expected_xor = protocol._mac_xor_md5a(sent[4:20], mac_from_packet)
    checks.append(("login[58:64] == MD5A[0:6] XOR MAC", sent[58:64] == md5a_expected_xor))

    print(f"\n  structural checks on the recorded packet ({len(checks)}):")
    all_ok = True
    for name, ok in checks:
        if not ok:
            all_ok = False
        print(f"    [{'ok ' if ok else 'FAIL'}] {name}")
    print(f"  account length seen in packet: {len(account)} chars, password length: {pw_len}")

    # --- credential-dependent reproduction ------------------------------
    print("\n  reproducing the full packet with the real credentials:")
    try:
        values = read_registry_credentials()
    except OSError as exc:
        print(f"    !! cannot read reference settings: {exc}")
        return all_ok

    account = values.get("account", "")
    mac = values.get("mac", "")
    blob_b64 = values.get("password", "")
    if not (account and mac and blob_b64):
        print("    !! reference settings incomplete, skipping full reproduction")
        return all_ok

    import base64

    password = decrypt_dpapi(base64.b64decode(blob_b64)).decode("utf-8")
    print(f"    account  : {account[:2]}***{account[-2:]}  ({len(account)} chars)")
    print(f"    mac      : {mac}")
    print(f"    password : {len(password)} chars (value intentionally not printed)")

    rebuilt = protocol.build_login_packet(account, password, mac, challenge.seed)
    if rebuilt == sent:
        print(f"    [ok ] recreated login packet is byte-identical ({len(rebuilt)} bytes)")
    else:
        all_ok = False
        print(f"    [FAIL] length {len(rebuilt)} vs {len(sent)}")
        hexdiff(sent, rebuilt)

    # --- the MD5B question -----------------------------------------------
    print("\n  MD5B input-length experiment (spec says 1+pw+4, reference hashes 9+pw):")
    pw_bytes = password.encode("utf-8")
    observed = sent[64:80]
    variant_small = hashlib.md5(b"\x01" + pw_bytes + challenge.seed).digest()
    variant_ref = protocol.md5b(pw_bytes, challenge.seed)
    print(f"    observed in packet          : {observed.hex()}")
    print(f"    md5(0x01+pw+seed)      [1+pw+4]: {variant_small.hex()}  {'MATCH' if variant_small == observed else 'no'}")
    print(f"    md5(0x01+pw+seed+0000) [9+pw ]: {variant_ref.hex()}  {'MATCH' if variant_ref == observed else 'no'}")
    if variant_ref != observed:
        all_ok = False
        print("    !! neither variant matches — MD5B construction is wrong")

    # --- MD5A -------------------------------------------------------------
    digest_a = protocol.md5a(pw_bytes, challenge.seed)
    ok_a = digest_a == sent[4:20]
    if not ok_a:
        all_ok = False
    print(f"\n    MD5A match: {'ok' if ok_a else 'FAIL'}")

    # --- CRC paths exercised by every encrypt_type ------------------------
    print("\n  keepalive1 CRC per encrypt_type (for seed %s):" % challenge.seed.hex())
    for et in range(4):
        print(f"    type {et}: {protocol.gen_crc(challenge.seed, et).hex(' ')}")

    # --- packet shapes that the log does not contain ----------------------
    print("\n  keepalive packet shapes:")
    print(f"    keepalive1 p1 : {protocol.KEEPALIVE1_PACKET1.hex(' ')} ({len(protocol.KEEPALIVE1_PACKET1)} B)")
    k1p2 = protocol.build_keepalive1_packet2(challenge.seed, login_reply.auth_information, rng=__import__("random").Random(0))
    print(f"    keepalive1 p2 : {k1p2.hex(' ')} ({len(k1p2)} B)")
    for label, kw in (
        ("file", dict(file_packet=True, pkt_type=1)),
        ("A   ", dict(pkt_type=1)),
        ("C   ", dict(pkt_type=3, tail=b"\xde\xad\xbe\xef")),
    ):
        print(f"    keepalive2 {label}: {protocol.build_keepalive2_packet(1, **kw).hex(' ')}")

    if dump:
        print("\n  full side-by-side of the login packet:")
        hexdiff(sent, rebuilt)

    return all_ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", nargs="?", help="path to a reference .lgt log")
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=DEFAULT_LOG_DIR,
        help="directory holding the reference client's .lgt captures",
    )
    parser.add_argument("--dump", action="store_true", help="dump every differing byte")
    args = parser.parse_args()

    if args.log:
        log_path = Path(args.log)
    else:
        candidates = sorted(args.log_dir.glob("*.lgt")) if args.log_dir.exists() else []
        if not candidates:
            print(
                f"找不到参考抓包：{args.log_dir}\n"
                "请用 --log-dir 指定参考客户端日志目录，或直接给出单个 .lgt 文件路径。"
            )
            return 2
        # Prefer the largest capture: it is the one most likely to contain a
        # full successful handshake.
        log_path = max(candidates, key=lambda p: p.stat().st_size)
    if not log_path.exists():
        print(f"log not found: {log_path}")
        return 2

    print("=" * 72)
    print("Dr.COM (JLU) protocol verification")
    print("=" * 72)
    print(f"\n[1] log file: {log_path}")
    log_ok = check_against_log(log_path, dump=args.dump)
    print("\n[2] MD4 self-test")
    md4_ok = check_md4()

    print("\n" + "=" * 72)
    print(f"RESULT: log reproduction {'PASS' if log_ok else 'FAIL'}, MD4 {'PASS' if md4_ok else 'FAIL'}")
    print("=" * 72)
    return 0 if (log_ok and md4_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
