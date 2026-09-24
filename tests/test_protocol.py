"""Protocol construction tests.

Two layers of evidence:

* :mod:`tests.test_protocol` (this file) checks *invariants* and reproduces the
  credential-free packets captured from a known-good client.
* ``tools/verify_against_log.py`` performs the strongest check — it rebuilds a
  whole 373-byte login packet from real credentials and byte-diffs it against
  the recorded capture.  That one needs the reference log plus the reference
  client's stored credentials, so it stays a local tool rather than a test.

Note on fixtures: the captured login packet is deliberately **not** stored in
this repository.  It contains the account string and ``MD5(0x03 0x01 + seed +
password)`` — unsalted, therefore crackable.  Only packets with no credential
material (challenge request/response, login *reply*) are committed.
"""

from __future__ import annotations

import hashlib

import pytest

from drcom import protocol


# --------------------------------------------------------------------------
# challenge
# --------------------------------------------------------------------------
def test_challenge_request_shape() -> None:
    packet = protocol.build_challenge_packet()
    assert len(packet) == 20
    assert packet[0] == 0x01
    assert packet[1] == 0x02
    assert packet[4] == 0x68
    assert packet[5:] == bytes(15)


def test_challenge_matches_recorded_packet(reference: dict) -> None:
    """Our builder's constant fields match the capture (bytes 2-3 are random)."""
    recorded = bytes.fromhex(reference["challenge_request"])
    built = protocol.build_challenge_packet()
    assert built[0] == recorded[0]
    assert built[1] == recorded[1]
    assert built[4] == recorded[4]
    assert built[5:] == recorded[5:]
    assert len(built) == len(recorded) == 20


def test_challenge_response_parsing(reference: dict) -> None:
    recorded = bytes.fromhex(reference["challenge_response"])
    parsed = protocol.parse_challenge_response(recorded)
    assert parsed.seed.hex() == reference["expected"]["seed"]
    assert parsed.ip == reference["expected"]["ip"]


def test_challenge_response_rejects_bad_opcode(reference: dict) -> None:
    recorded = bytearray(bytes.fromhex(reference["challenge_response"]))
    recorded[0] = 0x03
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_challenge_response(bytes(recorded))


def test_challenge_response_rejects_truncation() -> None:
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_challenge_response(b"\x02" * 10)


# --------------------------------------------------------------------------
# login packet geometry — the section 4.7 padding rules
# --------------------------------------------------------------------------
SEED = bytes((0x21, 0x79, 0x04, 0x0F))
MAC = "AA:BB:CC:DD:EE:FF"
ACCOUNT = "2023000001"


@pytest.mark.parametrize("length", range(1, 25))
def test_login_packet_geometry(length: int) -> None:
    password = "p" * length
    packet = protocol.build_login_packet(ACCOUNT, password, MAC, SEED)

    extra, jlu_padding = protocol._password_padding(length)
    assert len(packet) == protocol.LOGIN_BASE_LEN + extra

    counter = 312 + 2 + length
    assert packet[313] == length
    assert packet[counter] == 0x02
    assert packet[counter + 1] == 0x0C

    ror = (8 - length) if length <= 8 else jlu_padding
    assert packet[counter + ror + 14 : counter + ror + 16] == bytes((0x60, 0xA2))


@pytest.mark.parametrize(
    ("length", "expected_size"),
    [
        (1, 338),
        (8, 338),
        (9, 369),
        (12, 373),  # matches the recorded capture exactly
        (15, 376),
        (16, 374),  # 16 is special-cased: JLU_padding stays 0
    ],
)
def test_login_packet_sizes_match_reference(length: int, expected_size: int) -> None:
    packet = protocol.build_login_packet(ACCOUNT, "p" * length, MAC, SEED)
    assert len(packet) == expected_size


def test_recorded_login_packet_was_373_bytes_with_a_12_char_password() -> None:
    """Cross-check the arithmetic that the capture proves.

    The recorded login was 373 bytes and its byte [313] said the password was
    12 characters long.  Both must fall out of the same formula.
    """
    assert protocol.LOGIN_BASE_LEN + protocol._password_padding(12)[0] == 373


def test_login_packet_fixed_fields() -> None:
    packet = protocol.build_login_packet(ACCOUNT, "secret123", MAC, SEED)
    assert packet[0] == 0x03
    assert packet[1] == 0x01
    assert packet[2] == 0x00
    assert packet[3] == len(ACCOUNT) + 20
    assert packet[20 : 20 + len(ACCOUNT)] == ACCOUNT.encode()
    assert packet[56:58] == bytes((0x20, 0x03))
    assert packet[80] == 0x01
    assert packet[81:85] == bytes(4)
    assert packet[105] == 0x01
    assert packet[110:120] == b"LIYUANYUAN"
    assert packet[142:146] == bytes((10, 10, 10, 10))
    assert packet[146:150] == bytes(4)
    assert packet[310:312] == bytes((0x68, 0x00))
    assert packet[182:191] == bytes((0x44, 0x72, 0x43, 0x4F, 0x4D, 0x00, 0xCF, 0x07, 0x68))
    assert packet[246:286] == b"3dc79f5212e8170acfa9ec95f1d74916542be7b1"


def test_login_packet_os_version_fields() -> None:
    packet = protocol.build_login_packet(ACCOUNT, "secret123", MAC, SEED)
    assert int.from_bytes(packet[162:166], "little") == 0x94
    assert int.from_bytes(packet[166:170], "little") == 6
    assert int.from_bytes(packet[170:174], "little") == 2
    assert int.from_bytes(packet[174:178], "little") == 0x23F0
    assert int.from_bytes(packet[178:182], "little") == 0x02


def test_checksum1_is_self_consistent() -> None:
    packet = protocol.build_login_packet(ACCOUNT, "secret123", MAC, SEED)
    assert packet[97:105] == protocol.compute_checksum1(packet)


def test_checksum2_is_self_consistent() -> None:
    packet = protocol.build_login_packet(ACCOUNT, "secret123", MAC, SEED)
    counter = 312 + 2 + len("secret123")
    mac_bytes = protocol.mac_to_bytes(MAC)
    assert packet[counter + 2 : counter + 6] == protocol.compute_checksum2(
        packet[: counter + 2], mac_bytes
    )
    assert packet[counter + 8 : counter + 14] == mac_bytes


def test_mac_xor_md5a_matches_packet() -> None:
    packet = protocol.build_login_packet(ACCOUNT, "secret123", MAC, SEED)
    expected = protocol._mac_xor_md5a(packet[4:20], protocol.mac_to_bytes(MAC))
    assert packet[58:64] == expected


def test_md5b_hashes_nine_plus_length_bytes() -> None:
    """Regression guard for the spec/implementation discrepancy.

    Section 4.6 of the written spec says the digest input is ``1 + len(pw) + 4``
    bytes.  The implementation that actually authenticates allocates
    ``9 + len(pw)`` bytes and hashes the whole buffer, so four NUL bytes are
    part of the input.  We follow the implementation, and this test pins it.
    """
    password = b"secret123"
    ours = protocol.md5b(password, SEED)
    spec_variant = hashlib.md5(b"\x01" + password + SEED).digest()
    reference_variant = hashlib.md5(b"\x01" + password + SEED + bytes(4)).digest()

    assert ours == reference_variant
    assert ours != spec_variant


def test_md5a_definition() -> None:
    assert protocol.md5a(b"pw", SEED) == hashlib.md5(b"\x03\x01" + SEED + b"pw").digest()


# --------------------------------------------------------------------------
# password length limits
# --------------------------------------------------------------------------
def test_password_length_supported_range() -> None:
    assert protocol.password_length_supported(1)
    assert protocol.password_length_supported(16)
    assert not protocol.password_length_supported(17)
    assert not protocol.password_length_supported(0)


def test_long_password_does_not_crash() -> None:
    """Lengths >16 are outside the reference; we still produce a packet."""
    packet = protocol.build_login_packet(ACCOUNT, "x" * 40, MAC, SEED)
    assert len(packet) == protocol.LOGIN_BASE_LEN + protocol._password_padding(40)[0]


# --------------------------------------------------------------------------
# MAC parsing
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text", ["AA:BB:CC:DD:EE:FF", "aa:bb:cc:dd:ee:ff", "AA-BB-CC-DD-EE-FF"]
)
def test_mac_parsing_accepts_common_forms(text: str) -> None:
    assert protocol.mac_to_bytes(text) == bytes((0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF))


def test_mac_parsing_rejects_compact_form() -> None:
    """Compact 'aabbccddeeff' is ambiguous with the auth server's format, so we refuse it."""
    with pytest.raises(ValueError):
        protocol.mac_to_bytes("aabbccddeeff")


@pytest.mark.parametrize("text", ["", "AA:BB:CC", "ZZ:BB:CC:DD:EE:FF", "AA:BB:CC:DD:EE"])
def test_mac_parsing_rejects_garbage(text: str) -> None:
    with pytest.raises(ValueError):
        protocol.mac_to_bytes(text)


# --------------------------------------------------------------------------
# keepalive
# --------------------------------------------------------------------------
def test_keepalive1_packet1_is_the_documented_constant() -> None:
    assert protocol.KEEPALIVE1_PACKET1 == bytes((0x07, 0x01, 0x08, 0x00, 0x01, 0x00, 0x00, 0x00))


def test_keepalive1_packet2_is_42_bytes_not_38(reference: dict) -> None:
    """Spec 4.8/6.2: the reference declares 38 bytes but sends 42."""
    auth = bytes.fromhex(reference["expected"]["auth_information"])
    packet = protocol.build_keepalive1_packet2(SEED, auth)
    assert len(packet) == 42
    assert packet[0] == 0xFF
    assert packet[8:12] == SEED
    assert packet[12:20] == protocol.gen_crc(SEED, SEED[0] & 3)
    assert packet[20:36] == auth
    assert packet[38:42] == bytes(4)


def test_keepalive1_packet2_rejects_wrong_sizes() -> None:
    with pytest.raises(ValueError):
        protocol.build_keepalive1_packet2(b"\x00\x00\x00", bytes(16))
    with pytest.raises(ValueError):
        protocol.build_keepalive1_packet2(SEED, bytes(8))


def test_gen_crc_type0_is_a_constant() -> None:
    assert protocol.gen_crc(SEED, 0) == bytes((0xC7, 0x2F, 0x31, 0x01, 0x7E, 0x00, 0x00, 0x00))


def test_gen_crc_types_1_to_3_use_the_right_digest() -> None:
    from drcom.md4 import md4

    md5_hash = hashlib.md5(SEED).digest()
    assert protocol.gen_crc(SEED, 1) == bytes(
        (md5_hash[2], md5_hash[3], md5_hash[8], md5_hash[9], md5_hash[5], md5_hash[6], md5_hash[13], md5_hash[14])
    )

    md4_hash = md4(SEED)
    assert protocol.gen_crc(SEED, 2) == bytes(
        (md4_hash[1], md4_hash[2], md4_hash[8], md4_hash[9], md4_hash[4], md4_hash[5], md4_hash[11], md4_hash[12])
    )

    sha1_hash = hashlib.sha1(SEED).digest()
    assert protocol.gen_crc(SEED, 3) == bytes(
        (sha1_hash[2], sha1_hash[3], sha1_hash[9], sha1_hash[10], sha1_hash[5], sha1_hash[6], sha1_hash[15], sha1_hash[16])
    )


def test_gen_crc_rejects_unknown_type() -> None:
    with pytest.raises(protocol.ProtocolError):
        protocol.gen_crc(SEED, 4)


@pytest.mark.parametrize("pkt_type", [1, 3])
def test_keepalive2_packet_shape(pkt_type: int) -> None:
    packet = protocol.build_keepalive2_packet(7, pkt_type=pkt_type, tail=b"\xde\xad\xbe\xef")
    assert len(packet) == 40
    assert packet[0] == 0x07
    assert packet[1] == 7
    assert packet[2] == 0x28
    assert packet[4] == 0x0B
    assert packet[5] == pkt_type
    assert packet[6:8] == bytes((0xDC, 0x02))
    assert packet[8:10] == bytes((0x2F, 0x12))
    if pkt_type == 3:
        assert packet[16:20] == b"\xde\xad\xbe\xef"


def test_keepalive2_file_packet_uses_the_file_version() -> None:
    packet = protocol.build_keepalive2_packet(0, file_packet=True, pkt_type=1)
    assert packet[6:8] == bytes((0x0F, 0x27))


def test_keepalive2_counter_wraps_to_one_byte() -> None:
    assert protocol.build_keepalive2_packet(256)[1] == 0
    assert protocol.build_keepalive2_packet(511)[1] == 255


# --------------------------------------------------------------------------
# response parsing
# --------------------------------------------------------------------------
def test_login_response_success(reference: dict) -> None:
    outcome = protocol.parse_login_response(bytes.fromhex(reference["login_response_ok"]))
    assert outcome.success
    assert outcome.auth_information.hex() == reference["expected"]["auth_information"]
    assert len(outcome.auth_information) == 16


@pytest.mark.parametrize("code", [0x01, 0x02, 0x03, 0x04, 0x05, 0x07, 0x0B, 0x14, 0x15, 0x16, 0x17])
def test_login_failure_codes_have_human_text(code: int) -> None:
    reply = bytes([0x05, 0, 0, 0, code]) + bytes(95)
    outcome = protocol.parse_login_response(reply)
    assert not outcome.success
    assert outcome.error_code == code
    assert "未知错误" not in outcome.message
    assert outcome.advice


def test_login_error_code_table_matches_spec() -> None:
    assert protocol.LoginError.WRONG_PASSWORD == 0x03
    assert protocol.LoginError.MAC_CHECK_FAILED == 0x01
    assert protocol.LoginError.SERVER_BUSY == 0x02
    assert protocol.LoginError.MUST_USE_DHCP == 0x17
    assert protocol.LoginError.TOO_MANY_IPS == 0x14
    assert protocol.LoginError.IP_AND_MAC_MISMATCH == 0x16


def test_wrong_password_is_fatal_server_busy_is_not() -> None:
    assert protocol.is_fatal_login_error(protocol.LoginError.WRONG_PASSWORD)
    assert not protocol.is_fatal_login_error(protocol.LoginError.SERVER_BUSY)
    # 0x15 behaves as a wrong password on this network, so do not spin on it.
    assert protocol.is_fatal_login_error(protocol.LoginError.CLIENT_UPGRADE_REQUIRED)


def test_login_response_rejects_unknown_opcode() -> None:
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_login_response(bytes([0x09]) + bytes(99))


def test_keepalive1_response_parsing() -> None:
    reply = bytes([0x07]) + bytes(7) + SEED + bytes(88)
    result = protocol.parse_keepalive1_response(reply)
    assert result.seed == SEED
    assert result.encrypt_type == SEED[0] & 3


def test_notification_packets_are_recognised() -> None:
    assert protocol.looks_like_notification(bytes([0x4D]) + bytes(20))
    assert not protocol.looks_like_notification(bytes([0x07]) + bytes(20))
    assert not protocol.looks_like_notification(b"")


def test_keepalive2_response_file_packet() -> None:
    ok = protocol.parse_keepalive2_response(bytes([0x07, 0, 0x10]) + bytes(37), file_packet=True)
    assert ok.ok
    tolerated = protocol.parse_keepalive2_response(bytes([0x07, 0, 0x28]) + bytes(37), file_packet=True)
    assert tolerated.ok
    bad = protocol.parse_keepalive2_response(bytes([0x07, 0, 0x11]) + bytes(37), file_packet=True)
    assert not bad.ok


def test_keepalive2_response_carries_the_tail() -> None:
    reply = bytes([0x07, 0, 0x28]) + bytes(13) + b"\xca\xfe\xba\xbe" + bytes(20)
    result = protocol.parse_keepalive2_response(reply)
    assert result.ok
    assert result.tail == b"\xca\xfe\xba\xbe"


def test_keepalive2_response_rejects_bad_opcodes() -> None:
    assert not protocol.parse_keepalive2_response(bytes([0x08, 0, 0x28]) + bytes(37)).ok
    assert not protocol.parse_keepalive2_response(bytes([0x07, 0, 0x29]) + bytes(37)).ok
    assert not protocol.parse_keepalive2_response(b"\x07").ok
