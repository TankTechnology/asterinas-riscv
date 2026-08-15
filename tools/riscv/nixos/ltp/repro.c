// SPDX-License-Identifier: MPL-2.0
//
// Minimal static repro for LTP syscall bugs, run as /init in a bare initramfs.
// Prints per-probe [PASS]/[FAIL] lines plus __LTP_GATE_DONE__ / __LTP_GATE_PASS__
// / __LTP_GATE_FAIL__ so the existing boot_ltp_gate.py driver can collect it.

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <sched.h>
#include <stdio.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static int nfail = 0;

static void check(const char *name, int ok, long ret, int err) {
    printf("[%s] %s (ret=%ld errno=%d %s)\n", ok ? "PASS" : "FAIL", name,
           ret, err, strerror(err));
    if (!ok) nfail++;
}

int main(void) {
    if (mount("proc", "/proc", "proc", 0, NULL) != 0) {}
    if (mount("sysfs", "/sys", "sysfs", 0, NULL) != 0) {}
    if (mount("tmpfs", "/tmp", "tmpfs", 0, NULL) != 0) {}

    const char *file = "/tmp/tf";
    const char *sym = "/tmp/sl";
    int fd = open(file, O_CREAT | O_RDWR, 0644);
    (void)fd;
    if (symlink(file, sym) != 0) {
        printf("[FAIL] symlink setup (errno=%d %s)\n", errno, strerror(errno));
        nfail++;
    }

    char buf[256];
    errno = 0;
    // readlink(bufsize=0) must fail with EINVAL.
    long r = readlink(sym, buf, 0);
    check("readlink(sym,0) -> EINVAL", r == -1 && errno == EINVAL, r, errno);

    // readlinkat(dirfd, symlink, bufsiz=0) must fail with EINVAL.
    int dfd = open("/tmp", O_RDONLY | O_DIRECTORY);
    errno = 0;
    r = readlinkat(dfd, "sl", buf, 0);
    check("readlinkat(dfd,0) -> EINVAL", r == -1 && errno == EINVAL, r, errno);

    // RAW syscall (bypass musl wrapper) with bufsize=0.
    errno = 0;
    long raw = syscall(SYS_readlinkat, dfd, "sl", buf, 0);
    int e = errno;
    printf("[raw] SYS_readlinkat(dfd,sl,buf,0) ret=%ld errno=%d %s\n", raw, e, strerror(e));

    // readlink on a regular file must fail with EINVAL.
    errno = 0;
    r = readlink(file, buf, sizeof(buf));
    check("readlink(file) -> EINVAL", r == -1 && errno == EINVAL, r, errno);

    // sbrk(0) returns current break (not -1).
    void *b0 = sbrk(0);
    check("sbrk(0) ok", b0 != (void *)-1, (long)b0, errno);

    // sbrk(+8192) must extend and return the old break.
    errno = 0;
    void *b1 = sbrk(8192);
    check("sbrk(+8192) extends", b1 != (void *)-1, (long)b1, errno);

    // sbrk(-8192) must shrink and return the old break.
    errno = 0;
    void *b2 = sbrk(-8192);
    check("sbrk(-8192) shrinks", b2 != (void *)-1, (long)b2, errno);

    // RAW brk syscall: brk(0) then brk(cur + 8192) should extend (proves the
    // kernel brk works, isolating sbrk01's failure to musl's sbrk stub).
    errno = 0;
    long cur = syscall(SYS_brk, 0);
    long want = cur + 8192;
    long got = syscall(SYS_brk, want);
    printf("[raw] SYS_brk: cur=%ld brk(%ld)=%ld expect=%ld %s\n",
           cur, want, got, want, got == want ? "OK" : "MISMATCH");

    // pwrite errno cases: EBADF on a read-only fd, EFAULT on a NULL buffer.
    int ro = open(file, O_RDONLY);
    errno = 0;
    long pw = pwrite(ro, "x", 1, 0);
    check("pwrite(ro_fd) -> EBADF", pw == -1 && errno == EBADF, pw, errno);

    int rw = open(file, O_RDWR);
    errno = 0;
    pw = pwrite(rw, NULL, 1, 0);
    check("pwrite(NULL) -> EFAULT", pw == -1 && errno == EFAULT, pw, errno);

    // access(X_OK) on a 0555 file should return 0 (permission bits only).
    if (chmod(file, 0555) != 0) {}
    errno = 0;
    long ac = access(file, X_OK);
    printf("[raw] access(%s, X_OK) = %ld errno=%d %s\n", file, ac, errno, strerror(errno));

    // sched_setscheduler(SCHED_FIFO | SCHED_RESET_ON_FORK) then fork: the child
    // must be reset to SCHED_NORMAL (0) with priority 0. SCHED_RESET_ON_FORK =
    // 0x40000000 (musl does not export it). Exercises the raw sched_* syscalls.
    {
        struct sched_param p = { .sched_priority = 10 };
        errno = 0;
        long s = syscall(SYS_sched_setscheduler, getpid(),
                         SCHED_FIFO | 0x40000000u, &p);
        printf("[raw] sched_setscheduler(FIFO|RESET_ON_FORK,prio=10) ret=%ld errno=%d %s\n",
               s, errno, strerror(errno));

        int pfd[2];
        if (pipe(pfd) != 0) {
            printf("[FAIL] sched pipe setup\n");
            nfail++;
        } else {
            pid_t c = fork();
            if (c == 0) {
                char ch;
                (void)read(pfd[0], &ch, 1);   // block until parent has queried
                _exit(0);
            }
            close(pfd[0]);
            int pol = sched_getscheduler(c);
            int perr = errno;
            struct sched_param gp = { .sched_priority = -1 };
            (void)sched_getparam(c, &gp);
            int ppol = sched_getscheduler(getpid());
            printf("[sched] child policy=%d (want %d=SCHED_NORMAL) prio=%d (want 0) parent policy=%d (want %d=SCHED_FIFO)\n",
                   pol, SCHED_OTHER, gp.sched_priority, ppol, SCHED_FIFO);
            check("sched: child policy reset to SCHED_NORMAL",
                  pol == SCHED_OTHER, pol, perr);
            check("sched: child prio reset to 0",
                  gp.sched_priority == 0, gp.sched_priority, errno);
            check("sched: parent policy remains SCHED_FIFO",
                  ppol == SCHED_FIFO, ppol, errno);

            // Raw sched_getscheduler(120) / sched_getparam(121) bypass musl's
            // wrappers, to isolate a kernel bug from a musl-wrapper bug.
            errno = 0;
            long rg = syscall(120, c);   // SYS_sched_getscheduler
            int rge = errno;
            printf("[raw] SYS_sched_getscheduler(%d) = %ld errno=%d %s\n",
                   c, rg, rge, strerror(rge));
            errno = 0;
            long rp = syscall(121, c, &gp);   // SYS_sched_getparam
            int rpe = errno;
            printf("[raw] SYS_sched_getparam(%d) ret=%ld errno=%d prio=%d\n",
                   c, rp, rpe, gp.sched_priority);
            errno = 0;
            long rgs = syscall(120, getpid());   // parent raw getscheduler
            printf("[raw] SYS_sched_getscheduler(parent) = %ld errno=%d\n",
                   rgs, errno);
            (void)write(pfd[1], "x", 1);
            close(pfd[1]);
            int st = 0;
            (void)waitpid(c, &st, 0);
        }
    }

    // Fork latency under TCG: fork06 (1000 forks), fcntl14(_64) (5000 forks) and
    // epoll01 (fork-per-test in epoll_ctl) all timeout because a single fork is
    // ~O(address-space) under QEMU TCG. Measure it to quantify the 3 TIMEOUTs.
    {
        int NF = 20;
        struct timespec t0, t1;
        (void)clock_gettime(CLOCK_MONOTONIC, &t0);
        for (int i = 0; i < NF; i++) {
            pid_t c = fork();
            if (c == 0)
                _exit(0);
            int st;
            (void)waitpid(c, &st, 0);
        }
        (void)clock_gettime(CLOCK_MONOTONIC, &t1);
        long us = (t1.tv_sec - t0.tv_sec) * 1000000L +
                  (t1.tv_nsec - t0.tv_nsec) / 1000;
        printf("[fork] %d fork+wait cycles = %ld us (%.1f ms/fork)\n",
               NF, us, (double)us / NF / 1000.0);
    }

    printf("__LTP_GATE_DONE__\n");
    if (nfail == 0)
        printf("__LTP_GATE_PASS__\n");
    else
        printf("__LTP_GATE_FAIL__\n");
    return 0;
}
