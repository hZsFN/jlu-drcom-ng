/*
 * Loads the settings the Python build already wrote.
 *
 * The file is %APPDATA%\JLU-DrCOM-NG\config.json (Windows) or
 * ~/.config/jlu-drcom-ng/config.json (Linux).  A small targeted JSON scanner
 * keeps the binary tiny and dependency-free.
 *
 * Passwords:
 *   - Windows: DPAPI ciphertext ("D1:" prefix), decrypted by CryptUnprotectData.
 *   - Linux:   Fernet ciphertext ("F1:" prefix), decrypted using key.bin + libcrypto.
 */
#include "platform.h"
#include "config.h"

#ifdef _WIN32
#include <wincrypt.h>
#pragma comment(lib, "crypt32.lib")
#else
#include <openssl/crypto.h>
#include <openssl/evp.h>
#include <openssl/hmac.h>
#include <openssl/sha.h>
#endif

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ---------------------------------------------------------------- file IO */
static FILE *open_utf8_rb(const char *path)
{
#ifdef _WIN32
    WCHAR wpath[MAX_PATH * 2];
    if (MultiByteToWideChar(CP_UTF8, 0, path, -1, wpath, (int)(sizeof wpath / sizeof wpath[0])) > 0) {
        return _wfopen(wpath, L"rb");
    }
    return fopen(path, "rb");
#else
    return fopen(path, "rb");
#endif
}

/* ---------------------------------------------------------------- base64 */
static int b64_val(char c)
{
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    if (c == '+' || c == '-') return 62;
    if (c == '/' || c == '_') return 63;
    return -1;
}

static int base64_decode(const char *in, size_t in_len, uint8_t *out, size_t cap)
{
    size_t n = 0;
    int acc = 0, bits = 0;
    for (size_t i = 0; i < in_len; i++) {
        char ch = in[i];
        if (ch == '=' || ch == '\n' || ch == '\r' || ch == ' ') continue;
        int v = b64_val(ch);
        if (v < 0) return -1;
        acc = (acc << 6) | v;
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            if (n >= cap) return -2;
            out[n++] = (uint8_t)((acc >> bits) & 0xFF);
        }
    }
    return (int)n;
}

/* ---------------------------------------------------------------- crypto */
#ifdef _WIN32
static int dpapi_unprotect(const uint8_t *blob, size_t len, char *out, size_t cap)
{
    DATA_BLOB in, res;
    in.pbData = (BYTE *)blob;
    in.cbData = (DWORD)len;
    if (!CryptUnprotectData(&in, NULL, NULL, NULL, NULL, 0, &res)) return -1;
    size_t n = res.cbData < cap - 1 ? res.cbData : cap - 1;
    memcpy(out, res.pbData, n);
    out[n] = '\0';
    plat_secure_zero(res.pbData, res.cbData);
    LocalFree(res.pbData);
    return 0;
}
#else
static int fernet_unprotect(const uint8_t *token_b64, size_t token_b64_len,
                            const char *key_path, char *out, size_t cap,
                            char *err, size_t errcap)
{
    if (!key_path || !key_path[0]) {
        snprintf(err, errcap, "缺少 key.bin，无法解密 Fernet 密码");
        return -1;
    }
    FILE *kf = fopen(key_path, "rb");
    if (!kf) {
        snprintf(err, errcap, "无法打开密钥文件 %s", key_path);
        return -1;
    }
    uint8_t raw_key[32];
    size_t kgot = fread(raw_key, 1, sizeof raw_key, kf);
    fclose(kf);
    if (kgot < 32) {
        plat_secure_zero(raw_key, sizeof raw_key);
        snprintf(err, errcap, "密钥文件 key.bin 长度不足 32 字节");
        return -1;
    }

    uint8_t fkey[32];
    SHA256(raw_key, 32, fkey);
    plat_secure_zero(raw_key, sizeof raw_key);

    uint8_t tok[2048];
    int tlen = base64_decode((const char *)token_b64, token_b64_len, tok, sizeof tok);
    if (tlen < 1 + 8 + 16 + 16 + 32 || tok[0] != 0x80) {
        plat_secure_zero(fkey, sizeof fkey);
        plat_secure_zero(tok, sizeof tok);
        snprintf(err, errcap, "Fernet 密文格式无效");
        return -1;
    }

    size_t signed_len = (size_t)tlen - 32;
    uint8_t mac[32];
    unsigned int mac_len = 0;
    if (!HMAC(EVP_sha256(), fkey, 16, tok, signed_len, mac, &mac_len) ||
        mac_len != 32 ||
        CRYPTO_memcmp(mac, tok + signed_len, 32) != 0) {
        plat_secure_zero(fkey, sizeof fkey);
        plat_secure_zero(tok, sizeof tok);
        snprintf(err, errcap, "Fernet HMAC 校验失败（key.bin 不匹配或密文损坏）");
        return -1;
    }

    const uint8_t *iv = tok + 9;
    const uint8_t *ct = tok + 25;
    int ct_len = (int)(signed_len - 25);
    if (ct_len <= 0 || (ct_len % 16) != 0 || (size_t)ct_len >= cap + 16) {
        plat_secure_zero(fkey, sizeof fkey);
        plat_secure_zero(tok, sizeof tok);
        snprintf(err, errcap, "Fernet 密文长度异常");
        return -1;
    }

    uint8_t plain[1024];
    int outl = 0, finl = 0;
    EVP_CIPHER_CTX *ctx = EVP_CIPHER_CTX_new();
    int ok = 0;
    if (ctx &&
        EVP_DecryptInit_ex(ctx, EVP_aes_128_cbc(), NULL, fkey + 16, iv) == 1 &&
        EVP_DecryptUpdate(ctx, plain, &outl, ct, ct_len) == 1 &&
        EVP_DecryptFinal_ex(ctx, plain + outl, &finl) == 1) {
        size_t plen = (size_t)(outl + finl);
        if (plen < cap) {
            memcpy(out, plain, plen);
            out[plen] = '\0';
            ok = 1;
        }
    }
    if (ctx) EVP_CIPHER_CTX_free(ctx);
    plat_secure_zero(plain, sizeof plain);
    plat_secure_zero(fkey, sizeof fkey);
    plat_secure_zero(tok, sizeof tok);
    if (!ok) {
        snprintf(err, errcap, "Fernet 解密失败");
        return -1;
    }
    return 0;
}
#endif

/* ----------------------------------------------------------- tiny scanner */
static const char *skip_ws(const char *p)
{
    while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r') p++;
    return p;
}

/* Find `"key"` at or after `from`; returns the position just past the colon. */
static const char *find_key(const char *from, const char *key)
{
    char pat[96];
    snprintf(pat, sizeof pat, "\"%s\"", key);
    const char *p = strstr(from, pat);
    if (!p) return NULL;
    p += strlen(pat);
    p = skip_ws(p);
    if (*p != ':') return NULL;
    return skip_ws(p + 1);
}

/* Bounds of the object that follows `"key"`, e.g. the "auth" section. */
static int find_object(const char *json, const char *key, const char **beg, const char **end)
{
    const char *p = find_key(json, key);
    if (!p || *p != '{') return -1;
    int depth = 0;
    int in_str = 0, esc = 0;
    const char *q = p;
    for (; *q; q++) {
        char c = *q;
        if (in_str) {
            if (esc) esc = 0;
            else if (c == '\\') esc = 1;
            else if (c == '"') in_str = 0;
            continue;
        }
        if (c == '"') { in_str = 1; continue; }
        if (c == '{') depth++;
        else if (c == '}') {
            depth--;
            if (depth == 0) { *beg = p; *end = q + 1; return 0; }
        }
    }
    return -1;
}

/* Copy a string value out of [beg, end). */
static int get_str(const char *beg, const char *end, const char *key, char *out, size_t cap)
{
    char *scope = (char *)malloc((size_t)(end - beg) + 1);
    if (!scope) return -1;
    memcpy(scope, beg, (size_t)(end - beg));
    scope[end - beg] = '\0';

    int ok = -1;
    const char *p = find_key(scope, key);
    if (p && *p == '"') {
        p++;
        size_t n = 0;
        while (*p && *p != '"' && n < cap - 1) {
            if (*p == '\\' && p[1]) {
                p++;
                switch (*p) {
                case 'n': out[n++] = '\n'; break;
                case 't': out[n++] = '\t'; break;
                case '\\': out[n++] = '\\'; break;
                case '"': out[n++] = '"'; break;
                case '/': out[n++] = '/'; break;
                default: out[n++] = *p; break;
                }
                p++;
                continue;
            }
            out[n++] = *p++;
        }
        out[n] = '\0';
        ok = 0;
    }
    plat_secure_zero(scope, (size_t)(end - beg) + 1);
    free(scope);
    return ok;
}

static int get_num(const char *beg, const char *end, const char *key, double *out)
{
    const char *p = find_key(beg, key);
    if (!p || p >= end) return -1;
    char *stop = NULL;
    double v = strtod(p, &stop);
    if (stop == p) return -1;
    *out = v;
    return 0;
}

static int get_bool(const char *beg, const char *end, const char *key, int *out)
{
    const char *p = find_key(beg, key);
    if (!p || p >= end) return -1;
    if (strncmp(p, "true", 4) == 0) { *out = 1; return 0; }
    if (strncmp(p, "false", 5) == 0) { *out = 0; return 0; }
    return -1;
}

static void sibling_key_path(const char *config_path, char *out, size_t cap)
{
    snprintf(out, cap, "%s", config_path);
    char *sep1 = strrchr(out, '\\');
    char *sep2 = strrchr(out, '/');
    char *sep = sep1 > sep2 ? sep1 : sep2;
    if (sep) {
        snprintf(sep + 1, cap - (size_t)(sep + 1 - out), "key.bin");
    } else {
        snprintf(out, cap, "key.bin");
    }
}

/* ------------------------------------------------------------------- load */
int drcom_config_load(const char *path, engine_config *cfg, char *err, size_t errcap,
                      int *api_port_out)
{
    if (api_port_out) *api_port_out = 0;

    FILE *f = open_utf8_rb(path);
    if (!f) {
        snprintf(err, errcap, "打不开配置文件：%s", path);
        return -1;
    }
    fseek(f, 0, SEEK_END);
    long size = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (size <= 0 || size > 4 * 1024 * 1024) {
        fclose(f);
        snprintf(err, errcap, "配置文件大小异常：%ld 字节", size);
        return -1;
    }
    char *json = (char *)malloc((size_t)size + 1);
    if (!json) { fclose(f); return -1; }
    size_t got = fread(json, 1, (size_t)size, f);
    fclose(f);
    json[got] = '\0';

    const char *s, *e;
    double num;

    if (find_object(json, "auth", &s, &e) == 0) {
        char tmp[128];
        if (get_str(s, e, "server", tmp, sizeof tmp) == 0)
            snprintf(cfg->server, sizeof cfg->server, "%s", tmp);
        if (get_num(s, e, "port", &num) == 0) cfg->port = (int)num;
        if (get_num(s, e, "bind_port", &num) == 0) cfg->bind_port = (int)num;
        if (get_num(s, e, "timeout_ms", &num) == 0) cfg->timeout_ms = (int)num;
        if (get_num(s, e, "keepalive_interval", &num) == 0) cfg->keepalive_interval = num;
        if (get_num(s, e, "post_login_delay", &num) == 0) cfg->post_login_delay = num;
        if (get_num(s, e, "challenge_retries", &num) == 0) cfg->challenge_retries = (int)num;
        cfg->bind_address[0] = '\0';
        if (get_str(s, e, "bind_address", tmp, sizeof tmp) == 0)
            snprintf(cfg->bind_address, sizeof cfg->bind_address, "%s", tmp);
        if (get_bool(s, e, "auto_interface_fallback", &cfg->auto_interface_fallback) != 0)
            cfg->auto_interface_fallback = 0;
    }

    if (find_object(json, "reconnect", &s, &e) == 0) {
        int b = 0;
        if (get_bool(s, e, "enabled", &b) == 0) cfg->reconnect_enabled = b;
        if (get_num(s, e, "min_delay", &num) == 0) cfg->min_delay = num;
        if (get_num(s, e, "max_delay", &num) == 0) cfg->max_delay = num;
        if (get_num(s, e, "factor", &num) == 0) cfg->factor = num;
        if (get_num(s, e, "jitter", &num) == 0) cfg->jitter = num;
        if (get_num(s, e, "give_up_after", &num) == 0) cfg->give_up_after = (int)num;
    }

    if (api_port_out && find_object(json, "api", &s, &e) == 0) {
        if (get_num(s, e, "port", &num) == 0) *api_port_out = (int)num;
    }

    if (find_object(json, "logging", &s, &e) == 0) {
        int b = 0;
        if (get_bool(s, e, "protocol_hex", &b) == 0) cfg->protocol_hex = b;
    }

    char active_id[64] = {0};
    get_str(json, json + got, "active_account_id", active_id, sizeof active_id);

    char key_path[1024];
    sibling_key_path(path, key_path, sizeof key_path);

    /* ---- select active_account_id, falling back to first decryptable ---- */
    {
        const char *p = find_key(json, "accounts");
        if (!p || *p != '[') {
            snprintf(err, errcap, "配置里没有 accounts 列表");
            plat_secure_zero(json, got + 1);
            free(json);
            return -1;
        }

        int found = 0;
        int matched_active = 0;
        for (int pass = 0; pass < 2 && !found && !matched_active; pass++) {
            const char *q = p;
            int in_str = 0, esc = 0, depth = 0;
            const char *acct_begin = NULL;
            for (; *q; q++) {
                char c = *q;
                if (in_str) {
                    if (esc) esc = 0;
                    else if (c == '\\') esc = 1;
                    else if (c == '"') in_str = 0;
                    continue;
                }
                if (c == '"') { in_str = 1; continue; }
                if (c == '{') {
                    if (depth == 0) acct_begin = q;
                    depth++;
                } else if (c == '}') {
                    depth--;
                    if (depth == 0 && acct_begin) {
                        char id[64] = {0};
                        get_str(acct_begin, q + 1, "id", id, sizeof id);
                        int want_this = (pass == 0)
                            ? (active_id[0] && strcmp(id, active_id) == 0)
                            : 1;
                        if (want_this) {
                            if (pass == 0) matched_active = 1;
                            char pw[1024];
                            pw[0] = '\0';
                            if (get_str(acct_begin, q + 1, "password_protected", pw, sizeof pw) == 0 && pw[0]) {
                                char acc[128], mac[64];
                                acc[0] = mac[0] = '\0';
                                get_str(acct_begin, q + 1, "account", acc, sizeof acc);
                                get_str(acct_begin, q + 1, "mac", mac, sizeof mac);
                                if (decrypt_password_with_key(pw, key_path, cfg->password,
                                                              sizeof cfg->password, err, errcap) == 0) {
                                    snprintf(cfg->account, sizeof cfg->account, "%s", acc);
                                    snprintf(cfg->mac, sizeof cfg->mac, "%s", mac);
                                    found = 1;
                                }
                            } else if (pass == 0) {
                                snprintf(err, errcap, "当前激活账号未设置密码");
                            }
                            plat_secure_zero(pw, sizeof pw);
                            if (found || pass == 0) break;
                        }
                        acct_begin = NULL;
                    }
                } else if (c == ']' && depth == 0) {
                    break;
                }
            }
        }
        if (!found) {
            if (!err[0]) snprintf(err, errcap, "配置里没有可用的账号（或密码无法解密）");
            plat_secure_zero(json, got + 1);
            free(json);
            return -1;
        }
    }

    plat_secure_zero(json, got + 1);
    free(json);
    return 0;
}

int decrypt_password_with_key(const char *stored, const char *key_path,
                              char *out, size_t cap, char *err, size_t errcap)
{
    if (!stored || !stored[0]) {
        snprintf(err, errcap, "密码字段为空");
        return -1;
    }
    uint8_t blob[2048];
    int len = base64_decode(stored, strlen(stored), blob, sizeof blob);
    if (len < 4) {
        snprintf(err, errcap, "密码字段不是合法的 Base64");
        return -1;
    }
    if (blob[0] == 'D' && blob[1] == '1' && blob[2] == ':') {
#ifdef _WIN32
        int rc = dpapi_unprotect(blob + 3, (size_t)len - 3, out, cap);
        plat_secure_zero(blob, sizeof blob);
        if (rc == 0) return 0;
        snprintf(err, errcap, "DPAPI 解密失败（换过 Windows 账号？）");
        return -1;
#else
        plat_secure_zero(blob, sizeof blob);
        snprintf(err, errcap, "该密码由 Windows DPAPI 加密，Linux 无法解密");
        return -1;
#endif
    }
    if (blob[0] == 'F' && blob[1] == '1' && blob[2] == ':') {
#ifdef _WIN32
        (void)key_path;
        plat_secure_zero(blob, sizeof blob);
        snprintf(err, errcap, "该密码由 Fernet 加密，Windows 原生核心仅支持 DPAPI");
        return -1;
#else
        int rc = fernet_unprotect(blob + 3, (size_t)len - 3, key_path, out, cap, err, errcap);
        plat_secure_zero(blob, sizeof blob);
        return rc;
#endif
    }
    if (blob[0] == 'X' && blob[1] == '1' && blob[2] == ':') {
        plat_secure_zero(blob, sizeof blob);
        snprintf(err, errcap, "该密码是降级混淆存储，原生核心暂不支持");
        return -1;
    }
#ifdef _WIN32
    /* Legacy: a bare DPAPI blob with no tag. */
    int rc = dpapi_unprotect(blob, (size_t)len, out, cap);
    plat_secure_zero(blob, sizeof blob);
    if (rc == 0) return 0;
#else
    plat_secure_zero(blob, sizeof blob);
#endif
    snprintf(err, errcap, "DPAPI 解密失败");
    return -1;
}

int decrypt_password(const char *stored, char *out, size_t cap, char *err, size_t errcap)
{
    return decrypt_password_with_key(stored, NULL, out, cap, err, errcap);
}
