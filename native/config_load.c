/*
 * Loads the settings the Python build already wrote.
 *
 * The file is APPDATA\JLU-DrCOM-NG\config.json.  We do not need a general
 * JSON parser: four scalars per section and one account are enough, so a
 * small scanner that understands "find this key inside this section" keeps
 * the binary at ~150 KB and pulls in no dependency.
 *
 * The password is stored as Windows DPAPI ciphertext (tag "D1:" after a
 * Base64 envelope), produced by drcom/secrets_store.py.  CryptUnprotectData
 * reads it back for the same Windows user - no key file, no re-entry.
 */
#include "config.h"

#include <windows.h>
#include <wincrypt.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#pragma comment(lib, "crypt32.lib")

/* ---------------------------------------------------------------- base64 */
static int b64_val(char c)
{
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    if (c == '+') return 62;
    if (c == '/') return 63;
    return -1;
}

static int base64_decode(const char *in, uint8_t *out, size_t cap)
{
    size_t n = 0;
    int acc = 0, bits = 0;
    for (const char *p = in; *p; p++) {
        if (*p == '=' || *p == '\n' || *p == '\r' || *p == ' ') continue;
        int v = b64_val(*p);
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

/* ----------------------------------------------------------------- DPAPI */
static int dpapi_unprotect(const uint8_t *blob, size_t len, char *out, size_t cap)
{
    DATA_BLOB in, res;
    in.pbData = (BYTE *)blob;
    in.cbData = (DWORD)len;
    if (!CryptUnprotectData(&in, NULL, NULL, NULL, NULL, 0, &res)) return -1;
    size_t n = res.cbData < cap - 1 ? res.cbData : cap - 1;
    memcpy(out, res.pbData, n);
    out[n] = '\0';
    SecureZeroMemory(res.pbData, res.cbData);
    LocalFree(res.pbData);
    return 0;
}

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

/* ------------------------------------------------------------------- load */
int drcom_config_load(const char *path, engine_config *cfg, char *err, size_t errcap,
                      int *api_port_out)
{
    if (api_port_out) *api_port_out = 0;

    FILE *f = fopen(path, "rb");
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
        char host[64];
        if (get_str(s, e, "host", host, sizeof host) != 0) host[0] = '\0';
    }

    /* ---- first account that actually has a password ----
     * accounts is an array (not an object), so find_object does not apply;
     * walk the array and take the first entry whose password decrypts. */
    {
        const char *p = find_key(json, "accounts");
        if (!p || *p != '[') {
            snprintf(err, errcap, "配置里没有 accounts 列表");
            free(json);
            return -1;
        }
        const char *q = p;
        int in_str = 0, esc = 0, depth = 0;
        const char *acct_begin = NULL;
        int found = 0;
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
                    char pw[1024];
                    if (get_str(acct_begin, q + 1, "password_protected", pw, sizeof pw) == 0 && pw[0]) {
                        char acc[128], mac[64];
                        acc[0] = mac[0] = '\0';
                        get_str(acct_begin, q + 1, "account", acc, sizeof acc);
                        get_str(acct_begin, q + 1, "mac", mac, sizeof mac);
                        if (decrypt_password(pw, cfg->password, sizeof cfg->password, err, errcap) == 0) {
                            snprintf(cfg->account, sizeof cfg->account, "%s", acc);
                            snprintf(cfg->mac, sizeof cfg->mac, "%s", mac);
                            found = 1;
                            break;
                        }
                    }
                    acct_begin = NULL;
                }
            } else if (c == ']' && depth == 0) {
                break;
            }
        }
        if (!found) {
            if (!err[0]) snprintf(err, errcap, "配置里没有可用的账号（或密码无法解密）");
            free(json);
            return -1;
        }
    }

    free(json);
    return 0;
}

int decrypt_password(const char *stored, char *out, size_t cap, char *err, size_t errcap)
{
    if (!stored || !stored[0]) {
        snprintf(err, errcap, "密码字段为空");
        return -1;
    }
    uint8_t blob[2048];
    int len = base64_decode(stored, blob, sizeof blob);
    if (len < 4) {
        snprintf(err, errcap, "密码字段不是合法的 Base64");
        return -1;
    }
    if (blob[0] == 'D' && blob[1] == '1' && blob[2] == ':')
        return dpapi_unprotect(blob + 3, (size_t)len - 3, out, cap) == 0
                   ? 0
                   : (snprintf(err, errcap, "DPAPI 解密失败（换过 Windows 账号？）"), -1);
    if (blob[0] == 'F' && blob[1] == '1' && blob[2] == ':') {
        snprintf(err, errcap, "该密码由 Fernet 加密，原生核心暂不支持");
        return -1;
    }
    if (blob[0] == 'X' && blob[1] == '1' && blob[2] == ':') {
        snprintf(err, errcap, "该密码是降级混淆存储，原生核心暂不支持");
        return -1;
    }
    /* Legacy: a bare DPAPI blob with no tag. */
    return dpapi_unprotect(blob, (size_t)len, out, cap) == 0
               ? 0
               : (snprintf(err, errcap, "DPAPI 解密失败"), -1);
}
