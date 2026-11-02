/* tray.h - a notification-area icon owned by the daemon.
 *
 * Why the core owns the tray
 * --------------------------
 * The Python UI used to own it (pystray), which meant the UI process had to stay
 * resident just to keep an icon on screen - 60 MB of private commit for a
 * 16x16 bitmap.  Anything the icon can do (show the window, reconnect, quit) is
 * something the daemon can do too, so the icon belongs here.  Now closing the
 * window really ends the UI process and the resident set is the daemon alone.
 *
 * The icon lives on its own thread: Shell_NotifyIcon needs a window to receive
 * its callback messages, and that window needs a message pump, which cannot run
 * on the engine thread (it blocks in recv) nor on the API thread (it blocks in
 * select).
 */
#ifndef DRCOMD_TRAY_H
#define DRCOMD_TRAY_H

#include <windows.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct tray tray_t;

/* Called on the tray thread.  Implementations must only post work elsewhere -
 * doing real work here would block the icon's message pump and make the menu
 * appear hung. */
typedef void (*tray_show_fn)(void *ctx);
typedef void (*tray_reconnect_fn)(void *ctx);
typedef void (*tray_quit_fn)(void *ctx);

/* Create the icon and start its thread.  Returns NULL if the icon could not be
 * installed (no Explorer running, for instance) - callers must not fail startup
 * over this. */
tray_t *tray_create(tray_show_fn on_show, tray_reconnect_fn on_reconnect,
                    tray_quit_fn on_quit, void *ctx);

/* Update the hover text and the online/offline look.  Safe from any thread. */
void tray_set_state(tray_t *t, int online, const char *tooltip);

/* Pop a balloon notification.  Best effort; Explorer may have them switched
 * off, and that is not an error. */
void tray_notify(tray_t *t, const char *title, const char *text, int warning);

/* Remove the icon and stop the thread.  Blocks until the thread is gone. */
void tray_destroy(tray_t *t);

#ifdef __cplusplus
}
#endif

#endif /* DRCOMD_TRAY_H */
