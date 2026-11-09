// SPDX-License-Identifier: MPL-2.0
//
// Root-cause probe for the LTP sendfile07 "hang". The test is not a hang and
// not a kernel bug: it fills a 64 KiB non-blocking SOCK_DGRAM UNIX socketpair
// with 65536 one-byte write()s (which correctly returns EAGAIN when full), then
// calls sendfile(out_fd, in_fd, NULL, 1) which correctly returns EAGAIN. The
// test "times out" only because each 1-byte write costs ~1.2 ms under QEMU TCG,
// so the fill loop alone is ~80 s — over LTP's 30 s per-test watchdog.
//
// This probe prints elapsed-time progress to make that visible, and also
// compares the per-syscall cost against getpid (the TCG syscall floor) and a
// tmpfs file write. Run via run_sendfile_probe.sh.

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/sendfile.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#define MAX_FILL 70000
#define BASELINE_N 20000

static double now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000.0 + ts.tv_nsec / 1e6;
}

int main(void) {
    int p[2], fd, i;
    ssize_t r;
    double t0, t1;

    printf("[probe] start\n");
    fflush(stdout);

    /* Baseline: the TCG syscall floor. */
    t0 = now_ms();
    for (i = 0; i < BASELINE_N; i++) syscall(SYS_getpid);
    t1 = now_ms();
    printf("[probe] getpid x%d: %.2f us/op\n", BASELINE_N,
           (t1 - t0) * 1000 / BASELINE_N);
    fflush(stdout);

    /* Reference: a 1-byte write to a tmpfs file. */
    fd = open("f", O_CREAT | O_RDWR | O_TRUNC, 0644);
    t0 = now_ms();
    for (i = 0; i < BASELINE_N; i++) r = write(fd, "a", 1);
    t1 = now_ms();
    printf("[probe] tmpfs write x%d: %.2f us/op\n", BASELINE_N,
           (t1 - t0) * 1000 / BASELINE_N);
    fflush(stdout);
    close(fd);

    /* The actual sendfile07 path. */
    fd = open("in_file", O_CREAT | O_RDWR | O_TRUNC, 0644);
    write(fd, "aaaaaaaaaa", 10);
    close(fd);
    fd = open("in_file", O_RDONLY);

    if (socketpair(PF_UNIX, SOCK_DGRAM | SOCK_NONBLOCK, 0, p) != 0) {
        printf("[probe] socketpair failed errno=%d\n", errno);
        return 1;
    }

    t0 = now_ms();
    for (i = 0; i < MAX_FILL; ++i) {
        r = write(p[1], "a", 1);
        if (r < 0) {
            t1 = now_ms();
            printf("[probe] EAGAIN at write#%d after %.0f ms (%.1f us/write)\n",
                   i, t1 - t0, (t1 - t0) * 1000.0 / i);
            fflush(stdout);
            break;
        }
        if ((i % 10000) == 0) {
            printf("[probe] fill i=%d @ %.0f ms\n", i, now_ms() - t0);
            fflush(stdout);
        }
    }

    errno = 0;
    r = sendfile(p[1], fd, NULL, 1);
    printf("[probe] sendfile -> %zd errno=%d %s\n", r, errno, strerror(errno));
    fflush(stdout);

    printf("__LTP_GATE_DONE__\n");
    if (r < 0 && errno == EAGAIN)
        printf("__LTP_GATE_PASS__\n");
    else
        printf("__LTP_GATE_FAIL__\n");
    return 0;
}
