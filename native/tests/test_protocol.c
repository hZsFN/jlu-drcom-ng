/*
 * Byte-for-byte comparison of the native protocol layer against vectors
 * generated from drcom/protocol.py (see tools/make_golden.py).
 */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

#include "../protocol.h"
#include "../md4.h"
#include "../md5.h"
#include "golden.h"

static int hex2bytes(const char *hex, uint8_t *out, size_t cap)
{
    size_t n = hex ? strlen(hex) : 0;
    if (n % 2) return -1;
    n /= 2;
    if (n > cap) return -2;
    for (size_t i = 0; i < n; i++) {
        unsigned hi, lo;
        char a = hex[2*i], b = hex[2*i+1];
        hi = (a >= 'a') ? (unsigned)(a - 'a' + 10) : (a >= 'A') ? (unsigned)(a - 'A' + 10) : (unsigned)(a - '0');
        lo = (b >= 'a') ? (unsigned)(b - 'a' + 10) : (b >= 'A') ? (unsigned)(b - 'A' + 10) : (unsigned)(b - '0');
        out[i] = (uint8_t)((hi << 4) | lo);
    }
    return (int)n;
}

static void to_hex(const uint8_t *data, size_t len, char *out)
{
    static const char *D = "0123456789abcdef";
    for (size_t i = 0; i < len; i++) {
        out[2*i]   = D[data[i] >> 4];
        out[2*i+1] = D[data[i] & 0xF];
    }
    out[2*len] = '\0';
}

static int failures = 0;
static int checks = 0;

static void expect_hex(const char *label, const uint8_t *got, size_t len, const char *want)
{
    char hex[2048];
    to_hex(got, len, hex);
    checks++;
    if (want && strcmp(hex, want) == 0) return;
    failures++;
    printf("FAIL %s\n", label);
    printf("  got  %s\n", hex);
    printf("  want %s\n", want ? want : "(none)");
    if (strlen(hex) == strlen(want ? want : "")) {
        for (size_t i = 0; hex[i]; i++) {
            if (hex[i] != want[i]) {
                printf("  first difference at byte %zu (hex index %zu)\n", i / 2, i);
                break;
            }
        }
    }
}

static void run_case(const golden_case *c)
{
    const char *k = c->kind;

    if (strcmp(k, "login") == 0) {
        uint8_t seed[4];
        if (hex2bytes(c->seed_hex, seed, 4) != 4) { failures++; printf("FAIL %s: bad seed\n", c->name); return; }
        uint8_t out[PROTO_MAX_PACKET];
        size_t outlen = 0;
        int rc = proto_build_login(c->account, c->password, c->mac, seed, out, sizeof out, &outlen);
        checks++;
        if (rc != PROTO_OK) { failures++; printf("FAIL %s: rc=%d\n", c->name, rc); return; }
        expect_hex(c->name, out, outlen, c->expect_hex);
    }
    else if (strcmp(k, "challenge") == 0) {
        uint8_t rnd[2];
        if (hex2bytes(c->rng_hex, rnd, 2) != 2) { failures++; printf("FAIL %s: bad rng\n", c->name); return; }
        uint8_t out[PROTO_CHALLENGE_LEN];
        proto_build_challenge(rnd, out);
        expect_hex(c->name, out, sizeof out, c->expect_hex);
    }
    else if (strcmp(k, "checksum1") == 0) {
        uint8_t pkt[128];
        if (hex2bytes(c->data_hex, pkt, sizeof pkt) < 97) { failures++; printf("FAIL %s: short packet\n", c->name); return; }
        uint8_t out[8];
        proto_checksum1(pkt, out);
        expect_hex(c->name, out, 8, c->expect_hex);
    }
    else if (strcmp(k, "ka1p2") == 0) {
        uint8_t seed[4], auth[16], rnd[2];
        if (hex2bytes(c->seed_hex, seed, 4) != 4 ||
            hex2bytes(c->data_hex, auth, 16) != 16 ||
            hex2bytes(c->rng_hex, rnd, 2) != 2) {
            failures++; printf("FAIL %s: bad inputs\n", c->name); return;
        }
        uint8_t out[PROTO_KEEPALIVE1_PKT2_LEN];
        proto_build_keepalive1_pkt2(seed, auth, rnd, out);
        expect_hex(c->name, out, sizeof out, c->expect_hex);
    }
    else if (strcmp(k, "ka2") == 0) {
        uint8_t tail[4] = {0, 0, 0, 0};
        if (c->tail_hex && c->tail_hex[0]) hex2bytes(c->tail_hex, tail, 4);
        uint8_t out[PROTO_KEEPALIVE2_LEN];
        proto_build_keepalive2(c->counter, c->file_packet, c->pkt_type,
                               (c->pkt_type == 3) ? tail : NULL, out);
        expect_hex(c->name, out, sizeof out, c->expect_hex);
    }
    else if (strcmp(k, "gencrc") == 0) {
        uint8_t seed[4], out[8];
        hex2bytes(c->seed_hex, seed, 4);
        proto_gen_crc(seed, c->encrypt_type, out);
        expect_hex(c->name, out, 8, c->expect_hex);
    }
    else if (strcmp(k, "md5a") == 0 || strcmp(k, "md5b") == 0) {
        uint8_t seed[4], out[16];
        hex2bytes(c->seed_hex, seed, 4);
        const uint8_t *pw = (const uint8_t *)c->password;
        size_t len = strlen(c->password);
        if (k[3] == 'a') proto_md5a(pw, len, seed, out);
        else             proto_md5b(pw, len, seed, out);
        expect_hex(c->name, out, 16, c->expect_hex);
    }
    else if (strcmp(k, "md4") == 0 || strcmp(k, "md5") == 0) {
        uint8_t data[256], out[16];
        int n = hex2bytes(c->data_hex, data, sizeof data);
        if (n < 0) { failures++; printf("FAIL %s: bad data\n", c->name); return; }
        if (k[2] == '4') md4_digest(data, (size_t)n, out);
        else             md5_digest(data, (size_t)n, out);
        expect_hex(c->name, out, 16, c->expect_hex);
    }
    else if (strcmp(k, "parse_challenge") == 0) {
        uint8_t data[128];
        int n = hex2bytes(c->data_hex, data, sizeof data);
        proto_challenge_t ch;
        checks++;
        if (proto_parse_challenge(data, (size_t)n, &ch) != PROTO_OK) {
            failures++; printf("FAIL %s: rejected a valid packet\n", c->name); return;
        }
        char hex[16]; to_hex(ch.seed, 4, hex);
        if (strcmp(hex, "11223344") != 0) { failures++; printf("FAIL %s: seed=%s\n", c->name, hex); }
        snprintf(hex, sizeof hex, "%u.%u.%u.%u", ch.ip[0], ch.ip[1], ch.ip[2], ch.ip[3]);
        if (strcmp(hex, "10.100.61.99") != 0) { failures++; printf("FAIL %s: ip=%s\n", c->name, hex); }
    }
    else if (strcmp(k, "parse_ka1") == 0) {
        uint8_t data[128], seed[4];
        int n = hex2bytes(c->data_hex, data, sizeof data);
        int et = -1;
        checks++;
        if (proto_parse_keepalive1(data, (size_t)n, seed, &et) != PROTO_OK) {
            failures++; printf("FAIL %s: rejected\n", c->name); return;
        }
        char hex[16]; to_hex(seed, 4, hex);
        if (strcmp(hex, "fc112233") != 0) { failures++; printf("FAIL %s: seed=%s\n", c->name, hex); }
        if (et != 0) { failures++; printf("FAIL %s: encrypt_type=%d (want 0)\n", c->name, et); }
    }
    else if (strcmp(k, "parse_login") == 0) {
        uint8_t data[128];
        int n = hex2bytes(c->data_hex, data, sizeof data);
        proto_login_t lo;
        checks++;
        if (proto_parse_login(data, (size_t)n, &lo) != PROTO_OK) {
            failures++; printf("FAIL %s: rejected\n", c->name); return;
        }
        if (strstr(c->name, "ok")) {
            if (!lo.success) { failures++; printf("FAIL %s: expected success\n", c->name); }
            char hex[40]; to_hex(lo.auth, 16, hex);
            if (strcmp(hex, "000102030405060708090a0b0c0d0e0f") != 0) {
                failures++; printf("FAIL %s: auth=%s\n", c->name, hex);
            }
        } else {
            if (lo.success) { failures++; printf("FAIL %s: expected failure\n", c->name); }
            if (lo.error_code != PROTO_ERR_MAC_MISMATCH) {
                failures++; printf("FAIL %s: code=0x%02X\n", c->name, lo.error_code);
            }
        }
    }
    else if (strcmp(k, "parse_ka2") == 0 || strcmp(k, "parse_ka2_file") == 0) {
        uint8_t data[128];
        int n = hex2bytes(c->data_hex, data, sizeof data);
        int file = (strcmp(k, "parse_ka2_file") == 0);
        proto_ka_t ka;
        checks++;
        if (proto_parse_keepalive2(data, (size_t)n, file, &ka) != PROTO_OK || !ka.ok) {
            failures++; printf("FAIL %s: rejected\n", c->name); return;
        }
        if (!file) {
            char hex[16]; to_hex(ka.tail, 4, hex);
            if (strcmp(hex, "c0ffee01") != 0) { failures++; printf("FAIL %s: tail=%s\n", c->name, hex); }
        }
    }
    else {
        failures++;
        printf("FAIL %s: unknown kind '%s'\n", c->name, k);
    }
}

int main(void)
{
    printf("golden vectors: %d cases\n\n", GOLDEN_COUNT);
    for (int i = 0; i < GOLDEN_COUNT; i++) run_case(&GOLDEN[i]);

    /* Negative cases the Python side also rejects. */
    uint8_t seed[4] = {1, 2, 3, 4}, out[PROTO_MAX_PACKET];
    size_t outlen = 0;
    checks++;
    if (proto_build_login("a", "pw", "zz:zz:zz:zz:zz:zz", seed, out, sizeof out, &outlen) == PROTO_OK) {
        failures++; printf("FAIL: accepted a bogus MAC\n");
    }
    checks++;
    if (proto_build_login("a", "pw", "AA:BB:CC:DD:EE:FF", seed, out, 10, &outlen) != PROTO_ERR_BUF) {
        failures++; printf("FAIL: did not reject a short output buffer\n");
    }
    checks++;
    {
        uint8_t bad[8] = { 0x02, 0, 0, 0, 0, 0, 0, 0 };
        proto_challenge_t ch;
        if (proto_parse_challenge(bad, sizeof bad, &ch) == PROTO_OK) {
            failures++; printf("FAIL: accepted a truncated challenge reply\n");
        }
    }

    printf("\n%d checks, %d failure(s)\n", checks, failures);
    return failures ? 1 : 0;
}
