#include "md4.h"
#include <string.h>

static uint32_t rotl32(uint32_t x, int c) { return (x << c) | (x >> (32 - c)); }

#define F(x,y,z) (((x) & (y)) | (~(x) & (z)))
#define G(x,y,z) (((x) & (y)) | ((x) & (z)) | ((y) & (z)))
#define H(x,y,z) ((x) ^ (y) ^ (z))

#define FF(a,b,c,d,k,s) a = rotl32(a + F(b,c,d) + x[k], s)
#define GG(a,b,c,d,k,s) a = rotl32(a + G(b,c,d) + x[k] + 0x5A827999u, s)
#define HH(a,b,c,d,k,s) a = rotl32(a + H(b,c,d) + x[k] + 0x6ED9EBA1u, s)

static void md4_block(uint32_t h[4], const uint8_t *p)
{
    uint32_t x[16];
    for (int i = 0; i < 16; i++)
        x[i] = (uint32_t)p[i*4] | ((uint32_t)p[i*4+1] << 8) |
               ((uint32_t)p[i*4+2] << 16) | ((uint32_t)p[i*4+3] << 24);
    uint32_t a = h[0], b = h[1], c = h[2], d = h[3];

    FF(a,b,c,d, 0, 3); FF(d,a,b,c, 1, 7); FF(c,d,a,b, 2,11); FF(b,c,d,a, 3,19);
    FF(a,b,c,d, 4, 3); FF(d,a,b,c, 5, 7); FF(c,d,a,b, 6,11); FF(b,c,d,a, 7,19);
    FF(a,b,c,d, 8, 3); FF(d,a,b,c, 9, 7); FF(c,d,a,b,10,11); FF(b,c,d,a,11,19);
    FF(a,b,c,d,12, 3); FF(d,a,b,c,13, 7); FF(c,d,a,b,14,11); FF(b,c,d,a,15,19);

    GG(a,b,c,d, 0, 3); GG(d,a,b,c, 4, 5); GG(c,d,a,b, 8, 9); GG(b,c,d,a,12,13);
    GG(a,b,c,d, 1, 3); GG(d,a,b,c, 5, 5); GG(c,d,a,b, 9, 9); GG(b,c,d,a,13,13);
    GG(a,b,c,d, 2, 3); GG(d,a,b,c, 6, 5); GG(c,d,a,b,10, 9); GG(b,c,d,a,14,13);
    GG(a,b,c,d, 3, 3); GG(d,a,b,c, 7, 5); GG(c,d,a,b,11, 9); GG(b,c,d,a,15,13);

    HH(a,b,c,d, 0, 3); HH(d,a,b,c, 8, 9); HH(c,d,a,b, 4,11); HH(b,c,d,a,12,15);
    HH(a,b,c,d, 2, 3); HH(d,a,b,c,10, 9); HH(c,d,a,b, 6,11); HH(b,c,d,a,14,15);
    HH(a,b,c,d, 1, 3); HH(d,a,b,c, 9, 9); HH(c,d,a,b, 5,11); HH(b,c,d,a,13,15);
    HH(a,b,c,d, 3, 3); HH(d,a,b,c,11, 9); HH(c,d,a,b, 7,11); HH(b,c,d,a,15,15);

    h[0] += a; h[1] += b; h[2] += c; h[3] += d;
}

void md4_digest(const uint8_t *data, size_t len, uint8_t out[16])
{
    uint32_t h[4] = { 0x67452301u, 0xefcdab89u, 0x98badcfeu, 0x10325476u };
    size_t i = 0;
    for (; i + 64 <= len; i += 64) md4_block(h, data + i);

    uint8_t tail[128];
    size_t rest = len - i;
    memset(tail, 0, sizeof tail);
    if (rest) memcpy(tail, data + i, rest);
    tail[rest] = 0x80;
    size_t tlen = (rest < 56) ? 64 : 128;
    uint64_t bits = (uint64_t)len * 8;
    for (int k = 0; k < 8; k++) tail[tlen - 8 + k] = (uint8_t)(bits >> (8 * k));
    md4_block(h, tail);
    if (tlen == 128) md4_block(h, tail + 64);

    for (int k = 0; k < 4; k++) {
        out[k*4+0] = (uint8_t)(h[k]);
        out[k*4+1] = (uint8_t)(h[k] >> 8);
        out[k*4+2] = (uint8_t)(h[k] >> 16);
        out[k*4+3] = (uint8_t)(h[k] >> 24);
    }
}
