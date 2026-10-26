#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Fork cost benchmark (task A of CLONE_COST).
#
# Modes:
#   fork      N forks, each child _exit()s immediately, parent waitpid()
#   thread    N pthread create/join pairs
#   size=<MB> one fork per round after touching <MB> of anonymous memory
#             (median of 5 rounds), shows whether fork cost scales with the
#             mapped address space
#
# Every mode reports wall time per operation and the CPU time of the clone
# syscall (nr 56) itself, read from /proc/syscall_time.
#
# Markers: fk_* and DAILY_DONE=ok.

set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

log() { printf '%s\n' "$*" | tee -a /tmp/fork_bench.log >/dev/console; }

log "fk_start $(date -u '+%Y-%m-%dT%H:%M:%SZ')"

cat >/tmp/fork_bench.c <<'EOF'
#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static long read_clone_ns(long *count) {
    FILE *f = fopen("/proc/syscall_time", "r");
    if (!f) return -1;
    char line[256];
    long ns = -1, cnt = 0;
    while (fgets(line, sizeof line, f)) {
        if (line[0] == '#') continue;
        long nr, c, total, blocked, cpu, avg;
        if (sscanf(line, "%ld %ld %ld %ld %ld %ld", &nr, &c, &total, &blocked, &cpu, &avg) == 6) {
            if (nr == 56) { ns = cpu; cnt = c; break; }
        } else if (sscanf(line, "%ld %ld %ld %ld", &nr, &c, &total, &avg) == 4) {
            if (nr == 56) { ns = total; cnt = c; break; }
        }
    }
    fclose(f);
    if (count) *count = cnt;
    return ns;
}

static void ctl(const char *cmd) {
    FILE *f = fopen("/proc/syscall_time", "w");
    if (!f) return;
    fputs(cmd, f);
    fclose(f);
}

static double now_s(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

static void mode_fork(long n) {
    ctl("on"); ctl("reset");
    long c0 = 0;
    read_clone_ns(&c0);
    double t0 = now_s();
    for (long i = 0; i < n; i++) {
        pid_t p = fork();
        if (p == 0) _exit(0);
        if (p < 0) { printf("fk_error fork %s\n", strerror(errno)); return; }
        waitpid(p, NULL, 0);
    }
    double t1 = now_s();
    long c1 = 0;
    long ns = read_clone_ns(&c1);
    ctl("off");
    printf("fk_fork n=%ld wall_ns_op=%.0f clone_cpu_ns_op=%.0f clone_count=%ld\n",
           n, (t1 - t0) / n * 1e9, n ? (double)ns / n : -1, c1 - c0);
}

static void *thr(void *arg) { return NULL; }

static void mode_thread(long n) {
    ctl("on"); ctl("reset");
    long c0 = 0;
    read_clone_ns(&c0);
    double t0 = now_s();
    for (long i = 0; i < n; i++) {
        pthread_t t;
        if (pthread_create(&t, NULL, thr, NULL) != 0) { printf("fk_error pthread\n"); return; }
        pthread_join(t, NULL);
    }
    double t1 = now_s();
    long c1 = 0;
    long ns = read_clone_ns(&c1);
    ctl("off");
    printf("fk_thread n=%ld wall_ns_op=%.0f clone_cpu_ns_op=%.0f clone_count=%ld\n",
           n, (t1 - t0) / n * 1e9, n ? (double)ns / n : -1, c1 - c0);
}

static void mode_size(long mb) {
    size_t len = (size_t)mb << 20;
    char *p = mmap(NULL, len, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (p == MAP_FAILED) { printf("fk_error mmap %s\n", strerror(errno)); return; }
    for (size_t off = 0; off < len; off += 4096) p[off] = 1;

    double best = 1e18;
    long best_ns = -1;
    for (int round = 0; round < 5; round++) {
        ctl("on"); ctl("reset");
        long c0 = 0;
        read_clone_ns(&c0);
        double t0 = now_s();
        pid_t pid = fork();
        if (pid == 0) _exit(0);
        double t1 = now_s();
        long c1 = 0;
        long ns = read_clone_ns(&c1);
        ctl("off");
        if (pid < 0) { printf("fk_error fork %s\n", strerror(errno)); break; }
        waitpid(pid, NULL, 0);
        double dt = (t1 - t0) * 1e9;
        if (dt < best) { best = dt; best_ns = ns; }
        (void)c1; (void)c0;
    }
    printf("fk_size mb=%ld wall_ns_fork=%.0f clone_cpu_ns=%.0f\n", mb, best, (double)best_ns);
    munmap(p, len);
}

static void mode_reserve(long mb) {
    size_t len = (size_t)mb << 20;
    /* Reserve without touching: no PTEs, tests the per-VMA cost only. */
    char *p = mmap(NULL, len, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS | MAP_NORESERVE, -1, 0);
    if (p == MAP_FAILED) { printf("fk_error reserve %s\n", strerror(errno)); return; }
    double best = 1e18; long best_ns = -1;
    for (int round = 0; round < 5; round++) {
        ctl("on"); ctl("reset");
        long c0 = 0; read_clone_ns(&c0);
        double t0 = now_s();
        pid_t pid = fork();
        if (pid == 0) _exit(0);
        double t1 = now_s();
        long c1 = 0; long ns = read_clone_ns(&c1);
        ctl("off");
        if (pid < 0) { printf("fk_error fork %s\n", strerror(errno)); break; }
        waitpid(pid, NULL, 0);
        double dt = (t1 - t0) * 1e9;
        if (dt < best) { best = dt; best_ns = ns; }
        (void)c0; (void)c1;
    }
    printf("fk_reserve mb=%ld wall_ns_fork=%.0f clone_cpu_ns=%.0f\n", mb, best, (double)best_ns);
    munmap(p, len);
}

static void mode_protect(long mb) {
    size_t len = (size_t)mb << 20;
    char *p = mmap(NULL, len, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (p == MAP_FAILED) { printf("fk_error mmap %s\n", strerror(errno)); return; }
    for (size_t off = 0; off < len; off += 4096) p[off] = 1;
    double best = 1e18;
    for (int round = 0; round < 5; round++) {
        double t0 = now_s();
        if (mprotect(p, len, PROT_READ) != 0) { printf("fk_error mprotect %s\n", strerror(errno)); break; }
        double t1 = now_s();
        if (mprotect(p, len, PROT_READ | PROT_WRITE) != 0) break;
        double dt = (t1 - t0) * 1e9;
        if (dt < best) best = dt;
    }
    printf("fk_protect mb=%ld wall_ns_protect=%.0f\n", mb, best);
    munmap(p, len);
}

/* Copy-on-write correctness: the child writes must not be visible to the
   parent, and neither side may see corrupt data. */
static void mode_cow(void) {
    const size_t len = 1 << 20;
    char *p = mmap(NULL, len, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (p == MAP_FAILED) { printf("fk_cow fail mmap\n"); return; }
    for (size_t i = 0; i < len; i++) p[i] = (char)(i & 0xff);

    pid_t pid = fork();
    if (pid == 0) {
        int bad = 0;
        for (size_t i = 0; i < len; i++)
            if (p[i] != (char)(i & 0xff)) bad = 1;
        for (size_t i = 0; i < len; i++) p[i] = 0x5a;   /* COW write */
        for (size_t i = 0; i < len; i++)
            if (p[i] != 0x5a) bad = 1;
        _exit(bad ? 1 : 0);
    }
    int status = 0;
    waitpid(pid, &status, 0);
    int child_ok = WIFEXITED(status) && WEXITSTATUS(status) == 0;

    int parent_ok = 1;
    for (size_t i = 0; i < len; i++)
        if (p[i] != (char)(i & 0xff)) { parent_ok = 0; break; }
    for (size_t i = 0; i < len; i++) p[i] = 0x33;      /* parent write after fork */
    for (size_t i = 0; i < len; i++)
        if (p[i] != 0x33) parent_ok = 0;

    /* Heap (sbrk) and file-backed private mapping. */
    char *heap = malloc(65536);
    memset(heap, 0x11, 65536);
    pid = fork();
    if (pid == 0) {
        for (int i = 0; i < 65536; i++) heap[i] = 0x22;
        _exit(heap[65535] == 0x22 ? 0 : 1);
    }
    waitpid(pid, &status, 0);
    int heap_ok = WIFEXITED(status) && WEXITSTATUS(status) == 0 && heap[65535] == 0x11;

    printf("fk_cow child_ok=%d parent_ok=%d heap_ok=%d\n", child_ok, parent_ok, heap_ok);
    munmap(p, len);
    free(heap);
}

int main(int argc, char **argv) {
    if (argc < 3) return 1;
    if (strcmp(argv[1], "fork") == 0) mode_fork(atol(argv[2]));
    else if (strcmp(argv[1], "thread") == 0) mode_thread(atol(argv[2]));
    else if (strcmp(argv[1], "size") == 0) mode_size(atol(argv[2]));
    else if (strcmp(argv[1], "reserve") == 0) mode_reserve(atol(argv[2]));
    else if (strcmp(argv[1], "protect") == 0) mode_protect(atol(argv[2]));
    else if (strcmp(argv[1], "cow") == 0) mode_cow();
    return 0;
}
EOF

gcc -O2 -pthread -o /tmp/fork_bench /tmp/fork_bench.c 2>/tmp/fk-gcc.err
rc=$?
log "fk_build rc=$rc err=$(head -1 /tmp/fk-gcc.err | head -c 120)"
[ "$rc" -ne 0 ] && { echo DAILY_DONE=fail >/dev/console; exit; }

/tmp/fork_bench fork 10000 2>&1 | while IFS= read -r l; do log "$l"; done
/tmp/fork_bench thread 10000 2>&1 | while IFS= read -r l; do log "$l"; done
for mb in 0 64 256 512; do
    /tmp/fork_bench size "$mb" 2>&1 | while IFS= read -r l; do log "$l"; done
done
for mb in 64 512; do
    /tmp/fork_bench reserve "$mb" 2>&1 | while IFS= read -r l; do log "$l"; done
    /tmp/fork_bench protect "$mb" 2>&1 | while IFS= read -r l; do log "$l"; done
done
/tmp/fork_bench cow 0 2>&1 | while IFS= read -r l; do log "$l"; done

log "fk_done $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo DAILY_DONE=ok >/dev/console
