/*
 * drcomd - the resident core.
 *
 * One process, one worker thread for the network, one small HTTP listener that
 * speaks the same vocabulary the Python build's StatusServer already uses, so
 * the UI can treat either implementation as "the thing that authenticates".
 *
 *   GET  /status     JSON snapshot
 *   GET  /health     {"online":bool,"state":"..."}
 *   GET  /logs       recent events (ring buffer, newest last)
 *   POST /login      start / restart the session
 *   POST /logout     stop the session, stay resident
 *   POST /reconnect  stop + start
 *   POST /shutdown   exit the process
 *
 * Usage:
 *   drcomd.exe --config %APPDATA%\JLU-DrCOM-NG\config.json --api-port 61441
 *   drcomd.exe --dry-run          # load config, print what it found, exit
 */
/* Winsock2 must come before windows.h: windows.h pulls in the ancient winsock.h,
 * and the two then redefine ip_mreq and friends. */
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <shellapi.h>

#include "engine.h"
#include "config.h"
#include "tray.h"

#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <string.h>
#include <process.h>

#pragma comment(lib, "ws2_32.lib")
#pragma comment(lib, "shell32.lib")

/* ------------------------------------------------------------- log ring */
/* Sizing matters: this array is zero-initialised .data, so it is resident for
 * the life of the process.  200x256 was 50 KB for entries that average 60 bytes
 * and that nobody scrolls back through; 120x192 is 23 KB and still covers the
 * last couple of hours of events. */
#define LOG_LINES 120
#define LOG_LEN   192

static SRWLOCK   log_lock = SRWLOCK_INIT;
static char      log_ring[LOG_LINES][LOG_LEN];
static int       log_head = 0;     /* next slot to write */
static int       log_count = 0;
static volatile LONG g_stop = 0;
static int       g_verbose = 0;
static int       g_stdout_ok = -1;   /* -1 unknown, 0 unsafe, 1 safe */

/*
 * Whether writing to stdout can block forever.
 *
 * When the daemon is started by a service wrapper or a scheduled task with
 * stdout redirected into a pipe that nobody reads, the 4 KB pipe buffer fills
 * and printf() blocks - and it is called from the engine thread, so
 * authentication stops while /status still claims to be online.  Detect that
 * case once and keep the console quiet instead.
 */
static int stdout_is_safe(void)
{
    if (g_stdout_ok >= 0) return g_stdout_ok;
    HANDLE handle = GetStdHandle(STD_OUTPUT_HANDLE);
    if (handle == NULL || handle == INVALID_HANDLE_VALUE) {
        g_stdout_ok = 0;
        return 0;
    }
    g_stdout_ok = (GetFileType(handle) == FILE_TYPE_PIPE) ? 0 : 1;
    return g_stdout_ok;
}

static void log_line(const char *kind, const char *message, const char *detail)
{
    AcquireSRWLockExclusive(&log_lock);
    char *slot = log_ring[log_head];
    snprintf(slot, LOG_LEN, "%-16s %s%s%s", kind,
             message ? message : "",
             (detail && detail[0]) ? " | " : "",
             (detail && detail[0]) ? detail : "");
    log_head = (log_head + 1) % LOG_LINES;
    if (log_count < LOG_LINES) log_count++;
    if (stdout_is_safe()) {
        printf("%s\n", slot);
        fflush(stdout);
    }
    ReleaseSRWLockExclusive(&log_lock);
}

static void engine_event(void *user, const char *kind, const char *message,
                         const char *detail, int error_code)
{
    (void)user;
    (void)error_code;
    /* keepalive_ok fires every 20 s: worth watching with --verbose, too noisy
     * to keep in the ring buffer. */
    if (strcmp(kind, "keepalive_ok") == 0) {
        if (g_verbose) {
            log_line(kind, message, detail);
        }
        return;
    }
    log_line(kind, message, detail);
}

/* ------------------------------------------------------------ tray glue */
static const char *state_name(engine_state st);

/*
 * The tray callbacks run on the icon's own thread, where blocking is not an
 * option: the menu would freeze.  They only record what the user asked for and
 * let the main loop do it.
 */
enum { TRAY_CMD_NONE = 0, TRAY_CMD_SHOW = 1, TRAY_CMD_RECONNECT = 2, TRAY_CMD_QUIT = 3 };

static volatile LONG g_tray_cmd = TRAY_CMD_NONE;
static tray_t       *g_tray = NULL;

static void tray_on_show(void *ctx)
{
    (void)ctx;
    InterlockedExchange(&g_tray_cmd, TRAY_CMD_SHOW);
}

static void tray_on_reconnect(void *ctx)
{
    (void)ctx;
    InterlockedExchange(&g_tray_cmd, TRAY_CMD_RECONNECT);
}

static void tray_on_quit(void *ctx)
{
    (void)ctx;
    InterlockedExchange(&g_tray_cmd, TRAY_CMD_QUIT);
}

/* Path of the GUI, given on the command line by whoever started us. */
static char g_gui_path[MAX_PATH];

/*
 * Launch the window.
 *
 * The daemon is started by the GUI, so the GUI's path has to come back to us
 * somehow: the installer puts JLU-DrCOM-NG.exe one level above _internal, and a
 * source checkout puts main.py in the parent of native/.  Try the recorded path
 * first, then those two shapes.
 */
static int launch_gui(void)
{
    char candidate[MAX_PATH];
    const char *paths[3];
    int count = 0;

    if (g_gui_path[0]) paths[count++] = g_gui_path;

    char self[MAX_PATH];
    DWORD n = GetModuleFileNameA(NULL, self, sizeof self);
    if (n > 0 && n < sizeof self) {
        char *slash = strrchr(self, '\\');
        if (slash) {
            *slash = '\0';
            /* ...\_internal\drcomd.exe -> ...\JLU-DrCOM-NG.exe */
            snprintf(candidate, sizeof candidate, "%s\\..\\JLU-DrCOM-NG.exe", self);
            paths[count++] = candidate;
        }
    }

    for (int i = 0; i < count; i++) {
        if (GetFileAttributesA(paths[i]) == INVALID_FILE_ATTRIBUTES) continue;
        HINSTANCE rc = ShellExecuteA(NULL, "open", paths[i], NULL, NULL, SW_SHOWNORMAL);
        if ((INT_PTR)rc > 32) return 1;
        log_line("warn", "无法打开界面", paths[i]);
    }
    log_line("warn", "找不到界面程序 JLU-DrCOM-NG.exe", NULL);
    return 0;
}

/* Refresh the hover text; only touches the shell when something changed. */
static void tray_sync(engine_t *e, int *last_online, char *last_tip, size_t cap)
{
    if (!g_tray) return;
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
    if (online == *last_online && strcmp(tip, last_tip) == 0) return;
    *last_online = online;
    snprintf(last_tip, cap, "%s", tip);
    tray_set_state(g_tray, online, tip);
}

/* ---------------------------------------------------------------- status */
static const char *state_name(engine_state st)
{
    switch (st) {
    case ENGINE_STATE_IDLE:          return "idle";
    case ENGINE_STATE_BINDING:       return "binding";
    case ENGINE_STATE_CHALLENGING:   return "challenging";
    case ENGINE_STATE_AUTHENTICATING:return "authenticating";
    case ENGINE_STATE_ONLINE:        return "online";
    case ENGINE_STATE_RETRY_WAIT:    return "retry_wait";
    case ENGINE_STATE_FATAL:         return "fatal";
    case ENGINE_STATE_STOPPED:       return "stopped";
    default:                         return "unknown";
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

/* Mask the account the same way drcom/config.py does: kk****kk. */
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

static void build_status_json(engine_t *e, const engine_config *cfg, char *out, size_t cap)
{
    char acc[64], acc_esc[128], ip[16];
    masked_account(cfg->account, acc, sizeof acc);
    json_escape(acc, acc_esc, sizeof acc_esc);
    engine_get_ip(e, ip);

    FILETIME ft; GetSystemTimeAsFileTime(&ft);
    unsigned long long ticks = ((unsigned long long)ft.dwHighDateTime << 32) | ft.dwLowDateTime;
    double timestamp = (double)(ticks / 10000000ULL) - 11644473600.0;

    snprintf(out, cap,
             "{\"app\":\"JLU-DrCOM-NG\",\"core\":\"native\",\"version\":\"2.0.0\","
             "\"state\":\"%s\",\"online\":%s,\"ip\":\"%s\",\"account\":\"%s\","
             "\"server\":\"%s\",\"uptime_seconds\":%.1f,"
             "\"last_reason\":\"%s\",\"consecutive_failures\":%d,"
             "\"timestamp\":%.0f}",
             state_name(engine_get_state(e)),
             engine_is_online(e) ? "true" : "false",
             ip, acc_esc, cfg->server,
             engine_uptime(e),
             reason_name(engine_get_reason(e)),
             engine_get_consecutive_failures(e),
             timestamp);
}

/* /logs returns the newest entries only: the UI shows a handful and nobody
 * scrolls further.  40 x (192 bytes + JSON escaping) + envelope. */
#define LOGS_JSON_LIMIT 40
#define LOGS_JSON_CAP   (LOGS_JSON_LIMIT * (LOG_LEN * 2) + 64)

/* Kept off the stack (16 KB) and reused: requests are serialised by the accept
 * loop, so there is no concurrent writer. */
static char g_logs_buf[LOGS_JSON_CAP];

/* Append to a bounded buffer; returns 0 when it no longer fits.
 * snprintf returns the length it *wanted* to write, so a naive `used += n`
 * walks `used` past `cap` and makes the next `cap - used` underflow into a
 * gigantic size_t - an out-of-bounds write waiting to happen. */
static int append(char *out, size_t cap, size_t *used, const char *fmt, ...)
{
    if (*used >= cap) return 0;
    va_list args;
    va_start(args, fmt);
    int n = vsnprintf(out + *used, cap - *used, fmt, args);
    va_end(args);
    if (n < 0) return 0;
    if ((size_t)n >= cap - *used) {         /* truncated: stop here */
        *used = cap - 1;
        return 0;
    }
    *used += (size_t)n;
    return 1;
}

static void build_logs_json(char *out, size_t cap)
{
    size_t used = 0;
    append(out, cap, &used, "{\"lines\":[");
    AcquireSRWLockShared(&log_lock);
    int start = (log_head - log_count + LOG_LINES) % LOG_LINES;
    int first = log_count > LOGS_JSON_LIMIT ? log_count - LOGS_JSON_LIMIT : 0;
    int first_item = 1;
    for (int i = first; i < log_count; i++) {
        char esc[LOG_LEN * 2];
        json_escape(log_ring[(start + i) % LOG_LINES], esc, sizeof esc);
        /* No trailing comma on the last entry that fits: if the buffer fills we
         * stop mid-list and still close the array below. */
        if (!append(out, cap, &used, "%s\"%s\"", first_item ? "" : ",", esc)) break;
        first_item = 0;
    }
    ReleaseSRWLockShared(&log_lock);
    if (used + 3 < cap) {
        memcpy(out + used, "]}", 2);
        out[used + 2] = '\0';
    }
}

/* ------------------------------------------------------------------ HTTP */
static void http_reply(SOCKET s, int code, const char *body, const char *ctype)
{
    char head[256];
    int n = snprintf(head, sizeof head,
                     "HTTP/1.1 %d %s\r\nContent-Type: %s\r\nContent-Length: %d\r\n"
                     "Connection: close\r\n\r\n",
                     code, code == 200 ? "OK" : "Error", ctype, (int)strlen(body));
    send(s, head, n, 0);
    send(s, body, (int)strlen(body), 0);
}

static void handle_client(SOCKET s, engine_t *e, const engine_config *cfg)
{
    /* A client that connects and then says nothing must not be able to park this
     * thread forever: /logout and /shutdown are the only ways to reach the
     * daemon, so a wedged API thread makes the process unmanageable. */
    DWORD io_timeout = 2000;
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, (const char *)&io_timeout, sizeof io_timeout);
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, (const char *)&io_timeout, sizeof io_timeout);

    char req[2048];
    int n = recv(s, req, sizeof req - 1, 0);
    if (n <= 0) { closesocket(s); return; }
    req[n] = '\0';

    /* Only loopback clients: refuse anyone else outright. */
    struct sockaddr_in peer;
    int plen = sizeof peer;
    getpeername(s, (struct sockaddr *)&peer, &plen);
    if ((ntohl(peer.sin_addr.s_addr) >> 24) != 127) {
        http_reply(s, 403, "{\"error\":\"loopback only\"}", "application/json");
        closesocket(s);
        return;
    }

    char method[8] = {0}, path[256] = {0};
    if (sscanf(req, "%7s %255s", method, path) != 2) {
        http_reply(s, 400, "{\"error\":\"malformed request\"}", "application/json");
        closesocket(s);
        return;
    }
    /* Consume any body.  Closing a socket with unread data makes Windows send
     * an RST, and well-behaved clients (requests, urllib) then discard the
     * response we already wrote - the call looks like it failed although it
     * worked. */
    if (strcmp(method, "POST") == 0) {
        char *cl = strstr(req, "Content-Length:");
        if (cl) {
            long remaining = strtol(cl + 15, NULL, 10);
            char sink[256];
            while (remaining > 0) {
                int chunk = remaining < (long)sizeof sink ? (int)remaining : (int)sizeof sink;
                int got = recv(s, sink, chunk, 0);
                if (got <= 0) break;
                remaining -= got;
            }
        }
    }

    char *q = strchr(path, '?');
    if (q) *q = '\0';
    size_t plen_path = strlen(path);
    while (plen_path > 1 && path[plen_path - 1] == '/') path[--plen_path] = '\0';

    /* 1 KB is plenty for /status and /health; /logs has its own buffer. */
    char body[1024];

    if (strcmp(method, "GET") == 0) {
        if (strcmp(path, "/status") == 0 || strcmp(path, "/") == 0) {
            build_status_json(e, cfg, body, sizeof body);
            http_reply(s, 200, body, "application/json");
        } else if (strcmp(path, "/health") == 0) {
            snprintf(body, sizeof body, "{\"online\":%s,\"state\":\"%s\"}",
                     engine_is_online(e) ? "true" : "false",
                     state_name(engine_get_state(e)));
            http_reply(s, 200, body, "application/json");
        } else if (strcmp(path, "/logs") == 0) {
            build_logs_json(body, sizeof body);
            http_reply(s, 200, body, "application/json");
        } else {
            http_reply(s, 404, "{\"error\":\"unknown route\"}", "application/json");
        }
    } else if (strcmp(method, "POST") == 0) {
        if (strcmp(path, "/login") == 0 || strcmp(path, "/reconnect") == 0) {
            if (engine_is_running(e)) { engine_stop(e); engine_join(e, 5000); }
            /* engine_start fails when the previous worker has not finished yet.
             * Reporting ok there would tell the UI "logged in" while nothing
             * runs, which is the worst possible answer. */
            if (engine_start(e) != 0) {
                http_reply(s, 503, "{\"ok\":false,\"error\":\"engine busy\"}", "application/json");
            } else {
                http_reply(s, 200, "{\"ok\":true}", "application/json");
            }
        } else if (strcmp(path, "/logout") == 0) {
            engine_stop(e);
            if (!engine_join(e, 5000)) {
                http_reply(s, 503, "{\"ok\":false,\"error\":\"engine still running\"}", "application/json");
            } else {
                http_reply(s, 200, "{\"ok\":true}", "application/json");
            }
        } else if (strcmp(path, "/shutdown") == 0) {
            http_reply(s, 200, "{\"ok\":true}", "application/json");
            InterlockedExchange(&g_stop, 1);
        } else {
            http_reply(s, 404, "{\"error\":\"unknown route\"}", "application/json");
        }
    } else {
        http_reply(s, 400, "{\"error\":\"bad method\"}", "application/json");
    }
    closesocket(s);
}

static unsigned __stdcall api_thread(void *arg)
{
    struct { engine_t *e; engine_config *cfg; int port; } *ctx = arg;

    SOCKET srv = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (srv == INVALID_SOCKET) { log_line("error", "API socket 创建失败", NULL); return 1; }

    BOOL yes = TRUE;
    setsockopt(srv, SOL_SOCKET, SO_EXCLUSIVEADDRUSE, (const char *)&yes, sizeof yes);

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof addr);
    addr.sin_family = AF_INET;
    addr.sin_port = htons((u_short)ctx->port);
    /* InetPton rather than inet_addr: the latter returns INADDR_NONE on failure,
     * which is also the value of 255.255.255.255, so a typo would silently bind
     * the whole internet instead of loopback. */
    if (InetPtonA(AF_INET, "127.0.0.1", &addr.sin_addr) != 1) {
        log_line("error", "无法解析回环地址", NULL);
        closesocket(srv);
        return 1;
    }

    if (bind(srv, (struct sockaddr *)&addr, sizeof addr) != 0 || listen(srv, 8) != 0) {
        char msg[96];
        snprintf(msg, sizeof msg, "API 端口 %d 绑定失败（另一个 drcomd 在跑？）", ctx->port);
        log_line("error", msg, NULL);
        closesocket(srv);
        return 1;
    }
    {
        char msg[96];
        snprintf(msg, sizeof msg, "API 就绪：http://127.0.0.1:%d/status", ctx->port);
        log_line("info", msg, NULL);
    }

    while (!g_stop) {
        fd_set rd;
        FD_ZERO(&rd);
        FD_SET(srv, &rd);
        struct timeval tv = { 0, 200000 };
        int rc = select(0, &rd, NULL, NULL, &tv);
        if (rc <= 0) continue;
        SOCKET c = accept(srv, NULL, NULL);
        if (c == INVALID_SOCKET) continue;
        handle_client(c, ctx->e, ctx->cfg);
    }
    closesocket(srv);
    return 0;
}

/* ------------------------------------------------------------------ main */
static BOOL WINAPI ctrl_handler(DWORD type)
{
    (void)type;
    InterlockedExchange(&g_stop, 1);
    return TRUE;
}

static void usage(void)
{
    printf("drcomd - native Dr.COM keepalive core\n\n"
           "  --config <path>    settings file (default: %%APPDATA%%\\JLU-DrCOM-NG\\config.json)\n"
           "  --api-port <n>     status/control port (default: 61441)\n"
           "  --gui <path>       window binary the tray menu should open\n"
           "  --tray             show a notification-area icon (menu: open / reconnect / quit)\n"
           "  --no-api           run without the HTTP listener\n"
           "  --dry-run          load the config, print what was found, exit\n"
           "  --once             authenticate once and exit (no reconnect loop)\n"
           "  --verbose          also print keepalive ticks\n");
}

int main(int argc, char **argv)
{
    const char *config_path = NULL;
    int api_port = 0;              /* 0 = not given on the command line */
    int no_api = 0, dry_run = 0, once = 0, want_tray = 0;
    char default_path[MAX_PATH];
    static char path_buf[MAX_PATH];

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--config") == 0 && i + 1 < argc) config_path = argv[++i];
        else if (strcmp(argv[i], "--api-port") == 0 && i + 1 < argc) api_port = atoi(argv[++i]);
        else if (strcmp(argv[i], "--gui") == 0 && i + 1 < argc) {
            snprintf(g_gui_path, sizeof g_gui_path, "%s", argv[++i]);
        }
        else if (strcmp(argv[i], "--tray") == 0) want_tray = 1;
        else if (strcmp(argv[i], "--no-api") == 0) no_api = 1;
        else if (strcmp(argv[i], "--dry-run") == 0) dry_run = 1;
        else if (strcmp(argv[i], "--once") == 0) once = 1;
        else if (strcmp(argv[i], "--verbose") == 0) g_verbose = 1;
        else if (strcmp(argv[i], "--help") == 0 || strcmp(argv[i], "-h") == 0) { usage(); return 0; }
        else { printf("unknown argument: %s\n\n", argv[i]); usage(); return 2; }
    }

    if (!config_path) {
        const char *appdata = getenv("APPDATA");
        if (!appdata) appdata = "C:\\Users\\Public";
        snprintf(default_path, sizeof default_path, "%s\\JLU-DrCOM-NG\\config.json", appdata);
        config_path = default_path;
    }
    snprintf(path_buf, sizeof path_buf, "%s", config_path);

    engine_config cfg;
    engine_config_defaults(&cfg);
    char err[512] = {0};
    int cfg_api = 0;
    if (drcom_config_load(path_buf, &cfg, err, sizeof err, &cfg_api) != 0) {
        printf("配置加载失败：%s\n  (%s)\n", err, path_buf);
        return 3;
    }
    /* Command line wins over the file.  The GUI passes an explicit port because
     * it has to know where to look; falling back to the shared config.json is
     * only for someone starting the daemon by hand, and even then we avoid the
     * port the Python StatusServer may already own. */
    if (api_port == 0) api_port = (cfg_api > 0 && cfg_api != 8862) ? cfg_api : 61441;

    printf("drcomd 配置\n");
    printf("  配置文件 : %s\n", path_buf);
    { char masked[64]; masked_account(cfg.account, masked, sizeof masked); printf("  账号     : %s\n", masked); }
    /* MAC is personal data: never echo it, not even to the console. */
    printf("  MAC      : %s\n", cfg.mac[0] ? "(已读取)" : "(空)");
    printf("  服务器   : %s:%d\n", cfg.server, cfg.port);
    printf("  本地绑定 : %s:%d\n", cfg.bind_address[0] ? cfg.bind_address : "0.0.0.0", cfg.bind_port);
    printf("  保活间隔 : %.0f 秒\n", cfg.keepalive_interval);
    printf("  密码     : %s (%zu 字节)\n", cfg.password[0] ? "已解密" : "空", strlen(cfg.password));
    if (dry_run) return 0;

    if (!cfg.account[0] || !cfg.password[0]) {
        printf("账号或密码为空，无法认证\n");
        return 3;
    }
    if (once) cfg.reconnect_enabled = 0;

    SetConsoleCtrlHandler(ctrl_handler, TRUE);

    engine_t *e = engine_create(&cfg, engine_event, NULL);
    if (!e) { printf("engine_create 失败\n"); return 4; }

    if (engine_start(e) != 0) {
        printf("engine_start 失败\n");
        engine_destroy(e);
        return 4;
    }

    HANDLE api = NULL;
    static struct { engine_t *e; engine_config *cfg; int port; } ctx;
    if (!no_api) {
        ctx.e = e; ctx.cfg = &cfg; ctx.port = api_port;
        api = (HANDLE)_beginthreadex(NULL, 0, api_thread, &ctx, 0, NULL);
    }

    int last_online = -1;
    char last_tip[128] = {0};
    if (want_tray && !dry_run) {
        g_tray = tray_create(tray_on_show, tray_on_reconnect, tray_on_quit, NULL);
        if (g_tray) {
            log_line("info", "托盘图标已就绪", NULL);
        } else {
            /* Not fatal: without Explorer there is nowhere to put an icon, and
             * the daemon still authenticates. */
            log_line("warn", "托盘图标创建失败（资源管理器没在跑？）", NULL);
        }
    }

    if (once) {
        /* Wait for a terminal state, then leave. */
        while (!g_stop) {
            engine_state st = engine_get_state(e);
            if (st == ENGINE_STATE_ONLINE) {
                Sleep(3000);   /* prove one keepalive round works, then exit */
                break;
            }
            if (st == ENGINE_STATE_FATAL || st == ENGINE_STATE_STOPPED) break;
            Sleep(100);
        }
    } else {
        printf("drcomd 运行中（Ctrl+C 退出）\n");
        while (!g_stop) {
            tray_sync(e, &last_online, last_tip, sizeof last_tip);
            LONG cmd = InterlockedExchange(&g_tray_cmd, TRAY_CMD_NONE);
            if (cmd == TRAY_CMD_SHOW) {
                launch_gui();
            } else if (cmd == TRAY_CMD_RECONNECT) {
                if (engine_is_running(e)) { engine_stop(e); engine_join(e, 5000); }
                if (engine_start(e) != 0) {
                    log_line("warn", "重新连接失败（上一个线程还没退出）", NULL);
                }
            } else if (cmd == TRAY_CMD_QUIT) {
                break;
            }
            /* 200 ms is the same cadence the API thread uses; both are far below
             * the cost of a keepalive packet, so this does not register on CPU. */
            Sleep(200);
        }
    }

    printf("正在退出…\n");
    tray_destroy(g_tray);
    g_tray = NULL;
    engine_stop(e);
    engine_join(e, 4000);
    if (api) { WaitForSingleObject(api, 2000); CloseHandle(api); }
    engine_destroy(e);
    printf("已退出\n");
    return 0;
}
