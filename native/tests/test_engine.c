/*
 * Drives the native engine against tests/mock_server.py.
 *
 * It is not a unit test in the strict sense - it needs a fake server on
 * 127.0.0.1 - but it is the only way to prove the state machine, the
 * keepalive cadence and the stop path actually work together.
 *
 *   python native/tests/mock_server.py --port 51440 --duration 30 &
 *   native/tests/test_engine.exe 51440 51441
 */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <windows.h>

#include "../engine.h"

static int saw_online = 0;
static int saw_login_failed = 0;
static int saw_fatal = 0;
static int saw_stopped = 0;
static int keepalive_ok_count = 0;
static int retry_count = 0;
static char last_ip[16];
static int last_error_code = -100;

static void on_event(void *user, const char *kind, const char *message,
                     const char *detail, int error_code)
{
    (void)user;
    if (strcmp(kind, "online") == 0) {
        saw_online = 1;
        snprintf(last_ip, sizeof last_ip, "%s", message ? message : "");
        printf("[event] ONLINE  ip=%s\n", last_ip);
    } else if (strcmp(kind, "keepalive_ok") == 0) {
        keepalive_ok_count++;
    } else if (strcmp(kind, "login_failed") == 0) {
        saw_login_failed = 1;
        last_error_code = error_code;
        printf("[event] login_failed %s (0x%02X) %s\n",
               message ? message : "", error_code, detail ? detail : "");
    } else if (strcmp(kind, "fatal") == 0) {
        saw_fatal = 1;
        printf("[event] FATAL %s\n", message ? message : "");
    } else if (strcmp(kind, "retry_scheduled") == 0) {
        retry_count++;
        printf("[event] retry: %s\n", message ? message : "");
    } else if (strcmp(kind, "stopped") == 0) {
        saw_stopped = 1;
        printf("[event] stopped\n");
    } else if (strcmp(kind, "state") == 0) {
        /* too chatty for the log, kept for completeness */
    } else {
        printf("[event] %s %s %s\n", kind, message ? message : "", detail ? detail : "");
    }
}

static int failures = 0;

static void check(int condition, const char *what)
{
    printf("[%s] %s\n", condition ? " OK " : "FAIL", what);
    if (!condition) failures++;
}

int main(int argc, char **argv)
{
    int port = (argc > 1) ? atoi(argv[1]) : 51440;
    int bind_port = (argc > 2) ? atoi(argv[2]) : 51441;
    int seconds = (argc > 3) ? atoi(argv[3]) : 6;
    int fail_code = (argc > 4) ? (int)strtol(argv[4], NULL, 0) : 0;

    engine_config cfg;
    engine_config_defaults(&cfg);
    snprintf(cfg.server, sizeof cfg.server, "127.0.0.1");
    cfg.port = port;
    cfg.bind_port = bind_port;
    snprintf(cfg.account, sizeof cfg.account, "202300000001");
    snprintf(cfg.password, sizeof cfg.password, "abc12345");
    snprintf(cfg.mac, sizeof cfg.mac, "AA:BB:CC:DD:EE:FF");
    cfg.keepalive_interval = 0.6;   /* keep the test short */
    cfg.timeout_ms = 800;
    cfg.post_login_delay = 0.05;
    cfg.min_delay = 0.5;
    cfg.max_delay = 2.0;

    engine_t *e = engine_create(&cfg, on_event, NULL);
    if (!e) { printf("engine_create failed\n"); return 2; }

    printf("starting engine -> 127.0.0.1:%d (local %d), %ds, fail_code=0x%02X\n",
           port, bind_port, seconds, fail_code);
    if (engine_start(e) != 0) { printf("engine_start failed\n"); return 2; }

    for (int i = 0; i < seconds * 10; i++) {
        Sleep(100);
        if (saw_fatal) break;
    }

    printf("\nstopping...\n");
    DWORD t0 = GetTickCount();
    engine_stop(e);
    int joined = engine_join(e, 4000);
    DWORD elapsed = GetTickCount() - t0;
    printf("stop took %lu ms (joined=%d)\n\n", (unsigned long)elapsed, joined);

    check(joined == 1, "engine thread joined within 4s");
    check(elapsed < 1500, "stop is immediate, not waiting for a 20s keepalive sleep");
    check(saw_stopped == 1, "emitted a 'stopped' event");
    check(engine_get_state(e) != ENGINE_STATE_ONLINE, "state left ONLINE after stop");

    if (fail_code) {
        check(saw_login_failed == 1, "login failure was reported");
        check(last_error_code == fail_code, "error code reached the event sink");
    } else {
        check(saw_online == 1, "engine reached ONLINE");
        check(strcmp(last_ip, "10.100.61.99") == 0, "IP came from the challenge reply");
        check(keepalive_ok_count >= 2, "keepalive cycled at least twice");
        check(engine_uptime(e) == 0.0, "uptime resets once offline");
    }

    engine_destroy(e);
    printf("\n%d failure(s)\n", failures);
    return failures ? 1 : 0;
}
