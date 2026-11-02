/* MD5 (RFC 1321) - self-contained, no OpenSSL dependency. */
#ifndef DRCOMD_MD5_H
#define DRCOMD_MD5_H
#include <stddef.h>
#include <stdint.h>
void md5_digest(const uint8_t *data, size_t len, uint8_t out[16]);
#endif
