// SPDX-License-Identifier: MPL-2.0
//
// Self-contained stress/concurrency suite for the QEMU stress gate. Each
// sub-test exercises a kernel path with several workers and prints exactly one
// "STRESS <name> PASS|FAIL ..." line. Run as /init: it must never exit (the
// kernel panics when init exits with a nonzero code) and must reap orphans.
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/mman.h>
#include <sys/mount.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define PAGES (256)
#define PAGE_SIZE 4096

static int failures;

static void verdict(const char *name, int ok, const char *detail) {
    printf("STRESS %s %s %s\n", name, ok ? "PASS" : "FAIL", detail ? detail : "");
    fflush(stdout);
    if (!ok)
        failures++;
}

static void reap_all(void) {
    int status;
    while (waitpid(-1, &status, WNOHANG) > 0)
        ;
}

/* --- fork + exec + wait storm ------------------------------------------- */
static void test_fork_exec(void) {
    int workers = 4, per_worker = 40;
    for (int w = 0; w < workers; w++) {
        pid_t pid = fork();
        if (pid < 0) {
            verdict("fork_exec", 0, "fork failed");
            return;
        }
        if (pid == 0) {
            for (int i = 0; i < per_worker; i++) {
                pid_t child = fork();
                if (child == 0) {
                    execl("/bin/true", "true", (char *)NULL);
                    _exit(127);
                }
                if (child < 0)
                    _exit(1);
                int status;
                if (waitpid(child, &status, 0) < 0 || !WIFEXITED(status) || WEXITSTATUS(status) != 0)
                    _exit(1);
            }
            _exit(0);
        }
    }
    int ok = 1;
    for (int w = 0; w < workers; w++) {
        int status;
        if (waitpid(-1, &status, 0) < 0 || !WIFEXITED(status) || WEXITSTATUS(status) != 0)
            ok = 0;
    }
    verdict("fork_exec", ok, "4x40 fork/exec/wait");
}

/* --- pthread create/join + mutex + condvar ------------------------------- */
static pthread_mutex_t mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t cond = PTHREAD_COND_INITIALIZER;
static unsigned long counter;
static int cond_ready;

static void *thread_mutex_worker(void *arg) {
    (void)arg;
    for (int i = 0; i < 2000; i++) {
        pthread_mutex_lock(&mutex);
        counter++;
        if (cond_ready && counter % 512 == 0) {
            pthread_cond_signal(&cond);
        }
        pthread_mutex_unlock(&mutex);
        sched_yield();
    }
    return NULL;
}

static void *thread_cond_worker(void *arg) {
    (void)arg;
    pthread_mutex_lock(&mutex);
    cond_ready = 1;
    for (int i = 0; i < 100; i++) {
        struct timespec ts;
        clock_gettime(CLOCK_REALTIME, &ts);
        ts.tv_nsec += 1000000; /* 1 ms */
        if (ts.tv_nsec >= 1000000000) {
            ts.tv_sec++;
            ts.tv_nsec -= 1000000000;
        }
        pthread_cond_timedwait(&cond, &mutex, &ts);
    }
    pthread_mutex_unlock(&mutex);
    return NULL;
}

static void test_threads(void) {
    counter = 0;
    cond_ready = 0;
    pthread_t workers[4], cond_worker;
    int ok = 1;
    for (int i = 0; i < 4; i++)
        if (pthread_create(&workers[i], NULL, thread_mutex_worker, NULL) != 0)
            ok = 0;
    if (pthread_create(&cond_worker, NULL, thread_cond_worker, NULL) != 0)
        ok = 0;
    for (int i = 0; i < 4; i++)
        if (pthread_join(workers[i], NULL) != 0)
            ok = 0;
    if (pthread_join(cond_worker, NULL) != 0)
        ok = 0;
    if (counter != 4UL * 2000)
        ok = 0;
    verdict("threads", ok && counter == 4UL * 2000, "4x2000 mutex + condvar");
}

/* --- mmap/munmap/madvise + shared mapping -------------------------------- */
static volatile char *shared_region;

static void test_mmap(void) {
    int ok = 1;
    for (int round = 0; round < 100 && ok; round++) {
        char *region = mmap(NULL, PAGES * PAGE_SIZE, PROT_READ | PROT_WRITE,
                            MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (region == MAP_FAILED) {
            ok = 0;
            break;
        }
        for (int page = 0; page < PAGES; page++)
            region[page * PAGE_SIZE] = (char)round;
        if (madvise(region, PAGES * PAGE_SIZE, MADV_DONTNEED) != 0)
            ok = 0;
        if (munmap(region, PAGES * PAGE_SIZE) != 0)
            ok = 0;
    }
    shared_region = mmap(NULL, PAGE_SIZE, PROT_READ | PROT_WRITE,
                         MAP_SHARED | MAP_ANONYMOUS, -1, 0);
    if (shared_region == MAP_FAILED) {
        ok = 0;
    } else {
        pid_t child = fork();
        if (child == 0) {
            for (int i = 0; i < 1000; i++)
                shared_region[0] = 'A';
            _exit(0);
        }
        int status;
        if (waitpid(child, &status, 0) < 0 || !WIFEXITED(status))
            ok = 0;
        munmap((void *)shared_region, PAGE_SIZE);
    }
    verdict("mmap", ok, "100x 1MiB touch/madvise + shared page");
}

/* --- file I/O on a tmpfs ------------------------------------------------- */
static void *io_worker(void *arg) {
    int id = (int)(long)arg;
    char path[64];
    for (int i = 0; i < 200; i++) {
        snprintf(path, sizeof(path), "/tmp/stress-%d-%d", id, i % 8);
        int fd = open(path, O_CREAT | O_RDWR | O_TRUNC, 0644);
        if (fd < 0)
            return (void *)1;
        char buffer[4096];
        memset(buffer, id, sizeof(buffer));
        if (write(fd, buffer, sizeof(buffer)) != sizeof(buffer)) {
            close(fd);
            return (void *)1;
        }
        if (fsync(fd) != 0) {
            close(fd);
            return (void *)1;
        }
        close(fd);
        if (rename(path, path) != 0 || unlink(path) != 0)
            return (void *)1;
    }
    return NULL;
}

static void test_io(void) {
    pthread_t workers[4];
    int ok = 1;
    for (int i = 0; i < 4; i++)
        if (pthread_create(&workers[i], NULL, io_worker, (void *)(long)i) != 0)
            ok = 0;
    for (int i = 0; i < 4; i++) {
        void *result = NULL;
        if (pthread_join(workers[i], &result) != 0 || result != NULL)
            ok = 0;
    }
    verdict("io_tmpfs", ok, "4x200 create/write/fsync/rename/unlink");
}

/* --- pipe/socketpair + epoll -------------------------------------------- */
static void test_epoll(void) {
    enum { PAIRS = 64 };
    int sockets[PAIRS][2];
    int epfd = epoll_create1(0);
    int ok = epfd >= 0;
    for (int i = 0; i < PAIRS && ok; i++) {
        if (socketpair(AF_UNIX, SOCK_STREAM, 0, sockets[i]) != 0) {
            ok = 0;
            break;
        }
        struct epoll_event event = {.events = EPOLLIN, .data.u32 = (unsigned)i};
        if (epoll_ctl(epfd, EPOLL_CTL_ADD, sockets[i][0], &event) != 0)
            ok = 0;
    }
    for (int i = 0; i < PAIRS && ok; i++)
        if (write(sockets[i][1], "x", 1) != 1)
            ok = 0;
    int ready = 0;
    struct epoll_event events[PAIRS];
    while (ok && ready < PAIRS) {
        int count = epoll_wait(epfd, events, PAIRS, 5000);
        if (count <= 0) {
            ok = 0;
            break;
        }
        ready += count;
    }
    for (int i = 0; i < PAIRS; i++) {
        if (sockets[i][0] >= 0) {
            close(sockets[i][0]);
            close(sockets[i][1]);
        }
    }
    if (epfd >= 0)
        close(epfd);
    verdict("epoll_socket", ok && ready == PAIRS, "64 socketpairs + epoll_wait");
}

static void test_pipe(void) {
    int fds[2];
    if (pipe(fds) != 0) {
        verdict("pipe", 0, "pipe failed");
        return;
    }
    pid_t child = fork();
    if (child == 0) {
        close(fds[1]);
        char buffer[4096];
        unsigned long total = 0;
        ssize_t count;
        while ((count = read(fds[0], buffer, sizeof(buffer))) > 0)
            total += (unsigned long)count;
        _exit(total == 1UL * 1024 * 1024 ? 0 : 1);
    }
    close(fds[0]);
    char buffer[4096];
    memset(buffer, 'p', sizeof(buffer));
    int ok = 1;
    for (int i = 0; i < 256; i++)
        if (write(fds[1], buffer, sizeof(buffer)) != sizeof(buffer))
            ok = 0;
    close(fds[1]);
    int status;
    if (waitpid(child, &status, 0) < 0 || !WIFEXITED(status) || WEXITSTATUS(status) != 0)
        ok = 0;
    verdict("pipe", ok, "1 MiB producer/consumer");
}

/* --- signals ------------------------------------------------------------- */
static void test_rt_signals(void);
static volatile sig_atomic_t signal_count;

static void signal_handler(int signo) {
    (void)signo;
    signal_count++;
}

static void test_signals(void) {
    struct sigaction action;
    memset(&action, 0, sizeof(action));
    action.sa_handler = signal_handler;
    sigemptyset(&action.sa_mask);
    sigaction(SIGUSR1, &action, NULL);
    signal_count = 0;
    pid_t parent = getpid();
    pid_t child = fork();
    if (child == 0) {
        for (int i = 0; i < 500; i++) {
            kill(parent, SIGUSR1);
            sched_yield();
        }
        _exit(0);
    }
    int status = 0;
    int waited;
    do {
        waited = waitpid(child, &status, 0);
    } while (waited < 0 && errno == EINTR);
    /* Signals are asynchronous: give the pending ones a bounded moment. */
    for (int i = 0; i < 200 && signal_count < 500; i++)
        usleep(1000);
    /* Standard signals are not queued: a burst coalesces, so require
     * delivery rather than an exact count. */
    int ok = signal_count > 0 && waited == child && WIFEXITED(status) &&
             WEXITSTATUS(status) == 0;
    char detail[96];
    snprintf(detail, sizeof(detail), "count=%d child_status=%d", (int)signal_count, status);
    verdict("signals", ok, detail);
    test_rt_signals();
}

/* --- real-time signals must queue (no coalescing) ------------------------ */
static void test_rt_signals(void) {
    struct sigaction action;
    memset(&action, 0, sizeof(action));
    action.sa_handler = signal_handler;
    sigemptyset(&action.sa_mask);
    sigaction(SIGRTMIN + 1, &action, NULL);
    signal_count = 0;
    pid_t parent = getpid();
    pid_t child = fork();
    if (child == 0) {
        for (int i = 0; i < 500; i++) {
            kill(parent, SIGRTMIN + 1);
            sched_yield();
        }
        _exit(0);
    }
    int status = 0;
    int waited;
    do {
        waited = waitpid(child, &status, 0);
    } while (waited < 0 && errno == EINTR);
    for (int i = 0; i < 200 && signal_count < 500; i++)
        usleep(1000);
    int ok = signal_count == 500 && waited == child && WIFEXITED(status) &&
             WEXITSTATUS(status) == 0;
    char detail[96];
    snprintf(detail, sizeof(detail), "queued=%d child_status=%d", (int)signal_count, status);
    verdict("rt_signals", ok, detail);
}

int main(void) {
    mount("tmpfs", "/tmp", "tmpfs", 0, NULL);
    printf("STRESS_BEGIN\n");
    fflush(stdout);
    test_fork_exec();
    test_threads();
    test_mmap();
    test_io();
    test_epoll();
    test_pipe();
    test_signals();
    printf("STRESS_DONE failures=%d\n", failures);
    fflush(stdout);
    for (;;) {
        reap_all();
        pause();
    }
}
