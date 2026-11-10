// SPDX-License-Identifier: MPL-2.0
//
// Measures process creation one step at a time: fork alone, fork+exec,
// posix_spawn, vfork+exec, and vfork without exec. Options:
//
//   spawnbench <mode> [n] [parent_mem_mb] [vmas] [target]
//
// `parent_mem_mb` touches that much anonymous memory before the loop and
// `vmas` creates that many 4 KiB mappings, so the page-table copy in fork
// can be separated from the clone and exec costs. The child side CPU time
// and page faults come from getrusage(RUSAGE_CHILDREN), which also checks
// the kernel's children accounting.
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <sys/resource.h>
#include <time.h>
#include <unistd.h>

extern char **environ;

static double now(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

static void show_schedstat(const char *tag) {
    char buf[128];
    int fd = open("/proc/self/schedstat", O_RDONLY);
    if (fd < 0) return;
    ssize_t n = read(fd, buf, sizeof(buf) - 1);
    close(fd);
    if (n <= 0) return;
    buf[n] = 0;
    printf("  schedstat %s: %s", tag, buf);
}

static const char *target = "/bin/true";

static int once(const char *mode) {
    pid_t pid;
    if (strcmp(mode, "fork") == 0 || strcmp(mode, "forke") == 0) {
        pid = fork();
        if (pid < 0) return -1;
        if (pid == 0) {
            if (mode[4] == 'e') {
                execl(target, target, (char *)NULL);
                _exit(127);
            }
            _exit(0);
        }
    } else if (strcmp(mode, "vfork") == 0) {
        pid = vfork();
        if (pid < 0) return -1;
        if (pid == 0) _exit(0);
    } else if (strcmp(mode, "vforke") == 0) {
        pid = vfork();
        if (pid < 0) return -1;
        if (pid == 0) {
            execl(target, target, (char *)NULL);
            _exit(127);
        }
    } else if (strcmp(mode, "spawn") == 0) {
        char *argv[] = { (char *)target, NULL };
        int r = posix_spawn(&pid, target, NULL, NULL, argv, environ);
        if (r != 0) { errno = r; return -1; }
    } else {
        return -1;
    }
    int status = 0;
    if (waitpid(pid, &status, 0) < 0) return -1;
    return 0;
}

int main(int argc, char **argv) {
    const char *mode = argc > 1 ? argv[1] : "fork";
    int n = argc > 2 ? atoi(argv[2]) : 100;
    int mem_mb = argc > 3 ? atoi(argv[3]) : 0;
    int vmas = argc > 4 ? atoi(argv[4]) : 0;
    if (argc > 5) target = argv[5];

    if (mem_mb > 0) {
        size_t len = (size_t)mem_mb * 1024 * 1024;
        char *p = mmap(NULL, len, PROT_READ | PROT_WRITE,
                       MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (p == MAP_FAILED) { perror("mmap"); return 1; }
        for (size_t i = 0; i < len; i += 4096) p[i] = 1;
    }
    for (int i = 0; i < vmas; i++) {
        char *p = mmap(NULL, 4096, PROT_READ | PROT_WRITE,
                       MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (p == MAP_FAILED) { perror("mmap vma"); return 1; }
        *p = 1;
    }

    if (strcmp(mode, "forknw") == 0) {
        pid_t *pids = malloc(sizeof(pid_t) * n);
        double t0 = now();
        for (int i = 0; i < n; i++) {
            pids[i] = fork();
            if (pids[i] == 0) _exit(0);
            if (pids[i] < 0) { perror("fork"); return 1; }
        }
        double t1 = now();
        for (int i = 0; i < n; i++) waitpid(pids[i], NULL, 0);
        double t2 = now();
        printf("forknw n=%-4d forkloop=%8.1fms per=%7.1fus waitall=%8.1fms\n",
               n, (t1 - t0) * 1000.0, (t1 - t0) * 1e6 / n, (t2 - t1) * 1000.0);
        return 0;
    }

    if (once(mode) < 0) { perror("warmup"); return 1; }
    struct rusage r0, r1;
    getrusage(RUSAGE_CHILDREN, &r0);
    show_schedstat("before");
    double t0 = now();
    for (int i = 0; i < n; i++) {
        if (once(mode) < 0) { perror(mode); return 1; }
    }
    double t1 = now();
    show_schedstat("after");
    double us = (t1 - t0) * 1e6 / n;
    getrusage(RUSAGE_CHILDREN, &r1);
    double ut = (r1.ru_utime.tv_sec - r0.ru_utime.tv_sec) * 1e3 +
                (r1.ru_utime.tv_usec - r0.ru_utime.tv_usec) / 1e3;
    double st = (r1.ru_stime.tv_sec - r0.ru_stime.tv_sec) * 1e3 +
                (r1.ru_stime.tv_usec - r0.ru_stime.tv_usec) / 1e3;
    long dmin = r1.ru_minflt - r0.ru_minflt;
    long dmaj = r1.ru_majflt - r0.ru_majflt;
    printf("%-6s n=%-4d mem=%-4dMB vmas=%-6d total=%8.1fms per=%7.1fus child_ut=%6.1fms child_st=%6.1fms minflt=%ld majflt=%ld target=%s\n",
           mode, n, mem_mb, vmas, (t1 - t0) * 1000.0, us, ut, st,
           dmin, dmaj, target);
    return 0;
}
