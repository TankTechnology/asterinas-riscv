// SPDX-License-Identifier: MPL-2.0

// Diagnostic only: preload into the RISC-V lat_rpc client to retain its final
// sendto, poll and recvfrom calls in /opt/rpc-trace-<pid>.log. Each process
// writes at most 256 events when it exits. This is not a benchmark component.

#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>
#include <fcntl.h>

#define TRACE_RING 4096
#define TRACE_DUMP 256

struct trace_event {
    unsigned long long seq;
    unsigned long long ns;
    char op;
    int fd;
    int rc;
    int arg;
    unsigned int xid;
    int revents;
    unsigned long long duration_ns;
};

static struct trace_event trace_ring[TRACE_RING];
static unsigned long long trace_next;

static unsigned long long monotonic_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (unsigned long long)ts.tv_sec * 1000000000ULL + (unsigned long long)ts.tv_nsec;
}

static unsigned int xid_of(const void *buffer, size_t length) {
    if (length < 4 || buffer == NULL) return 0;
    unsigned int xid;
    __builtin_memcpy(&xid, buffer, sizeof(xid));
    return ntohl(xid);
}

static void record(char op, int fd, int rc, int arg, unsigned int xid,
                   int revents, unsigned long long duration_ns) {
    unsigned long long seq = __sync_fetch_and_add(&trace_next, 1);
    struct trace_event *event = &trace_ring[seq % TRACE_RING];
    *event = (struct trace_event){seq, monotonic_ns(), op, fd, rc, arg,
                                  xid, revents, duration_ns};
}

ssize_t sendto(int fd, const void *buffer, size_t length, int flags,
               const struct sockaddr *address, socklen_t address_length) {
    static ssize_t (*real_sendto)(int, const void *, size_t, int,
                                  const struct sockaddr *, socklen_t);
    if (!real_sendto) real_sendto = dlsym(RTLD_NEXT, "sendto");
    unsigned long long start = monotonic_ns();
    ssize_t result = real_sendto(fd, buffer, length, flags, address, address_length);
    int saved_errno = errno;
    record('S', fd, (int)result, saved_errno, xid_of(buffer, length), 0,
           monotonic_ns() - start);
    errno = saved_errno;
    return result;
}

ssize_t recvfrom(int fd, void *buffer, size_t length, int flags,
                 struct sockaddr *address, socklen_t *address_length) {
    static ssize_t (*real_recvfrom)(int, void *, size_t, int,
                                    struct sockaddr *, socklen_t *);
    if (!real_recvfrom) real_recvfrom = dlsym(RTLD_NEXT, "recvfrom");
    unsigned long long start = monotonic_ns();
    ssize_t result = real_recvfrom(fd, buffer, length, flags, address, address_length);
    int saved_errno = errno;
    record('R', fd, (int)result, saved_errno,
           xid_of(buffer, result >= 4 ? (size_t)result : 0), 0,
           monotonic_ns() - start);
    errno = saved_errno;
    return result;
}

int poll(struct pollfd *fds, nfds_t nfds, int timeout) {
    static int (*real_poll)(struct pollfd *, nfds_t, int);
    if (!real_poll) real_poll = dlsym(RTLD_NEXT, "poll");
    unsigned long long start = monotonic_ns();
    int result = real_poll(fds, nfds, timeout);
    int saved_errno = errno;
    record('P', nfds ? fds[0].fd : -1, result, timeout, 0,
           nfds ? fds[0].revents : 0, monotonic_ns() - start);
    errno = saved_errno;
    return result;
}

__attribute__((destructor)) static void dump_trace(void) {
    char path[128];
    snprintf(path, sizeof(path), "/opt/rpc-trace-%ld.log", (long)getpid());
    int fd = open(path, O_CREAT | O_WRONLY | O_TRUNC, 0600);
    if (fd < 0) return;
    unsigned long long end = trace_next;
    unsigned long long begin = end > TRACE_DUMP ? end - TRACE_DUMP : 0;
    dprintf(fd, "pid=%ld total_events=%llu retained=%llu\n",
            (long)getpid(), end, end - begin);
    for (unsigned long long seq = begin; seq < end; ++seq) {
        const struct trace_event *event = &trace_ring[seq % TRACE_RING];
        dprintf(fd, "%llu %llu %c fd=%d rc=%d arg=%d xid=%u revents=%d duration_ns=%llu\n",
                event->seq, event->ns, event->op, event->fd, event->rc,
                event->arg, event->xid, event->revents, event->duration_ns);
    }
    close(fd);
}
