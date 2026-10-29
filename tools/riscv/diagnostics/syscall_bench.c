// SPDX-License-Identifier: MPL-2.0
// Syscall and IPC microbenchmark for the Megrez guest.
//
// Python's os.getpid() adds interpreter and libc overhead on top of the
// syscall, and the guest has no compiler, so this is cross-compiled on the
// host (riscv64-linux-gnu-gcc -static) and pushed to the board. Timings use
// CLOCK_MONOTONIC, and every batch is long enough that the clock call itself
// disappears into the noise.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <time.h>
#include <unistd.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <sys/syscall.h>

static inline unsigned long rdcycle(void) {
    unsigned long value;
    __asm__ volatile("rdcycle %0" : "=r"(value));
    return value;
}

static double now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec * 1e9 + (double)ts.tv_nsec;
}

#ifdef NO_CYCLE
#define BENCH_CYCLE(label, count, body)
#else
#define BENCH_CYCLE(label, count, body)                                        \
    do {                                                                       \
        unsigned long start = rdcycle();                                       \
        for (long i = 0; i < (count); i++) { body; }                           \
        unsigned long elapsed = rdcycle() - start;                             \
        printf("C_%s_cyc %.1f\n", label, (double)elapsed / (count));           \
    } while (0)
#endif

#define BENCH(label, count, body)                                              \
    do {                                                                       \
        double start = now_ns();                                               \
        for (long i = 0; i < (count); i++) { body; }                           \
        double elapsed = now_ns() - start;                                     \
        printf("B_%s_ns %.1f\n", label, elapsed / (count));                    \
    } while (0)

int main(int argc, char **argv) {
    long n = argc > 1 ? atol(argv[1]) : 200000;

    volatile long sink = 0;
    BENCH("loop", n, { __asm__ volatile("" ::: "memory"); });
    BENCH_CYCLE("loop", n, { __asm__ volatile("" ::: "memory"); });
    BENCH_CYCLE("getpid", n, { sink += getpid(); });
    BENCH_CYCLE("clock", n, { struct timespec t2; clock_gettime(CLOCK_MONOTONIC, &t2); });
    BENCH("getpid", n, { sink += getpid(); });
    BENCH("raw_getpid", n, { sink += syscall(SYS_getpid); });
    BENCH("getppid", n, { sink += getppid(); });
    BENCH("close_bad", n, { sink += close(-1); });
    BENCH("getpid_err", n, { sink += syscall(0xFFFF, 1, 2); });
    BENCH("getuid", n, { sink += getuid(); });

    {
        struct timespec ts;
        BENCH("clock", n, { clock_gettime(CLOCK_MONOTONIC, &ts); });
    }

    int fds[2];
    if (pipe(fds) == 0) {
        char byte = 'x';
        BENCH("pipe", n / 4, {
            if (write(fds[1], &byte, 1) == 1) {
                if (read(fds[0], &byte, 1) == 1) sink += byte;
            }
        });
        close(fds[0]);
        close(fds[1]);
    }

    int sv[2];
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, sv) == 0) {
        char byte = 'x';
        BENCH("sockpair", n / 4, {
            if (send(sv[0], &byte, 1, 0) == 1) {
                if (recv(sv[1], &byte, 1, 0) == 1) sink += byte;
            }
        });
        close(sv[0]);
        close(sv[1]);
    }

    printf("sink=%ld\nZZCBENCH\n", sink);
    return 0;
}
