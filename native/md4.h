/* MD4 (RFC 1320) - hashlib dropped it on OpenSSL 3, so we carry our own. */
#ifndef DRCOMD_MD4_H
#define DRCOMD_MD4_H
#include <stddef.h>
#include <stdint.h>
void md4_digest(const uint8_t *data, size_t len, uint8_t out[16]);
#endif
