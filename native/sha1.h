/* SHA-1 (FIPS 180-4) - only needed for Dr.COM keepalive CRC type 3. */
#ifndef DRCOMD_SHA1_H
#define DRCOMD_SHA1_H
#include <stddef.h>
#include <stdint.h>
void sha1_digest(const uint8_t *data, size_t len, uint8_t out[20]);
#endif
