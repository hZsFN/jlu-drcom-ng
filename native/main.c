/*
 * drcomd - the resident Dr.COM keepalive core (Windows + Linux).
 *
 * One worker thread for the UDP protocol, one small loopback HTTP listener
 * secured by Host/Origin checks and a per-run X-DrCOM-Token, and (on Windows)
 * an inline notification-area icon on the main thread with zero polling.
 */
#include "platform.h"
#include "engine.h"
#include "config.h"
#include "tray.h"

#ifndef DRCOMD_VERSION
#define DRCOMD_VERSION "2.2.1"
#endif

#ifdef _WIN32
#include <iphlpapi.h>
#include <shellapi.h>
#pragma comment(lib, "iphlpapi.lib")
#pragma comment(lib, "shell32.lib")
#else
#include <ifaddrs.h>
#include <net/if.h>
#include <signal.h>
#include <sys/stat.h>
#endif

#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <string.h>

/* ------------------------------------------------------------- log ring */
#define LOG_LINES 120
#define LOG_LEN   320

typedef struct {
    uint64_t seq;
    char     text[LOG_LEN];
} log_entry_t;

static plat_rwlock_t log_lock = PLAT_RWLOCK_INIT;
static log_entry_t   log_ring[LOG_LINES];
static int           log_head = 0;
static int           log_count = 0;
static uint64_t      log_next_seq = 1;

static plat_atomic_t g_stop = 0;
static plat_event_t  g_stop_evt;
static int           g_verbose = 0;
static int           g_stdout_ok = -1;
static engine_t     *g_engine = NULL;
static plat_atomic_t g_reconnecting = 0;

static char g_gui_path[1024];
static char g_config_path[1024];
static char g_token_path[1024];
static char g_api_token[65];

typedef struct {
    uint64_t session_id;
    double   online_since_epoch;
    uint32_t if_index;
    uint64_t rx_base;
    uint64_t tx_base;
    int      has_baseline;
} traffic_baseline_t;

static plat_rwlock_t      g_traffic_lock = PLAT_RWLOCK_INIT;
static traffic_baseline_t g_traffic = {0};

#ifdef _WIN32
static int read_nic_counters_by_index(uint32_t if_index, uint64_t *rx_out, uint64_t *tx_out)
{
    if (if_index == 0) return 0;
    MIB_IF_ROW2 row;
    memset(&row, 0, sizeof row);
    row.InterfaceIndex = (NET_IFINDEX)if_index;
    if (GetIfEntry2(&row) != NO_ERROR) return 0;
    *rx_out = (uint64_t)row.InOctets;
    *tx_out = (uint64_t)row.OutOctets;
    return 1;
}

static uint32_t find_nic_index(const char *local_ip, const char *server_ip)
{
    uint32_t idx = 0;
    struct in_addr want;
    if (local_ip && local_ip[0] && inet_pton(AF_INET, local_ip, &want) == 1) {
        ULONG size = 0;
        if (GetIpAddrTable(NULL, &size, FALSE) == ERROR_INSUFFICIENT_BUFFER && size > 0) {
            PMIB_IPADDRTABLE table = (PMIB_IPADDRTABLE)malloc(size);
            if (table) {
                if (GetIpAddrTable(table, &size, FALSE) == NO_ERROR) {
                    for (DWORD i = 0; i < table->dwNumEntries; i++) {
                        if (table->table[i].dwAddr == want.s_addr && table->table[i].dwIndex != 0) {
                            idx = (uint32_t)table->table[i].dwIndex;
                            break;
                        }
                    }
                }
                free(table);
            }
        }
    }
    if (idx == 0 && server_ip && server_ip[0]) {
        struct in_addr srv;
        if (inet_pton(AF_INET, server_ip, &srv) == 1) {
            DWORD best = 0;
            if (GetBestInterface((IPAddr)srv.s_addr, &best) == NO_ERROR && best != 0) {
                idx = (uint32_t)best;
            }
        }
    }
    return idx;
}
#else
static int read_sysfs_u64(const char *path, uint64_t *out)
{
    FILE *f = fopen(path, "r");
    if (!f) return 0;
    unsigned long long v = 0;
    int ok = (fscanf(f, "%llu", &v) == 1);
    fclose(f);
    if (!ok) return 0;
    *out = (uint64_t)v;
    return 1;
}

static int read_nic_counters_by_index(uint32_t if_index, uint64_t *rx_out, uint64_t *tx_out)
{
    if (if_index == 0) return 0;
    char ifname[IF_NAMESIZE] = {0};
    if (!if_indextoname((unsigned int)if_index, ifname) || !ifname[0]) return 0;
    char p_rx[256], p_tx[256];
    snprintf(p_rx, sizeof p_rx, "/sys/class/net/%s/statistics/rx_bytes", ifname);
    snprintf(p_tx, sizeof p_tx, "/sys/class/net/%s/statistics/tx_bytes", ifname);
    uint64_t rx = 0, tx = 0;
    if (!read_sysfs_u64(p_rx, &rx) || !read_sysfs_u64(p_tx, &tx)) return 0;
    *rx_out = rx;
    *tx_out = tx;
    return 1;
}

static uint32_t find_nic_index_for_ip(const char *ip_text)
{
    struct in_addr want;
    if (!ip_text || !ip_text[0] || inet_pton(AF_INET, ip_text, &want) != 1) return 0;
    struct ifaddrs *ifaddr = NULL;
    if (getifaddrs(&ifaddr) != 0) return 0;
    uint32_t idx = 0;
    for (struct ifaddrs *ifa = ifaddr; ifa; ifa = ifa->ifa_next) {
        if (!ifa->ifa_addr || ifa->ifa_addr->sa_family != AF_INET || !ifa->ifa_name) continue;
        struct sockaddr_in *sa = (struct sockaddr_in *)ifa->ifa_addr;
        if (sa->sin_addr.s_addr == want.s_addr) {
            idx = (uint32_t)if_nametoindex(ifa->ifa_name);
            break;
        }
    }
    freeifaddrs(ifaddr);
    return idx;
}

static uint32_t find_nic_index(const char *local_ip, const char *server_ip)
{
    uint32_t idx = find_nic_index_for_ip(local_ip);
    if (idx != 0) return idx;
    if (server_ip && server_ip[0]) {
        int s = socket(AF_INET, SOCK_DGRAM, 0);
        if (s >= 0) {
            struct sockaddr_in dst;
            memset(&dst, 0, sizeof dst);
            dst.sin_family = AF_INET;
            dst.sin_port = htons(61440);
            if (inet_pton(AF_INET, server_ip, &dst.sin_addr) == 1 &&
                connect(s, (struct sockaddr *)&dst, sizeof dst) == 0) {
                struct sockaddr_in loc;
                socklen_t llen = (socklen_t)sizeof loc;
                if (getsockname(s, (struct sockaddr *)&loc, &llen) == 0) {
                    char route_ip[64] = {0};
                    if (inet_ntop(AF_INET, &loc.sin_addr, route_ip, sizeof route_ip)) {
                        idx = find_nic_index_for_ip(route_ip);
                    }
                }
            }
            close(s);
        }
    }
    return idx;
}
#endif

#ifdef _WIN32
static tray_t       *g_tray = NULL;
static int           g_last_online = -1;
static char          g_last_tip[128] = {0};
#endif

static int stdout_is_safe(void)
{
    if (g_stdout_ok >= 0) return g_stdout_ok;
#ifdef _WIN32
    HANDLE handle = GetStdHandle(STD_OUTPUT_HANDLE);
    if (handle == NULL || handle == INVALID_HANDLE_VALUE) {
        g_stdout_ok = 0;
        return 0;
    }
    g_stdout_ok = (GetFileType(handle) == FILE_TYPE_PIPE) ? 0 : 1;
#else
    g_stdout_ok = 1;
#endif
    return g_stdout_ok;
}

static void log_line(const char *kind, const char *message, const char *detail)
{
    plat_rwlock_wrlock(&log_lock);
    log_entry_t *slot = &log_ring[log_head];
    slot->seq = log_next_seq++;
    snprintf(slot->text, LOG_LEN, "%-16s %s%s%s", kind,
             message ? message : "",
             (detail && detail[0]) ? " | " : "",
             (detail && detail[0]) ? detail : "");
    log_head = (log_head + 1) % LOG_LINES;
    if (log_count < LOG_LINES) log_count++;
    if (stdout_is_safe()) {
        printf("%s\n", slot->text);
        fflush(stdout);
    }
    plat_rwlock_wrunlock(&log_lock);
}

static const char *state_name(engine_state st);

#ifdef _WIN32
static void tray_sync(engine_t *e)
{
    if (!g_tray || !e) return;
    int online = engine_is_online(e);
    char ip[16] = {0};
    engine_get_ip(e, ip);

    char tip[128];
    if (online) {
        snprintf(tip, sizeof tip, "JLU DrCOM NG · 已上线%s%s",
                 ip[0] ? " · " : "", ip);
    } else {
        snprintf(tip, sizeof tip, "JLU DrCOM NG · %s",
                 state_name(engine_get_state(e)));
    }
    if (online == g_last_online && strcmp(tip, g_last_tip) == 0) return;
    g_last_online = online;
    snprintf(g_last_tip, sizeof g_last_tip, "%s", tip);
    tray_set_state(g_tray, online, tip);
}
#else
static void tray_sync(engine_t *e) { (void)e; }
#endif

static void engine_event(void *user, const char *kind, const char *message,
                         const char *detail, int error_code)
{
    (void)user;
    (void)error_code;
    if (strcmp(kind, "keepalive_ok") == 0) {
        if (g_verbose) {
            log_line(kind, message, detail);
        }
        return;
    }
    if (strcmp(kind, "online") == 0) {
        char raw_acc[128] = {0}, server[64] = {0};
        if (g_engine) {
            engine_get_identity(g_engine, raw_acc, sizeof raw_acc, server, sizeof server);
        }
        uint32_t if_idx = find_nic_index(message, server);
        uint64_t rx = 0, tx = 0;
        int ok = read_nic_counters_by_index(if_idx, &rx, &tx);
        double now_epoch = plat_wall_seconds();
        plat_rwlock_wrlock(&g_traffic_lock);
        g_traffic.session_id++;
        g_traffic.online_since_epoch = now_epoch;
        g_traffic.if_index = ok ? if_idx : 0;
        g_traffic.rx_base = ok ? rx : 0;
        g_traffic.tx_base = ok ? tx : 0;
        g_traffic.has_baseline = ok;
        plat_rwlock_wrunlock(&g_traffic_lock);
    } else if (strcmp(kind, "stopped") == 0 || strcmp(kind, "fatal") == 0 ||
               strcmp(kind, "keepalive_failed") == 0) {
        plat_rwlock_wrlock(&g_traffic_lock);
        g_traffic.online_since_epoch = 0.0;
        g_traffic.has_baseline = 0;
        plat_rwlock_wrunlock(&g_traffic_lock);
    }
    log_line(kind, message, detail);
    if (strcmp(kind, "online") == 0 || strcmp(kind, "state") == 0 ||
        strcmp(kind, "ip") == 0 || strcmp(kind, "stopped") == 0 ||
        strcmp(kind, "fatal") == 0) {
        tray_sync(g_engine);
    }
}

/* ---------------------------------------------------------- config reload */
static int reload_full_config(engine_t *e, char *err, size_t errcap)
{
    if (!g_config_path[0]) {
        snprintf(err, errcap, "缺少配置文件路径");
        return -1;
    }
    engine_config fresh;
    engine_config_defaults(&fresh);
    if (drcom_config_load(g_config_path, &fresh, err, errcap, NULL) != 0) {
        return -1;
    }
    if (!fresh.account[0] || !fresh.password[0]) {
        plat_secure_zero(fresh.password, sizeof fresh.password);
        snprintf(err, errcap, "账号或密码为空");
        return -1;
    }
    engine_update_config(e, &fresh);
    plat_secure_zero(fresh.password, sizeof fresh.password);
    return 0;
}

static void request_stop_all(void)
{
    plat_atomic_store(&g_stop, 1);
    plat_event_set(&g_stop_evt);
#ifdef _WIN32
    tray_request_quit(g_tray);
#endif
}

/* ------------------------------------------------------------ tray glue */
#ifdef _WIN32
static int launch_gui(void)
{
    WCHAR candidate[MAX_PATH];
    WCHAR self[MAX_PATH];
    const WCHAR *paths[2];
    WCHAR given[MAX_PATH];
    int count = 0;

    if (g_gui_path[0]) {
        given[0] = L'\0';
        if (MultiByteToWideChar(CP_UTF8, 0, g_gui_path, -1, given, MAX_PATH) > 0) {
            paths[count++] = given;
        }
    }

    DWORD n = GetModuleFileNameW(NULL, self, (DWORD)ARRAYSIZE(self));
    if (n > 0 && n < ARRAYSIZE(self)) {
        WCHAR *slash = wcsrchr(self, L'\\');
        if (slash) {
            *slash = L'\0';
            if (swprintf(candidate, ARRAYSIZE(candidate), L"%s\\..\\JLU-DrCOM-NG.exe", self) > 0) {
                paths[count++] = candidate;
            }
        }
    }

    for (int i = 0; i < count; i++) {
        if (GetFileAttributesW(paths[i]) == INVALID_FILE_ATTRIBUTES) continue;
        HINSTANCE rc = ShellExecuteW(NULL, L"open", paths[i], NULL, NULL, SW_SHOWNORMAL);
        if ((INT_PTR)rc > 32) return 1;
        log_line("warn", "无法打开界面（ShellExecute 失败）", NULL);
    }
    log_line("warn", "找不到界面程序 JLU-DrCOM-NG.exe", NULL);
    return 0;
}

static unsigned __stdcall tray_reconnect_worker(void *arg)
{
    (void)arg;
    engine_t *e = g_engine;
    if (e) {
        if (engine_is_running(e)) {
            engine_stop(e);
            engine_join(e, 5000);
        }
        char rerr[256] = {0};
        reload_full_config(e, rerr, sizeof rerr);
        if (engine_start(e) != 0) {
            log_line("warn", "重新连接失败（上一个线程还没退出）", NULL);
        }
    }
    plat_atomic_store(&g_reconnecting, 0);
    return 0;
}

static void tray_on_show(void *ctx)
{
    (void)ctx;
    launch_gui();
}

static void tray_on_reconnect(void *ctx)
{
    (void)ctx;
    if (plat_atomic_xchg(&g_reconnecting, 1) == 1) return;
    HANDLE h = (HANDLE)_beginthreadex(NULL, 0, tray_reconnect_worker, NULL, 0, NULL);
    if (h) {
        CloseHandle(h);
    } else {
        plat_atomic_store(&g_reconnecting, 0);
    }
}

static void tray_on_quit(void *ctx)
{
    (void)ctx;
    request_stop_all();
}
#endif

/* ---------------------------------------------------------------- status */
static const char *state_name(engine_state st)
{
    switch (st) {
    case ENGINE_STATE_IDLE:           return "idle";
    case ENGINE_STATE_BINDING:        return "binding";
    case ENGINE_STATE_CHALLENGING:    return "challenging";
    case ENGINE_STATE_AUTHENTICATING: return "authenticating";
    case ENGINE_STATE_ONLINE:         return "online";
    case ENGINE_STATE_RETRY_WAIT:     return "retry_wait";
    case ENGINE_STATE_FATAL:          return "fatal";
    case ENGINE_STATE_STOPPED:        return "stopped";
    default:                          return "unknown";
    }
}

static const char *reason_name(offline_reason r)
{
    switch (r) {
    case OFFLINE_NONE:             return "";
    case OFFLINE_USER_LOGOUT:      return "user_logout";
    case OFFLINE_BIND_FAILED:      return "bind_failed";
    case OFFLINE_CHALLENGE_FAILED: return "challenge_failed";
    case OFFLINE_LOGIN_FAILED:     return "login_failed";
    case OFFLINE_TIMEOUT:          return "timeout";
    case OFFLINE_KEEPALIVE_FAILED: return "keepalive_failed";
    case OFFLINE_STOPPED:          return "stopped";
    default:                       return "";
    }
}

static void masked_account(const char *acc, char *out, size_t cap)
{
    size_t n = strlen(acc);
    if (n <= 2) { snprintf(out, cap, "%s", n ? "**" : "(未设置)"); return; }
    if (n <= 5) { snprintf(out, cap, "%c%s", acc[0], "****"); return; }
    snprintf(out, cap, "%.2s****%s", acc, acc + n - 2);
}

static void json_escape(const char *in, char *out, size_t cap)
{
    size_t j = 0;
    for (const char *p = in; *p && j + 2 < cap; p++) {
        if (*p == '"' || *p == '\\') { out[j++] = '\\'; out[j++] = *p; }
        else if ((unsigned char)*p < 0x20) out[j++] = ' ';
        else out[j++] = *p;
    }
    out[j] = '\0';
}

static void build_status_json(engine_t *e, char *out, size_t cap)
{
    char raw_acc[128] = {0}, server[64] = {0};
    engine_get_identity(e, raw_acc, sizeof raw_acc, server, sizeof server);

    char acc[64], acc_esc[128], srv_esc[128], ip[16];
    masked_account(raw_acc, acc, sizeof acc);
    json_escape(acc, acc_esc, sizeof acc_esc);
    json_escape(server, srv_esc, sizeof srv_esc);
    engine_get_ip(e, ip);

    int is_online = engine_is_online(e);
    uint64_t session_id = 0, rx_base = 0, tx_base = 0, session_rx = 0, session_tx = 0;
    uint32_t if_index = 0;
    double online_since = 0.0;
    int has_base = 0;

    plat_rwlock_rdlock(&g_traffic_lock);
    session_id = g_traffic.session_id;
    if (is_online) {
        online_since = g_traffic.online_since_epoch;
        if_index = g_traffic.if_index;
        rx_base = g_traffic.rx_base;
        tx_base = g_traffic.tx_base;
        has_base = g_traffic.has_baseline;
    }
    plat_rwlock_rdunlock(&g_traffic_lock);

    if (is_online && has_base && if_index != 0) {
        uint64_t cur_rx = 0, cur_tx = 0;
        if (read_nic_counters_by_index(if_index, &cur_rx, &cur_tx)) {
            session_rx = (cur_rx >= rx_base) ? (cur_rx - rx_base) : 0;
            session_tx = (cur_tx >= tx_base) ? (cur_tx - tx_base) : 0;
        }
    }

    snprintf(out, cap,
             "{\"app\":\"JLU-DrCOM-NG\",\"core\":\"native\",\"version\":\"%s\","
             "\"state\":\"%s\",\"online\":%s,\"ip\":\"%s\",\"account\":\"%s\","
             "\"server\":\"%s\",\"uptime_seconds\":%.1f,"
             "\"session_id\":%llu,\"online_since\":%.0f,"
             "\"traffic_if_index\":%u,\"traffic_rx_base\":%llu,\"traffic_tx_base\":%llu,"
             "\"session_rx\":%llu,\"session_tx\":%llu,"
             "\"last_reason\":\"%s\",\"consecutive_failures\":%d,"
             "\"timestamp\":%.0f}",
             DRCOMD_VERSION,
             state_name(engine_get_state(e)),
             is_online ? "true" : "false",
             ip, acc_esc, srv_esc,
             engine_uptime(e),
             (unsigned long long)session_id,
             online_since,
             (unsigned int)if_index,
             (unsigned long long)(has_base ? rx_base : 0),
             (unsigned long long)(has_base ? tx_base : 0),
             (unsigned long long)session_rx,
             (unsigned long long)session_tx,
             reason_name(engine_get_reason(e)),
             engine_get_consecutive_failures(e),
             plat_wall_seconds());
}

#define LOGS_JSON_LIMIT 60
#define LOGS_JSON_CAP   (LOGS_JSON_LIMIT * (LOG_LEN * 2) + 128)

static int append_bounded(char *out, size_t cap, size_t *used, size_t reserve_tail,
                          const char *fmt, ...)
{
    if (*used + reserve_tail >= cap) return 0;
    size_t avail = cap - *used - reserve_tail;
    va_list args;
    va_start(args, fmt);
    int n = vsnprintf(out + *used, avail, fmt, args);
    va_end(args);
    if (n < 0 || (size_t)n >= avail) {
        out[*used] = '\0';
        return 0;
    }
    *used += (size_t)n;
    return 1;
}

static char *build_logs_json(uint64_t since_seq)
{
    char *out = (char *)malloc(LOGS_JSON_CAP);
    if (!out) return NULL;
    size_t used = 0;

    plat_rwlock_rdlock(&log_lock);
    uint64_t cur_next = log_next_seq;
    int start = (log_head - log_count + LOG_LINES) % LOG_LINES;

    /* Count matching entries first so we take at most the newest LOGS_JSON_LIMIT. */
    int matched = 0;
    for (int i = 0; i < log_count; i++) {
        const log_entry_t *ent = &log_ring[(start + i) % LOG_LINES];
        if (ent->seq > since_seq) matched++;
    }
    int skip = matched > LOGS_JSON_LIMIT ? matched - LOGS_JSON_LIMIT : 0;

    append_bounded(out, LOGS_JSON_CAP, &used, 4,
                   "{\"next_seq\":%llu,\"lines\":[",
                   (unsigned long long)cur_next);

    int first_item = 1;
    int seen = 0;
    for (int i = 0; i < log_count; i++) {
        const log_entry_t *ent = &log_ring[(start + i) % LOG_LINES];
        if (ent->seq <= since_seq) continue;
        if (seen++ < skip) continue;
        char esc[LOG_LEN * 2];
        json_escape(ent->text, esc, sizeof esc);
        if (!append_bounded(out, LOGS_JSON_CAP, &used, 4, "%s\"%s\"",
                            first_item ? "" : ",", esc)) {
            break;
        }
        first_item = 0;
    }
    plat_rwlock_rdunlock(&log_lock);

    memcpy(out + used, "]}", 3);
    return out;
}

/* ------------------------------------------------------------- HTTP / auth */
static void init_token_path(const char *config_path)
{
    if (g_token_path[0]) return;
    snprintf(g_token_path, sizeof g_token_path, "%s", config_path);
    char *sep1 = strrchr(g_token_path, '\\');
    char *sep2 = strrchr(g_token_path, '/');
    char *sep = sep1 > sep2 ? sep1 : sep2;
    if (sep) {
        snprintf(sep + 1, sizeof g_token_path - (size_t)(sep + 1 - g_token_path),
                 "core-token.txt");
    } else {
        snprintf(g_token_path, sizeof g_token_path, "core-token.txt");
    }
}

static int init_api_token(const char *config_path)
{
    init_token_path(config_path);
    uint8_t raw[24];
    if (plat_random_bytes(raw, sizeof raw) != 0) {
        /* Extremely rare fallback: time + pid entropy. */
        uint64_t s = plat_rand_seed();
        for (size_t i = 0; i < sizeof raw; i++) {
            s = s * 6364136223846793005ULL + 1442695040888963407ULL;
            raw[i] = (uint8_t)(s >> 32);
        }
    }
    static const char hex[] = "0123456789abcdef";
    for (size_t i = 0; i < sizeof raw; i++) {
        g_api_token[i * 2]     = hex[(raw[i] >> 4) & 0xF];
        g_api_token[i * 2 + 1] = hex[raw[i] & 0xF];
    }
    g_api_token[sizeof raw * 2] = '\0';

#ifdef _WIN32
    WCHAR wpath[MAX_PATH * 2];
    FILE *f = NULL;
    if (MultiByteToWideChar(CP_UTF8, 0, g_token_path, -1, wpath, (int)ARRAYSIZE(wpath)) > 0) {
        f = _wfopen(wpath, L"wb");
    } else {
        f = fopen(g_token_path, "wb");
    }
#else
    FILE *f = fopen(g_token_path, "wb");
#endif
    if (f) {
        fprintf(f, "%s\n", g_api_token);
        fclose(f);
#ifndef _WIN32
        chmod(g_token_path, 0600);
#endif
    }
    return 0;
}

static int find_header_ci(const char *headers, size_t hdr_len, const char *name,
                          char *out, size_t cap)
{
    size_t nlen = strlen(name);
    const char *p = headers;
    const char *end = headers + hdr_len;
    /* Skip request line. */
    const char *nl = memchr(p, '\n', (size_t)(end - p));
    if (!nl) return 0;
    p = nl + 1;

    while (p < end) {
        const char *line_end = memchr(p, '\n', (size_t)(end - p));
        if (!line_end) line_end = end;
        size_t line_len = (size_t)(line_end - p);
        while (line_len && (p[line_len - 1] == '\r' || p[line_len - 1] == '\n')) line_len--;
        if (line_len == 0) break;
        if (line_len > nlen && plat_strnicmp(p, name, nlen) == 0 && p[nlen] == ':') {
            const char *val = p + nlen + 1;
            const char *vend = p + line_len;
            while (val < vend && (*val == ' ' || *val == '\t')) val++;
            while (vend > val && (vend[-1] == ' ' || vend[-1] == '\t')) vend--;
            size_t copy = (size_t)(vend - val);
            if (copy >= cap) copy = cap - 1;
            memcpy(out, val, copy);
            out[copy] = '\0';
            return 1;
        }
        p = line_end + 1;
    }
    out[0] = '\0';
    return 0;
}

static int is_loopback_hostname(const char *host)
{
    return plat_stricmp(host, "127.0.0.1") == 0 ||
           plat_stricmp(host, "localhost") == 0 ||
           plat_stricmp(host, "::1") == 0 ||
           plat_stricmp(host, "[::1]") == 0;
}

static int host_header_ok(const char *headers, size_t hdr_len)
{
    char host[256] = {0};
    if (!find_header_ci(headers, hdr_len, "Host", host, sizeof host) || !host[0]) {
        return 1; /* HTTP/1.0 clients may omit Host */
    }
    if (host[0] == '[') {
        char *rb = strchr(host, ']');
        if (!rb) return 0;
        rb[1] = '\0';
        return is_loopback_hostname(host);
    }
    char *colon = strrchr(host, ':');
    if (colon) *colon = '\0';
    return is_loopback_hostname(host);
}

static int origin_header_ok(const char *headers, size_t hdr_len)
{
    char origin[256] = {0};
    if (!find_header_ci(headers, hdr_len, "Origin", origin, sizeof origin) || !origin[0]) {
        return 1;
    }
    if (strcmp(origin, "null") == 0) return 1;

    const char *p = NULL;
    if (plat_strnicmp(origin, "http://", 7) == 0) p = origin + 7;
    else if (plat_strnicmp(origin, "https://", 8) == 0) p = origin + 8;
    else return 0;

    char host[256] = {0};
    size_t i = 0;
    if (*p == '[') {
        while (*p && *p != ']' && i + 1 < sizeof host) host[i++] = *p++;
        if (*p == ']' && i + 1 < sizeof host) host[i++] = *p++;
    } else {
        while (*p && *p != ':' && *p != '/' && i + 1 < sizeof host) host[i++] = *p++;
    }
    host[i] = '\0';
    return is_loopback_hostname(host);
}

static int token_header_ok(const char *headers, size_t hdr_len)
{
    if (!g_api_token[0]) return 1;
    char supplied[128] = {0};
    if (!find_header_ci(headers, hdr_len, "X-DrCOM-Token", supplied, sizeof supplied)) {
        return 0;
    }
    size_t elen = strlen(g_api_token);
    size_t slen = strlen(supplied);
    volatile unsigned char diff = (elen == slen) ? 0 : 1;
    size_t n = elen < slen ? elen : slen;
    for (size_t i = 0; i < n; i++) {
        diff |= (unsigned char)(g_api_token[i] ^ supplied[i]);
    }
    return diff == 0;
}

static void http_reply_status(plat_socket_t s, int code, const char *reason_txt,
                              const char *body, const char *ctype, const char *extra_hdr)
{
    char head[384];
    int blen = (int)strlen(body);
    int n = snprintf(head, sizeof head,
                     "HTTP/1.1 %d %s\r\nContent-Type: %s; charset=utf-8\r\n"
                     "Content-Length: %d\r\nCache-Control: no-store\r\n%s"
                     "Connection: close\r\n\r\n",
                     code, reason_txt, ctype, blen, extra_hdr ? extra_hdr : "");
    send(s, head, n, 0);
    if (blen > 0) send(s, body, blen, 0);
}

static void http_reply(plat_socket_t s, int code, const char *body, const char *ctype)
{
    const char *r = code == 200 ? "OK" : (code == 403 ? "Forbidden" : (code == 404 ? "Not Found" : "Error"));
    http_reply_status(s, code, r, body, ctype, NULL);
}

static void handle_client(plat_socket_t s, engine_t *e)
{
    plat_set_sock_timeout(s, 2000);

    char req[4096];
    int total = 0;
    const char *hdr_end = NULL;
    while (total < (int)sizeof req - 1) {
        int got = (int)recv(s, req + total, (int)sizeof req - 1 - total, 0);
        if (got <= 0) break;
        total += got;
        req[total] = '\0';
        hdr_end = strstr(req, "\r\n\r\n");
        if (hdr_end) break;
    }
    if (total <= 0) { plat_closesocket(s); return; }
    size_t hdr_len = hdr_end ? (size_t)(hdr_end + 4 - req) : (size_t)total;

    struct sockaddr_in peer;
    socklen_t plen = (socklen_t)sizeof peer;
    if (getpeername(s, (struct sockaddr *)&peer, &plen) != 0 ||
        (ntohl(peer.sin_addr.s_addr) >> 24) != 127) {
        http_reply(s, 403, "{\"error\":\"loopback only\"}", "application/json");
        plat_closesocket(s);
        return;
    }

    char method[12] = {0}, path[256] = {0};
    if (sscanf(req, "%11s %255s", method, path) != 2) {
        http_reply(s, 400, "{\"error\":\"malformed request\"}", "application/json");
        plat_closesocket(s);
        return;
    }

    if (strcmp(method, "OPTIONS") == 0) {
        http_reply_status(s, 405, "Method Not Allowed", "", "text/plain", "Allow: GET, POST\r\n");
        plat_closesocket(s);
        return;
    }

    if (!host_header_ok(req, hdr_len)) {
        http_reply(s, 403, "{\"error\":\"bad Host header\"}", "application/json");
        plat_closesocket(s);
        return;
    }

    if (!origin_header_ok(req, hdr_len)) {
        http_reply(s, 403, "{\"error\":\"cross-site requests are refused\"}", "application/json");
        plat_closesocket(s);
        return;
    }

    /* Drain any POST body taking already-buffered bytes into account. */
    if (strcmp(method, "POST") == 0) {
        char cl_buf[32] = {0};
        if (find_header_ci(req, hdr_len, "Content-Length", cl_buf, sizeof cl_buf)) {
            long cl = strtol(cl_buf, NULL, 10);
            if (cl > 4096) cl = 4096;
            long already = (long)((size_t)total - hdr_len);
            long remaining = cl - already;
            char sink[256];
            while (remaining > 0) {
                int chunk = remaining < (long)sizeof sink ? (int)remaining : (int)sizeof sink;
                int got = (int)recv(s, sink, chunk, 0);
                if (got <= 0) break;
                remaining -= got;
            }
        }
    }

    char query[128] = {0};
    char *q = strchr(path, '?');
    if (q) {
        snprintf(query, sizeof query, "%s", q + 1);
        *q = '\0';
    }
    size_t plen_path = strlen(path);
    while (plen_path > 1 && path[plen_path - 1] == '/') path[--plen_path] = '\0';

    char body[1536];

    if (strcmp(method, "GET") == 0) {
        if (strcmp(path, "/status") == 0 || strcmp(path, "/") == 0) {
            build_status_json(e, body, sizeof body);
            http_reply(s, 200, body, "application/json");
        } else if (strcmp(path, "/health") == 0) {
            snprintf(body, sizeof body, "{\"online\":%s,\"state\":\"%s\"}",
                     engine_is_online(e) ? "true" : "false",
                     state_name(engine_get_state(e)));
            http_reply(s, 200, body, "application/json");
        } else if (strcmp(path, "/logs") == 0) {
            if (!token_header_ok(req, hdr_len)) {
                http_reply(s, 403, "{\"error\":\"missing or bad X-DrCOM-Token\"}", "application/json");
                plat_closesocket(s);
                return;
            }
            uint64_t since = 0;
            char *sp = strstr(query, "since=");
            if (sp) since = (uint64_t)strtoull(sp + 6, NULL, 10);
            char *logs_json = build_logs_json(since);
            if (logs_json) {
                http_reply(s, 200, logs_json, "application/json");
                free(logs_json);
            } else {
                http_reply(s, 500, "{\"error\":\"out of memory\"}", "application/json");
            }
        } else {
            http_reply(s, 404, "{\"error\":\"unknown route\"}", "application/json");
        }
    } else if (strcmp(method, "POST") == 0) {
        if (!origin_header_ok(req, hdr_len)) {
            http_reply(s, 403, "{\"error\":\"cross-site requests are refused\"}", "application/json");
            plat_closesocket(s);
            return;
        }
        if (!token_header_ok(req, hdr_len)) {
            http_reply(s, 403, "{\"error\":\"missing or bad X-DrCOM-Token\"}", "application/json");
            plat_closesocket(s);
            return;
        }

        if (strcmp(path, "/login") == 0) {
            if (engine_is_running(e)) {
                http_reply(s, 200, "{\"ok\":true,\"already_running\":true}", "application/json");
            } else {
                char rerr[256] = {0};
                if (reload_full_config(e, rerr, sizeof rerr) != 0) {
                    log_line("warn", "登录前重载配置失败，使用现有配置", rerr);
                }
                if (engine_start(e) != 0) {
                    http_reply(s, 503, "{\"ok\":false,\"error\":\"engine busy\"}", "application/json");
                } else {
                    http_reply(s, 200, "{\"ok\":true}", "application/json");
                }
            }
        } else if (strcmp(path, "/reconnect") == 0) {
            if (engine_is_running(e)) { engine_stop(e); engine_join(e, 5000); }
            char rerr[256] = {0};
            if (reload_full_config(e, rerr, sizeof rerr) != 0) {
                log_line("warn", "重连前重载配置失败，使用现有配置", rerr);
            }
            if (engine_start(e) != 0) {
                http_reply(s, 503, "{\"ok\":false,\"error\":\"engine busy\"}", "application/json");
            } else {
                http_reply(s, 200, "{\"ok\":true,\"reloaded\":true}", "application/json");
            }
        } else if (strcmp(path, "/logout") == 0) {
            engine_stop(e);
            if (!engine_join(e, 5000)) {
                http_reply(s, 503, "{\"ok\":false,\"error\":\"engine still running\"}", "application/json");
            } else {
                http_reply(s, 200, "{\"ok\":true}", "application/json");
            }
        } else if (strcmp(path, "/reload") == 0) {
            engine_config fresh;
            engine_config_defaults(&fresh);
            char rerr[256] = {0};
            if (!g_config_path[0] ||
                drcom_config_load(g_config_path, &fresh, rerr, sizeof rerr, NULL) != 0) {
                http_reply(s, 500, "{\"ok\":false,\"error\":\"config reload failed\"}",
                           "application/json");
            } else {
                engine_update_live_policy(e, &fresh);
                plat_secure_zero(fresh.password, sizeof fresh.password);
                snprintf(body, sizeof body, "{\"ok\":true,\"protocol_hex\":%s}",
                         fresh.protocol_hex ? "true" : "false");
                http_reply(s, 200, body, "application/json");
            }
        } else if (strcmp(path, "/shutdown") == 0) {
            http_reply(s, 200, "{\"ok\":true}", "application/json");
            request_stop_all();
        } else {
            http_reply(s, 404, "{\"error\":\"unknown route\"}", "application/json");
        }
    } else {
        http_reply(s, 400, "{\"error\":\"bad method\"}", "application/json");
    }
    plat_closesocket(s);
}

typedef struct {
    engine_t *e;
    int       port;
} api_ctx_t;

static unsigned
#ifdef _WIN32
__stdcall
#endif
api_thread(void *arg)
{
    api_ctx_t *ctx = (api_ctx_t *)arg;

    plat_socket_t srv = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (srv == PLAT_INVALID_SOCKET) {
        log_line("error", "API socket 创建失败", NULL);
        return 1;
    }

#ifdef _WIN32
    BOOL yes = TRUE;
    setsockopt(srv, SOL_SOCKET, SO_EXCLUSIVEADDRUSE, (const char *)&yes, sizeof yes);
#else
    int yes = 1;
    setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof yes);
#endif

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof addr);
    addr.sin_family = AF_INET;
    addr.sin_port = htons((uint16_t)ctx->port);
    if (inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr) != 1) {
        log_line("error", "无法解析回环地址", NULL);
        plat_closesocket(srv);
        return 1;
    }

    if (bind(srv, (struct sockaddr *)&addr, sizeof addr) != 0 || listen(srv, 8) != 0) {
        char msg[96];
        snprintf(msg, sizeof msg, "API 端口 %d 绑定失败（另一个 drcomd 在跑？）", ctx->port);
        log_line("error", msg, NULL);
        plat_closesocket(srv);
        return 1;
    }
    {
        char msg[96];
        snprintf(msg, sizeof msg, "API 就绪：http://127.0.0.1:%d/status", ctx->port);
        log_line("info", msg, NULL);
    }

    while (!plat_atomic_load(&g_stop)) {
        fd_set rd;
        FD_ZERO(&rd);
        FD_SET(srv, &rd);
        struct timeval tv = { 0, 250000 };
#ifdef _WIN32
        int rc = select(0, &rd, NULL, NULL, &tv);
#else
        int rc = select(srv + 1, &rd, NULL, NULL, &tv);
#endif
        if (rc <= 0) continue;
        plat_socket_t c = accept(srv, NULL, NULL);
        if (c == PLAT_INVALID_SOCKET) continue;
        handle_client(c, ctx->e);
    }
    plat_closesocket(srv);
    return 0;
}

/* ------------------------------------------------------------------ main */
#ifdef _WIN32
static BOOL WINAPI ctrl_handler(DWORD type)
{
    (void)type;
    request_stop_all();
    return TRUE;
}
#else
static void sig_handler(int sig)
{
    (void)sig;
    plat_atomic_store(&g_stop, 1);
}
#endif

static void usage(void)
{
    printf("drcomd %s - native Dr.COM keepalive core\n\n"
           "  --config <path>      settings file\n"
           "  --token-file <path>  control token file (default: <config_dir>/core-token.txt)\n"
           "  --api-port <n>       status/control port (default: 61441)\n"
           "  --gui <path>         window binary the tray menu should open\n"
           "  --tray               show a notification-area icon (Windows)\n"
           "  --no-api             run without the HTTP listener\n"
           "  --dry-run            load the config, print what was found, exit\n"
           "  --once               authenticate once and exit (no reconnect loop)\n"
           "  --verbose            also print keepalive ticks\n"
           "  --dump-hex           dump every datagram as hex\n",
           DRCOMD_VERSION);
}

static int run_drcomd(int argc, char **argv)
{
    const char *config_path = NULL;
    int api_port = 0;
    int no_api = 0, dry_run = 0, once = 0, want_tray = 0, dump_hex = 0;
    char default_path[1024];

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--config") == 0 && i + 1 < argc) config_path = argv[++i];
        else if (strcmp(argv[i], "--token-file") == 0 && i + 1 < argc) {
            snprintf(g_token_path, sizeof g_token_path, "%s", argv[++i]);
        }
        else if (strcmp(argv[i], "--api-port") == 0 && i + 1 < argc) api_port = atoi(argv[++i]);
        else if (strcmp(argv[i], "--gui") == 0 && i + 1 < argc) {
            snprintf(g_gui_path, sizeof g_gui_path, "%s", argv[++i]);
        }
        else if (strcmp(argv[i], "--tray") == 0) want_tray = 1;
        else if (strcmp(argv[i], "--no-api") == 0) no_api = 1;
        else if (strcmp(argv[i], "--dry-run") == 0) dry_run = 1;
        else if (strcmp(argv[i], "--once") == 0) once = 1;
        else if (strcmp(argv[i], "--verbose") == 0) g_verbose = 1;
        else if (strcmp(argv[i], "--dump-hex") == 0) dump_hex = 1;
        else if (strcmp(argv[i], "--help") == 0 || strcmp(argv[i], "-h") == 0) { usage(); return 0; }
        else { printf("unknown argument: %s\n\n", argv[i]); usage(); return 2; }
    }

    if (!config_path) {
#ifdef _WIN32
        const char *appdata = getenv("APPDATA");
        if (!appdata) appdata = "C:\\Users\\Public";
        snprintf(default_path, sizeof default_path, "%s\\JLU-DrCOM-NG\\config.json", appdata);
#else
        const char *xdg = getenv("XDG_CONFIG_HOME");
        if (xdg && xdg[0]) {
            snprintf(default_path, sizeof default_path, "%s/jlu-drcom-ng/config.json", xdg);
        } else {
            const char *home = getenv("HOME");
            if (!home) home = "/tmp";
            snprintf(default_path, sizeof default_path, "%s/.config/jlu-drcom-ng/config.json", home);
        }
#endif
        config_path = default_path;
    }
    snprintf(g_config_path, sizeof g_config_path, "%s", config_path);

    engine_config cfg;
    engine_config_defaults(&cfg);
    char err[512] = {0};
    int cfg_api = 0;
    if (drcom_config_load(g_config_path, &cfg, err, sizeof err, &cfg_api) != 0) {
        printf("配置加载失败：%s\n  (%s)\n", err, g_config_path);
        return 3;
    }
    if (api_port == 0) api_port = (cfg_api > 0 && cfg_api != 8862) ? cfg_api : 61441;
    if (dump_hex) cfg.protocol_hex = 1;

    printf("drcomd %s 配置\n", DRCOMD_VERSION);
    printf("  配置文件 : %s\n", g_config_path);
    { char masked[64]; masked_account(cfg.account, masked, sizeof masked); printf("  账号     : %s\n", masked); }
    printf("  MAC      : %s\n", cfg.mac[0] ? "(已读取)" : "(空)");
    printf("  服务器   : %s:%d\n", cfg.server, cfg.port);
    printf("  本地绑定 : %s:%d\n", cfg.bind_address[0] ? cfg.bind_address : "0.0.0.0", cfg.bind_port);
    printf("  保活间隔 : %.0f 秒\n", cfg.keepalive_interval);
    printf("  协议转储 : %s\n", cfg.protocol_hex ? "开" : "关");
    printf("  接口回退 : %s\n", cfg.auto_interface_fallback ? "开" : "关");
    printf("  密码     : %s (%zu 字节)\n", cfg.password[0] ? "已解密" : "空", strlen(cfg.password));
    if (dry_run) {
        plat_secure_zero(cfg.password, sizeof cfg.password);
        return 0;
    }

    if (!cfg.account[0] || !cfg.password[0]) {
        printf("账号或密码为空，无法认证\n");
        return 3;
    }
    if (once) cfg.reconnect_enabled = 0;

    if (plat_event_create(&g_stop_evt) != 0) return 4;

#ifdef _WIN32
    SetConsoleCtrlHandler(ctrl_handler, TRUE);
#else
    signal(SIGINT, sig_handler);
    signal(SIGTERM, sig_handler);
    signal(SIGPIPE, SIG_IGN);
#endif

    engine_t *e = engine_create(&cfg, engine_event, NULL);
    plat_secure_zero(cfg.password, sizeof cfg.password);
    if (!e) { printf("engine_create 失败\n"); plat_event_destroy(&g_stop_evt); return 4; }
    g_engine = e;

    if (engine_start(e) != 0) {
        printf("engine_start 失败\n");
        engine_destroy(e);
        plat_event_destroy(&g_stop_evt);
        return 4;
    }

    plat_thread_t api = 0;
    static api_ctx_t ctx;
    if (!no_api) {
        init_api_token(g_config_path);
        ctx.e = e;
        ctx.port = api_port;
        plat_thread_create(&api, api_thread, &ctx);
    }

#ifdef _WIN32
    if (want_tray && !once) {
        g_tray = tray_create_inline(tray_on_show, tray_on_reconnect, tray_on_quit, NULL);
        if (g_tray) {
            log_line("info", "托盘图标已就绪", NULL);
            tray_sync(e);
        } else {
            log_line("warn", "托盘图标创建失败（资源管理器没在跑？）", NULL);
        }
    }
    /* Trim startup cold pages once initialisation settles. */
    SetProcessWorkingSetSizeEx(GetCurrentProcess(), (SIZE_T)-1, (SIZE_T)-1, 0);
#endif

    if (once) {
        while (!plat_atomic_load(&g_stop)) {
            engine_state st = engine_get_state(e);
            if (st == ENGINE_STATE_ONLINE) {
                plat_sleep_ms(3000);
                break;
            }
            if (st == ENGINE_STATE_FATAL || st == ENGINE_STATE_STOPPED) break;
            plat_sleep_ms(100);
        }
    } else {
        printf("drcomd 运行中（Ctrl+C 退出）\n");
#ifdef _WIN32
        if (g_tray) {
            tray_run_loop(g_tray);
        } else {
            while (!plat_atomic_load(&g_stop)) {
                plat_event_wait_ms(&g_stop_evt, 60000);
            }
        }
#else
        while (!plat_atomic_load(&g_stop)) {
            plat_event_wait_ms(&g_stop_evt, 1000);
        }
#endif
    }

    printf("正在退出…\n");
    plat_atomic_store(&g_stop, 1);
    plat_event_set(&g_stop_evt);
#ifdef _WIN32
    if (g_tray) {
        tray_destroy(g_tray);
        g_tray = NULL;
    }
#endif
    engine_stop(e);
    engine_join(e, 4000);
    if (api) plat_thread_join(&api, 2000);
    g_engine = NULL;
    engine_destroy(e);
    plat_event_destroy(&g_stop_evt);
    printf("已退出\n");
    return 0;
}

#ifdef _WIN32
int wmain(int argc, wchar_t **argv)
{
    char **uargv = (char **)calloc((size_t)argc + 1, sizeof(char *));
    if (!uargv) return 1;
    for (int i = 0; i < argc; i++) {
        int need = WideCharToMultiByte(CP_UTF8, 0, argv[i], -1, NULL, 0, NULL, NULL);
        uargv[i] = (char *)malloc((size_t)(need > 0 ? need : 1));
        if (uargv[i] && need > 0) {
            WideCharToMultiByte(CP_UTF8, 0, argv[i], -1, uargv[i], need, NULL, NULL);
        } else if (uargv[i]) {
            uargv[i][0] = '\0';
        }
    }
    int rc = run_drcomd(argc, uargv);
    for (int i = 0; i < argc; i++) free(uargv[i]);
    free(uargv);
    return rc;
}
#else
int main(int argc, char **argv)
{
    return run_drcomd(argc, argv);
}
#endif
