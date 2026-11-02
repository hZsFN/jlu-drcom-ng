/* tray.c - notification-area icon for drcomd.
 *
 * Design notes
 * ------------
 * One hidden window, one message pump, one thread.  The icon is re-added after
 * an Explorer restart (the "TaskbarCreated" broadcast) - without that, the icon
 * silently disappears the first time Explorer crashes, which on a client that
 * runs for days is not a rare event.
 *
 * The icon bitmap is loaded from the running executable's own resources, so the
 * installer has to carry a .ico and rc.exe has to be called by build.bat.  If the
 * resource is missing we fall back to the stock application icon rather than
 * leaving the user with an invisible entry.
 */
#include "tray.h"

#include <shellapi.h>
#include <process.h>
#include <string.h>
#include <stdio.h>

#include "resource.h"

/* Each translation unit that calls into user32/shell32 needs the import
 * libraries; a pragma here keeps build.bat from having to remember them. */
#pragma comment(lib, "user32.lib")
#pragma comment(lib, "shell32.lib")

#define WM_TRAY_CALLBACK   (WM_APP + 1)
#define TRAY_ICON_ID       1

/* Menu command ids.  Kept out of the low range Windows reserves. */
#define CMD_SHOW           40001
#define CMD_RECONNECT      40002
#define CMD_QUIT           40003

struct tray {
    HWND hwnd;
    HANDLE thread;
    HICON icon_online;
    HICON icon_offline;
    int online;
    int visible;
    UINT taskbar_created_msg;
    char tooltip[128];
    tray_show_fn on_show;
    tray_reconnect_fn on_reconnect;
    tray_quit_fn on_quit;
    void *ctx;
};

/* One tray per process, and the window procedure needs it without being able to
 * carry a user pointer (WM_CREATE is the only clean way, and even then the
 * pointer must already be stored). */
static tray_t *g_tray = NULL;

static HICON load_icon(int which)
{
    /* The exe carries a multi-size icon; the shell picks the right size. */
    HICON icon = (HICON)LoadImageA(GetModuleHandleA(NULL),
                                   MAKEINTRESOURCEA(which),
                                   IMAGE_ICON, 0, 0,
                                   LR_DEFAULTSIZE | LR_SHARED);
    if (!icon) {
        icon = LoadIconA(NULL, IDI_APPLICATION);
    }
    return icon;
}

static void add_icon(tray_t *t)
{
    NOTIFYICONDATAA nid;
    memset(&nid, 0, sizeof nid);
    nid.cbSize = sizeof nid;
    nid.hWnd = t->hwnd;
    nid.uID = TRAY_ICON_ID;
    nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP;
    nid.uCallbackMessage = WM_TRAY_CALLBACK;
    nid.hIcon = t->online ? t->icon_online : t->icon_offline;
    snprintf(nid.szTip, sizeof nid.szTip, "%s", t->tooltip);
    if (Shell_NotifyIconA(NIM_ADD, &nid)) {
        t->visible = 1;
        /* Windows 2000-era icons are 32x32 unless we say otherwise; ask for the
         * modern behaviour so the icon is not a blurry upscale on high DPI. */
        nid.uVersion = NOTIFYICON_VERSION_4;
        Shell_NotifyIconA(NIM_SETVERSION, &nid);
    }
}

static void remove_icon(tray_t *t)
{
    if (!t->visible) return;
    NOTIFYICONDATAA nid;
    memset(&nid, 0, sizeof nid);
    nid.cbSize = sizeof nid;
    nid.hWnd = t->hwnd;
    nid.uID = TRAY_ICON_ID;
    Shell_NotifyIconA(NIM_DELETE, &nid);
    t->visible = 0;
}

static void show_menu(tray_t *t)
{
    HMENU menu = CreatePopupMenu();
    if (!menu) return;

    AppendMenuA(menu, MF_STRING, CMD_SHOW, "打开窗口(&O)");
    AppendMenuA(menu, MF_STRING, CMD_RECONNECT, "重新连接(&R)");
    AppendMenuA(menu, MF_SEPARATOR, 0, NULL);
    AppendMenuA(menu, MF_STRING, CMD_QUIT, "退出(&Q)");

    POINT pt;
    GetCursorPos(&pt);

    /* Required, or the menu stays up when the user clicks elsewhere: the
     * foreground window must be ours for the dismiss-on-outside-click to work. */
    SetForegroundWindow(t->hwnd);
    TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_BOTTOMALIGN,
                   pt.x, pt.y, 0, t->hwnd, NULL);
    PostMessageA(t->hwnd, WM_NULL, 0, 0);
    DestroyMenu(menu);
}

static LRESULT CALLBACK tray_wndproc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp)
{
    tray_t *t = g_tray;

    switch (msg) {
    case WM_TRAY_CALLBACK:
        if (!t) break;
        /* NOTIFYICON_VERSION_4 puts the real event in LOWORD(lParam); older
         * shells put it in lParam directly.  Handle both. */
        {
            UINT event = LOWORD(lp);
            if (event == 0) event = (UINT)lp;
            if (event == WM_LBUTTONUP || event == NIN_SELECT || event == NIN_KEYSELECT) {
                if (t->on_show) t->on_show(t->ctx);
            } else if (event == WM_RBUTTONUP || event == WM_CONTEXTMENU) {
                show_menu(t);
            }
        }
        return 0;

    case WM_COMMAND:
        if (!t) break;
        switch (LOWORD(wp)) {
        case CMD_SHOW:      if (t->on_show) t->on_show(t->ctx); break;
        case CMD_RECONNECT: if (t->on_reconnect) t->on_reconnect(t->ctx); break;
        case CMD_QUIT:      if (t->on_quit) t->on_quit(t->ctx); break;
        default: break;
        }
        return 0;

    case WM_DESTROY:
        PostQuitMessage(0);
        return 0;

    default:
        if (t && t->taskbar_created_msg && msg == t->taskbar_created_msg) {
            /* Explorer came back.  Every icon it was hosting is gone, so put
             * ours up again - otherwise the user has a client with no UI at
             * all, and no way to get one without reboot. */
            t->visible = 0;
            add_icon(t);
            return 0;
        }
        break;
    }
    return DefWindowProcA(hwnd, msg, wp, lp);
}

static unsigned __stdcall tray_thread(void *arg)
{
    tray_t *t = arg;

    WNDCLASSA wc;
    memset(&wc, 0, sizeof wc);
    wc.lpfnWndProc = tray_wndproc;
    wc.hInstance = GetModuleHandleA(NULL);
    wc.lpszClassName = "DrcomdTrayWindow";
    RegisterClassA(&wc);

    /* A real (if invisible) window rather than HWND_MESSAGE: message-only
     * windows do not receive the TaskbarCreated broadcast. */
    t->hwnd = CreateWindowExA(0, wc.lpszClassName, "drcomd",
                              WS_OVERLAPPED, 0, 0, 0, 0,
                              NULL, NULL, wc.hInstance, NULL);
    if (!t->hwnd) return 1;

    t->taskbar_created_msg = RegisterWindowMessageA("TaskbarCreated");
    add_icon(t);

    MSG msg;
    while (GetMessageA(&msg, NULL, 0, 0) > 0) {
        TranslateMessage(&msg);
        DispatchMessageA(&msg);
    }

    remove_icon(t);
    if (t->hwnd) { DestroyWindow(t->hwnd); t->hwnd = NULL; }
    return 0;
}

tray_t *tray_create(tray_show_fn on_show, tray_reconnect_fn on_reconnect,
                    tray_quit_fn on_quit, void *ctx)
{
    tray_t *t = (tray_t *)calloc(1, sizeof *t);
    if (!t) return NULL;

    t->on_show = on_show;
    t->on_reconnect = on_reconnect;
    t->on_quit = on_quit;
    t->ctx = ctx;
    t->online = 0;
    t->tooltip[0] = '\0';
    t->icon_online = load_icon(IDI_APP_ONLINE);
    t->icon_offline = load_icon(IDI_APP_OFFLINE);

    g_tray = t;
    t->thread = (HANDLE)_beginthreadex(NULL, 0, tray_thread, t, 0, NULL);
    if (!t->thread) {
        g_tray = NULL;
        free(t);
        return NULL;
    }
    return t;
}

void tray_set_state(tray_t *t, int online, const char *tooltip)
{
    if (!t) return;
    t->online = online ? 1 : 0;
    if (tooltip) snprintf(t->tooltip, sizeof t->tooltip, "%s", tooltip);

    if (!t->hwnd) return;
    NOTIFYICONDATAA nid;
    memset(&nid, 0, sizeof nid);
    nid.cbSize = sizeof nid;
    nid.hWnd = t->hwnd;
    nid.uID = TRAY_ICON_ID;
    nid.uFlags = NIF_ICON | NIF_TIP;
    nid.hIcon = t->online ? t->icon_online : t->icon_offline;
    snprintf(nid.szTip, sizeof nid.szTip, "%s", t->tooltip);
    /* Fails harmlessly if the icon is not up yet (thread still starting). */
    Shell_NotifyIconA(NIM_MODIFY, &nid);
}

void tray_notify(tray_t *t, const char *title, const char *text, int warning)
{
    if (!t || !t->hwnd || !t->visible) return;
    NOTIFYICONDATAA nid;
    memset(&nid, 0, sizeof nid);
    nid.cbSize = sizeof nid;
    nid.hWnd = t->hwnd;
    nid.uID = TRAY_ICON_ID;
    nid.uFlags = NIF_INFO;
    nid.dwInfoFlags = warning ? NIIF_WARNING : NIIF_INFO;
    snprintf(nid.szInfoTitle, sizeof nid.szInfoTitle, "%s", title ? title : "");
    snprintf(nid.szInfo, sizeof nid.szInfo, "%s", text ? text : "");
    Shell_NotifyIconA(NIM_MODIFY, &nid);
}

void tray_destroy(tray_t *t)
{
    if (!t) return;
    if (t->hwnd) PostMessageA(t->hwnd, WM_CLOSE, 0, 0);
    if (t->thread) {
        WaitForSingleObject(t->thread, 3000);
        CloseHandle(t->thread);
    }
    g_tray = NULL;
    free(t);
}
