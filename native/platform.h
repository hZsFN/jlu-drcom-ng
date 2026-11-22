/*
 * Cross-platform OS primitives for drcomd (Windows + POSIX/Linux).
 *
 * Note: on Windows, winsock2.h MUST be included before windows.h so the legacy
 * winsock.h is never pulled in.
 */
#ifndef DRCOMD_PLATFORM_H
#define DRCOMD_PLATFORM_H

#ifndef _WIN32
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#endif

#include <stddef.h>
#include <stdint.h>
#include <string.h>

#ifdef _WIN32

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <bcrypt.h>
#include <process.h>

#pragma comment(lib, "ws2_32.lib")
#pragma comment(lib, "bcrypt.lib")

typedef SOCKET plat_socket_t;
#define PLAT_INVALID_SOCKET INVALID_SOCKET
#define plat_closesocket(s) closesocket(s)
#define plat_last_sock_err() ((int)WSAGetLastError())
#define plat_stricmp _stricmp
#define plat_strnicmp _strnicmp

static __inline void plat_net_init(void)
{
    WSADATA wsa;
    WSAStartup(MAKEWORD(2, 2), &wsa);
}

static __inline void plat_net_cleanup(void)
{
    WSACleanup();
}

static __inline void plat_set_sock_timeout(plat_socket_t s, int timeout_ms)
{
    DWORD tv = (DWORD)(timeout_ms > 0 ? timeout_ms : 1);
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, (const char *)&tv, sizeof tv);
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, (const char *)&tv, sizeof tv);
}

typedef SRWLOCK plat_rwlock_t;
#define PLAT_RWLOCK_INIT SRWLOCK_INIT
static __inline void plat_rwlock_init(plat_rwlock_t *l) { InitializeSRWLock(l); }
static __inline void plat_rwlock_destroy(plat_rwlock_t *l) { (void)l; }
static __inline void plat_rwlock_rdlock(plat_rwlock_t *l) { AcquireSRWLockShared(l); }
static __inline void plat_rwlock_rdunlock(plat_rwlock_t *l) { ReleaseSRWLockShared(l); }
static __inline void plat_rwlock_wrlock(plat_rwlock_t *l) { AcquireSRWLockExclusive(l); }
static __inline void plat_rwlock_wrunlock(plat_rwlock_t *l) { ReleaseSRWLockExclusive(l); }

typedef HANDLE plat_event_t;
static __inline int plat_event_create(plat_event_t *ev)
{
    *ev = CreateEventW(NULL, TRUE, FALSE, NULL);
    return *ev ? 0 : -1;
}
static __inline void plat_event_destroy(plat_event_t *ev)
{
    if (ev && *ev) { CloseHandle(*ev); *ev = NULL; }
}
static __inline void plat_event_set(plat_event_t *ev)
{
    if (ev && *ev) SetEvent(*ev);
}
static __inline void plat_event_reset(plat_event_t *ev)
{
    if (ev && *ev) ResetEvent(*ev);
}
static __inline int plat_event_is_set(plat_event_t *ev)
{
    return (ev && *ev && WaitForSingleObject(*ev, 0) == WAIT_OBJECT_0) ? 1 : 0;
}
/* Returns 1 if signaled (stop requested), 0 if timed out. */
static __inline int plat_event_wait_ms(plat_event_t *ev, int ms)
{
    DWORD w = (DWORD)(ms > 0 ? ms : 1);
    return WaitForSingleObject(*ev, w) == WAIT_OBJECT_0 ? 1 : 0;
}

typedef HANDLE plat_thread_t;
typedef unsigned (__stdcall *plat_thread_fn)(void *);
static __inline int plat_thread_create(plat_thread_t *t, plat_thread_fn fn, void *arg)
{
    *t = (HANDLE)_beginthreadex(NULL, 0, fn, arg, 0, NULL);
    return *t ? 0 : -1;
}
static __inline int plat_thread_is_alive(plat_thread_t t)
{
    return (t && WaitForSingleObject(t, 0) == WAIT_TIMEOUT) ? 1 : 0;
}
static __inline int plat_thread_join(plat_thread_t *t, int timeout_ms)
{
    if (!t || !*t) return 1;
    DWORD w = timeout_ms < 0 ? INFINITE : (DWORD)timeout_ms;
    if (WaitForSingleObject(*t, w) == WAIT_OBJECT_0) {
        CloseHandle(*t);
        *t = NULL;
        return 1;
    }
    return 0;
}

static __inline double plat_now_seconds(void)
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

static __inline double plat_wall_seconds(void)
{
    FILETIME ft;
    GetSystemTimeAsFileTime(&ft);
    unsigned long long ticks = ((unsigned long long)ft.dwHighDateTime << 32) | ft.dwLowDateTime;
    return (double)(ticks / 10000000ULL) - 11644473600.0;
}

static __inline void plat_sleep_ms(int ms)
{
    if (ms > 0) Sleep((DWORD)ms);
}

static __inline uint64_t plat_rand_seed(void)
{
    return (uint64_t)GetTickCount64();
}

static __inline void plat_secure_zero(void *ptr, size_t len)
{
    if (ptr && len) SecureZeroMemory(ptr, len);
}

static __inline int plat_random_bytes(uint8_t *buf, size_t len)
{
    NTSTATUS st = BCryptGenRandom(NULL, buf, (ULONG)len, BCRYPT_USE_SYSTEM_PREFERRED_RNG);
    return st == 0 ? 0 : -1;
}

typedef volatile LONG plat_atomic_t;
static __inline long plat_atomic_load(plat_atomic_t *p)
{
    return (long)InterlockedCompareExchange(p, 0, 0);
}
static __inline void plat_atomic_store(plat_atomic_t *p, long v)
{
    InterlockedExchange(p, (LONG)v);
}
static __inline long plat_atomic_xchg(plat_atomic_t *p, long v)
{
    return (long)InterlockedExchange(p, (LONG)v);
}

#else /* POSIX / Linux */

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdlib.h>
#include <strings.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/types.h>
#include <arpa/inet.h>
#include <netinet/in.h>
#include <time.h>
#include <unistd.h>

typedef int plat_socket_t;
#define PLAT_INVALID_SOCKET (-1)
#define plat_closesocket(s) close(s)
#define plat_last_sock_err() errno
#define plat_stricmp strcasecmp
#define plat_strnicmp strncasecmp

static inline void plat_net_init(void) {}
static inline void plat_net_cleanup(void) {}

static inline void plat_set_sock_timeout(plat_socket_t s, int timeout_ms)
{
    if (timeout_ms <= 0) timeout_ms = 1;
    struct timeval tv;
    tv.tv_sec = timeout_ms / 1000;
    tv.tv_usec = (timeout_ms % 1000) * 1000;
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
}

typedef pthread_rwlock_t plat_rwlock_t;
#define PLAT_RWLOCK_INIT PTHREAD_RWLOCK_INITIALIZER
static inline void plat_rwlock_init(plat_rwlock_t *l) { pthread_rwlock_init(l, NULL); }
static inline void plat_rwlock_destroy(plat_rwlock_t *l) { pthread_rwlock_destroy(l); }
static inline void plat_rwlock_rdlock(plat_rwlock_t *l) { pthread_rwlock_rdlock(l); }
static inline void plat_rwlock_rdunlock(plat_rwlock_t *l) { pthread_rwlock_unlock(l); }
static inline void plat_rwlock_wrlock(plat_rwlock_t *l) { pthread_rwlock_wrlock(l); }
static inline void plat_rwlock_wrunlock(plat_rwlock_t *l) { pthread_rwlock_unlock(l); }

typedef struct {
    pthread_mutex_t mu;
    pthread_cond_t  cv;
    int             signaled;
    int             ready;
} plat_event_t;

static inline int plat_event_create(plat_event_t *ev)
{
    memset(ev, 0, sizeof *ev);
    if (pthread_mutex_init(&ev->mu, NULL) != 0) return -1;
    pthread_condattr_t attr;
    pthread_condattr_init(&attr);
#ifdef CLOCK_MONOTONIC
    pthread_condattr_setclock(&attr, CLOCK_MONOTONIC);
#endif
    int rc = pthread_cond_init(&ev->cv, &attr);
    pthread_condattr_destroy(&attr);
    if (rc != 0) {
        pthread_mutex_destroy(&ev->mu);
        return -1;
    }
    ev->signaled = 0;
    ev->ready = 1;
    return 0;
}

static inline void plat_event_destroy(plat_event_t *ev)
{
    if (ev && ev->ready) {
        pthread_cond_destroy(&ev->cv);
        pthread_mutex_destroy(&ev->mu);
        ev->ready = 0;
    }
}

static inline void plat_event_set(plat_event_t *ev)
{
    if (!ev || !ev->ready) return;
    pthread_mutex_lock(&ev->mu);
    ev->signaled = 1;
    pthread_cond_broadcast(&ev->cv);
    pthread_mutex_unlock(&ev->mu);
}

static inline void plat_event_reset(plat_event_t *ev)
{
    if (!ev || !ev->ready) return;
    pthread_mutex_lock(&ev->mu);
    ev->signaled = 0;
    pthread_mutex_unlock(&ev->mu);
}

static inline int plat_event_is_set(plat_event_t *ev)
{
    if (!ev || !ev->ready) return 0;
    pthread_mutex_lock(&ev->mu);
    int s = ev->signaled;
    pthread_mutex_unlock(&ev->mu);
    return s;
}

static inline int plat_event_wait_ms(plat_event_t *ev, int ms)
{
    if (!ev || !ev->ready) return 1;
    if (ms <= 0) ms = 1;
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    ts.tv_sec += ms / 1000;
    ts.tv_nsec += (long)(ms % 1000) * 1000000L;
    if (ts.tv_nsec >= 1000000000L) {
        ts.tv_sec += 1;
        ts.tv_nsec -= 1000000000L;
    }
    pthread_mutex_lock(&ev->mu);
    while (!ev->signaled) {
        int rc = pthread_cond_timedwait(&ev->cv, &ev->mu, &ts);
        if (rc == ETIMEDOUT) break;
    }
    int s = ev->signaled;
    pthread_mutex_unlock(&ev->mu);
    return s ? 1 : 0;
}

typedef struct {
    pthread_t       tid;
    int             valid;
    pthread_mutex_t mu;
    pthread_cond_t  done_cv;
    int             finished;
    unsigned (*fn)(void *);
    void           *arg;
} plat_thread_wrap_t;

typedef plat_thread_wrap_t *plat_thread_t;
typedef unsigned (*plat_thread_fn)(void *);

static inline void *plat_thread_trampoline(void *p)
{
    plat_thread_wrap_t *w = (plat_thread_wrap_t *)p;
    w->fn(w->arg);
    pthread_mutex_lock(&w->mu);
    w->finished = 1;
    pthread_cond_broadcast(&w->done_cv);
    pthread_mutex_unlock(&w->mu);
    return NULL;
}

static inline int plat_thread_create(plat_thread_t *t, plat_thread_fn fn, void *arg)
{
    plat_thread_wrap_t *w = (plat_thread_wrap_t *)calloc(1, sizeof *w);
    if (!w) return -1;
    pthread_mutex_init(&w->mu, NULL);
    pthread_condattr_t attr;
    pthread_condattr_init(&attr);
#ifdef CLOCK_MONOTONIC
    pthread_condattr_setclock(&attr, CLOCK_MONOTONIC);
#endif
    pthread_cond_init(&w->done_cv, &attr);
    pthread_condattr_destroy(&attr);
    w->fn = fn;
    w->arg = arg;
    if (pthread_create(&w->tid, NULL, plat_thread_trampoline, w) != 0) {
        pthread_cond_destroy(&w->done_cv);
        pthread_mutex_destroy(&w->mu);
        free(w);
        return -1;
    }
    w->valid = 1;
    *t = w;
    return 0;
}

static inline int plat_thread_is_alive(plat_thread_t t)
{
    if (!t || !t->valid) return 0;
    pthread_mutex_lock(&t->mu);
    int alive = !t->finished;
    pthread_mutex_unlock(&t->mu);
    return alive;
}

static inline int plat_thread_join(plat_thread_t *t, int timeout_ms)
{
    if (!t || !*t) return 1;
    plat_thread_wrap_t *w = *t;
    if (!w->valid) return 1;
    if (timeout_ms < 0) {
        pthread_join(w->tid, NULL);
    } else {
        struct timespec ts;
        clock_gettime(CLOCK_MONOTONIC, &ts);
        ts.tv_sec += timeout_ms / 1000;
        ts.tv_nsec += (long)(timeout_ms % 1000) * 1000000L;
        if (ts.tv_nsec >= 1000000000L) {
            ts.tv_sec += 1;
            ts.tv_nsec -= 1000000000L;
        }
        pthread_mutex_lock(&w->mu);
        while (!w->finished) {
            if (pthread_cond_timedwait(&w->done_cv, &w->mu, &ts) == ETIMEDOUT) break;
        }
        int done = w->finished;
        pthread_mutex_unlock(&w->mu);
        if (!done) return 0;
        pthread_join(w->tid, NULL);
    }
    pthread_cond_destroy(&w->done_cv);
    pthread_mutex_destroy(&w->mu);
    free(w);
    *t = NULL;
    return 1;
}

static inline double plat_now_seconds(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}

static inline double plat_wall_seconds(void)
{
    return (double)time(NULL);
}

static inline void plat_sleep_ms(int ms)
{
    if (ms <= 0) return;
    struct timespec req;
    req.tv_sec = ms / 1000;
    req.tv_nsec = (long)(ms % 1000) * 1000000L;
    nanosleep(&req, NULL);
}

static inline uint64_t plat_rand_seed(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ((uint64_t)ts.tv_sec << 20) ^ (uint64_t)ts.tv_nsec ^ (uint64_t)getpid();
}

static inline void plat_secure_zero(void *ptr, size_t len)
{
    if (!ptr || !len) return;
    volatile unsigned char *p = (volatile unsigned char *)ptr;
    while (len--) *p++ = 0;
}

static inline int plat_random_bytes(uint8_t *buf, size_t len)
{
    int fd = open("/dev/urandom", O_RDONLY);
    if (fd < 0) return -1;
    size_t got = 0;
    while (got < len) {
        ssize_t n = read(fd, buf + got, len - got);
        if (n <= 0) {
            if (n < 0 && errno == EINTR) continue;
            close(fd);
            return -1;
        }
        got += (size_t)n;
    }
    close(fd);
    return 0;
}

typedef volatile int plat_atomic_t;
static inline long plat_atomic_load(plat_atomic_t *p)
{
    return (long)__atomic_load_n(p, __ATOMIC_SEQ_CST);
}
static inline void plat_atomic_store(plat_atomic_t *p, long v)
{
    __atomic_store_n(p, (int)v, __ATOMIC_SEQ_CST);
}
static inline long plat_atomic_xchg(plat_atomic_t *p, long v)
{
    return (long)__atomic_exchange_n(p, (int)v, __ATOMIC_SEQ_CST);
}

#endif /* _WIN32 */

#endif /* DRCOMD_PLATFORM_H */
