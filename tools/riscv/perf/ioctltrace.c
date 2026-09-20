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
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
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
static int out_fd = -1;
static int tracing;

/* Appends one line, retrying nothing: a lost line is better than a shim that
 * blocks. Called with no locks held that the caller could also take. */
static void emit(const char *line, size_t length)
{
    ssize_t written = write(out_fd, line, length);
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

__attribute__((constructor)) static void start_tracing(void)
{
    const char *path = getenv("ASTERINAS_IOCTLTRACE_OUT");

    real_ioctl = dlsym(RTLD_NEXT, "ioctl");
    real_poll = dlsym(RTLD_NEXT, "poll");
    if (!path || !real_ioctl)
        return;
    out_fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0644);
    tracing = out_fd >= 0;
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
