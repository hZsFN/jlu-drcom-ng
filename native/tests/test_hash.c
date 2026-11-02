#include <stdio.h>
#include <string.h>
#include "../md4.h"
#include "../md5.h"

static void show(const char *label, const uint8_t d[16])
{
    printf("%-22s ", label);
    for (int i = 0; i < 16; i++) printf("%02x", d[i]);
    printf("\n");
}

int main(void)
{
    struct { const char *in; const char *md4; } v4[] = {
        {"",               "31d6cfe0d16ae931b73c59d7e0c089c0"},
        {"a",              "bde52cb31de33e46245e05fbdbd6fb24"},
        {"abc",            "a448017aaf21d8525fc10ae87aa6729d"},
        {"message digest", "d9130a8164549fe818874806e1c7014b"},
        {"abcdefghijklmnopqrstuvwxyz", "d79e1c308aa5bbcdeea8ed63df412da9"},
    };
    int fail = 0;
    for (unsigned i = 0; i < sizeof v4 / sizeof v4[0]; i++) {
        uint8_t d[16]; char hex[33];
        md4_digest((const uint8_t *)v4[i].in, strlen(v4[i].in), d);
        for (int k = 0; k < 16; k++) sprintf(hex + k*2, "%02x", d[k]);
        int ok = strcmp(hex, v4[i].md4) == 0;
        printf("[%s] md4(%-28s) = %s\n", ok ? " OK " : "FAIL", v4[i].in, hex);
        if (!ok) { printf("       expected %s\n", v4[i].md4); fail++; }
    }

    struct { const char *in; const char *md5; } v5[] = {
        {"",    "d41d8cd98f00b204e9800998ecf8427e"},
        {"abc", "900150983cd24fb0d6963f7d28e17f72"},
        {"message digest", "f96b697d7cb7938d525a2f31aaf161d0"},
        {"abcdefghijklmnopqrstuvwxyz", "c3fcd3d76192e4007dfb496cca67e13b"},
    };
    for (unsigned i = 0; i < sizeof v5 / sizeof v5[0]; i++) {
        uint8_t d[16]; char hex[33];
        md5_digest((const uint8_t *)v5[i].in, strlen(v5[i].in), d);
        for (int k = 0; k < 16; k++) sprintf(hex + k*2, "%02x", d[k]);
        int ok = strcmp(hex, v5[i].md5) == 0;
        printf("[%s] md5(%-28s) = %s\n", ok ? " OK " : "FAIL", v5[i].in, hex);
        if (!ok) { printf("       expected %s\n", v5[i].md5); fail++; }
    }
    (void)show;
    printf("\n%s\n", fail ? "FAILURES" : "all hash vectors pass");
    return fail ? 1 : 0;
}
