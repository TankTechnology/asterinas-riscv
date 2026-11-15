// SPDX-License-Identifier: MPL-2.0
//
// Init process for the LMBench comparison gate. Runs each benchmark in a child
// with a per-test watchdog, so one hanging case cannot stall the rest, and
// prints BENCH_RUN/BENCH_END markers around the benchmark's own output. The
// same initramfs boots both Asterinas and a Linux baseline kernel.
#define _GNU_SOURCE
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define PER_TEST_SECONDS 30

struct test {
    const char *name;
    const char *bin;
    const char *args[5];
};

static const struct test tests[] = {
    {"syscall_null", "lat_syscall", {"null", NULL}},
    {"syscall_read", "lat_syscall", {"read", NULL}},
    {"syscall_write", "lat_syscall", {"write", NULL}},
    {"syscall_stat", "lat_syscall", {"stat", "/etc/passwd", NULL}},
    {"syscall_fstat", "lat_syscall", {"fstat", "/etc/passwd", NULL}},
    {"syscall_open", "lat_syscall", {"open", "/etc/passwd", NULL}},
    {"pipe_latency", "lat_pipe", {NULL}},
    {"fork_exec", "lat_proc", {"fork", NULL}},
    {"exec", "lat_proc", {"exec", NULL}},
    {"shell", "lat_proc", {"shell", NULL}},
    {"pipe_bandwidth", "bw_pipe", {NULL}},
    {"signal_install", "lat_sig", {"install", NULL}},
    {"signal_catch", "lat_sig", {"catch", NULL}},
    {"file_ops", "lat_fs", {"/tmp", NULL}},
    {"pagefault", "lat_pagefault", {"/tmp/pagefault", NULL}},
    {"mmap", "lat_mmap", {"64m", "/tmp/mmapfile", NULL}},
    {"mem_bandwidth", "bw_mem", {"64m", "mem", NULL}},
    {"ctx_switch", "lat_ctx", {"-s", "64", "4", NULL}},
};

static double monotonic_seconds(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

static void run_test(const struct test *test) {
    printf("BENCH_RUN %s\n", test->name);
    fflush(stdout);
    pid_t child = fork();
    if (child == 0) {
        char path[64];
        snprintf(path, sizeof(path), "/bench/%s", test->bin);
        char *argv[8];
        int count = 0;
        argv[count++] = (char *)test->bin;
        for (int i = 0; i < 5 && test->args[i] != NULL; i++)
            argv[count++] = (char *)test->args[i];
        argv[count] = NULL;
        execv(path, argv);
        _exit(127);
    }
    if (child < 0) {
        printf("BENCH_END %s status=spawn-failed\n", test->name);
        return;
    }
    double deadline = monotonic_seconds() + PER_TEST_SECONDS;
    int status = 0;
    int timed_out = 0;
    for (;;) {
        pid_t waited = waitpid(child, &status, WNOHANG);
        if (waited == child)
            break;
        if (waited < 0 && errno != EINTR)
            break;
        if (monotonic_seconds() >= deadline) {
            timed_out = 1;
            kill(child, SIGKILL);
            waitpid(child, &status, 0);
            break;
        }
        struct timespec pause = {0, 5 * 1000 * 1000};
        nanosleep(&pause, NULL);
    }
    const char *outcome = timed_out ? "timeout"
                          : (WIFEXITED(status) && WEXITSTATUS(status) == 0) ? "ok"
                          : "error";
    printf("BENCH_END %s status=%s\n", test->name, outcome);
    fflush(stdout);
}

int main(void) {
    /* lmbench's compute_enough() derives a measurement interval from repeated
     * getrusage()-timed pointer-chase runs; on Asterinas that loop does not
     * converge quickly, so pin the interval to 10 ms instead. */
    setenv("ENOUGH", "10000", 1);
    mkdir("/dev", 0755);
    mkdir("/proc", 0755);
    mkdir("/sys", 0755);
    mkdir("/tmp", 0755);
    mount("devtmpfs", "/dev", "devtmpfs", 0, NULL);
    mount("proc", "/proc", "proc", 0, NULL);
    mount("sysfs", "/sys", "sysfs", 0, NULL);
    printf("BENCH_BEGIN\n");
    fflush(stdout);
    for (size_t i = 0; i < sizeof(tests) / sizeof(tests[0]); i++)
        run_test(&tests[i]);
    printf("BENCH_DONE\n");
    fflush(stdout);
    for (;;) {
        int status;
        while (waitpid(-1, &status, WNOHANG) > 0)
            ;
        pause();
    }
}
