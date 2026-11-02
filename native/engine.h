/*
 * The authentication engine: one worker thread that owns the UDP socket and
 * runs the whole Dr.COM exchange.  Communicates outward through an event
 * callback; nothing here knows about sockets being shared, threads being
 * joined, or what the UI looks like.
 */
#ifndef DRCOMD_ENGINE_H
#define DRCOMD_ENGINE_H

#include <stdint.h>

typedef enum {
    ENGINE_STATE_IDLE = 0,
    ENGINE_STATE_BINDING,
    ENGINE_STATE_CHALLENGING,
    ENGINE_STATE_AUTHENTICATING,
    ENGINE_STATE_ONLINE,
    ENGINE_STATE_RETRY_WAIT,
    ENGINE_STATE_FATAL,
    ENGINE_STATE_STOPPED
} engine_state;

typedef enum {
    OFFLINE_NONE = 0,
    OFFLINE_USER_LOGOUT,
    OFFLINE_BIND_FAILED,
    OFFLINE_CHALLENGE_FAILED,
    OFFLINE_LOGIN_FAILED,
    OFFLINE_TIMEOUT,
    OFFLINE_KEEPALIVE_FAILED,
    OFFLINE_STOPPED
} offline_reason;

typedef struct {
    /* wire */
    char   server[64];
    int    port;
    char   bind_address[64];
    int    bind_port;
    int    timeout_ms;
    double keepalive_interval;
    int    challenge_retries;
    double post_login_delay;
    int    auto_interface_fallback;
    /* reconnect policy */
    int    reconnect_enabled;
    double min_delay;
    double max_delay;
    double factor;
    double jitter;
    int    give_up_after;
    /* credentials */
    char   account[128];
    char   password[256];
    char   mac[64];
} engine_config;

/* Event sink.  `kind` is one of:
 *   "started" "ip" "online" "login_failed" "fatal" "bind_failed"
 *   "challenge_failed" "keepalive_ok" "keepalive_failed"
 *   "retry_scheduled" "gave_up" "reconnect_disabled" "error" "stopped"
 * Called on the engine thread; the sink must not block.
 */
typedef void (*engine_event_fn)(void *user, const char *kind,
                                const char *message, const char *detail,
                                int error_code);

typedef struct engine engine_t;

/* Fill *cfg with the JLU defaults. */
void engine_config_defaults(engine_config *cfg);

engine_t *engine_create(const engine_config *cfg, engine_event_fn fn, void *user);
int       engine_start(engine_t *e);        /* 0 ok, -1 already running */
void      engine_stop(engine_t *e);         /* asynchronous */
int       engine_join(engine_t *e, int timeout_ms);  /* 1 joined, 0 timed out */
void      engine_destroy(engine_t *e);

engine_state   engine_get_state(engine_t *e);
offline_reason engine_get_reason(engine_t *e);
int            engine_is_online(engine_t *e);
int            engine_is_running(engine_t *e);
double         engine_uptime(engine_t *e);
/* Current session facts, safe to read from any thread. */
void           engine_get_ip(engine_t *e, char out[16]);
int            engine_get_consecutive_failures(engine_t *e);

#endif
