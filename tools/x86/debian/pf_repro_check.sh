#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Repro for the page-fault panic "might break atomic mode" (page_fault.rs:10).
#
# Hypothesis: a user page fault taken while the syscall holds spin locks
# (preempt_count > 0) enters the generic page-fault handler, which blocks on a
# sleepable mutex and trips the atomic-mode panic.
#
# The program sends messages whose source is a freshly mmap'ed, never touched
# anonymous page, so the very first touch happens inside the kernel copy while
# the socket write path holds its locks.
#
# Modes:
#   plain   one sender process, 1 MiB pages, 12,000 rounds
#   threads 4 sender threads, 256 KiB pages, 12,000 rounds each
#   press   the "threads" load plus a large resident allocation (heap pressure)
#
# Markers: pfr_* and DAILY_DONE=ok.

set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

log() { printf '%s\n' "$*" | tee -a /tmp/pf_repro.log >/dev/console; }

log "pfr_start $(date -u '+%Y-%m-%dT%H:%M:%SZ')"

cat >/tmp/pf_repro.c <<'EOF'
#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>

static int sv[2];
static volatile int stop;

static void *reader(void *arg) {
    char buf[65536];
    while (!stop) {
        ssize_t n = recv(sv[1], buf, sizeof buf, 0);
        if (n < 0 && errno != EAGAIN) break;
    }
    return NULL;
}

/* Holds the Vmar write lock for a long time, so a faulting thread that needs
   the Vmar read lock has to wait for it. */
static void *churn(void *arg) {
    const size_t big = 64UL << 20;
    while (!stop) {
        char *p = mmap(NULL, big, PROT_READ | PROT_WRITE,
                       MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (p == MAP_FAILED) break;
        for (size_t off = 0; off < big; off += 4096) p[off] = 1;
        munmap(p, big);
    }
    return NULL;
}

static void *sender(void *arg) {
    long rounds = (long)arg;
    const size_t len = 256 * 1024;
    for (long i = 0; i < rounds; i++) {
        char *m = mmap(NULL, len, PROT_READ | PROT_WRITE,
                       MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (m == MAP_FAILED) return (void *)1;
        struct iovec iov = {.iov_base = m, .iov_len = len};
        struct msghdr h = {.msg_iov = &iov, .msg_iovlen = 1};
        if (sendmsg(sv[0], &h, 0) < 0) {
            fprintf(stderr, "sendmsg: %s\n", strerror(errno));
            munmap(m, len);
            return (void *)1;
        }
        munmap(m, len);
    }
    return NULL;
}

int main(int argc, char **argv) {
    const char *mode = argc > 1 ? argv[1] : "plain";
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, sv) < 0) { perror("socketpair"); return 1; }

    pthread_t rd;
    pthread_create(&rd, NULL, reader, NULL);

    if (strcmp(mode, "plain") == 0) {
        const size_t len = 1 << 20;
        for (int i = 0; i < 12000; i++) {
            char *m = mmap(NULL, len, PROT_READ | PROT_WRITE,
                           MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
            if (m == MAP_FAILED) { perror("mmap"); break; }
            struct iovec iov = {.iov_base = m, .iov_len = len};
            struct msghdr h = {.msg_iov = &iov, .msg_iovlen = 1};
            if (sendmsg(sv[0], &h, 0) < 0) { perror("sendmsg"); munmap(m, len); break; }
            munmap(m, len);
        }
        printf("pfr_plain_done\n");
    } else {
        if (strcmp(mode, "press") == 0) {
            /* Heap/frame pressure so a fault-time allocation goes to the
               global pool instead of a per-CPU cache. */
            size_t big = 768UL << 20;
            char *p = mmap(NULL, big, PROT_READ | PROT_WRITE,
                           MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
            if (p != MAP_FAILED) {
                for (size_t off = 0; off < big; off += 4096) p[off] = 1;
                printf("pfr_pressure_ok %zu\n", big);
            }
        }
        if (strcmp(mode, "churn") == 0) {
            pthread_t ct;
            pthread_create(&ct, NULL, churn, NULL);
        }
        pthread_t th[4];
        for (int i = 0; i < 4; i++) pthread_create(&th[i], NULL, sender, (void *)12000L);
        for (int i = 0; i < 4; i++) pthread_join(th[i], NULL);
        printf("pfr_threads_done\n");
    }

    stop = 1;
    shutdown(sv[0], SHUT_WR);
    pthread_join(rd, NULL);
    printf("pfr_%s_complete\n", mode);
    return 0;
}
EOF

gcc -O2 -pthread -o /tmp/pf_repro /tmp/pf_repro.c 2>/tmp/pfr-gcc.err
rc=$?
log "pfr_build rc=$rc err=$(head -1 /tmp/pfr-gcc.err | head -c 120)"
if [ "$rc" -ne 0 ]; then
    echo DAILY_DONE=fail >/dev/console
    exit
fi

for mode in plain threads press churn; do
    log "pfr_mode=$mode start $(date -u '+%H:%M:%S')"
    timeout 300 /tmp/pf_repro "$mode" 2>&1 | while IFS= read -r line; do log "pfr_out $line"; done
    log "pfr_mode=$mode rc=$? end $(date -u '+%H:%M:%S')"
done

log "pfr_done"
echo DAILY_DONE=ok >/dev/console
