// SPDX-License-Identifier: MPL-2.0
//
// NIXOS-N6 seccomp TSYNC probe: install a BPF filter that blocks `getpid`
// with SECCOMP_FILTER_FLAG_TSYNC, then verify the filter applies to every
// thread in the process (the caller plus two clone()d threads).

#define _GNU_SOURCE
#include <errno.h>
#include <sched.h>
#include <signal.h>
#include <stdio.h>
#include <sys/syscall.h>
#include <unistd.h>

#include <linux/filter.h>
#include <linux/seccomp.h>

#define NTHREADS 2

static volatile int filter_installed;
static volatile int done[NTHREADS + 1];
static volatile int blocked[NTHREADS + 1];
static char stacks[NTHREADS][65536] __attribute__((aligned(16)));

/// Returns 1 if `getpid` was blocked (EPERM), 0 if it succeeded.
static int probe_getpid(void) {
    long r = syscall(SYS_getpid);
    return r < 0 ? 1 : 0;
}

static int thread_fn(void *arg) {
    long idx = (long)arg;
    while (!filter_installed) {
        /* spin until the filter is installed */
    }
    blocked[idx] = probe_getpid();
    done[idx] = 1;
    return 0;
}

int main(void) {
    setbuf(stdout, NULL);
    printf(">>> N6 seccomp TSYNC probe start <<<\n");

    // BPF filter: block `getpid` with EPERM, allow everything else.
    // JEQ jt=1 (equal -> deny), jf=0 (not equal -> allow).
    struct sock_filter filter[] = {
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, 0), /* load seccomp_data.nr */
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_getpid, 1, 0),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM),
    };
    struct sock_fprog prog = {.len = 4, .filter = filter};

    // Spawn two threads sharing the address space.
    for (long i = 1; i <= NTHREADS; i++) {
        void *stack_top = stacks[i - 1] + sizeof(stacks[i - 1]);
        long tid = clone(thread_fn, stack_top,
                         CLONE_VM | CLONE_SIGHAND | CLONE_THREAD | CLONE_FS |
                             CLONE_FILES | SIGCHLD,
                         (void *)i);
        if (tid < 0) {
            printf("__SECCOMP__ clone(%ld) failed: errno=%d\n", i, errno);
            return 1;
        }
    }

    // Install the filter with TSYNC (applies to every thread).
    long r = syscall(SYS_seccomp, SECCOMP_SET_MODE_FILTER,
                     SECCOMP_FILTER_FLAG_TSYNC, &prog);
    if (r != 0) {
        printf("__SECCOMP__ seccomp(TSYNC) failed: errno=%d\n", errno);
        return 1;
    }

    // Release the threads, then probe the calling thread itself.
    filter_installed = 1;
    blocked[0] = probe_getpid();

    // Wait for each thread to finish probing.
    for (long i = 1; i <= NTHREADS; i++) {
        while (!done[i]) {
            /* spin */
        }
    }

    printf("__SECCOMP__ results:");
    for (long i = 0; i <= NTHREADS; i++) {
        printf("%d", blocked[i]);
    }
    printf("\n");

    int all_blocked = 1;
    for (long i = 0; i <= NTHREADS; i++) {
        if (!blocked[i]) {
            all_blocked = 0;
        }
    }
    printf(all_blocked ? "__SECCOMP__ TSYNC_PASS\n" : "__SECCOMP__ TSYNC_FAIL\n");
    printf(">>> N6 seccomp TSYNC probe done <<<\n");
    return 0;
}
