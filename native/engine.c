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
#include "platform.h"
#include "engine.h"
#include "protocol.h"

#ifdef _WIN32
#include <iphlpapi.h>
#pragma comment(lib, "iphlpapi.lib")
#else
#include <ifaddrs.h>
#include <net/if.h>
#endif

#include <stdio.h>
#include <string.h>
#include <stdlib.h>

struct engine {
    engine_config cfg;
    engine_event_fn fn;
    void *user;

    plat_thread_t thread;
    plat_event_t  stop_evt;
    plat_rwlock_t lock;
    plat_atomic_t state;
    plat_atomic_t reason;

    plat_socket_t sock;
    struct sockaddr_in peer;

    plat_atomic_t consecutive_failures;
    double      online_since;      /* 0 when offline */
    double      started_at;
    char        ip[16];
    uint8_t     auth_info[16];
    plat_atomic_t running;
    int         ws_ready;
    /* Live copy of cfg.protocol_hex: the UI can flip the switch while a
     * session is up, and the setting must not require a restart to apply. */
    plat_atomic_t protocol_hex;
};

/* ----------------------------------------------------------------- utils */
static double now_seconds(void)
{
    return plat_now_seconds();
}

/* Sleep that a stop request can cut short.  Returns 0 when stopping. */
static int engine_wait(engine_t *e, double seconds)
{
    int ms = (int)(seconds * 1000.0);
    if (ms <= 0) ms = 1;
    return plat_event_wait_ms(&e->stop_evt, ms) == 0 ? 1 : 0;
}

static void emit(engine_t *e, const char *kind, const char *message,
                 const char *detail, int error_code)
{
    if (e->fn) e->fn(e->user, kind, message, detail, error_code);
}

/* --------------------------------------------------------- packet dumping */
/* How many bytes of a datagram go into the log line.  Sixty-four covers
 * everything we have ever had to look at: the opcode/length header, the byte
 * that carries the IP, the account run at [20:..] and the MAC at [58:64].
 * The body hashes past 64 are checked against the reference capture offline,
 * not by eye in a log. */
#define DUMP_BYTES 64

/*
 * Space-separated lowercase hex, matching the format the reference Qt client
 * writes to its .lgt captures (tools/verify_against_log.py parses both).
 *
 * A redacted byte becomes "--" instead of being dropped, so offsets in the log
 * line still line up with the real datagram -- which is the entire point of
 * having the dump.  The login packet carries the account string at [20:...]
 * and the password-derived hashes after it, and writing those to a log file
 * would defeat the account masking the rest of the program does.
 */
static void hex_encode(const uint8_t *data, size_t len, size_t max_bytes,
                       size_t redact_from, size_t redact_to,
                       char *out, size_t cap)
{
    size_t shown = len < max_bytes ? len : max_bytes;
    size_t used = 0;
    for (size_t i = 0; i < shown; i++) {
        if (used + 4 >= cap) break;
        int hidden = (redact_to > redact_from) && (i >= redact_from) && (i < redact_to);
        used += (size_t)snprintf(out + used, cap - used, hidden ? "-- " : "%02x ", data[i]);
    }
    if (redact_to > redact_from && redact_from < shown) {
        used += (size_t)snprintf(out + used, cap - used, " (敏感字段已用 -- 屏蔽)");
    }
    if (len > shown) {
        snprintf(out + used, cap - used, " ... (+%lu bytes)", (unsigned long)(len - shown));
    }
}

/* One dump line per datagram, at most.  Off unless the user turned it on. */
static void dump_packet(engine_t *e, const char *dir, const char *label,
                        const uint8_t *data, size_t len,
                        size_t redact_from, size_t redact_to)
{
    if (!plat_atomic_load(&e->protocol_hex)) return;
    char hex[DUMP_BYTES * 4 + 96];
    hex_encode(data, len, DUMP_BYTES, redact_from, redact_to, hex, sizeof hex);
    char head[96];
    snprintf(head, sizeof head, "%s %-22s len=%lu", dir, label, (unsigned long)len);
    emit(e, "packet", head, hex, -1);
}

static void set_state(engine_t *e, engine_state st, const char *message)
{
    plat_atomic_store(&e->state, (long)st);
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
    plat_rwlock_rdlock(&e->lock);
    engine_config c = e->cfg;
    plat_rwlock_rdunlock(&e->lock);
    int exponent = (int)plat_atomic_load(&e->consecutive_failures) - 1;
    if (exponent < 0) exponent = 0;
    double delay = c.min_delay;
    for (int i = 0; i < exponent && delay < c.max_delay; i++) delay *= c.factor;
    if (delay > c.max_delay) delay = c.max_delay;
    if (c.jitter > 0.0) {
        double r = (double)rand() / (double)RAND_MAX;      /* 0..1 */
        delay *= 1.0 + (r * 2.0 - 1.0) * c.jitter;
    }
    if (delay < 1.0) delay = 1.0;
    if (in_quiet_hours(0) && delay < 300.0) delay = 300.0;
    return delay;
}

/* ------------------------------------------------------- bind candidates */
#define MAX_BIND_CANDIDATES 8

static int collect_local_ipv4(char out[][16], int cap)
{
#ifdef _WIN32
    ULONG size = 16 * 1024;
    IP_ADAPTER_ADDRESSES *addrs = (IP_ADAPTER_ADDRESSES *)malloc(size);
    if (!addrs) return 0;
    ULONG flags = GAA_FLAG_SKIP_ANYCAST | GAA_FLAG_SKIP_MULTICAST | GAA_FLAG_SKIP_DNS_SERVER;
    ULONG rc = GetAdaptersAddresses(AF_INET, flags, NULL, addrs, &size);
    if (rc == ERROR_BUFFER_OVERFLOW) {
        free(addrs);
        addrs = (IP_ADAPTER_ADDRESSES *)malloc(size);
        if (!addrs) return 0;
        rc = GetAdaptersAddresses(AF_INET, flags, NULL, addrs, &size);
    }
    if (rc != NO_ERROR) { free(addrs); return 0; }

    int n = 0;
    for (IP_ADAPTER_ADDRESSES *a = addrs; a && n < cap; a = a->Next) {
        if (a->OperStatus != IfOperStatusUp) continue;
        if (a->IfType == IF_TYPE_SOFTWARE_LOOPBACK) continue;
        for (IP_ADAPTER_UNICAST_ADDRESS *u = a->FirstUnicastAddress; u && n < cap; u = u->Next) {
            if (!u->Address.lpSockaddr) continue;
            if (u->Address.lpSockaddr->sa_family != AF_INET) continue;
            struct sockaddr_in *sa = (struct sockaddr_in *)u->Address.lpSockaddr;
            char buf[16];
            if (!inet_ntop(AF_INET, &sa->sin_addr, buf, (socklen_t)sizeof buf)) continue;
            /* 169.254/16 is what Windows invents when DHCP failed: binding to
             * it can never reach the campus server. */
            if (strncmp(buf, "169.254.", 8) == 0) continue;
            if (strcmp(buf, "0.0.0.0") == 0) continue;
            int dup = 0;
            for (int i = 0; i < n; i++) if (strcmp(out[i], buf) == 0) { dup = 1; break; }
            if (!dup) snprintf(out[n++], 16, "%s", buf);
        }
    }
    free(addrs);
    return n;
#else
    struct ifaddrs *ifaddr = NULL;
    if (getifaddrs(&ifaddr) != 0) return 0;
    int n = 0;
    for (struct ifaddrs *ifa = ifaddr; ifa && n < cap; ifa = ifa->ifa_next) {
        if (!ifa->ifa_addr || ifa->ifa_addr->sa_family != AF_INET) continue;
        if (ifa->ifa_flags & IFF_LOOPBACK) continue;
        if (!(ifa->ifa_flags & IFF_UP)) continue;
        struct sockaddr_in *sa = (struct sockaddr_in *)ifa->ifa_addr;
        char buf[16];
        if (!inet_ntop(AF_INET, &sa->sin_addr, buf, (socklen_t)sizeof buf)) continue;
        if (strncmp(buf, "169.254.", 8) == 0 || strcmp(buf, "127.0.0.1") == 0 ||
            strcmp(buf, "0.0.0.0") == 0) continue;
        int dup = 0;
        for (int i = 0; i < n; i++) if (strcmp(out[i], buf) == 0) { dup = 1; break; }
        if (!dup) snprintf(out[n++], 16, "%s", buf);
    }
    freeifaddrs(ifaddr);
    return n;
#endif
}

/* The address the OS would use to reach the server.  Asked the only way that
 * needs no packets: connect() on a UDP socket only fills in the route. */
static void default_route_ip(const char *server, int port, char out[16])
{
    out[0] = '\0';
    plat_socket_t s = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (s == PLAT_INVALID_SOCKET) return;
    struct sockaddr_in to;
    memset(&to, 0, sizeof to);
    to.sin_family = AF_INET;
    to.sin_port = htons((uint16_t)port);
    if (inet_pton(AF_INET, server, &to.sin_addr) == 1 && connect(s, (struct sockaddr *)&to, sizeof to) == 0) {
        struct sockaddr_in me;
        socklen_t me_len = (socklen_t)sizeof me;
        if (getsockname(s, (struct sockaddr *)&me, &me_len) == 0) {
            inet_ntop(AF_INET, &me.sin_addr, out, 16);
        }
    }
    plat_closesocket(s);
}

static int build_bind_candidates(engine_t *e, char out[][16], int cap)
{
    plat_rwlock_rdlock(&e->lock);
    char pinned[64], server[64];
    snprintf(pinned, sizeof pinned, "%s", e->cfg.bind_address);
    snprintf(server, sizeof server, "%s", e->cfg.server);
    int port = e->cfg.port;
    int fallback = e->cfg.auto_interface_fallback;
    plat_rwlock_rdunlock(&e->lock);

    if (pinned[0] && strcmp(pinned, "0.0.0.0") != 0) {
        snprintf(out[0], 16, "%s", pinned);
        return 1;
    }

    int n = 0;
    snprintf(out[n++], 16, "0.0.0.0");
    if (!fallback || n >= cap) return n;

    char routed[16];
    default_route_ip(server, port, routed);
    if (routed[0] && strcmp(routed, "0.0.0.0") != 0) {
        snprintf(out[n++], 16, "%s", routed);
    }
    n += collect_local_ipv4(&out[n], cap - n);
    return n;
}

/* -------------------------------------------------------------- socket IO */
static int send_packet(engine_t *e, const uint8_t *data, size_t len)
{
    int rc = (int)sendto(e->sock, (const char *)data, (int)len, 0,
                         (const struct sockaddr *)&e->peer, sizeof e->peer);
    if (rc == (int)len) return 0;

    int err = plat_last_sock_err();
    char detail[160];
    snprintf(detail, sizeof detail, "sendto 失败：%s:%u，errno=%d",
             e->cfg.server, (unsigned)e->cfg.port, err);
    emit(e, "send_failed", "发包失败", detail, err);
    return -1;
}

/*
 * Send with a dump line.  The label names the step ("[Login sent]"), so a
 * capture reads like the reference client's log and the two can be diffed.
 *
 * `redact_from`/`redact_to` blank a byte range; pass from >= to for none.
 * Only the login packet needs it -- the account string sits at [20:...] and
 * the salted-password section near the end.  The MAC at [58:80] is
 * deliberately *not* blanked: "MAC 不符" (0x0B) is the single most common
 * login failure and the dump is the only place that shows which MAC was
 * actually sent.  A MAC is not a credential -- it is visible to every peer on
 * the same segment -- so this leaks nothing a LAN neighbour could not read.
 */
static int send_labeled(engine_t *e, const uint8_t *data, size_t len,
                        const char *label, size_t redact_from, size_t redact_to)
{
    dump_packet(e, "tx", label, data, len, redact_from, redact_to);
    return send_packet(e, data, len);
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
        if (plat_event_is_set(&e->stop_evt)) return -1;
        struct sockaddr_in from;
        socklen_t fromlen = (socklen_t)sizeof from;
        int n = (int)recvfrom(e->sock, (char *)buf, (int)cap, 0,
                              (struct sockaddr *)&from, &fromlen);
        if (n <= 0) return -1;                       /* timeout or error */
        if (from.sin_addr.s_addr != e->peer.sin_addr.s_addr) continue;
        if (skip_notifications && proto_looks_like_notification(buf, (size_t)n)) continue;
        return n;
    }
    return -1;
}

/* Receive with a dump line on whatever eventually arrived.  The reply is not
 * redacted: the server echoes no account string back. */
static int recv_labeled(engine_t *e, uint8_t *buf, size_t cap, int skip_notifications,
                        int max_packets, const char *label)
{
    int n = recv_packet(e, buf, cap, skip_notifications, max_packets);
    if (n > 0) dump_packet(e, "rx", label, buf, (size_t)n, 0, 0);
    return n;
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
    if (send_labeled(e, packet, sizeof packet, "[Challenge sent]", 0, 0) != 0) return -1;

    int n = recv_labeled(e, reply, sizeof reply, 0, 32, "[Challenge recv]");
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
        if (plat_event_is_set(&e->stop_evt)) return -1;
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
    if (send_labeled(e, p1, 8, "[Keepalive1 sent]", 0, 0) != 0) return -1;

    int n = recv_labeled(e, reply, sizeof reply, 1, 32, "[Keepalive1 recv]");
    if (n < 0) return -1;
    if (proto_parse_keepalive1(reply, (size_t)n, seed, &encrypt_type) != PROTO_OK) return -2;

    uint8_t rnd[2] = { (uint8_t)(rand() & 0xFF), (uint8_t)(rand() & 0xFF) };
    uint8_t packet2[PROTO_KEEPALIVE1_PKT2_LEN];
    if (proto_build_keepalive1_pkt2(seed, e->auth_info, rnd, packet2) != PROTO_OK) return -1;
    if (send_labeled(e, packet2, sizeof packet2, "[Keepalive1 answer sent]", 0, 0) != 0) return -1;

    n = recv_labeled(e, reply, sizeof reply, 0, 32, "[Keepalive1 answer recv]");
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
        if (send_labeled(e, packet, sizeof packet, "[Keepalive2_file sent]", 0, 0) != 0) return -1;
        (*counter)++;
        int n = recv_labeled(e, reply, sizeof reply, 0, 32, "[Keepalive2_file recv]");
        if (n < 0) return -1;
        if (proto_parse_keepalive2(reply, (size_t)n, 1, &ka) != PROTO_OK) {
            if (n == 0 || reply[0] != 0x07) return -1;
        }
    }

    proto_build_keepalive2(*counter, 0, 1, NULL, packet);
    if (send_labeled(e, packet, sizeof packet, "[Keepalive2_A sent]", 0, 0) != 0) return -1;
    (*counter)++;
    int n = recv_labeled(e, reply, sizeof reply, 0, 32, "[Keepalive2_A recv]");
    if (n < 0) return -1;
    if (proto_parse_keepalive2(reply, (size_t)n, 0, &ka) != PROTO_OK || !ka.ok) return -1;
    uint8_t tail[4];
    memcpy(tail, ka.tail, 4);

    proto_build_keepalive2(*counter, 0, 3, tail, packet);
    if (send_labeled(e, packet, sizeof packet, "[Keepalive2_C sent]", 0, 0) != 0) return -1;
    (*counter)++;
    n = recv_labeled(e, reply, sizeof reply, 0, 32, "[Keepalive2_C recv]");
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

    while (!plat_event_is_set(&e->stop_evt)) {
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
        plat_atomic_store(&e->consecutive_failures, 0);
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
        plat_atomic_store(&e->reason, OFFLINE_CHALLENGE_FAILED);
        set_state(e, ENGINE_STATE_RETRY_WAIT, "挑战失败");
        plat_atomic_store(&e->consecutive_failures, plat_atomic_load(&e->consecutive_failures) + 1);
        return "transient";
    }

    plat_rwlock_wrlock(&e->lock);
    snprintf(e->ip, sizeof e->ip, "%s", ip);
    plat_rwlock_wrunlock(&e->lock);
    emit(e, "ip", ip, NULL, -1);

    if (!engine_wait(e, e->cfg.post_login_delay)) return "transient";
    set_state(e, ENGINE_STATE_AUTHENTICATING, "正在提交认证");

    int rc = proto_build_login(e->cfg.account, e->cfg.password, e->cfg.mac,
                               seed, packet, sizeof packet, &len);
    if (rc != PROTO_OK) {
        plat_atomic_store(&e->reason, OFFLINE_LOGIN_FAILED);
        set_state(e, ENGINE_STATE_FATAL, "配置不完整");
        emit(e, "error", "配置错误：账号或 MAC 格式不正确", NULL, -1);
        return "fatal";
    }
    size_t account_end = 20 + strlen(e->cfg.account);
    if (account_end > len) account_end = len;
    if (send_labeled(e, packet, len, "[Login sent]", 4, account_end) != 0) {
        plat_secure_zero(packet, len);
        plat_atomic_store(&e->consecutive_failures, plat_atomic_load(&e->consecutive_failures) + 1);
        return "transient";
    }
    plat_secure_zero(packet, len);

    uint8_t reply[256];
    int n = recv_labeled(e, reply, sizeof reply, 0, 32, "[Login recv]");
    if (n < 0) {
        plat_atomic_store(&e->reason, OFFLINE_TIMEOUT);
        set_state(e, ENGINE_STATE_RETRY_WAIT, "登录响应超时");
        emit(e, "login_failed", "登录响应超时", "服务器未在超时时间内回复登录请求。", -1);
        plat_atomic_store(&e->consecutive_failures, plat_atomic_load(&e->consecutive_failures) + 1);
        return "transient";
    }

    proto_login_t lo;
    if (proto_parse_login(reply, (size_t)n, &lo) != PROTO_OK) {
        plat_atomic_store(&e->consecutive_failures, plat_atomic_load(&e->consecutive_failures) + 1);
        set_state(e, ENGINE_STATE_RETRY_WAIT, "登录响应格式异常");
        return "transient";
    }

    if (!lo.success) {
        plat_atomic_store(&e->reason, OFFLINE_LOGIN_FAILED);
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
        plat_atomic_store(&e->consecutive_failures, plat_atomic_load(&e->consecutive_failures) + 1);
        set_state(e, ENGINE_STATE_RETRY_WAIT, name);
        return "transient";
    }

    /* online */
    memcpy(e->auth_info, lo.auth, 16);
    plat_rwlock_wrlock(&e->lock);
    e->online_since = now_seconds();
    plat_rwlock_wrunlock(&e->lock);
    set_state(e, ENGINE_STATE_ONLINE, "已上线");
    emit(e, "online", ip, NULL, -1);

    offline_reason why = keepalive_loop(e);
    plat_rwlock_wrlock(&e->lock);
    e->online_since = 0.0;
    plat_rwlock_wrunlock(&e->lock);
    plat_atomic_store(&e->reason, why);
    if (why != OFFLINE_USER_LOGOUT) {
        plat_atomic_store(&e->consecutive_failures, plat_atomic_load(&e->consecutive_failures) + 1);
    }
    return "transient";
}

/* ---------------------------------------------------------------- socket */
/* Interface index that owns *addr*, or 0 when no local adapter has it.
 *
 * Needed because binding a UDP socket to a source address does not decide the
 * egress route: the system still picks one from its routing table.  On a laptop
 * with a phone hotspot up as well, the route to the campus server can keep
 * going out of the hotspot even though the socket is bound to the wired
 * address, and sendto then fails with WSAENETUNREACH -- which reads like "the
 * server is silent".  IP_UNICAST_IF pins the outgoing interface and makes the
 * address the user picked actually mean something. */
#ifdef _WIN32
static DWORD iface_index_for_address(const char *addr)
{
    struct in_addr want;
    if (!addr || inet_pton(AF_INET, addr, &want) != 1) return 0;

    ULONG size = 0;
    if (GetIpAddrTable(NULL, &size, 0) != ERROR_INSUFFICIENT_BUFFER) return 0;
    PMIB_IPADDRTABLE table = (PMIB_IPADDRTABLE)malloc(size);
    if (!table) return 0;

    DWORD index = 0;
    if (GetIpAddrTable(table, &size, 0) == NO_ERROR) {
        for (DWORD i = 0; i < table->dwNumEntries; i++) {
            if (table->table[i].dwAddr == want.s_addr) {
                index = table->table[i].dwIndex;
                break;
            }
        }
    }
    free(table);
    return index;
}
#else
static int iface_name_for_address(const char *addr, char *out, size_t cap)
{
    struct in_addr want;
    if (!addr || inet_pton(AF_INET, addr, &want) != 1) return 0;
    struct ifaddrs *ifaddr = NULL;
    if (getifaddrs(&ifaddr) != 0) return 0;
    int found = 0;
    for (struct ifaddrs *ifa = ifaddr; ifa; ifa = ifa->ifa_next) {
        if (!ifa->ifa_addr || ifa->ifa_addr->sa_family != AF_INET || !ifa->ifa_name) continue;
        struct sockaddr_in *sa = (struct sockaddr_in *)ifa->ifa_addr;
        if (sa->sin_addr.s_addr == want.s_addr) {
            snprintf(out, cap, "%s", ifa->ifa_name);
            found = 1;
            break;
        }
    }
    freeifaddrs(ifaddr);
    return found;
}
#endif

static const char *bind_error_hint(int rc)
{
    int code = -rc;
#ifdef _WIN32
    switch (code) {
    case 10048:  /* WSAEADDRINUSE */
        return "端口被其它程序占用。";
    case 10049:  /* WSAEADDRNOTAVAIL */
        return "这个地址不属于本机任何在用网卡：检查设置里的绑定地址。";
    case 10013:  /* WSAEACCES */
        return "端口落在系统保留区间或被安全软件拦住：换一个端口，或以管理员身份运行。";
    case 10047:  /* WSAEAFNOSUPPORT */
        return "该地址不是 IPv4 地址。";
    default:
        return "绑定失败。";
    }
#else
    switch (code) {
    case EADDRINUSE:
        return "端口被其它程序占用（可用 ss -ulnp | grep 61440 查看）。";
    case EADDRNOTAVAIL:
        return "这个地址不属于本机任何在用网卡：检查设置里的绑定地址。";
    case EACCES:
    case EPERM:
        return "没有权限绑定该端口或被防火墙/策略拦截。";
    case EAFNOSUPPORT:
        return "该地址不是 IPv4 地址。";
    default:
        return "绑定失败。";
    }
#endif
}

static int bind_socket(engine_t *e, const char *addr)
{
    e->sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (e->sock == PLAT_INVALID_SOCKET) return -1;

    plat_set_sock_timeout(e->sock, e->cfg.timeout_ms);

    struct sockaddr_in local;
    memset(&local, 0, sizeof local);
    local.sin_family = AF_INET;
    local.sin_port = htons((uint16_t)e->cfg.bind_port);
    if (addr && addr[0] && strcmp(addr, "0.0.0.0") != 0) {
        if (inet_pton(AF_INET, addr, &local.sin_addr) != 1) {
            plat_closesocket(e->sock); e->sock = PLAT_INVALID_SOCKET;
            return -2;
        }
    } else {
        local.sin_addr.s_addr = INADDR_ANY;
    }

    if (bind(e->sock, (struct sockaddr *)&local, sizeof local) != 0) {
        int err = plat_last_sock_err();
        plat_closesocket(e->sock);
        e->sock = PLAT_INVALID_SOCKET;
        return err ? -err : -3;
    }

    if (local.sin_addr.s_addr != INADDR_ANY) {
#ifdef _WIN32
        DWORD idx = iface_index_for_address(addr);
        if (idx == 0) {
            emit(e, "bind_note", "指定地址不属于本机任何网卡", addr, -1);
        } else {
            u_int be_index = htonl((u_long)idx);
            if (setsockopt(e->sock, IPPROTO_IP, IP_UNICAST_IF,
                           (const char *)&be_index, sizeof be_index) != 0) {
                char detail[128];
                snprintf(detail, sizeof detail, "接口 %lu，errno=%d",
                         (unsigned long)idx, plat_last_sock_err());
                emit(e, "bind_note", "指定网卡未能锁定出口", detail, -1);
            }
        }
#else
        char ifname[64] = {0};
        if (!iface_name_for_address(addr, ifname, sizeof ifname)) {
            emit(e, "bind_note", "指定地址不属于本机任何网卡", addr, -1);
        } else {
#ifdef SO_BINDTODEVICE
            if (setsockopt(e->sock, SOL_SOCKET, SO_BINDTODEVICE,
                           ifname, (socklen_t)strlen(ifname) + 1) != 0) {
                /* Non-root processes often get EPERM on SO_BINDTODEVICE; source
                 * IP binding still remains active. */
                if (errno != EPERM && errno != EACCES) {
                    emit(e, "bind_note", "未能按接口名锁定出口", ifname, errno);
                }
            }
#endif
        }
#endif
    }

    memset(&e->peer, 0, sizeof e->peer);
    e->peer.sin_family = AF_INET;
    e->peer.sin_port = htons((uint16_t)e->cfg.port);
    if (inet_pton(AF_INET, e->cfg.server, &e->peer.sin_addr) != 1) {
        plat_closesocket(e->sock); e->sock = PLAT_INVALID_SOCKET;
        return -4;
    }
    return 0;
}

/* ------------------------------------------------------------------ loop */
static unsigned
#ifdef _WIN32
__stdcall
#endif
engine_thread(void *arg)
{
    engine_t *e = (engine_t *)arg;
    char cands[MAX_BIND_CANDIDATES][16];
    int cand_count = 0;
    int cand_index = 0;

    emit(e, "started", "认证线程已启动", NULL, -1);

    while (!plat_event_is_set(&e->stop_evt)) {
        if (cand_count == 0) {
            cand_count = build_bind_candidates(e, cands, MAX_BIND_CANDIDATES);
            cand_index = 0;
        }
        if (cand_index >= cand_count) { cand_index = 0; cand_count = 0; continue; }

        const char *addr = cands[cand_index];
        const char *shown = (addr[0] && strcmp(addr, "0.0.0.0") != 0) ? addr : "0.0.0.0";
        char msg[96];
        snprintf(msg, sizeof msg, "正在绑定 %s", shown);
        set_state(e, ENGINE_STATE_BINDING, msg);
        if (cand_index > 0) {
            const char *prev = cands[cand_index - 1];
            char note[128];
            snprintf(note, sizeof note,
                     "上一次在 %s 上收不到认证服务器响应，改用 %s 重试",
                     (prev[0] && strcmp(prev, "0.0.0.0") != 0) ? prev : "0.0.0.0",
                     shown);
            emit(e, "bind_retry", note, NULL, -1);
        }

        int rc = bind_socket(e, addr);
        if (rc != 0) {
            char detail[200];
            snprintf(detail, sizeof detail,
                     "无法绑定 %s:%d（错误 %d）。%s",
                     shown, e->cfg.bind_port, rc, bind_error_hint(rc));
            if (cand_index == 0) {
                plat_atomic_store(&e->reason, OFFLINE_BIND_FAILED);
                set_state(e, ENGINE_STATE_RETRY_WAIT, "绑定失败");
                emit(e, "bind_failed", "绑定端口失败", detail, rc);
                plat_atomic_store(&e->consecutive_failures, plat_atomic_load(&e->consecutive_failures) + 1);
                cand_index = 0;
                cand_count = 0;
            } else {
                emit(e, "bind_failed", "该地址绑定失败，试下一个", detail, rc);
                cand_index++;
                continue;
            }
        } else {
            const char *result = authenticate(e);
            if (e->sock != PLAT_INVALID_SOCKET) {
                plat_closesocket(e->sock);
                e->sock = PLAT_INVALID_SOCKET;
            }
            if (plat_event_is_set(&e->stop_evt)) break;
            if (strcmp(result, "fatal") == 0) {
                set_state(e, ENGINE_STATE_FATAL, "需要人工处理");
                goto done;
            }
            if (cand_index + 1 < cand_count) {
                cand_index++;
                continue;
            }
            cand_index = 0;
            cand_count = 0;
        }

        plat_rwlock_rdlock(&e->lock);
        int reconnect_enabled = e->cfg.reconnect_enabled;
        int give_up_after = e->cfg.give_up_after;
        plat_rwlock_rdunlock(&e->lock);

        if (!reconnect_enabled) {
            emit(e, "reconnect_disabled", "自动重连已关闭，请手动登录", NULL, -1);
            set_state(e, ENGINE_STATE_FATAL, "自动重连已关闭");
            goto done;
        }
        int failures = (int)plat_atomic_load(&e->consecutive_failures);
        if (give_up_after > 0 && failures >= give_up_after) {
            emit(e, "gave_up", "连续失败次数过多，已暂停自动重连",
                 "问题多半不在网络抖动，而在配置或环境。", -1);
            set_state(e, ENGINE_STATE_FATAL, "已暂停自动重连");
            goto done;
        }

        double delay = next_backoff(e);
        snprintf(msg, sizeof msg, "%.0f 秒后重试（第 %d 次失败）", delay, failures);
        set_state(e, ENGINE_STATE_RETRY_WAIT, msg);
        emit(e, "retry_scheduled", msg, NULL, -1);
        if (!engine_wait(e, delay)) break;
    }

done:
    if (e->sock != PLAT_INVALID_SOCKET) {
        plat_closesocket(e->sock);
        e->sock = PLAT_INVALID_SOCKET;
    }
    if ((engine_state)plat_atomic_load(&e->state) != ENGINE_STATE_FATAL) {
        set_state(e, ENGINE_STATE_STOPPED, "已停止");
    }
    plat_atomic_store(&e->running, 0);
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
    cfg->protocol_hex = 0;
}

engine_t *engine_create(const engine_config *cfg, engine_event_fn fn, void *user)
{
    engine_t *e = (engine_t *)calloc(1, sizeof *e);
    if (!e) return NULL;
    e->cfg = *cfg;
    e->fn = fn;
    e->user = user;
    e->sock = PLAT_INVALID_SOCKET;
    plat_atomic_store(&e->protocol_hex, cfg->protocol_hex ? 1 : 0);
    plat_atomic_store(&e->state, ENGINE_STATE_IDLE);
    plat_atomic_store(&e->reason, OFFLINE_NONE);
    plat_rwlock_init(&e->lock);
    if (plat_event_create(&e->stop_evt) != 0) {
        plat_rwlock_destroy(&e->lock);
        free(e);
        return NULL;
    }

    plat_net_init();
    e->ws_ready = 1;
    srand((unsigned)plat_rand_seed());
    return e;
}

int engine_start(engine_t *e)
{
    if (!e) return -1;
    if (e->thread) {
        if (plat_thread_is_alive(e->thread)) return -1;
        plat_thread_join(&e->thread, -1);
    }
    plat_atomic_store(&e->running, 0);
    plat_event_reset(&e->stop_evt);
    plat_atomic_store(&e->consecutive_failures, 0);
    plat_rwlock_wrlock(&e->lock);
    e->online_since = 0.0;
    e->ip[0] = '\0';
    plat_rwlock_wrunlock(&e->lock);
    e->started_at = now_seconds();
    plat_atomic_store(&e->state, ENGINE_STATE_IDLE);
    plat_atomic_store(&e->reason, OFFLINE_NONE);
    if (plat_thread_create(&e->thread, engine_thread, e) != 0) return -1;
    plat_atomic_store(&e->running, 1);
    return 0;
}

void engine_stop(engine_t *e)
{
    if (!e) return;
    plat_atomic_store(&e->reason, OFFLINE_USER_LOGOUT);
    plat_event_set(&e->stop_evt);
}

int engine_join(engine_t *e, int timeout_ms)
{
    if (!e || !e->thread) return 1;
    return plat_thread_join(&e->thread, timeout_ms);
}

void engine_destroy(engine_t *e)
{
    if (!e) return;
    engine_stop(e);
    if (e->thread) {
        plat_thread_join(&e->thread, 5000);
    }
    if (e->sock != PLAT_INVALID_SOCKET) {
        plat_closesocket(e->sock);
        e->sock = PLAT_INVALID_SOCKET;
    }
    plat_event_destroy(&e->stop_evt);
    plat_rwlock_destroy(&e->lock);
    if (e->ws_ready) { plat_net_cleanup(); e->ws_ready = 0; }
    plat_secure_zero(&e->cfg.password, sizeof e->cfg.password);
    plat_secure_zero(e->auth_info, sizeof e->auth_info);
    free(e);
}

engine_state engine_get_state(engine_t *e)
{
    return e ? (engine_state)plat_atomic_load(&e->state) : ENGINE_STATE_IDLE;
}

offline_reason engine_get_reason(engine_t *e)
{
    return e ? (offline_reason)plat_atomic_load(&e->reason) : OFFLINE_NONE;
}

int engine_is_online(engine_t *e)
{
    return engine_get_state(e) == ENGINE_STATE_ONLINE;
}

int engine_is_running(engine_t *e)
{
    if (!e || !e->thread) return 0;
    return plat_thread_is_alive(e->thread);
}

double engine_uptime(engine_t *e)
{
    if (!e) return 0.0;
    plat_rwlock_rdlock(&e->lock);
    double since = e->online_since;
    plat_rwlock_rdunlock(&e->lock);
    if (since <= 0.0) return 0.0;
    return now_seconds() - since;
}

void engine_get_ip(engine_t *e, char out[16])
{
    if (!e || !out) return;
    plat_rwlock_rdlock(&e->lock);
    snprintf(out, 16, "%s", e->ip);
    plat_rwlock_rdunlock(&e->lock);
}

int engine_get_consecutive_failures(engine_t *e)
{
    return e ? (int)plat_atomic_load(&e->consecutive_failures) : 0;
}

void engine_set_protocol_hex(engine_t *e, int on)
{
    if (!e) return;
    plat_atomic_store(&e->protocol_hex, on ? 1 : 0);
    plat_rwlock_wrlock(&e->lock);
    e->cfg.protocol_hex = on ? 1 : 0;
    plat_rwlock_wrunlock(&e->lock);
    emit(e, "note", on ? "协议十六进制转储：开" : "协议十六进制转储：关", NULL, -1);
}

void engine_update_config(engine_t *e, const engine_config *fresh)
{
    if (!e || !fresh) return;
    plat_rwlock_wrlock(&e->lock);
    plat_secure_zero(e->cfg.password, sizeof e->cfg.password);
    e->cfg = *fresh;
    plat_rwlock_wrunlock(&e->lock);
    plat_atomic_store(&e->protocol_hex, fresh->protocol_hex ? 1 : 0);
}

void engine_update_live_policy(engine_t *e, const engine_config *fresh)
{
    if (!e || !fresh) return;
    int hex_changed = 0;
    plat_rwlock_wrlock(&e->lock);
    hex_changed = (e->cfg.protocol_hex != (fresh->protocol_hex ? 1 : 0));
    e->cfg.protocol_hex = fresh->protocol_hex ? 1 : 0;
    e->cfg.reconnect_enabled = fresh->reconnect_enabled;
    e->cfg.min_delay = fresh->min_delay;
    e->cfg.max_delay = fresh->max_delay;
    e->cfg.factor = fresh->factor;
    e->cfg.jitter = fresh->jitter;
    e->cfg.give_up_after = fresh->give_up_after;
    plat_rwlock_wrunlock(&e->lock);
    plat_atomic_store(&e->protocol_hex, fresh->protocol_hex ? 1 : 0);
    if (hex_changed) {
        emit(e, "note", fresh->protocol_hex ? "协议十六进制转储：开" : "协议十六进制转储：关",
             NULL, -1);
    }
}

void engine_get_identity(engine_t *e, char *account_out, size_t account_cap,
                         char *server_out, size_t server_cap)
{
    if (!e) return;
    plat_rwlock_rdlock(&e->lock);
    if (account_out && account_cap) snprintf(account_out, account_cap, "%s", e->cfg.account);
    if (server_out && server_cap) snprintf(server_out, server_cap, "%s", e->cfg.server);
    plat_rwlock_rdunlock(&e->lock);
}
