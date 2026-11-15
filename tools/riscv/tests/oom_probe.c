// SPDX-License-Identifier: MPL-2.0
//
// Memory-exhaustion probe. Fill a fixed amount of RAM, then spawn children that
// stay alive (each pins a kernel stack). The kernel must reject creation with
// ENOMEM/EAGAIN and stay alive; after the children exit, creation must recover.
// The fill stops short of the last free page so the probe itself never faults.
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>
#include <sys/mman.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#define CHUNK (16UL * 1024 * 1024)
#define FILL_TARGET (1700UL * 1024 * 1024)
#define MAX_CHILDREN 600
#define MAX_THREADS 400

static void *idle_thread(void *arg) {
    (void)arg;
    for (;;)
        pause();
    return NULL;
}

static void read_meminfo(const char *tag) {
    char buf[256];
    int fd = open("/proc/meminfo", 0);
    if (fd < 0)
        return;
    int n = read(fd, buf, sizeof(buf) - 1);
    close(fd);
    if (n <= 0)
        return;
    buf[n] = 0;
    char *line = strstr(buf, "MemFree:");
    if (line) {
        char *nl = strchr(line, '\n');
        if (nl)
            *nl = 0;
        printf("OOM_PROBE %s %s\n", tag, line);
        fflush(stdout);
    }
}

int main(void) {
    mkdir("/proc", 0755);
    mount("proc", "/proc", "proc", 0, NULL);
    read_meminfo("before");

    unsigned long allocated = 0;
    while (allocated < FILL_TARGET) {
        void *p = mmap(NULL, CHUNK, PROT_READ | PROT_WRITE,
                       MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (p == MAP_FAILED)
            break;
        memset(p, 0x5a, CHUNK);
        allocated += CHUNK;
    }
    printf("OOM_PROBE filled=%luMB\n", allocated >> 20);
    fflush(stdout);
    read_meminfo("filled");

    pid_t children[MAX_CHILDREN];
    int spawned = 0, failed = 0, fail_errno = 0;
    for (int i = 0; i < MAX_CHILDREN; i++) {
        pid_t child = fork();
        if (child < 0) {
            failed = 1;
            fail_errno = errno;
            break;
        }
        if (child == 0) {
            for (;;)
                pause();
        }
        children[spawned++] = child;
    }
    printf("OOM_PROBE spawned=%d failed=%d errno=%d\n", spawned, failed, fail_errno);
    fflush(stdout);

    // Thread creation takes the other task-creation path. musl gives every
    // thread a stack, so eventually pthread_create must fail cleanly instead
    // of taking the kernel down.
    int threads = 0, thread_failed = 0, thread_errno = 0;
    for (int i = 0; i < MAX_THREADS; i++) {
        pthread_t thread;
        int rc = pthread_create(&thread, NULL, idle_thread, NULL);
        if (rc != 0) {
            thread_failed = 1;
            thread_errno = rc;
            break;
        }
        threads++;
    }
    printf("OOM_PROBE threads=%d failed=%d errno=%d\n", threads, thread_failed,
           thread_errno);
    fflush(stdout);

    for (int i = 0; i < spawned; i++)
        kill(children[i], SIGKILL);
    while (waitpid(-1, NULL, 0) > 0)
        ;

    int recovered = 0;
    pid_t child = fork();
    if (child == 0)
        _exit(0);
    if (child > 0) {
        int status;
        waitpid(child, &status, 0);
        recovered = WIFEXITED(status) && WEXITSTATUS(status) == 0;
    }

    pthread_t probe_thread;
    int thread_recovered = pthread_create(&probe_thread, NULL, idle_thread, NULL) == 0;
    if (thread_recovered)
        pthread_join(probe_thread, NULL);

    read_meminfo("recovered");
    printf("OOM_PROBE recovered=%d thread_recovered=%d\n", recovered, thread_recovered);
    fflush(stdout);
    for (;;)
        pause();
}
