"""MD4 correctness — RFC 1320 test vectors."""

from __future__ import annotations

import pytest

from drcom.md4 import md4, md4_hex

VECTORS = [
    (b"", "31d6cfe0d16ae931b73c59d7e0c089c0"),
    (b"a", "bde52cb31de33e46245e05fbdbd6fb24"),
    (b"abc", "a448017aaf21d8525fc10ae87aa6729d"),
    (b"message digest", "d9130a8164549fe818874806e1c7014b"),
    (b"abcdefghijklmnopqrstuvwxyz", "d79e1c308aa5bbcdeea8ed63df412da9"),
    (
        b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789",
        "043f8582f241db351ce627e153e7f0e4",
    ),
    (b"1234567890" * 8, "e33b4ddc9c38f2199c3e7b164fcc0536"),
]


@pytest.mark.parametrize(("data", "expected"), VECTORS)
def test_rfc1320_vectors(data: bytes, expected: str) -> None:
    assert md4_hex(data) == expected


def test_rfc1320_million_a_vector() -> None:
    """The last RFC vector — kept separate so its huge id does not flood the report."""
    assert md4_hex(b"a" * 1000000) == "bbce80cc6bb65e5c6745e30d4eeca9a4"


def test_returns_16_bytes() -> None:
    assert len(md4(b"anything")) == 16


def test_hashlib_has_no_md4_so_we_need_our_own() -> None:
    """Document *why* this module exists (spec 6.2)."""
    import hashlib

    assert "md4" not in hashlib.algorithms_available


def test_block_boundary_lengths() -> None:
    """Padding must be correct at 55/56/64-byte boundaries."""
    # 56 bytes is exactly where the length field needs a second block.
    for length in (54, 55, 56, 57, 63, 64, 65, 119, 120, 128):
        digest = md4(b"\x00" * length)
        assert len(digest) == 16
        # Length-extension sanity: different lengths → different digests.
        assert digest != md4(b"\x00" * (length + 1))
