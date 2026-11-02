#include "protocol.h"
#include "md5.h"
#include "md4.h"
#include "sha1.h"
#include <string.h>

/* 40 ASCII characters + NUL: the array must hold 41 or the compiler warns
 * (C4295) that the terminator does not fit.  Only the first 40 bytes are
 * ever copied onto the wire. */
static const uint8_t SERVICE_PACK[41] = "3dc79f5212e8170acfa9ec95f1d74916542be7b1";
static const uint8_t HOSTNAME_BLOB[9] = { 0x44,0x72,0x43,0x4F,0x4D,0x00,0xCF,0x07,0x68 };
static const uint8_t KA1_PACKET1[8] = { 0x07,0x01,0x08,0x00,0x01,0x00,0x00,0x00 };
static const uint8_t KA_VERSION_FILE[2]   = { 0x0F, 0x27 };
static const uint8_t KA_VERSION_NORMAL[2] = { 0xDC, 0x02 };

const uint8_t *proto_keepalive1_pkt1(void) { return KA1_PACKET1; }

/* ---------------------------------------------------------------- MAC */
int proto_mac_to_bytes(const char *mac, uint8_t out[6])
{
    if (!mac || !out) return PROTO_ERR_ARGS;
    uint8_t nib[12];
    int n = 0, digits = 0;
    for (const char *p = mac; *p && n < 12; p++) {
        char c = *p;
        if (c == ':' || c == '-' || c == '.') {
            if (digits == 0) return PROTO_ERR_ARGS;  /* empty segment */
            digits = 0;
            continue;
        }
        int v;
        if (c >= '0' && c <= '9') v = c - '0';
        else if (c >= 'a' && c <= 'f') v = c - 'a' + 10;
        else if (c >= 'A' && c <= 'F') v = c - 'A' + 10;
        else return PROTO_ERR_ARGS;
        if (digits >= 2) return PROTO_ERR_ARGS;      /* segment longer than 2 */
        nib[n++] = (uint8_t)v;
        digits++;
    }
    if (n != 12 || digits != 2) return PROTO_ERR_ARGS;
    for (int i = 0; i < 6; i++) out[i] = (uint8_t)((nib[2*i] << 4) | nib[2*i+1]);
    return PROTO_OK;
}

/* ------------------------------------------------------------ padding */
int proto_password_padding(int pw_len, int *extra, int *jlu_padding)
{
    if (!extra || !jlu_padding) return PROTO_ERR_ARGS;
    if (pw_len <= 8) { *extra = 0; *jlu_padding = 0; return PROTO_OK; }
    int jlu = (pw_len != 16) ? pw_len / 4 : 0;
    *extra = 28 + pw_len - 8 + jlu;
    *jlu_padding = jlu;
    return PROTO_OK;
}

/* --------------------------------------------------------------- MD5s */
void proto_md5a(const uint8_t *pw, size_t pwlen, const uint8_t seed[4], uint8_t out[16])
{
    uint8_t buf[6 + 256];
    if (pwlen > 256) pwlen = 256;
    buf[0] = 0x03; buf[1] = 0x01;
    memcpy(buf + 2, seed, 4);
    if (pwlen) memcpy(buf + 6, pw, pwlen);
    md5_digest(buf, 6 + pwlen, out);
}

void proto_md5b(const uint8_t *pw, size_t pwlen, const uint8_t seed[4], uint8_t out[16])
{
    /* 1 + password + seed + four zero bytes; the zeros are load-bearing. */
    uint8_t buf[1 + 256 + 4 + 4];
    if (pwlen > 256) pwlen = 256;
    memset(buf, 0, sizeof buf);
    buf[0] = 0x01;
    if (pwlen) memcpy(buf + 1, pw, pwlen);
    memcpy(buf + 1 + pwlen, seed, 4);
    md5_digest(buf, 1 + pwlen + 4 + 4, out);
}

void proto_mac_xor_md5a(const uint8_t digest[16], const uint8_t mac[6], uint8_t out[6])
{
    uint64_t sum = 0;
    for (int i = 0; i < 6; i++) sum = digest[i] + sum * 256;
    uint64_t mac_int = 0;
    for (int i = 0; i < 6; i++) mac_int = mac[i] + mac_int * 256;
    sum ^= mac_int;
    for (int i = 0; i < 6; i++) out[i] = (uint8_t)((sum >> (8 * (5 - i))) & 0xFF);
}

/* ---------------------------------------------------------- checksums */
void proto_checksum1(const uint8_t *packet, uint8_t out[8])
{
    uint8_t buf[97 + 4];
    memcpy(buf, packet, 97);
    buf[97] = 0x14; buf[98] = 0x00; buf[99] = 0x07; buf[100] = 0x0B;
    uint8_t digest[16];
    md5_digest(buf, 101, digest);
    memcpy(out, digest, 8);
}

void proto_checksum2(const uint8_t *prefix, size_t prefix_len,
                     const uint8_t mac[6], uint8_t out[4])
{
    int counter = (int)prefix_len - 2;
    uint8_t buf[1 + 512];
    memset(buf, 0, sizeof buf);
    memcpy(buf, prefix, prefix_len);
    buf[counter + 2] = 0x01; buf[counter + 3] = 0x26; buf[counter + 4] = 0x07;
    buf[counter + 5] = 0x11; buf[counter + 6] = 0x00; buf[counter + 7] = 0x00;
    memcpy(buf + counter + 8, mac, 6);

    uint32_t acc = 1234;
    for (int i = 0; i < counter + 14; i += 4) {
        uint32_t word = (uint32_t)buf[i] | ((uint32_t)buf[i+1] << 8) |
                        ((uint32_t)buf[i+2] << 16) | ((uint32_t)buf[i+3] << 24);
        acc ^= word;
    }
    acc = (uint32_t)(1968ull * acc);
    out[0] = (uint8_t)(acc); out[1] = (uint8_t)(acc >> 8);
    out[2] = (uint8_t)(acc >> 16); out[3] = (uint8_t)(acc >> 24);
}

int proto_gen_crc(const uint8_t seed[4], int encrypt_type, uint8_t out[8])
{
    static const uint8_t FIXED[8] = { 0xC7,0x2F,0x31,0x01,0x7E,0x00,0x00,0x00 };
    uint8_t h[20];
    switch (encrypt_type) {
    case 0:
        memcpy(out, FIXED, 8);
        return PROTO_OK;
    case 1: {
        static const uint8_t idx[8] = { 2,3,8,9,5,6,13,14 };
        md5_digest(seed, 4, h);
        for (int i = 0; i < 8; i++) out[i] = h[idx[i]];
        return PROTO_OK;
    }
    case 2: {
        static const uint8_t idx[8] = { 1,2,8,9,4,5,11,12 };
        md4_digest(seed, 4, h);
        for (int i = 0; i < 8; i++) out[i] = h[idx[i]];
        return PROTO_OK;
    }
    case 3: {
        static const uint8_t idx[8] = { 2,3,9,10,5,6,15,16 };
        sha1_digest(seed, 4, h);
        for (int i = 0; i < 8; i++) out[i] = h[idx[i]];
        return PROTO_OK;
    }
    default:
        return PROTO_ERR_ARGS;
    }
}

/* ----------------------------------------------------------- builders */
int proto_build_challenge(uint8_t rand2[2], uint8_t out[PROTO_CHALLENGE_LEN])
{
    if (!out) return PROTO_ERR_ARGS;
    memset(out, 0, PROTO_CHALLENGE_LEN);
    out[0] = 0x01;
    out[1] = 0x02;
    if (rand2) { out[2] = rand2[0]; out[3] = rand2[1]; }
    out[4] = 0x68;
    return PROTO_OK;
}

int proto_build_login(const char *account, const char *password, const char *mac,
                      const uint8_t seed[4], uint8_t *out, size_t out_cap, size_t *out_len)
{
    if (!account || !password || !mac || !seed || !out) return PROTO_ERR_ARGS;

    const uint8_t *pw = (const uint8_t *)password;
    size_t pwlen = strlen(password);
    size_t acctlen = strlen(account);
    uint8_t macb[6];
    if (proto_mac_to_bytes(mac, macb) != PROTO_OK) return PROTO_ERR_ARGS;
    if (acctlen + 20 > 255) return PROTO_ERR_ARGS;

    int extra = 0, jlu_padding = 0;
    proto_password_padding((int)pwlen, &extra, &jlu_padding);
    size_t total = (size_t)PROTO_LOGIN_BASE_LEN + (size_t)extra;
    if (total > out_cap) return PROTO_ERR_BUF;

    memset(out, 0, total);

    out[0] = 0x03; out[1] = 0x01; out[2] = 0x00;
    out[3] = (uint8_t)(acctlen + 20);

    uint8_t digest_a[16], digest_b[16], xored[6];
    proto_md5a(pw, pwlen, seed, digest_a);
    memcpy(out + 4, digest_a, 16);
    memcpy(out + 20, account, acctlen);

    out[56] = 0x20; out[57] = 0x03;
    proto_mac_xor_md5a(digest_a, macb, xored);
    memcpy(out + 58, xored, 6);
    proto_md5b(pw, pwlen, seed, digest_b);
    memcpy(out + 64, digest_b, 16);

    out[80] = 0x01;

    proto_checksum1(out, out + 97);
    out[105] = 0x01;
    memcpy(out + 110, "LIYUANYUAN", 10);

    out[142] = 10; out[143] = 10; out[144] = 10; out[145] = 10;

    /* OS version struct: 0x94, 6, 2, 0x23F0, 2 (little-endian u32 each) */
    {
        static const uint32_t vals[5] = { 0x94u, 6u, 2u, 0x23F0u, 2u };
        for (int i = 0; i < 5; i++) {
            uint32_t v = vals[i];
            size_t off = 162 + (size_t)i * 4;
            out[off+0] = (uint8_t)(v);       out[off+1] = (uint8_t)(v >> 8);
            out[off+2] = (uint8_t)(v >> 16); out[off+3] = (uint8_t)(v >> 24);
        }
    }

    memcpy(out + 182, HOSTNAME_BLOB, 9);
    memcpy(out + 246, SERVICE_PACK, 40);

    out[310] = 0x68; out[311] = 0x00;

    size_t counter = PROTO_PW_SECTION_OFFSET;
    out[counter + 1] = (uint8_t)pwlen;
    counter += 2;

    for (size_t i = 0; i < pwlen; i++) {
        uint8_t mixed = (uint8_t)(digest_a[i % 16] ^ pw[i]);
        out[counter + i] = (uint8_t)(((mixed << 3) & 0xFF) + (mixed >> 5));
    }
    counter += pwlen;

    out[counter] = 0x02;
    out[counter + 1] = 0x0C;
    proto_checksum2(out, counter + 2, macb, out + counter + 2);
    memcpy(out + counter + 8, macb, 6);

    int ror_padding = (pwlen <= 8) ? (int)(8 - pwlen) : jlu_padding;
    out[counter + ror_padding + 14] = 0x60;
    out[counter + ror_padding + 15] = 0xA2;

    if (out_len) *out_len = total;
    return PROTO_OK;
}

int proto_build_keepalive1_pkt2(const uint8_t seed[4], const uint8_t auth[16],
                                const uint8_t rand2[2],
                                uint8_t out[PROTO_KEEPALIVE1_PKT2_LEN])
{
    if (!seed || !auth || !out) return PROTO_ERR_ARGS;
    memset(out, 0, PROTO_KEEPALIVE1_PKT2_LEN);
    out[0] = 0xFF;
    memcpy(out + 8, seed, 4);
    if (proto_gen_crc(seed, seed[0] & 3, out + 12) != PROTO_OK) return PROTO_ERR_ARGS;
    memcpy(out + 20, auth, 16);
    if (rand2) { out[36] = rand2[0]; out[37] = rand2[1]; }
    return PROTO_OK;
}

int proto_build_keepalive2(int counter, int file_packet, int pkt_type,
                           const uint8_t *tail4,
                           uint8_t out[PROTO_KEEPALIVE2_LEN])
{
    if (!out) return PROTO_ERR_ARGS;
    memset(out, 0, PROTO_KEEPALIVE2_LEN);
    out[0] = 0x07;
    out[1] = (uint8_t)(counter & 0xFF);
    out[2] = 0x28;
    out[4] = 0x0B;
    out[5] = (uint8_t)pkt_type;
    memcpy(out + 6, file_packet ? KA_VERSION_FILE : KA_VERSION_NORMAL, 2);
    out[8] = 0x2F;
    out[9] = 0x12;
    if (pkt_type == 3 && tail4) memcpy(out + 16, tail4, 4);
    return PROTO_OK;
}

/* ------------------------------------------------------------ parsers */
int proto_parse_challenge(const uint8_t *data, size_t len, proto_challenge_t *out)
{
    if (!data || !out) return PROTO_ERR_ARGS;
    if (len < 24) return PROTO_ERR_SHAPE;
    if (data[0] != 0x02) return PROTO_ERR_SHAPE;
    memcpy(out->seed, data + 4, 4);
    memcpy(out->ip, data + 20, 4);
    return PROTO_OK;
}

int proto_parse_login(const uint8_t *data, size_t len, proto_login_t *out)
{
    if (!data || !out) return PROTO_ERR_ARGS;
    memset(out, 0, sizeof *out);
    out->error_code = -1;
    if (len == 0) return PROTO_ERR_SHAPE;
    if (data[0] == 0x04) {
        if (len < 39) return PROTO_ERR_SHAPE;
        out->success = 1;
        memcpy(out->auth, data + 23, 16);
        return PROTO_OK;
    }
    if (data[0] == 0x05) {
        out->success = 0;
        if (len > 4) out->error_code = data[4];
        return PROTO_OK;
    }
    return PROTO_ERR_SHAPE;
}

int proto_parse_keepalive1(const uint8_t *data, size_t len,
                           uint8_t seed_out[4], int *encrypt_type)
{
    if (!data || !seed_out) return PROTO_ERR_ARGS;
    if (len < 12) return PROTO_ERR_SHAPE;
    if (data[0] != 0x07) return PROTO_ERR_SHAPE;
    memcpy(seed_out, data + 8, 4);
    if (encrypt_type) *encrypt_type = data[8] & 3;
    return PROTO_OK;
}

int proto_parse_keepalive2(const uint8_t *data, size_t len, int file_packet, proto_ka_t *out)
{
    if (!data || !out) return PROTO_ERR_ARGS;
    memset(out, 0, sizeof *out);
    if (len < 3) return PROTO_ERR_SHAPE;
    if (data[0] != 0x07) return PROTO_ERR_SHAPE;
    if (file_packet) {
        if (data[2] != 0x10 && data[2] != 0x28) return PROTO_ERR_SHAPE;
        out->ok = 1;
        return PROTO_OK;
    }
    if (data[2] != 0x28) return PROTO_ERR_SHAPE;
    if (len >= 20) memcpy(out->tail, data + 16, 4);
    out->ok = 1;
    return PROTO_OK;
}

int proto_looks_like_notification(const uint8_t *data, size_t len)
{
    return (len > 0 && data && data[0] == 0x4D);
}

/* Error names stay ASCII; the UI layer owns the Chinese wording. */
const char *proto_login_error_text(int code)
{
    switch (code) {
    case PROTO_ERR_MAC_CHECK_FAILED:     return "MAC_CHECK_FAILED";
    case PROTO_ERR_SERVER_BUSY:          return "SERVER_BUSY";
    case PROTO_ERR_WRONG_PASSWORD:       return "WRONG_PASSWORD";
    case PROTO_ERR_INSUFFICIENT_BALANCE: return "INSUFFICIENT_BALANCE";
    case PROTO_ERR_ACCOUNT_FROZEN:       return "ACCOUNT_FROZEN";
    case PROTO_ERR_IP_MISMATCH:          return "IP_MISMATCH";
    case PROTO_ERR_MAC_MISMATCH:         return "MAC_MISMATCH";
    case PROTO_ERR_TOO_MANY_IPS:         return "TOO_MANY_IPS";
    case PROTO_ERR_CLIENT_UPGRADE:       return "CLIENT_UPGRADE_REQUIRED";
    case PROTO_ERR_IP_AND_MAC_MISMATCH:  return "IP_AND_MAC_MISMATCH";
    case PROTO_ERR_MUST_USE_DHCP:        return "MUST_USE_DHCP";
    default:                             return "UNKNOWN";
    }
}

int proto_login_error_is_fatal(int code)
{
    if (code == PROTO_ERR_SERVER_BUSY) return 0;
    return proto_login_error_text(code)[0] != 'U';
}
