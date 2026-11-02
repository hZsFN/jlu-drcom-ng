/*
 * Dr.COM authentication engine (JLU variant), native implementation.
 *
 * Threading model
 * ---------------
 * One worker thread owns the UDP socket and the protocol state machine.
 * Every sleep goes through engine_wait(), which blocks on a stop event so
 * "logout" and program exit take effect immediately instead of after a 20 s
 * keepalive wait.
 *
 * Reconnect policy
 * ----------------
 * Errors split into transient (server busy, timeouts, socket hiccups) and
 * fatal (wrong password, MAC mismatch, must-use-DHCP, ...).  Transient errors
 * back off exponentially with jitter; fatal ones stop the loop and wait for
 * the user, because retrying identical credentials forever is the "疯狂重试"
 * behaviour the brief asks us to avoid.
 */
#include "engine.h"
#include "protocol.h"

#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <process.h>

#pragma comment(lib, "ws2_32.lib")

struct engine {
    engine_config cfg;
    engine_event_fn fn;
    void *user;

    HANDLE      thread;
    HANDLE      stop_evt;
    SRWLOCK     lock;
    volatile LONG state;
    volatile LONG reason;

    SOCKET      sock;
    struct sockaddr_in peer;

    int         consecutive_failures;
    double      online_since;      /* 0 when offline */
    double      started_at;
    char        ip[16];
    uint8_t     auth_info[16];
    volatile LONG running;
    int         ws_ready;
};

/* ----------------------------------------------------------------- utils */
static double now_seconds(void)
{
    LARGE_INTEGER freq, counter;
    static LARGE_INTEGER cached_freq;
    if (cached_freq.QuadPart == 0) {
        QueryPerformanceFrequency(&freq);
        cached_freq = freq;
    }
    QueryPerformanceCounter(&counter);
    return (double)counter.QuadPart / (double)cached_freq.QuadPart;
}

/* Sleep that a stop request can cut short.  Returns 0 when stopping. */
static int engine_wait(engine_t *e, double seconds)
{
    DWORD ms = (DWORD)(seconds * 1000.0);
    if (ms == 0) ms = 1;
    DWORD rc = WaitForSingleObject(e->stop_evt, ms);
    return rc == WAIT_TIMEOUT;
}

static void emit(engine_t *e, const char *kind, const char *message,
                 const char *detail, int error_code)
{
    if (e->fn) e->fn(e->user, kind, message, detail, error_code);
}

static void set_state(engine_t *e, engine_state st, const char *message)
{
    InterlockedExchange(&e->state, (LONG)st);
    emit(e, "state", message ? message : "", NULL, -1);
}

/* ------------------------------------------------------------- backoff */
static int in_quiet_hours(int unused)
{
    (void)unused;
    return 0;   /* quiet hours are UI-side policy in the native build */
}

static double next_backoff(engine_t *e)
{
    const engine_config *c = &e->cfg;
    int exponent = e->consecutive_failures - 1;
    if (exponent < 0) exponent = 0;
    double delay = c->min_delay;
    for (int i = 0; i < exponent && delay < c->max_delay; i++) delay *= c->factor;
    if (delay > c->max_delay) delay = c->max_delay;
    if (c->jitter > 0.0) {
        double r = (double)rand() / (double)RAND_MAX;      /* 0..1 */
        delay *= 1.0 + (r * 2.0 - 1.0) * c->jitter;
    }
    if (delay < 1.0) delay = 1.0;
    if (in_quiet_hours(0) && delay < 300.0) delay = 300.0;
    return delay;
}

/* -------------------------------------------------------------- socket IO */
static int send_packet(engine_t *e, const uint8_t *data, size_t len)
{
    int rc = sendto(e->sock, (const char *)data, (int)len, 0,
                    (const struct sockaddr *)&e->peer, sizeof e->peer);
    return rc == (int)len ? 0 : -1;
}

/*
 * Receive one datagram.  Server notices (opcode 0x4D) are skipped when
 * `skip_notifications` is set; datagrams from an unexpected peer are dropped.
 * Returns the length, or -1 on timeout / error / stop request.
 */
static int recv_packet(engine_t *e, uint8_t *buf, size_t cap, int skip_notifications,
                       int max_packets)
{
    for (int i = 0; i < max_packets; i++) {
        if (WaitForSingleObject(e->stop_evt, 0) == WAIT_OBJECT_0) return -1;
        struct sockaddr_in from;
        int fromlen = sizeof from;
        int n = recvfrom(e->sock, (char *)buf, (int)cap, 0,
                         (struct sockaddr *)&from, &fromlen);
        if (n <= 0) return -1;                       /* timeout or error */
        if (from.sin_addr.s_addr != e->peer.sin_addr.s_addr) continue;
        if (skip_notifications && proto_looks_like_notification(buf, (size_t)n)) continue;
        return n;
    }
    return -1;
}

/* ------------------------------------------------------------ challenge */
static int do_challenge(engine_t *e, uint8_t seed[4], char ip_out[16])
{
    uint8_t rnd[2];
    uint8_t packet[PROTO_CHALLENGE_LEN];
    uint8_t reply[256];

    rnd[0] = (uint8_t)(rand() & 0xFF);
    rnd[1] = (uint8_t)(rand() & 0xFF);
    if (proto_build_challenge(rnd, packet) != PROTO_OK) return -1;
    if (send_packet(e, packet, sizeof packet) != 0) return -1;

    int n = recv_packet(e, reply, sizeof reply, 0, 32);
    if (n < 0) return -1;

    proto_challenge_t ch;
    if (proto_parse_challenge(reply, (size_t)n, &ch) != PROTO_OK) return -2;
    memcpy(seed, ch.seed, 4);
    snprintf(ip_out, 16, "%u.%u.%u.%u", ch.ip[0], ch.ip[1], ch.ip[2], ch.ip[3]);
    return 0;
}

static int obtain_challenge(engine_t *e, uint8_t seed[4], char ip_out[16])
{
    int retries = e->cfg.challenge_retries > 0 ? e->cfg.challenge_retries : 1;
    set_state(e, ENGINE_STATE_CHALLENGING, "正在获取挑战码");
    for (int attempt = 1; attempt <= retries; attempt++) {
        int rc = do_challenge(e, seed, ip_out);
        if (rc == 0) return 0;
        if (WaitForSingleObject(e->stop_evt, 0) == WAIT_OBJECT_0) return -1;
        if (attempt < retries && !engine_wait(e, 0.5)) return -1;
    }
    return -1;
}

/* ------------------------------------------------------------- keepalive */
static int keepalive_1(engine_t *e)
{
    uint8_t reply[256];
    uint8_t seed[4];
    int encrypt_type = 0;

    const uint8_t *p1 = proto_keepalive1_pkt1();
    if (send_packet(e, p1, 8) != 0) return -1;

    int n = recv_packet(e, reply, sizeof reply, 1, 32);
    if (n < 0) return -1;
    if (proto_parse_keepalive1(reply, (size_t)n, seed, &encrypt_type) != PROTO_OK) return -2;

    uint8_t rnd[2] = { (uint8_t)(rand() & 0xFF), (uint8_t)(rand() & 0xFF) };
    uint8_t packet2[PROTO_KEEPALIVE1_PKT2_LEN];
    if (proto_build_keepalive1_pkt2(seed, e->auth_info, rnd, packet2) != PROTO_OK) return -1;
    if (send_packet(e, packet2, sizeof packet2) != 0) return -1;

    n = recv_packet(e, reply, sizeof reply, 0, 32);
    if (n <= 0) return -1;
    if (reply[0] != 0x07) return -3;
    return 0;
}

/* One keepalive-2 round: optional file packet, then the A -> C exchange. */
static int keepalive_2(engine_t *e, int *counter, int first)
{
    uint8_t packet[PROTO_KEEPALIVE2_LEN];
    uint8_t reply[256];
    proto_ka_t ka;

    if (first) {
        proto_build_keepalive2(*counter, 1, 1, NULL, packet);
        if (send_packet(e, packet, sizeof packet) != 0) return -1;
        (*counter)++;
        int n = recv_packet(e, reply, sizeof reply, 0, 32);
        if (n < 0) return -1;
        if (proto_parse_keepalive2(reply, (size_t)n, 1, &ka) != PROTO_OK) {
            if (n == 0 || reply[0] != 0x07) return -1;
        }
    }

    proto_build_keepalive2(*counter, 0, 1, NULL, packet);
    if (send_packet(e, packet, sizeof packet) != 0) return -1;
    (*counter)++;
    int n = recv_packet(e, reply, sizeof reply, 0, 32);
    if (n < 0) return -1;
    if (proto_parse_keepalive2(reply, (size_t)n, 0, &ka) != PROTO_OK || !ka.ok) return -1;
    uint8_t tail[4];
    memcpy(tail, ka.tail, 4);

    proto_build_keepalive2(*counter, 0, 3, tail, packet);
    if (send_packet(e, packet, sizeof packet) != 0) return -1;
    (*counter)++;
    n = recv_packet(e, reply, sizeof reply, 0, 32);
    if (n < 0) return -1;
    if (proto_parse_keepalive2(reply, (size_t)n, 0, &ka) != PROTO_OK || !ka.ok) return -1;
    return 0;
}

static offline_reason keepalive_loop(engine_t *e)
{
    int counter = 0;
    int first = 1;
    int consecutive_ka2_failures = 0;
    double interval = e->cfg.keepalive_interval;

    while (WaitForSingleObject(e->stop_evt, 0) == WAIT_TIMEOUT) {
        if (keepalive_1(e) != 0) {
            emit(e, "keepalive_failed", "保活失败（keepalive1）", "keepalive1 未通过", -1);
            return OFFLINE_TIMEOUT;
        }
        if (!engine_wait(e, e->cfg.post_login_delay)) return OFFLINE_USER_LOGOUT;

        int ok = keepalive_2(e, &counter, first) == 0;
        first = 0;
        if (!ok) {
            consecutive_ka2_failures++;
            if (consecutive_ka2_failures >= 5) {
                emit(e, "keepalive_failed", "保活失败（keepalive2 连续失败）",
                     "连续 5 次 keepalive2 未通过", -1);
                return OFFLINE_KEEPALIVE_FAILED;
            }
            if (!engine_wait(e, 0.5)) return OFFLINE_USER_LOGOUT;
            continue;
        }
        consecutive_ka2_failures = 0;
        /* A full round went through: the session really is healthy, so the
         * backoff counter can safely start over. */
        e->consecutive_failures = 0;
        emit(e, "keepalive_ok", "保活正常", NULL, -1);
        if (!engine_wait(e, interval)) return OFFLINE_USER_LOGOUT;
    }
    return OFFLINE_USER_LOGOUT;
}

/* ----------------------------------------------------------------- login */
/* Returns "ok", "transient" or "fatal" (same contract as the Python build). */
static const char *authenticate(engine_t *e)
{
    uint8_t packet[PROTO_MAX_PACKET];
    size_t len = 0;
    uint8_t seed[4];
    char ip[16];

    if (obtain_challenge(e, seed, ip) != 0) {
        const char *detail = "服务器没有在超时时间内回应，或响应格式不对。\n"
                             "常见原因：网线未连接 / 未接入校园网 / 服务器暂时不可用。";
        emit(e, "challenge_failed", "获取挑战码失败", detail, -1);
        InterlockedExchange(&e->reason, OFFLINE_CHALLENGE_FAILED);
        set_state(e, ENGINE_STATE_RETRY_WAIT, "挑战失败");
        e->consecutive_failures++;
        return "transient";
    }

    /* Guard the IP string: /status may otherwise read it mid-write, and a
     * half-updated address is exactly the kind of bug that shows up once a month
     * and never reproduces. */
    AcquireSRWLockExclusive(&e->lock);
    snprintf(e->ip, sizeof e->ip, "%s", ip);
    ReleaseSRWLockExclusive(&e->lock);
    emit(e, "ip", ip, NULL, -1);

    if (!engine_wait(e, e->cfg.post_login_delay)) return "transient";
    set_state(e, ENGINE_STATE_AUTHENTICATING, "正在提交认证");

    int rc = proto_build_login(e->cfg.account, e->cfg.password, e->cfg.mac,
                               seed, packet, sizeof packet, &len);
    if (rc != PROTO_OK) {
        InterlockedExchange(&e->reason, OFFLINE_LOGIN_FAILED);
        set_state(e, ENGINE_STATE_FATAL, "配置不完整");
        emit(e, "error", "配置错误：账号或 MAC 格式不正确", NULL, -1);
        return "fatal";
    }
    if (send_packet(e, packet, len) != 0) { e->consecutive_failures++; return "transient"; }

    uint8_t reply[256];
    int n = recv_packet(e, reply, sizeof reply, 0, 32);
    if (n < 0) {
        InterlockedExchange(&e->reason, OFFLINE_TIMEOUT);
        set_state(e, ENGINE_STATE_RETRY_WAIT, "登录响应超时");
        emit(e, "login_failed", "登录响应超时", "服务器未在超时时间内回复登录请求。", -1);
        e->consecutive_failures++;
        return "transient";
    }

    proto_login_t lo;
    if (proto_parse_login(reply, (size_t)n, &lo) != PROTO_OK) {
        e->consecutive_failures++;
        set_state(e, ENGINE_STATE_RETRY_WAIT, "登录响应格式异常");
        return "transient";
    }

    if (!lo.success) {
        InterlockedExchange(&e->reason, OFFLINE_LOGIN_FAILED);
        const char *name = proto_login_error_text(lo.error_code);
        char detail[96];
        snprintf(detail, sizeof detail, "服务端返回错误码 0x%02X (%s)",
                 lo.error_code, name);
        emit(e, "login_failed", name, detail, lo.error_code);
        if (proto_login_error_is_fatal(lo.error_code)) {
            set_state(e, ENGINE_STATE_FATAL, name);
            emit(e, "fatal", name, "这类错误重试无用，请按提示修正配置后手动重试。",
                 lo.error_code);
            return "fatal";
        }
        e->consecutive_failures++;
        set_state(e, ENGINE_STATE_RETRY_WAIT, name);
        return "transient";
    }

    /* online */
    memcpy(e->auth_info, lo.auth, 16);
    e->online_since = now_seconds();
    /* NB: no counter reset here.  "The server accepted our login" is not proof
     * that the session survives - a network that eats keepalive packets will
     * accept every login and drop every keepalive.  If we cleared the counter
     * here, that case would retry at min_delay forever and never reach
     * give_up_after; keepalive_loop clears it once a round actually succeeds. */
    set_state(e, ENGINE_STATE_ONLINE, "已上线");
    emit(e, "online", ip, NULL, -1);

    offline_reason why = keepalive_loop(e);
    e->online_since = 0.0;
    InterlockedExchange(&e->reason, why);
    if (why != OFFLINE_USER_LOGOUT) {
        /* Back off on keepalive failures too, otherwise a session that keeps
         * dying re-authenticates every few seconds, for days. */
        e->consecutive_failures++;
    }
    return "transient";
}

/* ---------------------------------------------------------------- socket */
static int bind_socket(engine_t *e)
{
    e->sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (e->sock == INVALID_SOCKET) return -1;

    DWORD timeout = (DWORD)e->cfg.timeout_ms;
    setsockopt(e->sock, SOL_SOCKET, SO_RCVTIMEO, (const char *)&timeout, sizeof timeout);

    struct sockaddr_in local;
    memset(&local, 0, sizeof local);
    local.sin_family = AF_INET;
    local.sin_port = htons((u_short)e->cfg.bind_port);
    if (e->cfg.bind_address[0] && strcmp(e->cfg.bind_address, "0.0.0.0") != 0) {
        if (InetPtonA(AF_INET, e->cfg.bind_address, &local.sin_addr) != 1) {
            closesocket(e->sock); e->sock = INVALID_SOCKET;
            return -2;
        }
    } else {
        local.sin_addr.s_addr = INADDR_ANY;
    }

    if (bind(e->sock, (struct sockaddr *)&local, sizeof local) != 0) {
        int err = WSAGetLastError();
        closesocket(e->sock);
        e->sock = INVALID_SOCKET;
        return err ? -err : -3;
    }

    memset(&e->peer, 0, sizeof e->peer);
    e->peer.sin_family = AF_INET;
    e->peer.sin_port = htons((u_short)e->cfg.port);
    if (InetPtonA(AF_INET, e->cfg.server, &e->peer.sin_addr) != 1) {
        closesocket(e->sock); e->sock = INVALID_SOCKET;
        return -4;
    }
    return 0;
}

/* ------------------------------------------------------------------ loop */
static unsigned __stdcall engine_thread(void *arg)
{
    engine_t *e = (engine_t *)arg;

    emit(e, "started", "认证线程已启动", NULL, -1);

    while (WaitForSingleObject(e->stop_evt, 0) == WAIT_TIMEOUT) {
        set_state(e, ENGINE_STATE_BINDING, "正在绑定端口");
        int rc = bind_socket(e);
        if (rc != 0) {
            InterlockedExchange(&e->reason, OFFLINE_BIND_FAILED);
            char detail[128];
            snprintf(detail, sizeof detail,
                     "无法绑定 %s:%d（错误 %d）。端口可能被其它程序占用，"
                     "或需要管理员权限。", e->cfg.bind_address[0] ? e->cfg.bind_address : "0.0.0.0",
                     e->cfg.bind_port, rc);
            set_state(e, ENGINE_STATE_RETRY_WAIT, "绑定失败");
            emit(e, "bind_failed", "绑定端口失败", detail, rc);
            e->consecutive_failures++;
        } else {
            const char *result = authenticate(e);
            if (e->sock != INVALID_SOCKET) { closesocket(e->sock); e->sock = INVALID_SOCKET; }
            if (WaitForSingleObject(e->stop_evt, 0) == WAIT_OBJECT_0) break;
            if (strcmp(result, "fatal") == 0) {
                set_state(e, ENGINE_STATE_FATAL, "需要人工处理");
                goto done;
            }
        }

        if (!e->cfg.reconnect_enabled) {
            emit(e, "reconnect_disabled", "自动重连已关闭，请手动登录", NULL, -1);
            set_state(e, ENGINE_STATE_FATAL, "自动重连已关闭");
            goto done;
        }
        if (e->cfg.give_up_after > 0 && e->consecutive_failures >= e->cfg.give_up_after) {
            emit(e, "gave_up", "连续失败次数过多，已暂停自动重连",
                 "问题多半不在网络抖动，而在配置或环境。", -1);
            set_state(e, ENGINE_STATE_FATAL, "已暂停自动重连");
            goto done;
        }

        double delay = next_backoff(e);
        char msg[96];
        snprintf(msg, sizeof msg, "%.0f 秒后重试（第 %d 次失败）", delay, e->consecutive_failures);
        set_state(e, ENGINE_STATE_RETRY_WAIT, msg);
        emit(e, "retry_scheduled", msg, NULL, -1);
        if (!engine_wait(e, delay)) break;
    }

done:
    if (e->sock != INVALID_SOCKET) { closesocket(e->sock); e->sock = INVALID_SOCKET; }
    if (InterlockedCompareExchange(&e->state, ENGINE_STATE_STOPPED, ENGINE_STATE_STOPPED)
        != ENGINE_STATE_FATAL) {
        set_state(e, ENGINE_STATE_STOPPED, "已停止");
    }
    InterlockedExchange(&e->running, 0);
    emit(e, "stopped", "认证已停止", NULL, -1);
    return 0;
}

/* ------------------------------------------------------------------- API */
void engine_config_defaults(engine_config *cfg)
{
    memset(cfg, 0, sizeof *cfg);
    snprintf(cfg->server, sizeof cfg->server, "%s", PROTO_AUTH_SERVER);
    cfg->port = PROTO_AUTH_PORT;
    cfg->bind_port = PROTO_BIND_PORT;
    cfg->timeout_ms = PROTO_RECV_TIMEOUT_MS;
    cfg->keepalive_interval = (double)PROTO_KEEPALIVE_INTERVAL_S;
    cfg->challenge_retries = 3;
    cfg->post_login_delay = 0.2;
    cfg->reconnect_enabled = 1;
    cfg->min_delay = 5.0;
    cfg->max_delay = 300.0;
    cfg->factor = 2.0;
    cfg->jitter = 0.25;
    cfg->give_up_after = 8;
}

engine_t *engine_create(const engine_config *cfg, engine_event_fn fn, void *user)
{
    engine_t *e = (engine_t *)calloc(1, sizeof *e);
    if (!e) return NULL;
    e->cfg = *cfg;
    e->fn = fn;
    e->user = user;
    e->sock = INVALID_SOCKET;
    e->state = ENGINE_STATE_IDLE;
    e->reason = OFFLINE_NONE;
    InitializeSRWLock(&e->lock);
    e->stop_evt = CreateEventW(NULL, TRUE, FALSE, NULL);
    if (!e->stop_evt) { free(e); return NULL; }

    WSADATA wsa;
    if (WSAStartup(MAKEWORD(2, 2), &wsa) == 0) e->ws_ready = 1;
    /* GetTickCount wraps every 49 days; this process is meant to stay up for
     * months, so seed from the 64-bit counter instead. */
    srand((unsigned)GetTickCount64());
    return e;
}

int engine_start(engine_t *e)
{
    if (!e) return -1;
    /* A finished worker leaves its handle behind; reclaim it so the engine can
     * be started again after FATAL or after /logout + /login. */
    if (e->thread) {
        if (WaitForSingleObject(e->thread, 0) != WAIT_OBJECT_0) return -1;
        CloseHandle(e->thread);
        e->thread = NULL;
    }
    InterlockedExchange(&e->running, 0);
    ResetEvent(e->stop_evt);
    e->consecutive_failures = 0;
    e->online_since = 0.0;
    e->ip[0] = '\0';
    e->started_at = now_seconds();
    InterlockedExchange(&e->state, ENGINE_STATE_IDLE);
    InterlockedExchange(&e->reason, OFFLINE_NONE);
    e->thread = (HANDLE)_beginthreadex(NULL, 0, engine_thread, e, 0, NULL);
    if (!e->thread) return -1;
    InterlockedExchange(&e->running, 1);
    return 0;
}

void engine_stop(engine_t *e)
{
    if (!e) return;
    InterlockedExchange(&e->reason, OFFLINE_USER_LOGOUT);
    SetEvent(e->stop_evt);
}

int engine_join(engine_t *e, int timeout_ms)
{
    if (!e || !e->thread) return 1;
    DWORD rc = WaitForSingleObject(e->thread, (DWORD)timeout_ms);
    if (rc == WAIT_OBJECT_0) return 1;
    return 0;
}

void engine_destroy(engine_t *e)
{
    if (!e) return;
    engine_stop(e);
    if (e->thread) {
        WaitForSingleObject(e->thread, 5000);
        CloseHandle(e->thread);
        e->thread = NULL;
    }
    if (e->sock != INVALID_SOCKET) { closesocket(e->sock); e->sock = INVALID_SOCKET; }
    if (e->stop_evt) { CloseHandle(e->stop_evt); e->stop_evt = NULL; }
    if (e->ws_ready) { WSACleanup(); e->ws_ready = 0; }
    free(e);
}

engine_state engine_get_state(engine_t *e)
{
    return e ? (engine_state)InterlockedCompareExchange(&e->state, 0, 0) : ENGINE_STATE_IDLE;
}

offline_reason engine_get_reason(engine_t *e)
{
    return e ? (offline_reason)InterlockedCompareExchange(&e->reason, 0, 0) : OFFLINE_NONE;
}

int engine_is_online(engine_t *e)
{
    return engine_get_state(e) == ENGINE_STATE_ONLINE;
}

int engine_is_running(engine_t *e)
{
    if (!e || !e->thread) return 0;
    return WaitForSingleObject(e->thread, 0) == WAIT_TIMEOUT;
}

double engine_uptime(engine_t *e)
{
    if (!e || e->online_since <= 0.0) return 0.0;
    return now_seconds() - e->online_since;
}

void engine_get_ip(engine_t *e, char out[16])
{
    if (!e || !out) return;
    AcquireSRWLockShared(&e->lock);
    snprintf(out, 16, "%s", e->ip);
    ReleaseSRWLockShared(&e->lock);
}

int engine_get_consecutive_failures(engine_t *e)
{
    return e ? e->consecutive_failures : 0;
}
