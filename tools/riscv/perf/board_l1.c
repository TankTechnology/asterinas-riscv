// SPDX-License-Identifier: MPL-2.0
//
// L1 micro-benchmark for the board: the cheap, repeatable numbers the perf
// contract asks for first (syscall latency, process creation, memory copy,
// pipe round trip, thread scaling, file metadata plus fsync).
//
// Every line it prints is `NAME=value`, and the last field is always the unit,
// so a shell driver can collect samples without parsing prose:
//
//   getpid=5.8 us
//   memcpy=4200 MiB/s
//
// Build (static, no libc on the board needed):
//   riscv64-linux-musl-gcc -O2 -static -pthread -o board_l1 board_l1.c
//
// It writes only under /tmp and removes what it creates.

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define ITER_SYSCALL 200000
#define ITER_OPEN 20000
#define ITER_FORKEXEC 50
#define ITER_PIPE 20000
#define COPY_BYTES (256u << 20)
#define FS_FILES 200
#define FS_FILE_BYTES 4096
#define THREADS 4
#define THREAD_LOOPS 200000

static double now_s(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

static void report(const char *name, double value, const char *unit) {
    printf("%s=%.3f %s\n", name, value, unit);
    fflush(stdout);
}

static void bench_getpid(void) {
    double start = now_s();
    for (int i = 0; i < ITER_SYSCALL; i++)
        (void)getpid();
    report("getpid", (now_s() - start) / ITER_SYSCALL * 1e6, "us");
}

static void bench_write_null(void) {
    int fd = open("/dev/null", O_WRONLY);
    if (fd < 0) {
        report("write_null", -1, "us");
        return;
    }
    double start = now_s();
    for (int i = 0; i < ITER_SYSCALL; i++)
        (void)write(fd, "x", 1);
    report("write_null", (now_s() - start) / ITER_SYSCALL * 1e6, "us");
    close(fd);
}

static void bench_open_close(void) {
    double start = now_s();
    for (int i = 0; i < ITER_OPEN; i++) {
        int fd = open("/dev/null", O_RDONLY);
        if (fd >= 0)
            close(fd);
    }
    report("open_close", (now_s() - start) / ITER_OPEN * 1e6, "us");
}

// Path resolution and metadata fetch, the two halves of `stat`.
//
// `stat_path` includes reading the path string, parsing it, walking the dentry
// tree and fetching the inode; `fstat_fd` skips all of that and only fetches
// the metadata, so the difference between them is what path resolution costs
// on an already-cached path. Both run on a file that is guaranteed to exist.
static void bench_stat(void) {
    const char *path = "/bin/true";
    struct stat st;
    if (stat(path, &st) != 0) {
        report("stat_path", -1, "us");
        return;
    }
    for (int i = 0; i < 1000; i++)
        (void)stat(path, &st);

    double start = now_s();
    for (int i = 0; i < ITER_SYSCALL; i++)
        (void)stat(path, &st);
    report("stat_path", (now_s() - start) / ITER_SYSCALL * 1e6, "us");

    int fd = open(path, O_RDONLY);
    if (fd < 0) {
        report("fstat_fd", -1, "us");
        return;
    }
    start = now_s();
    for (int i = 0; i < ITER_SYSCALL; i++)
        (void)fstat(fd, &st);
    report("fstat_fd", (now_s() - start) / ITER_SYSCALL * 1e6, "us");
    close(fd);
}

static void bench_fork_exec(void) {
    double start = now_s();
    for (int i = 0; i < ITER_FORKEXEC; i++) {
        pid_t pid = fork();
        if (pid == 0) {
            execl("/bin/true", "true", (char *)NULL);
            _exit(127);
        }
        int status = 0;
        if (pid > 0)
            (void)waitpid(pid, &status, 0);
    }
    report("fork_exec_wait", (now_s() - start) / ITER_FORKEXEC * 1e3, "ms");
}

static void bench_memcpy(void) {
    unsigned char *src = malloc(COPY_BYTES);
    unsigned char *dst = malloc(COPY_BYTES);
    if (!src || !dst) {
        report("memcpy", -1, "MiB/s");
        free(src);
        free(dst);
        return;
    }
    // Seed the source with values the compiler cannot fold: a constant fill
    // lets `-O2` prove what the copy produces and drop it entirely, which is
    // how this benchmark first reported petabytes per second.
    volatile unsigned seed = (unsigned)getpid() ^ (unsigned)time(NULL);
    for (size_t i = 0; i < COPY_BYTES; i++)
        src[i] = (unsigned char)(seed ^ (i * 31u));

    double start_time = now_s();
    memcpy(dst, src, COPY_BYTES);
    double elapsed = now_s() - start_time;

    // Touch the result so the copy has to be materialized.
    volatile unsigned char acc = 0;
    for (size_t i = 0; i < COPY_BYTES; i += 4096)
        acc ^= dst[i];
    (void)acc;

    report("memcpy", (double)COPY_BYTES / (1u << 20) / elapsed, "MiB/s");
    free(src);
    free(dst);
}

static void bench_pipe(void) {
    int fds[2];
    if (pipe(fds) != 0) {
        report("pipe_rtt", -1, "us");
        return;
    }
    char byte = 'x';
    double start = now_s();
    for (int i = 0; i < ITER_PIPE; i++) {
        (void)write(fds[1], &byte, 1);
        (void)read(fds[0], &byte, 1);
    }
    report("pipe_rtt", (now_s() - start) / ITER_PIPE * 1e6, "us");
    close(fds[0]);
    close(fds[1]);
}

static long counter;
static pthread_mutex_t counter_lock = PTHREAD_MUTEX_INITIALIZER;

static void *thread_body(void *arg) {
    (void)arg;
    for (int i = 0; i < THREAD_LOOPS; i++) {
        pthread_mutex_lock(&counter_lock);
        counter++;
        pthread_mutex_unlock(&counter_lock);
    }
    return NULL;
}

static void bench_threads(void) {
    pthread_t threads[THREADS];
    double start = now_s();
    for (int i = 0; i < THREADS; i++)
        pthread_create(&threads[i], NULL, thread_body, NULL);
    for (int i = 0; i < THREADS; i++)
        pthread_join(threads[i], NULL);
    double elapsed = now_s() - start;
    report("mutex_ops", (double)(THREADS * THREAD_LOOPS) / elapsed / 1e6, "Mops/s");
}

// create + write + fsync + unlink, the metadata-heavy path a package manager
// exercises.
static void bench_fs_create(void) {
    char path[64];
    char payload[FS_FILE_BYTES];
    memset(payload, 0x33, sizeof payload);
    double start = now_s();
    for (int i = 0; i < FS_FILES; i++) {
        snprintf(path, sizeof path, "/tmp/l1-fs-%d", i);
        int fd = open(path, O_CREAT | O_TRUNC | O_RDWR, 0600);
        if (fd < 0)
            continue;
        (void)write(fd, payload, sizeof payload);
        (void)fsync(fd);
        close(fd);
    }
    double create_elapsed = now_s() - start;
    report("fs_create_sync", create_elapsed / FS_FILES * 1e6, "us");
    start = now_s();
    for (int i = 0; i < FS_FILES; i++) {
        snprintf(path, sizeof path, "/tmp/l1-fs-%d", i);
        (void)unlink(path);
    }
    report("fs_unlink", (now_s() - start) / FS_FILES * 1e6, "us");
}

// 64 MiB sequential write with fsync, then read back.
static void bench_fs_stream(void) {
    const char *path = "/tmp/l1-stream.bin";
    size_t bytes = 64u << 20;
    char *buffer = malloc(bytes);
    if (!buffer) {
        report("fs_write", -1, "MiB/s");
        return;
    }
    memset(buffer, 0x77, bytes);
    int fd = open(path, O_CREAT | O_TRUNC | O_RDWR, 0600);
    if (fd < 0) {
        report("fs_write", -1, "MiB/s");
        free(buffer);
        return;
    }
    double start = now_s();
    (void)write(fd, buffer, bytes);
    (void)fsync(fd);
    double write_elapsed = now_s() - start;
    report("fs_write", (double)bytes / (1u << 20) / write_elapsed, "MiB/s");

    start = now_s();
    (void)lseek(fd, 0, SEEK_SET);
    size_t got = 0;
    while (got < bytes) {
        ssize_t n = read(fd, buffer + got, bytes - got);
        if (n <= 0)
            break;
        got += (size_t)n;
    }
    report("fs_read", (double)got / (1u << 20) / (now_s() - start), "MiB/s");
    close(fd);
    unlink(path);
    free(buffer);
}

int main(void) {
    bench_getpid();
    bench_write_null();
    bench_open_close();
    bench_stat();
    bench_fork_exec();
    bench_memcpy();
    bench_pipe();
    bench_threads();
    bench_fs_create();
    bench_fs_stream();
    return 0;
}
