// SPDX-License-Identifier: MPL-2.0
//
// Reproducer for the missing PTRACE_O_TRACECLONE support.
//
// The child TRACEMEs, stops, lets the parent set TRACECLONE, then creates a
// thread. Before the fix the clone returns EOPNOTSUPP, so pthread_create
// fails and the tracer never sees a stop for the new thread. After the fix
// pthread_create succeeds, the new thread enters a ptrace-stop, and the
// tracer must continue it.

#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <sys/ptrace.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static void *thread_fn(void *arg)
{
    (void)arg;
    return NULL;
}

static double now(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

int main(void)
{
    int fds[2];
    if (pipe(fds) != 0) {
        perror("pipe");
        return 1;
    }

    pid_t pid = fork();
    if (pid == 0) {
        close(fds[0]);
        if (ptrace(PTRACE_TRACEME, 0, 0, 0) != 0) {
            _exit(2);
        }
        raise(SIGSTOP);

        pthread_t t;
        int rc = pthread_create(&t, NULL, thread_fn, NULL);
        write(fds[1], &rc, sizeof(rc));
        if (rc == 0) {
            pthread_join(t, NULL);
        }
        _exit(0);
    }

    close(fds[1]);
    int st;
    if (waitpid(pid, &st, 0) < 0) {
        perror("waitpid");
        return 1;
    }

    errno = 0;
    long opt = ptrace(PTRACE_SETOPTIONS, pid, 0, (void *)PTRACE_O_TRACECLONE);
    printf("SETOPTIONS traceclone rc=%ld errno=%d (%s)\n", opt, errno,
           strerror(errno));

    if (ptrace(PTRACE_CONT, pid, 0, 0) != 0) {
        perror("PTRACE_CONT");
        return 1;
    }

    int new_stops = 0;
    int child_exited = 0;
    double deadline = now() + 5.0;
    while (!child_exited && now() < deadline) {
        pid_t w = waitpid(-1, &st, WNOHANG);
        if (w > 0) {
            if (WIFEXITED(st) || WIFSIGNALED(st)) {
                if (w == pid) {
                    child_exited = 1;
                }
                continue;
            }
            if (WIFSTOPPED(st)) {
                if (w != pid) {
                    new_stops++;
                }
                ptrace(PTRACE_CONT, w, 0, 0);
            }
        } else {
            struct timespec ts = { 0, 5 * 1000 * 1000 };
            nanosleep(&ts, NULL);
        }
    }

    int rc = -1;
    ssize_t n = read(fds[0], &rc, sizeof(rc));
    printf("CLONE pthread_create_rc=%d new_tracee_stops=%d child_exited=%d "
           "read=%zd\n", rc, new_stops, child_exited, n);

    int pass = rc == 0 && new_stops >= 1 && child_exited;
    printf("RESULT traceclone %s\n", pass ? "PASS" : "FAIL");
    return pass ? 0 : 1;
}
