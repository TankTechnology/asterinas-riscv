// SPDX-License-Identifier: MPL-2.0
//
// Fork cost attribution benchmark (FORK_42X round).
//
// Modes are both run in one pass:
//   * RSS sweep: map and TOUCH 1/32/128/256 MB, then time fork+exit N times
//     (child only _exit()s, it never touches a page) and report the median.
//     If the cost tracks RSS, the address-space/PTE copy is the cause; if it
//     is flat, the fixed process-creation path is.
//   * Variants at RSS 0 and 256 MB: fork, clone(CLONE_VM|SIGCHLD) (no address
//     space copy) and vfork.
//
// Output is machine readable:
//   RB info rss_mb <n> actual_mb <m>
//   RB rss_mb <n> fork_us <median>
//   RB variant <name> us <median>            (RSS 0)
//   RB variant256 <name> us <median>         (RSS 256 MB)
//
// N defaults to 200 (override with SB_N) and every reported value is the
// median of N measured fork+waitpid pairs; 5 warm-up forks are discarded.

#define _GNU_SOURCE
#include <errno.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#ifndef CLONE_VM
#define CLONE_VM 0x00000100
#endif
#ifndef CLONE_SIGHAND
#define CLONE_SIGHAND 0x00000800
#endif

static int g_n = 200;

static double now_ns(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (double)t.tv_sec * 1e9 + (double)t.tv_nsec;
}

static int cmp_double(const void *a, const void *b)
{
    double x = *(const double *)a, y = *(const double *)b;
    return (x > y) - (x < y);
}

static double median(double *v, int n)
{
    qsort(v, (size_t)n, sizeof(*v), cmp_double);
    if (n % 2)
        return v[n / 2];
    return (v[n / 2 - 1] + v[n / 2]) / 2.0;
}

static double current_rss_mb(void)
{
    FILE *f = fopen("/proc/self/status", "r");
    char line[256];
    double kb = -1;
    if (!f)
        return -1;
    while (fgets(line, sizeof(line), f)) {
        if (sscanf(line, "VmRSS: %lf", &kb) == 1)
            break;
    }
    fclose(f);
    return kb / 1024.0;
}

static char *map_and_touch(size_t bytes)
{
    char *p = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
                   MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (p == MAP_FAILED)
        return NULL;
    /* Keep the Linux control side from collapsing 512 PTEs into one THP. */
    (void)madvise(p, bytes, MADV_NOHUGEPAGE);
    for (size_t off = 0; off < bytes; off += 4096)
        p[off] = 1;
    return p;
}

static int child_fn(void *arg)
{
    (void)arg;
    _exit(0);
}

static double bench_fork(void)
{
    double t0 = now_ns();
    pid_t p = fork();
    if (p == 0)
        _exit(0);
    int st;
    if (p > 0)
        waitpid(p, &st, 0);
    return (now_ns() - t0) / 1000.0;
}

static double bench_clone_vm(void *stack_top)
{
    double t0 = now_ns();
    pid_t p = clone(child_fn, stack_top, CLONE_VM | CLONE_SIGHAND | SIGCHLD, NULL);
    if (p < 0)
        return -1;
    int st;
    waitpid(p, &st, 0);
    return (now_ns() - t0) / 1000.0;
}

static double bench_vfork(void)
{
    double t0 = now_ns();
    pid_t p = vfork();
    if (p == 0)
        _exit(0);
    int st;
    if (p > 0)
        waitpid(p, &st, 0);
    return (now_ns() - t0) / 1000.0;
}

enum kind { K_FORK, K_CLONE_VM, K_VFORK };

static void measure(const char *label, int variant, void *stack_top, int n)
{
    double *v = malloc(sizeof(double) * (size_t)n);
    if (!v) {
        printf("RB error %s oom\n", label);
        return;
    }
    for (int i = 0; i < 5; i++) {
        if (variant == K_FORK)
            (void)bench_fork();
        else if (variant == K_CLONE_VM)
            (void)bench_clone_vm(stack_top);
        else
            (void)bench_vfork();
    }
    for (int i = 0; i < n; i++) {
        if (variant == K_FORK)
            v[i] = bench_fork();
        else if (variant == K_CLONE_VM)
            v[i] = bench_clone_vm(stack_top);
        else
            v[i] = bench_vfork();
    }
    printf("RB %s us %.1f\n", label, median(v, n));
    free(v);
}

static void sweep_one(int mb)
{
    size_t bytes = (size_t)mb << 20;
    char *map = map_and_touch(bytes);
    if (!map) {
        printf("RB rss_mb %d mmap_failed errno=%d\n", mb, errno);
        return;
    }
    printf("RB info rss_mb %d actual_mb %.1f\n", mb, current_rss_mb());
    double *v = malloc(sizeof(double) * (size_t)g_n);
    for (int i = 0; i < 5; i++)
        (void)bench_fork();
    for (int i = 0; i < g_n; i++)
        v[i] = bench_fork();
    printf("RB rss_mb %d fork_us %.1f\n", mb, median(v, g_n));
    free(v);
    munmap(map, bytes);
}

int main(void)
{
    const char *n_env = getenv("SB_N");
    if (n_env && atoi(n_env) > 0)
        g_n = atoi(n_env);
    printf("SB2_START n=%d\n", g_n);

    /* Single-size mode used for the /proc/syscall_time phase attribution: the
     * clone counter then covers exactly g_n fork+exit calls at one size. */
    const char *only = getenv("SB_ONLY");
    if (only && strcmp(only, "rss256") == 0) {
        sweep_one(256);
        printf("RB DONE\n");
        return 0;
    }

    /* ---- RSS sweep: fork+exit, child never touches a page (also Task D) ---- */
    static const int sizes_mb[] = { 1, 32, 128, 256 };
    for (unsigned k = 0; k < sizeof(sizes_mb) / sizeof(sizes_mb[0]); k++)
        sweep_one(sizes_mb[k]);

    /* ---- Variants: no mapping, then 256 MB of touched anonymous memory ---- */
    void *stack = mmap(NULL, 1 << 20, PROT_READ | PROT_WRITE,
                       MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    char *stack_top = (stack == MAP_FAILED) ? NULL
                                            : (char *)stack + (1 << 20);

    measure("variant fork", K_FORK, NULL, g_n);
    if (stack_top)
        measure("variant clone_vm", K_CLONE_VM, stack_top, g_n);
    else
        printf("RB variant clone_vm us -1 (stack mmap failed)\n");
    measure("variant vfork", K_VFORK, NULL, g_n);

    size_t big = (size_t)256 << 20;
    char *map = map_and_touch(big);
    if (map) {
        printf("RB info rss_mb 256 variant actual_mb %.1f\n", current_rss_mb());
        measure("variant256 fork", K_FORK, NULL, g_n);
        if (stack_top)
            measure("variant256 clone_vm", K_CLONE_VM, stack_top, g_n);
        measure("variant256 vfork", K_VFORK, NULL, g_n);
        munmap(map, big);
    } else {
        printf("RB variant256 mmap_failed errno=%d\n", errno);
    }
    if (stack != MAP_FAILED)
        munmap(stack, 1 << 20);

    printf("RB DONE\n");
    return 0;
}
