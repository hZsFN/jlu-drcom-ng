#include "sha1.h"
#include <string.h>

static uint32_t rotl32(uint32_t x, int c) { return (x << c) | (x >> (32 - c)); }

static void sha1_block(uint32_t h[5], const uint8_t *p)
{
    uint32_t w[80];
    for (int i = 0; i < 16; i++)
        w[i] = ((uint32_t)p[i*4] << 24) | ((uint32_t)p[i*4+1] << 16) |
               ((uint32_t)p[i*4+2] << 8) | (uint32_t)p[i*4+3];
    for (int i = 16; i < 80; i++)
        w[i] = rotl32(w[i-3] ^ w[i-8] ^ w[i-14] ^ w[i-16], 1);

    uint32_t a = h[0], b = h[1], c = h[2], d = h[3], e = h[4];
    for (int i = 0; i < 80; i++) {
        uint32_t f, k;
        if (i < 20)      { f = (b & c) | (~b & d);           k = 0x5A827999u; }
        else if (i < 40) { f = b ^ c ^ d;                    k = 0x6ED9EBA1u; }
        else if (i < 60) { f = (b & c) | (b & d) | (c & d);  k = 0x8F1BBCDCu; }
        else             { f = b ^ c ^ d;                    k = 0xCA62C1D6u; }
        uint32_t t = rotl32(a, 5) + f + e + k + w[i];
        e = d; d = c; c = rotl32(b, 30); b = a; a = t;
    }
    h[0] += a; h[1] += b; h[2] += c; h[3] += d; h[4] += e;
}

void sha1_digest(const uint8_t *data, size_t len, uint8_t out[20])
{
    uint32_t h[5] = { 0x67452301u, 0xEFCDAB89u, 0x98BADCFEu, 0x10325476u, 0xC3D2E1F0u };
    size_t i = 0;
    for (; i + 64 <= len; i += 64) sha1_block(h, data + i);

    uint8_t tail[128];
    size_t rest = len - i;
    memset(tail, 0, sizeof tail);
    if (rest) memcpy(tail, data + i, rest);
    tail[rest] = 0x80;
    size_t tlen = (rest < 56) ? 64 : 128;
    uint64_t bits = (uint64_t)len * 8;
    for (int k = 0; k < 8; k++) tail[tlen - 1 - k] = (uint8_t)(bits >> (8 * k));
    sha1_block(h, tail);
    if (tlen == 128) sha1_block(h, tail + 64);

    for (int k = 0; k < 5; k++) {
        out[k*4+0] = (uint8_t)(h[k] >> 24);
        out[k*4+1] = (uint8_t)(h[k] >> 16);
        out[k*4+2] = (uint8_t)(h[k] >> 8);
        out[k*4+3] = (uint8_t)(h[k]);
    }
}
