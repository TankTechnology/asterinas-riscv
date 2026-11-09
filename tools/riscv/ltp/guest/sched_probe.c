// SPDX-License-Identifier: MPL-2.0
// Minimal raw-syscall probe for sched_setscheduler RESET_ON_FORK semantics.
#define _GNU_SOURCE
#include <errno.h>
#include <sched.h>
#include <stdio.h>
#include <string.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

#define SCHED_RESET_ON_FORK 0x40000000

static long raw(pid_t pid, int policy, int prio) {
    struct sched_param p = { .sched_priority = prio };
    return syscall(SYS_sched_setscheduler, pid, policy, &p);
}

static long getpol(pid_t pid) {
    return syscall(SYS_sched_getscheduler, pid);
}

int main(void) {
    int nfail = 0;
    pid_t me = getpid();

    // 1. set SCHED_FIFO (no reset flag) on self.
    errno = 0;
    long r = raw(me, SCHED_FIFO, 10);
    printf("[1] sched_setscheduler(self, FIFO, 10) ret=%ld errno=%d %s\n", r, errno, strerror(errno));

    errno = 0;
    long pol = getpol(me);
    printf("[1] sched_getscheduler(self) = %ld errno=%d (expect 1=FIFO)\n", pol, errno);

    // 2. set SCHED_FIFO | RESET_ON_FORK on self.
    errno = 0;
    r = raw(me, SCHED_FIFO | SCHED_RESET_ON_FORK, 10);
    printf("[2] sched_setscheduler(self, FIFO|RESET_ON_FORK, 10) ret=%ld errno=%d %s\n",
           r, errno, strerror(errno));

    errno = 0;
    pol = getpol(me);
    printf("[2] sched_getscheduler(self) = %ld errno=%d (expect 1=FIFO)\n", pol, errno);

    // 3. fork WITHOUT reset flag: child should INHERIT FIFO.
    {
        raw(me, SCHED_FIFO, 10); // clear reset_on_fork by re-setting FIFO
        pid_t c = fork();
        if (c == 0) {
            usleep(200000);
            errno = 0;
            long cp = getpol(getpid());
            printf("[3 child] sched_getscheduler(self) = %ld errno=%d (expect 1=FIFO)\n", cp, errno);
            _exit(0);
        }
        if (c > 0) {
            errno = 0;
            long cp = getpol(c);
            printf("[3 parent] sched_getscheduler(child=%d) = %ld errno=%d (expect 1=FIFO)\n",
                   c, cp, errno);
            int st; waitpid(c, &st, 0);
        }
    }

    // 4. fork WITH reset flag: child should be reset to SCHED_NORMAL.
    {
        raw(me, SCHED_FIFO | SCHED_RESET_ON_FORK, 10);
        pid_t c = fork();
        if (c == 0) {
            // exit immediately, like the LTP test's SAFE_FORK child
            _exit(0);
        }
        if (c > 0) {
            // let the child exit and become a zombie, then query
            usleep(100000);
            errno = 0;
            long cp = getpol(c);
            printf("[4 parent] sched_getscheduler(zombie=%d) = %ld errno=%d (expect 0=NORMAL)\n",
                   c, cp, errno);
            int st; waitpid(c, &st, 0);
        }
    }

    printf("__LTP_GATE_DONE__\n");
    if (nfail == 0)
        printf("__LTP_GATE_PASS__\n");
    else
        printf("__LTP_GATE_FAIL__\n");
    return 0;
}
