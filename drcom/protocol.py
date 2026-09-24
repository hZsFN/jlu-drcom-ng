"""Dr.COM (JLU variant) wire protocol — packet construction and checksums.

Everything in this module is pure: bytes in, bytes out, no sockets.  That
makes it trivially testable against the recorded packet dumps in
``tests/data/`` (which were produced by a known-good implementation).

Offsets quoted in the comments are 0-based and match the C++ reference
implementation that the captured traffic came from.
"""

from __future__ import annotations

import hashlib
import os
import struct
from dataclasses import dataclass, field
from enum import IntEnum

from .md4 import md4

__all__ = [
    "AUTH_SERVER",
    "AUTH_PORT",
    "BIND_PORT",
    "RECV_TIMEOUT_MS",
    "KEEPALIVE_INTERVAL_S",
    "Keepalive1Result",
    "LoginError",
    "LoginOutcome",
    "ProtocolError",
    "build_challenge_packet",
    "build_keepalive1_packet2",
    "build_keepalive2_packet",
    "build_login_packet",
    "gen_crc",
    "parse_challenge_response",
    "parse_keepalive2_response",
    "parse_login_response",
    "mac_to_bytes",
]

# --------------------------------------------------------------------------
# Network parameters (protocol spec, section 4.1)
# --------------------------------------------------------------------------
AUTH_SERVER = "10.100.61.3"
AUTH_PORT = 61440
BIND_PORT = 61440
RECV_TIMEOUT_MS = 3000
KEEPALIVE_INTERVAL_S = 20.0

#: Login packet length when the password is 8 characters or shorter.
LOGIN_BASE_LEN = 338

#: Offset where the variable-length password section starts.
_PW_SECTION_OFFSET = 312

#: The fixed 40-byte service-pack blob the client is expected to send.
_SERVICE_PACK = b"3dc79f5212e8170acfa9ec95f1d74916542be7b1"

#: The fixed hostname blob: b"DrCOM" followed by fixed bytes.
_HOSTNAME_BLOB = bytes((0x44, 0x72, 0x43, 0x4F, 0x4D, 0x00, 0xCF, 0x07, 0x68))

#: First keepalive-1 packet is a constant.
KEEPALIVE1_PACKET1 = bytes((0x07, 0x01, 0x08, 0x00, 0x01, 0x00, 0x00, 0x00))

#: The "file packet" magic used on the first keepalive-2 round.
_KEEPALIVE_VERSION_FILE = bytes((0x0F, 0x27))
#: Version bytes for ordinary keepalive-2 packets.
_KEEPALIVE_VERSION_NORMAL = bytes((0xDC, 0x02))

#: Keepalive-2 packet size (fixed).
KEEPALIVE2_LEN = 40
#: Keepalive-1 second packet size — 42, **not** 38.  The reference declares
#: a 38-byte array but sends 42 bytes; the server accepts 42.  See section 4.8.
KEEPALIVE1_PACKET2_LEN = 42


class ProtocolError(Exception):
    """Raised when a received packet does not match the expected shape."""


# --------------------------------------------------------------------------
# Login failure codes — server sends these in response byte [4] when [0]==0x05
# --------------------------------------------------------------------------
class LoginError(IntEnum):
    MAC_CHECK_FAILED = 0x01
    SERVER_BUSY = 0x02
    WRONG_PASSWORD = 0x03
    INSUFFICIENT_BALANCE = 0x04
    ACCOUNT_FROZEN = 0x05
    IP_MISMATCH = 0x07
    MAC_MISMATCH = 0x0B
    TOO_MANY_IPS = 0x14
    CLIENT_UPGRADE_REQUIRED = 0x15
    IP_AND_MAC_MISMATCH = 0x16
    MUST_USE_DHCP = 0x17


#: Human-readable (Simplified Chinese) explanation per failure code, plus the
#: concrete next step we suggest to the user.
_LOGIN_ERROR_TEXT: dict[int, tuple[str, str]] = {
    LoginError.MAC_CHECK_FAILED: ("MAC 校验失败", "网卡物理地址被改动或客户端伪造 MAC，请核对本机 MAC 是否与账号绑定的网卡一致。"),
    LoginError.SERVER_BUSY: ("服务器忙", "认证服务器当前过载，稍等片刻会自动重试（无需手动操作）。"),
    LoginError.WRONG_PASSWORD: ("密码错误", "请检查校园网密码；若密码刚改过，请在此处同步更新。"),
    LoginError.INSUFFICIENT_BALANCE: ("余额不足", "网费余额不足，请先充值后再登录。"),
    LoginError.ACCOUNT_FROZEN: ("账号被冻结", "账号已被停机/冻结，请联系网络中心处理。"),
    LoginError.IP_MISMATCH: ("IP 不匹配", "本机 IP 与账号绑定的 IP 不一致，请确认网线插在正确的端口上，或改为自动获取 IP（DHCP）。"),
    LoginError.MAC_MISMATCH: ("MAC 不匹配", "本机网卡 MAC 与账号绑定的不一致。若刚更换网卡/主板，需要先在自助服务里解绑，或使用旧网卡的 MAC。"),
    LoginError.TOO_MANY_IPS: ("IP 数超限", "该账号同时在线的设备数已达上限，请先注销其它设备。"),
    LoginError.CLIENT_UPGRADE_REQUIRED: ("需要升级客户端", "服务端要求升级客户端；该码在本校网络下通常等同于密码错误，请先核对密码。"),
    LoginError.IP_AND_MAC_MISMATCH: ("IP 和 MAC 都不匹配", "本机 IP 与网卡 MAC 均与账号绑定信息不符，请检查接入端口并确认使用了正确的网卡。"),
    LoginError.MUST_USE_DHCP: ("必须使用 DHCP", "服务端要求本机通过 DHCP 获取地址：请把网卡设置为「自动获得 IP 地址」。"),
}

#: Per-code retry hint.  ``True`` == retrying verbatim will never help, so the
#: reconnect loop must stop and wait for the user to fix the configuration.
_LOGIN_ERROR_FATAL: dict[int, bool] = {
    LoginError.MAC_CHECK_FAILED: True,
    LoginError.WRONG_PASSWORD: True,
    LoginError.INSUFFICIENT_BALANCE: True,
    LoginError.ACCOUNT_FROZEN: True,
    LoginError.IP_MISMATCH: True,
    LoginError.MAC_MISMATCH: True,
    LoginError.TOO_MANY_IPS: True,
    LoginError.CLIENT_UPGRADE_REQUIRED: True,  # behaves as wrong password at JLU
    LoginError.IP_AND_MAC_MISMATCH: True,
    LoginError.MUST_USE_DHCP: True,
    LoginError.SERVER_BUSY: False,  # transient, retry with backoff
}


def describe_login_error(code: int) -> tuple[str, str]:
    """Return ``(short_text, advice)`` for a login failure code."""
    entry = _LOGIN_ERROR_TEXT.get(code)
    if entry:
        return entry
    return (
        f"未知错误（错误码 0x{code:02X}）",
        f"服务端返回了未收录的错误码 0x{code:02X}，请把日志反馈给开发者。",
    )


def is_fatal_login_error(code: int) -> bool:
    """Whether retrying the same credentials is pointless."""
    return _LOGIN_ERROR_FATAL.get(code, False)


# --------------------------------------------------------------------------
# Crypto helpers (section 4.6)
# --------------------------------------------------------------------------
def mac_to_bytes(mac: str) -> bytes:
    """Parse ``aa:bb:cc:dd:ee:ff`` (or ``aa-bb-...``) into 6 bytes."""
    cleaned = mac.strip().replace("-", ":").replace(".", ":")
    parts = [p for p in cleaned.split(":") if p]
    if len(parts) != 6:
        raise ValueError(f"MAC 地址格式不正确：{mac!r}（应形如 AA:BB:CC:DD:EE:FF）")
    try:
        out = bytes(int(p, 16) for p in parts)
    except ValueError as exc:
        raise ValueError(f"MAC 地址含非十六进制字符：{mac!r}") from exc
    if any(len(p) > 2 for p in parts):
        raise ValueError(f"MAC 地址分段过长：{mac!r}")
    return out


def md5a(password: bytes, seed: bytes) -> bytes:
    """``MD5(0x03 0x01 + seed + password)`` — section 4.6."""
    return hashlib.md5(b"\x03\x01" + seed + password).digest()


def md5b(password: bytes, seed: bytes) -> bytes:
    """``MD5(0x01 + password + seed + 0x00000000)``.

    Note the four trailing NUL bytes.  The written spec (section 4.6) says the
    digest input is ``1 + len(password) + 4`` bytes, but the *working*
    implementation allocates ``9 + len(password)`` bytes, zero-fills it, and
    hashes the whole buffer — so the four zeros are part of the input.  We
    follow the implementation because the captured successful login proves it
    is what the server accepts.  ``tests/test_protocol.py`` keeps a regression
    test for the exact byte length.
    """
    buf = bytearray(9 + len(password))
    buf[0] = 0x01
    buf[1 : 1 + len(password)] = password
    buf[1 + len(password) : 5 + len(password)] = seed
    # bytes [5+len .. 9+len) stay zero
    return hashlib.md5(bytes(buf)).digest()


def _mac_xor_md5a(digest: bytes, mac_bytes: bytes) -> bytes:
    """XOR MD5A's first 6 bytes with the MAC, both read big-endian."""
    sum_ = 0
    for i in range(6):
        sum_ = digest[i] + sum_ * 256
    mac_int = 0
    for i in range(6):
        mac_int = mac_bytes[i] + mac_int * 256
    sum_ ^= mac_int
    return bytes((sum_ >> (8 * (5 - i))) & 0xFF for i in range(6))


#: Passwords longer than this are outside what the reference implementation
#: actually defines — see :func:`password_length_supported`.
REFERENCE_MAX_PASSWORD_LEN = 16


def password_length_supported(length: int) -> bool:
    """Whether *length* is covered by the verified reference behaviour.

    The reference implementation XORs every password byte with ``MD5A[i]``,
    but ``MD5A`` is only 16 bytes long.  For passwords longer than 16
    characters that is a buffer over-read — the reference reads whatever
    happens to sit after the array on the stack, so its output is not
    reproducible.  We substitute a deterministic extension (the digest repeats)
    so the client still runs, but a length >16 is *not* verified and the UI
    warns about it.
    """
    return 1 <= length <= REFERENCE_MAX_PASSWORD_LEN


def _password_padding(password_len: int) -> tuple[int, int]:
    """Return ``(extra_length, jlu_padding)`` for the variable-length section.

    ``extra_length`` is what gets added to :data:`LOGIN_BASE_LEN`; it is 0 for
    passwords of 8 characters or fewer.
    """
    if password_len <= 8:
        return 0, 0
    jlu_padding = password_len // 4 if password_len != 16 else 0
    return 28 + password_len - 8 + jlu_padding, jlu_padding


def compute_checksum1(packet: bytes) -> bytes:
    """``MD5(packet[0:97] + [0x14,0x00,0x07,0x0b])[0:8]`` — section 4.6."""
    return hashlib.md5(packet[:97] + bytes((0x14, 0x00, 0x07, 0x0B))).digest()[:8]


def compute_checksum2(packet_prefix: bytes, mac_bytes: bytes) -> bytes:
    """The XOR-fold checksum written just after the password section.

    *packet_prefix* must be ``login_packet[0 : counter+2)``.  The scratch
    buffer mirrors the reference implementation: 6 bytes of
    ``01 26 07 11 00 00`` then the MAC, then implicit zeros.
    """
    counter = len(packet_prefix) - 2
    buf = bytearray(counter + 18)
    buf[0 : counter + 2] = packet_prefix
    buf[counter + 2 : counter + 8] = bytes((0x01, 0x26, 0x07, 0x11, 0x00, 0x00))
    buf[counter + 8 : counter + 14] = mac_bytes

    acc = 1234
    for i in range(0, counter + 14, 4):
        chunk = buf[i : i + 4]
        if len(chunk) < 4:  # pragma: no cover - defensive, buffer is big enough
            chunk = chunk + bytes(4 - len(chunk))
        (ret,) = struct.unpack("<I", chunk)
        acc ^= ret
    acc = (1968 * acc) & 0xFFFFFFFF
    return struct.pack("<I", acc)


# --------------------------------------------------------------------------
# Packet builders
# --------------------------------------------------------------------------
def build_challenge_packet(rng: "random.Random | None" = None) -> bytes:
    """Build the 20-byte challenge packet (section 4.2)."""
    if rng is None:
        rand_bytes = os.urandom(2)
    else:
        rand_bytes = bytes((rng.randrange(256), rng.randrange(256)))
    packet = bytearray(20)
    packet[0] = 0x01
    packet[1] = 0x02
    packet[2] = rand_bytes[0]
    packet[3] = rand_bytes[1]
    packet[4] = 0x68
    return bytes(packet)


def build_login_packet(account: str, password: str, mac: str, seed: bytes) -> bytes:
    """Build the variable-length login packet (sections 4.3 + 4.7).

    :raises ValueError: if *seed* is not 4 bytes or *mac* is malformed.
    """
    if len(seed) != 4:
        raise ValueError(f"seed 必须是 4 字节，收到 {len(seed)}")

    account_bytes = account.encode("utf-8")
    password_bytes = password.encode("utf-8")
    mac_bytes = mac_to_bytes(mac)

    extra, jlu_padding = _password_padding(len(password_bytes))
    packet = bytearray(LOGIN_BASE_LEN + extra)

    # --- fixed header -------------------------------------------------
    packet[0] = 0x03
    packet[1] = 0x01
    packet[2] = 0x00
    packet[3] = len(account_bytes) + 20

    digest_a = md5a(password_bytes, seed)
    packet[4:20] = digest_a
    packet[20 : 20 + len(account_bytes)] = account_bytes

    packet[56] = 0x20
    packet[57] = 0x03
    packet[58:64] = _mac_xor_md5a(digest_a, mac_bytes)
    packet[64:80] = md5b(password_bytes, seed)

    packet[80] = 0x01
    # packet[81:85] = host IP, all zero (leave as-is)

    packet[97:105] = compute_checksum1(bytes(packet))
    packet[105] = 0x01
    packet[110:120] = b"LIYUANYUAN"

    packet[142:146] = bytes((10, 10, 10, 10))  # primary DNS
    # packet[146:150] = DHCP server, all zero

    # OS version structure (Windows 6.2 build 0x23f0, platform 2)
    struct.pack_into("<I", packet, 162, 0x94)
    struct.pack_into("<I", packet, 166, 6)
    struct.pack_into("<I", packet, 170, 2)
    struct.pack_into("<I", packet, 174, 0x23F0)
    struct.pack_into("<I", packet, 178, 0x02)

    packet[182:191] = _HOSTNAME_BLOB
    packet[246:286] = _SERVICE_PACK

    packet[310] = 0x68
    packet[311] = 0x00

    # --- variable-length password section -----------------------------
    counter = _PW_SECTION_OFFSET
    packet[counter + 1] = len(password_bytes)
    counter += 2

    for i, ch in enumerate(password_bytes):
        # `digest_a[i]` for i >= 16 is a deliberate, documented extension: the
        # reference reads out of bounds there, which is not reproducible.  See
        # password_length_supported().
        mixed = digest_a[i % len(digest_a)] ^ ch
        # rotate left by 3 within a byte
        packet[counter + i] = (((mixed << 3) & 0xFF) + (mixed >> 5)) & 0xFF
    counter += len(password_bytes)

    packet[counter] = 0x02
    packet[counter + 1] = 0x0C

    packet[counter + 2 : counter + 6] = compute_checksum2(bytes(packet[: counter + 2]), mac_bytes)
    packet[counter + 8 : counter + 14] = mac_bytes

    ror_padding = (8 - len(password_bytes)) if len(password_bytes) <= 8 else jlu_padding
    packet[counter + ror_padding + 14] = 0x60
    packet[counter + ror_padding + 15] = 0xA2

    return bytes(packet)


def gen_crc(seed: bytes, encrypt_type: int) -> bytes:
    """8-byte CRC for keepalive-1 packet 2 (section 4.6)."""
    if encrypt_type == 0:
        return bytes((0xC7, 0x2F, 0x31, 0x01, 0x7E, 0x00, 0x00, 0x00))
    if encrypt_type == 1:
        h = hashlib.md5(seed).digest()
        return bytes((h[2], h[3], h[8], h[9], h[5], h[6], h[13], h[14]))
    if encrypt_type == 2:
        h = md4(seed)
        return bytes((h[1], h[2], h[8], h[9], h[4], h[5], h[11], h[12]))
    if encrypt_type == 3:
        h = hashlib.sha1(seed).digest()
        return bytes((h[2], h[3], h[9], h[10], h[5], h[6], h[15], h[16]))
    raise ProtocolError(f"未知的 encrypt_type: {encrypt_type}")


def build_keepalive1_packet2(
    keepalive1_seed: bytes, auth_information: bytes, rng: "random.Random | None" = None
) -> bytes:
    """Build the 42-byte second keepalive-1 packet (section 4.8)."""
    if len(keepalive1_seed) != 4:
        raise ValueError(f"keepalive1_seed 必须是 4 字节，收到 {len(keepalive1_seed)}")
    if len(auth_information) != 16:
        raise ValueError(f"auth_information 必须是 16 字节，收到 {len(auth_information)}")

    if rng is None:
        tail = os.urandom(2)
    else:
        tail = bytes((rng.randrange(256), rng.randrange(256)))

    packet = bytearray(KEEPALIVE1_PACKET2_LEN)
    packet[0] = 0xFF
    packet[8:12] = keepalive1_seed
    packet[12:20] = gen_crc(keepalive1_seed, keepalive1_seed[0] & 3)
    packet[20:36] = auth_information
    packet[36] = tail[0]
    packet[37] = tail[1]
    # packet[38:42] stay zero
    return bytes(packet)


def build_keepalive2_packet(
    counter: int, *, file_packet: bool = False, pkt_type: int = 1, tail: bytes = b""
) -> bytes:
    """Build a 40-byte keepalive-2 packet (section 4.9).

    *pkt_type* is 1 for the A packet and 3 for the C packet.  For type 3 the
    4-byte *tail* taken from the previous response goes at offset 16.
    """
    packet = bytearray(KEEPALIVE2_LEN)
    packet[0] = 0x07
    packet[1] = counter & 0xFF
    packet[2] = 0x28
    packet[4] = 0x0B
    packet[5] = pkt_type
    packet[6:8] = _KEEPALIVE_VERSION_FILE if file_packet else _KEEPALIVE_VERSION_NORMAL
    packet[8] = 0x2F
    packet[9] = 0x12
    if pkt_type == 3:
        packet[16:20] = tail if len(tail) == 4 else bytes(4)
        # packet[28:32] = host IP, all zero
    return bytes(packet)


# --------------------------------------------------------------------------
# Response parsers
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ChallengeResponse:
    """Parsed 76-byte challenge reply."""

    seed: bytes
    ip: str


def parse_challenge_response(data: bytes) -> ChallengeResponse:
    """Validate and parse the challenge reply (section 4.2)."""
    if len(data) < 24:
        raise ProtocolError(f"challenge 响应长度不足：{len(data)} 字节（期望 76）")
    if data[0] != 0x02:
        raise ProtocolError(f"challenge 响应首字节应为 0x02，实际 0x{data[0]:02X}")
    return ChallengeResponse(seed=data[4:8], ip=".".join(str(b) for b in data[20:24]))


@dataclass(frozen=True)
class LoginOutcome:
    """Parsed login reply."""

    success: bool
    auth_information: bytes = b""
    error_code: int | None = None
    raw: bytes = field(default=b"", repr=False)

    @property
    def message(self) -> str:
        if self.success:
            return "登录成功"
        if self.error_code is None:
            return "登录失败（服务端未返回错误码）"
        return describe_login_error(self.error_code)[0]

    @property
    def advice(self) -> str:
        if self.success:
            return ""
        if self.error_code is None:
            return "服务端返回了无法识别的响应，请查看日志面板中的原始报文。"
        return describe_login_error(self.error_code)[1]


def parse_login_response(data: bytes) -> LoginOutcome:
    """Parse the 100-byte login reply (sections 4.4 + 4.5)."""
    if not data:
        raise ProtocolError("登录响应为空")
    if data[0] == 0x04:
        if len(data) < 39:
            raise ProtocolError(f"登录成功响应长度不足：{len(data)} 字节（需要 ≥39）")
        return LoginOutcome(success=True, auth_information=data[23:39], raw=bytes(data))
    if data[0] == 0x05:
        code = data[4] if len(data) > 4 else None
        return LoginOutcome(success=False, error_code=code, raw=bytes(data))
    raise ProtocolError(f"登录响应首字节异常：0x{data[0]:02X}（长度 {len(data)}）")


@dataclass(frozen=True)
class Keepalive1Result:
    """Outcome of keepalive-1."""

    seed: bytes
    encrypt_type: int


@dataclass(frozen=True)
class Keepalive2Result:
    """Outcome of a single keepalive-2 exchange."""

    ok: bool
    tail: bytes = b""
    detail: str = ""


def parse_keepalive2_response(data: bytes, *, file_packet: bool = False) -> Keepalive2Result:
    """Validate a keepalive-2 reply (section 4.9)."""
    if len(data) < 3:
        return Keepalive2Result(False, detail=f"响应过短（{len(data)} 字节）")
    if data[0] != 0x07:
        return Keepalive2Result(False, detail=f"首字节应为 0x07，实际 0x{data[0]:02X}")
    if file_packet:
        # 0x10 == "filepacket received"; 0x28 is also tolerated.
        if data[2] not in (0x10, 0x28):
            return Keepalive2Result(False, detail=f"file packet 响应 [2]=0x{data[2]:02X}")
        return Keepalive2Result(True, detail="file packet 已接收")
    if data[2] != 0x28:
        return Keepalive2Result(False, detail=f"响应 [2] 应为 0x28，实际 0x{data[2]:02X}")
    tail = bytes(data[16:20]) if len(data) >= 20 else b""
    return Keepalive2Result(True, tail=tail)


def parse_keepalive1_response(data: bytes) -> Keepalive1Result:
    """Validate the first keepalive-1 reply and extract the seed.

    Notification packets (``[0] == 0x4D``) are reported via
    :class:`NotificationPacket` so the caller can keep waiting.
    """
    if len(data) < 12:
        raise ProtocolError(f"keepalive1 响应过短：{len(data)} 字节")
    if data[0] != 0x07:
        raise ProtocolError(f"keepalive1 响应首字节应为 0x07，实际 0x{data[0]:02X}")
    return Keepalive1Result(seed=data[8:12], encrypt_type=data[8] & 3)


class NotificationPacket(Exception):
    """Signals a server *notice* packet (opcode 0x4D) during keepalive-1.

    These carry server-side announcements and must be ignored; the client keeps
    waiting for the real keepalive-1 reply.
    """

    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        super().__init__(f"收到服务端通知包（{len(raw)} 字节）")


def looks_like_notification(data: bytes) -> bool:
    """True when *data* is a server notice packet that should be skipped."""
    return bool(data) and data[0] == 0x4D
