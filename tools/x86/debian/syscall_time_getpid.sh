#!/bin/sh
# Self-contained correctness/overhead check for /proc/syscall_time.
set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
log() { printf '%s\n' "$*" >/dev/console; }
log "stg_start $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
cat >/tmp/getpid_bench.c <<'C'
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static inline uint64_t rdtsc(void) {
    unsigned int lo, hi;
    __asm__ volatile("rdtsc" : "=a"(lo), "=d"(hi));
    return ((uint64_t)hi << 32) | lo;
}
static void ctl(const char *s) {
    int fd = open("/proc/syscall_time", O_WRONLY);
    if (fd < 0) { perror("open ctl"); exit(1); }
    if (write(fd, s, strlen(s)) < 0) { perror("write ctl"); exit(1); }
    close(fd);
}
static void run_loop(void) {
    volatile pid_t p = 0;
    for (long i = 0; i < 1000000; i++) p = getpid();
    (void)p;
}
int main(void) {
    ctl("off");
    uint64_t t0 = rdtsc(); run_loop(); uint64_t t1 = rdtsc();
    ctl("on"); ctl("reset");
    uint64_t t2 = rdtsc(); run_loop(); uint64_t t3 = rdtsc();

    int fd = open("/proc/syscall_time", O_RDONLY);
    static char buf[65536];
    ssize_t n = read(fd, buf, sizeof(buf) - 1);
    close(fd);
    if (n <= 0) { printf("read failed\n"); return 1; }
    buf[n] = 0;

    unsigned long long total = 0, gp_total = 0, gp_count = 0;
    char *line = strtok(buf, "\n");
    while (line) {
        if (line[0] != '#') {
            unsigned long long nr, count, ns, avg;
            if (sscanf(line, "%llu %llu %llu %llu", &nr, &count, &ns, &avg) == 4) {
                total += ns;
                if (nr == 39) { gp_total = ns; gp_count = count; }
            }
        }
        line = strtok(NULL, "\n");
    }
    double overhead = 100.0 * ((double)(t3 - t2) - (double)(t1 - t0)) / (double)(t1 - t0);
    printf("stg_getpid count=%llu share=%.1f total_ns=%llu off_cycles=%llu on_cycles=%llu overhead_pct=%.2f\n",
           gp_count, total ? 100.0 * (double)gp_total / (double)total : 0.0,
           total, (unsigned long long)(t1 - t0), (unsigned long long)(t3 - t2), overhead);
    return 0;
}
C
gcc -O2 -o /tmp/getpid_bench /tmp/getpid_bench.c 2>/tmp/stg-gcc.err
log "stg_build rc=$? err=$(head -1 /tmp/stg-gcc.err | head -c 120)"
/tmp/getpid_bench 2>&1 | while IFS= read -r line; do log "$line"; done
log "stg_done $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo DAILY_DONE=ok >/dev/console
