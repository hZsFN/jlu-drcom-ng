/*
 * Dr.COM (JLU variant) wire protocol - packet building and parsing.
 *
 * Pure functions: bytes in, bytes out, no sockets.  The layout mirrors the
 * reference implementation recorded in drcom/protocol.py; native/tests
 * compares every byte against vectors generated from that module.
 */
#ifndef DRCOMD_PROTOCOL_H
#define DRCOMD_PROTOCOL_H

#include <stddef.h>
#include <stdint.h>

/* ---- network parameters ---- */
#define PROTO_AUTH_SERVER        "10.100.61.3"
#define PROTO_AUTH_PORT          61440
#define PROTO_BIND_PORT          61440
#define PROTO_RECV_TIMEOUT_MS    3000
#define PROTO_KEEPALIVE_INTERVAL_S 20

/* ---- sizes ---- */
#define PROTO_LOGIN_BASE_LEN     338
#define PROTO_PW_SECTION_OFFSET  312
#define PROTO_KEEPALIVE2_LEN     40
#define PROTO_KEEPALIVE1_PKT2_LEN 42
#define PROTO_CHALLENGE_LEN      20
#define PROTO_MAX_PASSWORD       16
#define PROTO_MAX_PACKET         512

/* ---- login failure codes (server byte [4] when [0]==0x05) ---- */
enum proto_login_error {
    PROTO_ERR_MAC_CHECK_FAILED      = 0x01,
    PROTO_ERR_SERVER_BUSY           = 0x02,
    PROTO_ERR_WRONG_PASSWORD        = 0x03,
    PROTO_ERR_INSUFFICIENT_BALANCE  = 0x04,
    PROTO_ERR_ACCOUNT_FROZEN        = 0x05,
    PROTO_ERR_IP_MISMATCH           = 0x07,
    PROTO_ERR_MAC_MISMATCH          = 0x0B,
    PROTO_ERR_TOO_MANY_IPS          = 0x14,
    PROTO_ERR_CLIENT_UPGRADE        = 0x15,
    PROTO_ERR_IP_AND_MAC_MISMATCH   = 0x16,
    PROTO_ERR_MUST_USE_DHCP         = 0x17
};

/* Portable return codes. */
#define PROTO_OK          0
#define PROTO_ERR_ARGS   -1
#define PROTO_ERR_SHAPE  -2   /* packet does not match the expected shape */
#define PROTO_ERR_BUF    -3   /* output buffer too small */

/* ---- primitives ---- */
int  proto_mac_to_bytes(const char *mac, uint8_t out[6]);
int  proto_password_padding(int pw_len, int *extra, int *jlu_padding);
void proto_md5a(const uint8_t *pw, size_t pwlen, const uint8_t seed[4], uint8_t out[16]);
void proto_md5b(const uint8_t *pw, size_t pwlen, const uint8_t seed[4], uint8_t out[16]);
void proto_mac_xor_md5a(const uint8_t digest[16], const uint8_t mac[6], uint8_t out[6]);
void proto_checksum1(const uint8_t *packet, uint8_t out[8]);
void proto_checksum2(const uint8_t *prefix, size_t prefix_len,
                     const uint8_t mac[6], uint8_t out[4]);
int  proto_gen_crc(const uint8_t seed[4], int encrypt_type, uint8_t out[8]);

/* ---- builders: return PROTO_OK or a negative error ---- */
int proto_build_challenge(uint8_t rand2[2], uint8_t out[PROTO_CHALLENGE_LEN]);
int proto_build_login(const char *account, const char *password, const char *mac,
                      const uint8_t seed[4], uint8_t *out, size_t out_cap, size_t *out_len);
int proto_build_keepalive1_pkt2(const uint8_t seed[4], const uint8_t auth[16],
                                const uint8_t rand2[2],
                                uint8_t out[PROTO_KEEPALIVE1_PKT2_LEN]);
/* The fixed 8-byte first keepalive-1 packet. */
const uint8_t *proto_keepalive1_pkt1(void);

int proto_build_keepalive2(int counter, int file_packet, int pkt_type,
                           const uint8_t *tail4,
                           uint8_t out[PROTO_KEEPALIVE2_LEN]);

/* ---- parsers ---- */
typedef struct {
    uint8_t seed[4];
    uint8_t ip[4];
} proto_challenge_t;

typedef struct {
    int     success;
    uint8_t auth[16];
    int     error_code;    /* -1 when the server sent no usable code */
} proto_login_t;

typedef struct {
    int     ok;
    uint8_t tail[4];       /* keepalive-2 only */
} proto_ka_t;

int proto_parse_challenge(const uint8_t *data, size_t len, proto_challenge_t *out);
int proto_parse_login(const uint8_t *data, size_t len, proto_login_t *out);
int proto_parse_keepalive1(const uint8_t *data, size_t len,
                           uint8_t seed_out[4], int *encrypt_type);
int proto_parse_keepalive2(const uint8_t *data, size_t len, int file_packet, proto_ka_t *out);
int proto_looks_like_notification(const uint8_t *data, size_t len);

/* Human-readable text for a failure code; never returns NULL. */
const char *proto_login_error_text(int code);
int proto_login_error_is_fatal(int code);

#endif
