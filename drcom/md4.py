"""Pure-Python MD4 (RFC 1320).

Why hand-rolled: ``hashlib`` does not ship MD4 on modern OpenSSL builds
(OpenSSL 3 moved it to the legacy provider), and pulling in a third-party
crypto package just for one 40-line hash would bloat the single-file exe.
The JLU Dr.COM server uses MD4 as encryption type 2 for the keepalive CRC,
so we need it.

Verified against the RFC 1320 test vectors, see ``tests/test_md4.py``.
"""

from __future__ import annotations

import struct

__all__ = ["md4", "md4_hex"]

_MASK = 0xFFFFFFFF


def _lrot(value: int, count: int) -> int:
    return ((value << count) | (value >> (32 - count))) & _MASK


def md4(data: bytes) -> bytes:
    """Return the 16-byte MD4 digest of *data*."""
    # --- padding -------------------------------------------------------
    original_bit_len = len(data) * 8
    padded = bytearray(data)
    padded.append(0x80)
    while len(padded) % 64 != 56:
        padded.append(0x00)
    padded += struct.pack("<Q", original_bit_len & 0xFFFFFFFFFFFFFFFF)

    h0, h1, h2, h3 = 0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476

    # --- one pass per 64-byte block ------------------------------------
    for offset in range(0, len(padded), 64):
        x = struct.unpack("<16I", padded[offset : offset + 64])
        a, b, c, d = h0, h1, h2, h3

        # Round 1: F(x,y,z) = (x & y) | (~x & z)
        for i in range(16):
            k = i
            s = (3, 7, 11, 19)[i % 4]
            f = (b & c) | (~b & d)
            a = _lrot((a + f + x[k]) & _MASK, s)
            a, b, c, d = d, a, b, c

        # Round 2: G(x,y,z) = (x & y) | (x & z) | (y & z)  + sqrt(2) const
        for i in range(16):
            k = (0, 4, 8, 12, 1, 5, 9, 13, 2, 6, 10, 14, 3, 7, 11, 15)[i]
            s = (3, 5, 9, 13)[i % 4]
            g = (b & c) | (b & d) | (c & d)
            a = _lrot((a + g + x[k] + 0x5A827999) & _MASK, s)
            a, b, c, d = d, a, b, c

        # Round 3: H(x,y,z) = x ^ y ^ z  + sqrt(3) const
        for i in range(16):
            k = (0, 8, 4, 12, 2, 10, 6, 14, 1, 9, 5, 13, 3, 11, 7, 15)[i]
            s = (3, 9, 11, 15)[i % 4]
            h = b ^ c ^ d
            a = _lrot((a + h + x[k] + 0x6ED9EBA1) & _MASK, s)
            a, b, c, d = d, a, b, c

        h0 = (h0 + a) & _MASK
        h1 = (h1 + b) & _MASK
        h2 = (h2 + c) & _MASK
        h3 = (h3 + d) & _MASK

    return struct.pack("<4I", h0, h1, h2, h3)


def md4_hex(data: bytes) -> str:
    """Hex digest helper (mainly for tests / logs)."""
    return md4(data).hex()
