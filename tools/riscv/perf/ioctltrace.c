// SPDX-License-Identifier: MPL-2.0
//
// An `LD_PRELOAD` shim that records the DRM ioctls a process issues and the
// waits it performs, so a client that stops can be told apart from a server
// that will not answer.
//
// Why this exists rather than a probe that reimplements the calls: a probe can
// only ask the questions its author thought of, and it asks them on its own
// terms. It took three rounds to find that `drm_prime_handle` was twelve bytes
// and not sixteen, and the question that finally settled it — "what command
// number does a *real* client send?" — cannot be asked by anything except the
// client. This records what the process actually did, in the order it did it.
//
// `poll` is here for the same reason. A renderer that never appears looks
// identical whether the client is waiting on the X socket, on the DRM node, or
// on a descriptor that was never valid; the call that blocks names which.
//
// It deliberately does not interpose `dlsym`. An earlier shim did, to answer
// the same question, and crashed the X server: interposing `dlsym` breaks
// glibc's own internal symbol resolution. `ioctl` and `poll` are leaves by
// comparison.
//
// Output is one line per call, appended to `$ASTERINAS_IOCTLTRACE_OUT`. Writes
// go through `write(2)` on a preopened descriptor, so the tracing never
// allocates, never takes a lock the traced program might hold, and cannot
// deadlock the client it is measuring.
//
// Build (host cross toolchain):
//   riscv64-linux-gnu-gcc -O2 -shared -fPIC -o ioctltrace.so ioctltrace.c -ldl

#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

/* The DRM ioctl type byte, from `_IOC_TYPE`. Everything else is left alone:
 * a client makes far more non-DRM calls than DRM ones, and the question here
 * is only about the device. */
#define DRM_IOC_TYPE 'd'

/* A bounded line: enough for the directive and its outcome, short enough that
 * a partial write cannot interleave badly with another thread's. */
#define LINE_MAX 160

static int (*real_ioctl)(int, unsigned long, ...);
static int (*real_poll)(struct pollfd *, nfds_t, int);
static int (*real_futex)(int *, int, int, const struct timespec *, int *, int);
static ssize_t (*real_write)(int, const void *, size_t);
static ssize_t (*real_read)(int, void *, size_t);
static int (*real_epoll_wait)(int, struct epoll_event *, int, int);
static int (*real_epoll_ctl)(int, int, int, struct epoll_event *);
static ssize_t (*real_recvmsg)(int, struct msghdr *, int);
static int out_fd = -1;
static int tracing;

/* Appends one line, retrying nothing: a lost line is better than a shim that
 * blocks. Called with no locks held that the caller could also take.
 *
 * It calls the resolved `write` and not the symbol. Interposing `write` means
 * the symbol is this file's own function, so writing through it here would
 * re-enter `trace_io`, emit again, and recurse until the stack is gone — which
 * is how an earlier revision of this shim stopped the X server from starting
 * at all. */
static void emit(const char *line, size_t length)
{
    ssize_t written;

    if (!real_write || out_fd < 0)
        return;
    written = real_write(out_fd, line, length);
    (void)written;
}

static void emit_u64(char *at, size_t *used, unsigned long long value, int base)
{
    char digits[24];
    int count = 0;
    if (value == 0)
        digits[count++] = '0';
    while (value > 0 && count < (int)sizeof(digits)) {
        unsigned long long digit = value % (unsigned long long)base;
        digits[count++] = (char)(digit < 10 ? '0' + digit : 'a' + digit - 10);
        value /= (unsigned long long)base;
    }
    while (count > 0 && *used < LINE_MAX - 1)
        at[(*used)++] = digits[--count];
}

static size_t append(char *at, size_t used, const char *text)
{
    size_t length;

    /* Checked before the subtraction, not after: `LINE_MAX - 1 - used` wraps
     * to a huge value once `used` has passed the end, and the copy that follows
     * would run off the buffer. The compiler is what made this visible. */
    if (used >= LINE_MAX - 1)
        return used;
    length = strlen(text);
    if (length > LINE_MAX - 1 - used)
        length = LINE_MAX - 1 - used;
    memcpy(at + used, text, length);
    return used + length;
}

static size_t append_dec(char *at, size_t used, long long value)
{
    if (value < 0) {
        if (used < LINE_MAX - 1)
            at[used++] = '-';
        value = -value;
    }
    emit_u64(at, &used, (unsigned long long)value, 10);
    return used;
}

static size_t append_hex(char *at, size_t used, unsigned long long value)
{
    used = append(at, used, "0x");
    emit_u64(at, &used, value, 16);
    return used;
}

/* Writes `value` as decimal into `at`, returning the new length. Used to build
 * the `/proc/self/fd/` path without `snprintf`, so the shim stays free of
 * stdio's allocation and locking. */
static size_t append_number(char *at, size_t used, int value)
{
    char digits[16];
    int count = 0;
    unsigned magnitude = value < 0 ? (unsigned)-value : (unsigned)value;

    if (magnitude == 0)
        digits[count++] = '0';
    while (magnitude > 0 && count < (int)sizeof(digits)) {
        digits[count++] = (char)('0' + magnitude % 10);
        magnitude /= 10;
    }
    if (value < 0 && used < LINE_MAX - 1)
        at[used++] = '-';
    while (count > 0 && used < LINE_MAX - 1)
        at[used++] = digits[--count];
    return used;
}

/* Answers "which descriptor is this" with the name the kernel has for it, so a
 * reader does not have to guess from the number. `card0`, `renderD128` and a
 * socket all look alike as integers and mean entirely different things. */
static size_t append_fd_path(char *at, size_t used, int fd)
{
    char link[32];
    char target[96];
    size_t length = 0;
    ssize_t resolved;

    used = append_number(at, used, fd);

    length = append(link, length, "/proc/self/fd/");
    length = append_number(link, length, fd);
    link[length] = '\0';

    resolved = readlink(link, target, sizeof(target) - 1);
    if (resolved <= 0)
        return used;
    target[resolved] = '\0';

    used = append(at, used, "(");
    used = append(at, used, target);
    return append(at, used, ")");
}

/* Records one socket-level call with its result. These are what is left once
 * the driver calls stop: a server that has stopped touching the device is
 * either in its event loop or blocked writing to a client, and the two are
 * indistinguishable from `/proc`. */
static void trace_io(const char *name, int fd, long result, int saved_errno)
{
    char line[LINE_MAX];
    size_t used = 0;

    if (!tracing)
        return;
    used = append(line, used, name);
    used = append(line, used, " pid=");
    used = append_dec(line, used, (long long)getpid());
    used = append(line, used, " fd=");
    used = append_fd_path(line, used, fd);
    used = append(line, used, " ret=");
    used = append_dec(line, used, result);
    if (result < 0) {
        used = append(line, used, " errno=");
        used = append_dec(line, used, saved_errno);
    }
    used = append(line, used, "\n");
    emit(line, used);
}

ssize_t write(int fd, const void *buffer, size_t count)
{
    ssize_t result, saved;

    if (!real_write)
        real_write = dlsym(RTLD_NEXT, "write");
    if (!real_write) {
        errno = ENOSYS;
        return -1;
    }

    result = real_write(fd, buffer, count);
    saved = errno;
    /* The console is skipped: the log is the question, and every line of it
     * would otherwise generate another. */
    if (count > 0 && fd > STDERR_FILENO)
        trace_io("WRITE", fd, (long)result, saved);
    errno = saved;
    return result;
}

ssize_t read(int fd, void *buffer, size_t count)
{
    ssize_t result, saved;

    if (!real_read)
        real_read = dlsym(RTLD_NEXT, "read");
    if (!real_read) {
        errno = ENOSYS;
        return -1;
    }

    result = real_read(fd, buffer, count);
    saved = errno;
    trace_io("READ", fd, (long)result, saved);
    errno = saved;
    return result;
}

/* What a caller puts in `epoll_data` is opaque: Xorg stores a pointer of its
 * own, so reading the union as an integer yields garbage rather than the
 * descriptor. The descriptor is only recoverable from the `epoll_ctl` that
 * registered it, so the pair is remembered there and looked up here. A fixed
 * table, walked linearly: the sets involved are a handful of descriptors and
 * this must not allocate. */
struct epoll_registration {
    int epfd;
    void *tag;
    int fd;
};

#define EPOLL_REGISTRATIONS_MAX 256
static struct epoll_registration epoll_registrations[EPOLL_REGISTRATIONS_MAX];
static int epoll_registration_count;
static int epoll_overflows;

static void remember_registration(int epfd, void *tag, int fd)
{
    for (int index = 0; index < epoll_registration_count; index++) {
        if (epoll_registrations[index].epfd == epfd &&
            epoll_registrations[index].tag == tag) {
            epoll_registrations[index].fd = fd;
            return;
        }
    }
    if (epoll_registration_count < EPOLL_REGISTRATIONS_MAX) {
        epoll_registrations[epoll_registration_count].epfd = epfd;
        epoll_registrations[epoll_registration_count].tag = tag;
        epoll_registrations[epoll_registration_count].fd = fd;
        epoll_registration_count++;
    } else {
        epoll_overflows++;
    }
}

/* Returns the descriptor registered under `tag`, or a negative value when it
 * was never seen — which is itself worth reporting rather than printing a
 * number that reads like a descriptor. */
static int registration_fd(int epfd, void *tag)
{
    for (int index = 0; index < epoll_registration_count; index++) {
        if (epoll_registrations[index].epfd == epfd &&
            epoll_registrations[index].tag == tag) {
            return epoll_registrations[index].fd;
        }
    }
    return -1;
}

int epoll_ctl(int epfd, int operation, int fd, struct epoll_event *event)
{
    int result, saved;

    if (!real_epoll_ctl)
        real_epoll_ctl = dlsym(RTLD_NEXT, "epoll_ctl");
    if (!real_epoll_ctl) {
        errno = ENOSYS;
        return -1;
    }

    result = real_epoll_ctl(epfd, operation, fd, event);
    saved = errno;

    if (tracing && result == 0 && event && operation != EPOLL_CTL_DEL) {
        char line[LINE_MAX];
        size_t used = 0;
        used = append(line, used, "EPOLL_CTL pid=");
        used = append_dec(line, used, (long long)getpid());
        used = append(line, used, " fd=");
        used = append_fd_path(line, used, fd);
        used = append(line, used, " events=");
        used = append_dec(line, used, (long long)event->events);
        used = append(line, used, "\n");
        emit(line, used);
        remember_registration(epfd, event->data.ptr, fd);
    }

    errno = saved;
    return result;
}

/* The server's event loop. A server sitting here with a long timeout is idle
 * and would answer a client the moment one arrived; a server that never
 * reaches here is stuck somewhere the socket trace will name. */
int epoll_wait(int epfd, struct epoll_event *events, int maxevents, int timeout)
{
    int result, saved;

    if (!real_epoll_wait)
        real_epoll_wait = dlsym(RTLD_NEXT, "epoll_wait");
    if (!real_epoll_wait) {
        errno = ENOSYS;
        return -1;
    }

    if (tracing) {
        char line[LINE_MAX];
        size_t used = 0;
        used = append(line, used, "EPOLL_WAIT pid=");
        used = append_dec(line, used, (long long)getpid());
        used = append(line, used, " timeout=");
        used = append_dec(line, used, timeout);
        used = append(line, used, "\n");
        emit(line, used);
    }

    result = real_epoll_wait(epfd, events, maxevents, timeout);
    saved = errno;

    if (tracing) {
        char line[LINE_MAX];
        size_t used = 0;
        used = append(line, used, "EPOLL_DONE pid=");
        used = append_dec(line, used, (long long)getpid());
        used = append(line, used, " ret=");
        used = append_dec(line, used, result);
        /* *Which* descriptor is ready, not just how many. A wait that keeps
         * returning without the server doing anything is a descriptor that
         * reports ready and never has anything to give; the count alone cannot
         * name it, and naming it is the whole question. The caller stores the
         * descriptor it is interested in `data.fd`, which is what `epoll` hands
         * back untouched. */
        for (int index = 0; index < result && index < 4 && used < LINE_MAX - 60;
             index++) {
            int fd = registration_fd(epfd, events[index].data.ptr);
            used = append(line, used, " rdy=");
            if (fd >= 0)
                used = append_fd_path(line, used, fd);
            else
                used = append(line, used, "unregistered");
            used = append(line, used, ":");
            used = append_dec(line, used, (long long)events[index].events);
        }
        used = append(line, used, "\n");
        emit(line, used);
    }

    errno = saved;
    return result;
}

/* The X server exchanges descriptors with its clients through `recvmsg`, which
 * is why a socket-level trace that stops at `read` shows a server apparently
 * ignoring data a client sent. The result and the amount of control data
 * received are what say whether the message was taken. */
ssize_t recvmsg(int sockfd, struct msghdr *msg, int flags)
{
    ssize_t result, saved;
    size_t control_len = msg ? msg->msg_controllen : 0;

    if (!real_recvmsg)
        real_recvmsg = dlsym(RTLD_NEXT, "recvmsg");
    if (!real_recvmsg) {
        errno = ENOSYS;
        return -1;
    }

    result = real_recvmsg(sockfd, msg, flags);
    saved = errno;

    if (tracing) {
        char line[LINE_MAX];
        size_t used = 0;
        used = append(line, used, "RECVMSG pid=");
        used = append_dec(line, used, (long long)getpid());
        used = append(line, used, " fd=");
        used = append_fd_path(line, used, sockfd);
        used = append(line, used, " ret=");
        used = append_dec(line, used, (long long)result);
        used = append(line, used, " ctrl=");
        used = append_dec(line, used, (long long)msg->msg_controllen);
        if (result < 0) {
            used = append(line, used, " errno=");
            used = append_dec(line, used, saved);
        }
        used = append(line, used, "\n");
        emit(line, used);
        (void)control_len;
    }

    errno = saved;
    return result;
}

__attribute__((constructor)) static void start_tracing(void)
{
    const char *path = getenv("ASTERINAS_IOCTLTRACE_OUT");

    real_ioctl = dlsym(RTLD_NEXT, "ioctl");
    real_poll = dlsym(RTLD_NEXT, "poll");
    /* `emit` needs this before the first traced call, so it is resolved here
     * rather than lazily inside `write`. */
    real_write = dlsym(RTLD_NEXT, "write");
    if (!path || !real_ioctl || !real_write)
        return;
    out_fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0644);
    tracing = out_fd >= 0;
}

/* Waiting on a lock and waiting on a socket look the same from `/proc`: both
 * are `S`. Which call blocks is the whole difference between "the server is
 * deadlocked" and "the server is waiting for a client", so `futex` is traced
 * alongside them. */
int futex(int *uaddr, int operation, int value, const struct timespec *timeout,
          int *uaddr2, int value3)
{
    int result, saved;

    if (!real_futex)
        real_futex = dlsym(RTLD_NEXT, "futex");

    if (tracing) {
        char line[LINE_MAX];
        size_t used = 0;
        used = append(line, used, "FUTEX pid=");
        used = append_dec(line, used, (long long)getpid());
        used = append(line, used, " op=");
        used = append_hex(line, used, (unsigned long long)operation);
        used = append(line, used, " uaddr=");
        used = append_hex(line, used, (unsigned long long)(uintptr_t)uaddr);
        used = append(line, used, "\n");
        emit(line, used);
    }

    if (!real_futex) {
        errno = ENOSYS;
        return -1;
    }
    result = real_futex(uaddr, operation, value, timeout, uaddr2, value3);
    saved = errno;

    if (tracing) {
        char line[LINE_MAX];
        size_t used = 0;
        used = append(line, used, "FUTEX-DONE pid=");
        used = append_dec(line, used, (long long)getpid());
        used = append(line, used, " ret=");
        used = append_dec(line, used, result);
        used = append(line, used, "\n");
        emit(line, used);
    }

    errno = saved;
    return result;
}

int ioctl(int fd, unsigned long request, ...)
{
    va_list arguments;
    void *argument;
    int result, saved;

    va_start(arguments, request);
    argument = va_arg(arguments, void *);
    va_end(arguments);

    if (!real_ioctl)
        real_ioctl = dlsym(RTLD_NEXT, "ioctl");
    if (!real_ioctl) {
        errno = ENOSYS;
        return -1;
    }

    result = real_ioctl(fd, request, argument);
    saved = errno;

    if (tracing && ((request >> 8) & 0xff) == DRM_IOC_TYPE) {
        char line[LINE_MAX];
        size_t used = 0;
        used = append(line, used, "IOCTL pid=");
        used = append_dec(line, used, (long long)getpid());
        used = append(line, used, " fd=");
        used = append_fd_path(line, used, fd);
        used = append(line, used, " cmd=");
        used = append_hex(line, used, request);
        used = append(line, used, " ret=");
        used = append_dec(line, used, result);
        if (result < 0) {
            used = append(line, used, " errno=");
            used = append_dec(line, used, saved);
        }
        used = append(line, used, "\n");
        emit(line, used);
    }

    errno = saved;
    return result;
}

int poll(struct pollfd *fds, nfds_t count, int timeout)
{
    if (!real_poll)
        real_poll = dlsym(RTLD_NEXT, "poll");
    if (!real_poll) {
        errno = ENOSYS;
        return -1;
    }

    if (tracing) {
        char line[LINE_MAX];
        size_t used = 0;
        used = append(line, used, "POLL pid=");
        used = append_dec(line, used, (long long)getpid());
        used = append(line, used, " nfds=");
        used = append_dec(line, used, (long long)count);
        used = append(line, used, " timeout=");
        used = append_dec(line, used, timeout);
        /* Only the first few: the answer is "which device", and a client with
         * a wide descriptor set would otherwise push it off the end of the
         * line. */
        for (nfds_t index = 0; index < count && index < 4 && used < LINE_MAX - 40;
             index++) {
            used = append(line, used, " w=");
            used = append_fd_path(line, used, fds[index].fd);
            used = append(line, used, ":");
            used = append_dec(line, used, fds[index].events);
        }
        used = append(line, used, "\n");
        emit(line, used);
    }

    return real_poll(fds, count, timeout);
}
