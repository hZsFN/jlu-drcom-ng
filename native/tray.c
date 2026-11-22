/* tray.c - notification-area icon for drcomd (Windows inline message pump).
 *
 * Runs directly on the main thread via tray_create_inline() + tray_run_loop(),
 * eliminating both an extra OS thread and any Sleep(200) polling loop.
 * All Shell_NotifyIconW calls execute strictly on the window-owning thread.
 */
#ifdef _WIN32

#include "tray.h"
#include "resource.h"

#include <shellapi.h>
#include <string.h>
#include <stdio.h>
#include <stdlib.h>

#pragma comment(lib, "user32.lib")
#pragma comment(lib, "shell32.lib")

#define WM_TRAY_CALLBACK   (WM_APP + 1)
#define WM_APP_SYNC        (WM_APP + 2)
#define WM_APP_QUIT        (WM_APP + 3)
#define WM_APP_NOTIFY      (WM_APP + 4)
#define TRAY_ICON_ID       1

#define CMD_SHOW           40001
#define CMD_RECONNECT      40002
#define CMD_QUIT           40003

struct tray {
    HWND hwnd;
    HICON icon_online;
    HICON icon_offline;
    SRWLOCK lock;
    int online;
    int visible;
    UINT taskbar_created_msg;
    WCHAR tooltip[128];
    int has_notify;
    WCHAR notify_title[64];
    WCHAR notify_text[256];
    int notify_warn;
    tray_show_fn on_show;
    tray_reconnect_fn on_reconnect;
    tray_quit_fn on_quit;
    void *ctx;
};

static tray_t *g_tray = NULL;

static WCHAR *wide(const char *src, WCHAR *dst, size_t cap)
{
    if (cap == 0) return dst;
    dst[0] = L'\0';
    if (!src) return dst;
    int n = MultiByteToWideChar(CP_UTF8, 0, src, -1, dst, (int)cap);
    if (n <= 0) dst[0] = L'\0';
    return dst;
}

static HICON load_icon(int which)
{
    HICON icon = (HICON)LoadImageW(GetModuleHandleW(NULL),
                                   MAKEINTRESOURCEW(which),
                                   IMAGE_ICON, 0, 0,
                                   LR_DEFAULTSIZE | LR_SHARED);
    if (!icon) {
        icon = LoadIconW(NULL, MAKEINTRESOURCEW(32512));
    }
    return icon;
}

static void add_icon(tray_t *t)
{
    NOTIFYICONDATAW nid;
    memset(&nid, 0, sizeof nid);
    nid.cbSize = sizeof nid;
    nid.hWnd = t->hwnd;
    nid.uID = TRAY_ICON_ID;
    nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP;
    nid.uCallbackMessage = WM_TRAY_CALLBACK;
    AcquireSRWLockShared(&t->lock);
    nid.hIcon = t->online ? t->icon_online : t->icon_offline;
    wcsncpy(nid.szTip, t->tooltip, ARRAYSIZE(nid.szTip) - 1);
    ReleaseSRWLockShared(&t->lock);
    if (Shell_NotifyIconW(NIM_ADD, &nid)) {
        t->visible = 1;
        nid.uVersion = NOTIFYICON_VERSION_4;
        Shell_NotifyIconW(NIM_SETVERSION, &nid);
    }
}

static void apply_sync(tray_t *t)
{
    if (!t->hwnd) return;
    NOTIFYICONDATAW nid;
    memset(&nid, 0, sizeof nid);
    nid.cbSize = sizeof nid;
    nid.hWnd = t->hwnd;
    nid.uID = TRAY_ICON_ID;
    nid.uFlags = NIF_ICON | NIF_TIP;
    AcquireSRWLockShared(&t->lock);
    nid.hIcon = t->online ? t->icon_online : t->icon_offline;
    wcsncpy(nid.szTip, t->tooltip, ARRAYSIZE(nid.szTip) - 1);
    ReleaseSRWLockShared(&t->lock);
    Shell_NotifyIconW(NIM_MODIFY, &nid);
}

static void apply_notify(tray_t *t)
{
    if (!t->hwnd || !t->visible) return;
    NOTIFYICONDATAW nid;
    memset(&nid, 0, sizeof nid);
    nid.cbSize = sizeof nid;
    nid.hWnd = t->hwnd;
    nid.uID = TRAY_ICON_ID;
    nid.uFlags = NIF_INFO;
    AcquireSRWLockExclusive(&t->lock);
    if (!t->has_notify) {
        ReleaseSRWLockExclusive(&t->lock);
        return;
    }
    t->has_notify = 0;
    nid.dwInfoFlags = t->notify_warn ? NIIF_WARNING : NIIF_INFO;
    wcsncpy(nid.szInfoTitle, t->notify_title, ARRAYSIZE(nid.szInfoTitle) - 1);
    wcsncpy(nid.szInfo, t->notify_text, ARRAYSIZE(nid.szInfo) - 1);
    ReleaseSRWLockExclusive(&t->lock);
    Shell_NotifyIconW(NIM_MODIFY, &nid);
}

static void remove_icon(tray_t *t)
{
    if (!t->visible) return;
    NOTIFYICONDATAW nid;
    memset(&nid, 0, sizeof nid);
    nid.cbSize = sizeof nid;
    nid.hWnd = t->hwnd;
    nid.uID = TRAY_ICON_ID;
    Shell_NotifyIconW(NIM_DELETE, &nid);
    t->visible = 0;
}

static void show_menu(tray_t *t)
{
    HMENU menu = CreatePopupMenu();
    if (!menu) return;

    AppendMenuW(menu, MF_STRING, CMD_SHOW, L"打开窗口(&O)");
    AppendMenuW(menu, MF_STRING, CMD_RECONNECT, L"重新连接(&R)");
    AppendMenuW(menu, MF_SEPARATOR, 0, NULL);
    AppendMenuW(menu, MF_STRING, CMD_QUIT, L"退出(&Q)");

    POINT pt;
    GetCursorPos(&pt);

    SetForegroundWindow(t->hwnd);
    TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_BOTTOMALIGN,
                   pt.x, pt.y, 0, t->hwnd, NULL);
    PostMessageW(t->hwnd, WM_NULL, 0, 0);
    DestroyMenu(menu);
}

static LRESULT CALLBACK tray_wndproc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp)
{
    tray_t *t = g_tray;

    switch (msg) {
    case WM_TRAY_CALLBACK:
        if (!t) break;
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

    case WM_APP_SYNC:
        if (t) apply_sync(t);
        return 0;

    case WM_APP_NOTIFY:
        if (t) apply_notify(t);
        return 0;

    case WM_APP_QUIT:
        PostQuitMessage(0);
        return 0;

    case WM_CLOSE:
        DestroyWindow(hwnd);
        return 0;

    case WM_DESTROY:
        PostQuitMessage(0);
        return 0;

    default:
        if (t && t->taskbar_created_msg && msg == t->taskbar_created_msg) {
            t->visible = 0;
            add_icon(t);
            return 0;
        }
        break;
    }
    return DefWindowProcW(hwnd, msg, wp, lp);
}

tray_t *tray_create_inline(tray_show_fn on_show, tray_reconnect_fn on_reconnect,
                           tray_quit_fn on_quit, void *ctx)
{
    tray_t *t = (tray_t *)calloc(1, sizeof *t);
    if (!t) return NULL;

    InitializeSRWLock(&t->lock);
    t->on_show = on_show;
    t->on_reconnect = on_reconnect;
    t->on_quit = on_quit;
    t->ctx = ctx;
    t->online = 0;
    t->tooltip[0] = L'\0';
    t->icon_online = load_icon(IDI_APP_ONLINE);
    t->icon_offline = load_icon(IDI_APP_OFFLINE);

    WNDCLASSW wc;
    memset(&wc, 0, sizeof wc);
    wc.lpfnWndProc = tray_wndproc;
    wc.hInstance = GetModuleHandleW(NULL);
    wc.lpszClassName = L"DrcomdTrayWindow";
    RegisterClassW(&wc);

    g_tray = t;
    t->hwnd = CreateWindowExW(0, wc.lpszClassName, L"drcomd",
                              WS_OVERLAPPED, 0, 0, 0, 0,
                              NULL, NULL, wc.hInstance, NULL);
    if (!t->hwnd) {
        g_tray = NULL;
        free(t);
        return NULL;
    }
    t->taskbar_created_msg = RegisterWindowMessageW(L"TaskbarCreated");
    add_icon(t);
    return t;
}

void tray_run_loop(tray_t *t)
{
    if (!t || !t->hwnd) return;
    MSG msg;
    while (GetMessageW(&msg, NULL, 0, 0) > 0) {
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }
}

void tray_set_state(tray_t *t, int online, const char *tooltip)
{
    if (!t) return;
    AcquireSRWLockExclusive(&t->lock);
    t->online = online ? 1 : 0;
    if (tooltip) wide(tooltip, t->tooltip, ARRAYSIZE(t->tooltip));
    ReleaseSRWLockExclusive(&t->lock);
    if (t->hwnd) PostMessageW(t->hwnd, WM_APP_SYNC, 0, 0);
}

void tray_notify(tray_t *t, const char *title, const char *text, int warning)
{
    if (!t || !t->hwnd) return;
    AcquireSRWLockExclusive(&t->lock);
    wide(title, t->notify_title, ARRAYSIZE(t->notify_title));
    wide(text, t->notify_text, ARRAYSIZE(t->notify_text));
    t->notify_warn = warning ? 1 : 0;
    t->has_notify = 1;
    ReleaseSRWLockExclusive(&t->lock);
    PostMessageW(t->hwnd, WM_APP_NOTIFY, 0, 0);
}

void tray_request_quit(tray_t *t)
{
    if (t && t->hwnd) PostMessageW(t->hwnd, WM_APP_QUIT, 0, 0);
}

void tray_destroy(tray_t *t)
{
    if (!t) return;
    remove_icon(t);
    if (t->hwnd) {
        DestroyWindow(t->hwnd);
        t->hwnd = NULL;
    }
    g_tray = NULL;
    free(t);
}

#endif /* _WIN32 */
