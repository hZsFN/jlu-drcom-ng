/* tray.h - notification-area icon owned by drcomd (Windows only). */
#ifndef DRCOMD_TRAY_H
#define DRCOMD_TRAY_H

#ifdef _WIN32
#include "platform.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct tray tray_t;

typedef void (*tray_show_fn)(void *ctx);
typedef void (*tray_reconnect_fn)(void *ctx);
typedef void (*tray_quit_fn)(void *ctx);

/* Initialize the tray window and icon on the CURRENT thread (no extra thread). */
tray_t *tray_create_inline(tray_show_fn on_show, tray_reconnect_fn on_reconnect,
                           tray_quit_fn on_quit, void *ctx);

/* Run the Win32 message loop on the calling thread until tray_request_quit(). */
void tray_run_loop(tray_t *t);

/* Thread-safe: update online state / tooltip and wake the tray thread to apply. */
void tray_set_state(tray_t *t, int online, const char *tooltip);

/* Thread-safe: pop a balloon notification (best effort). */
void tray_notify(tray_t *t, const char *title, const char *text, int warning);

/* Thread-safe: ask tray_run_loop() to exit promptly. */
void tray_request_quit(tray_t *t);

/* Remove icon and free resources (call on the same thread after tray_run_loop). */
void tray_destroy(tray_t *t);

#ifdef __cplusplus
}
#endif

#endif /* _WIN32 */
#endif /* DRCOMD_TRAY_H */
