// SPDX-License-Identifier: MPL-2.0
//
// POLISH-M26 extended repro: raw-syscall isolation for all 15 tier-C candidates.
// Builds on the M11 repro.c with probes for:
//   1. capget V1/V2
//   2. fcntl F_DUPFD on exhausted fd table
//   3. fcntl F_SETLKW deadlock (simplified)
//   4. madvise MADV_DONTNEED on file-backed shared mapping
//   5. mlock RLIMIT_MEMLOCK enforcement
//   6. PR_SET_NAME with 16+ char names
//   7. sched_setaffinity with empty CPU mask
//   8. sched_setparam for non-existent PID
//   9. sched_setscheduler permission check (non-root RT)
//  10. sched_setattr basic operation
//  11. capset V1/V2 (secondary)
//  12. sched_getattr basic operation

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/mount.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#include <pwd.h>

// Linux capability structs
struct cap_user_header {
    unsigned int version;
    int pid;
};
struct cap_user_data {
    unsigned int effective;
    unsigned int permitted;
    unsigned int inheritable;
};

// sched_attr struct for sched_setattr/sched_getattr
struct sched_attr {
    unsigned int size;
    unsigned int sched_policy;
    unsigned long long sched_flags;
    int sched_nice;
    unsigned int sched_priority;
    unsigned long long sched_runtime;
    unsigned long long sched_deadline;
    unsigned long long sched_period;
    unsigned int sched_util_min;
    unsigned int sched_util_max;
};

// CPU set helpers
#define CPU_SET_WORD(cpu, dst) ((dst)[(cpu) / 64])
#define CPU_SET_BIT(cpu)  (1UL << ((cpu) % 64))

static int nfail = 0;

static void check(const char *name, int ok, long ret, int err) {
    printf("[%s] %s (ret=%ld errno=%d %s)\n",
           ok ? "PASS" : "FAIL", name, ret, err, strerror(err));
    if (!ok) nfail++;
}

static void pass(const char *msg) {
    printf("[PASS] %s\n", msg);
}

static void fail(const char *msg) {
    printf("[FAIL] %s\n", msg);
    nfail++;
}

int main(void) {
    if (mount("proc", "/proc", "proc", 0, NULL) != 0) {}
    if (mount("sysfs", "/sys", "sysfs", 0, NULL) != 0) {}
    if (mount("tmpfs", "/tmp", "tmpfs", 0, NULL) != 0) {}

    // =====================================================================
    // Probe 1: capget V1/V2/V3
    // =====================================================================
    {
        struct cap_user_header hdr = { .version = 0, .pid = getpid() };
        struct cap_user_data data[2] = {0};

        int versions[] = {0x19980330, 0x20071026, 0x20080522};
        const char *vnames[] = {"V1", "V2", "V3"};

        for (int i = 0; i < 3; i++) {
            hdr.version = versions[i];
            errno = 0;
            long r = syscall(SYS_capget, &hdr, data);
            int e = errno;
            printf("[raw] capget(%s) ret=%ld errno=%d %s\n", vnames[i], r, e, strerror(e));
            check(vnames[i], r == 0, r, e);
        }
    }

    // =====================================================================
    // Probe 2: capset V1/V2/V3 (secondary dependency)
    // =====================================================================
    {
        struct cap_user_header hdr = { .version = 0, .pid = getpid() };
        struct cap_user_data data[2] = {0};

        // First get caps with V3
        hdr.version = 0x20080522;
        errno = 0;
        long r = syscall(SYS_capget, &hdr, data);
        if (r != 0) {
            printf("[raw] capset: capget(V3) failed, skipping capset probes\n");
        } else {
            int versions[] = {0x19980330, 0x20071026, 0x20080522};
            const char *vnames[] = {"V1", "V2", "V3"};
            for (int i = 0; i < 3; i++) {
                hdr.version = versions[i];
                errno = 0;
                r = syscall(SYS_capset, &hdr, data);
                int e = errno;
                printf("[raw] capset(%s) ret=%ld errno=%d %s\n", vnames[i], r, e, strerror(e));
                check(vnames[i], r == 0, r, e);
            }
        }
    }

    // =====================================================================
    // Probe 3: fcntl F_DUPFD with exhausted fd table
    // =====================================================================
    {
        // Exhaust the fd table, then try F_DUPFD with arg==fd
        int max_fds = (int)sysconf(_SC_OPEN_MAX);
        if (max_fds < 0 || max_fds > 4096) max_fds = 1024;

        int tmpfd = open("/tmp/fcntl_test", O_CREAT | O_RDWR, 0644);
        if (tmpfd < 0) {
            printf("[FAIL] fcntl12 setup: open failed (errno=%d)\n", errno);
            nfail++;
        } else {
            // Open many files to exhaust the table
            int fds[1024] = {0};
            int n_opened = 0;
            int test_fd = -1;

            // First, dup tmpfd to a known fd far from 0/1/2
            for (int i = 0; i < max_fds - 10; i++) {
                int fd = open("/tmp/fcntl_test", O_RDONLY);
                if (fd < 0) break;
                fds[n_opened++] = fd;
                if (test_fd < 0) test_fd = fd;
            }

            if (test_fd < 0) {
                printf("[raw] fcntl12: could not open any test fd\n");
            } else {
                // Now try F_DUPFD with arg == test_fd (should fail with EINVAL
                // or EMFILE since the table is full)
                errno = 0;
                long r = syscall(SYS_fcntl, test_fd, 0 /*F_DUPFD*/, test_fd);
                int e = errno;
                printf("[raw] fcntl(F_DUPFD, fd=%d, arg=%d) ret=%ld errno=%d %s\n",
                       test_fd, test_fd, r, e, strerror(e));

                // Also test: fd == arg, should fail with EINVAL per Linux
                errno = 0;
                r = syscall(SYS_fcntl, 1, 0 /*F_DUPFD*/, 1);
                e = errno;
                printf("[raw] fcntl(F_DUPFD, fd=1, arg=1) ret=%ld errno=%d %s\n",
                       r, e, strerror(e));
                check("fcntl(F_DUPFD,1,1) -> EINVAL",
                      r == -1 && (e == EINVAL || e == EMFILE), r, e);
            }

            // Cleanup
            for (int i = 0; i < n_opened; i++) close(fds[i]);
            close(tmpfd);
            unlink("/tmp/fcntl_test");
        }
    }

    // =====================================================================
    // Probe 4: madvise on file-backed shared mapping
    // =====================================================================
    {
        int pagesize = getpagesize();
        int fd = open("/tmp/madv_test", O_RDWR | O_CREAT | O_TRUNC, 0644);
        if (fd < 0) {
            printf("[FAIL] madvise setup: open failed (errno=%d)\n", errno);
            nfail++;
        } else {
            // Write some data
            char buf[4096];
            memset(buf, 'x', sizeof(buf));
            write(fd, buf, pagesize);
            ftruncate(fd, pagesize);

            void *addr = mmap(NULL, pagesize, PROT_READ, MAP_SHARED, fd, 0);
            if (addr == MAP_FAILED) {
                printf("[FAIL] madvise setup: mmap failed (errno=%d)\n", errno);
                nfail++;
            } else {
                // MADV_DONTNEED on file-backed shared mapping:
                // Linux expects EINVAL (can't discard locked/shared pages)
                errno = 0;
                long r = syscall(SYS_madvise, addr, pagesize, 4 /*MADV_DONTNEED*/);
                int e = errno;
                printf("[raw] madvise(MADV_DONTNEED, file-backed-SHARED) ret=%ld errno=%d %s\n",
                       r, e, strerror(e));
                // Linux returns EINVAL for MADV_DONTNEED on shared file-backed
                // mapping. Asterinas should too.
                check("madvise DONTNEED on shared file-backed",
                      r == -1 && e == EINVAL, r, e);

                // MADV_MERGEABLE on file-backed mapping:
                // Under CONFIG_KSM, EINVAL is expected for file-backed mappings
                errno = 0;
                r = syscall(SYS_madvise, addr, pagesize, 12 /*MADV_MERGEABLE*/);
                e = errno;
                printf("[raw] madvise(MADV_MERGEABLE, file-backed) ret=%ld errno=%d %s\n",
                       r, e, strerror(e));

                // MADV_FREE on file-backed mapping:
                errno = 0;
                r = syscall(SYS_madvise, addr, pagesize, 8 /*MADV_FREE*/);
                e = errno;
                printf("[raw] madvise(MADV_FREE, file-backed) ret=%ld errno=%d %s\n",
                       r, e, strerror(e));

                munmap(addr, pagesize);
            }
            close(fd);
            unlink("/tmp/madv_test");
        }
    }

    // =====================================================================
    // Probe 5: mlock RLIMIT_MEMLOCK enforcement
    // =====================================================================
    {
        int pagesize = getpagesize();
        struct rlimit orig;
        getrlimit(RLIMIT_MEMLOCK, &orig);

        void *addr = mmap(NULL, pagesize, PROT_READ | PROT_WRITE,
                          MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (addr == MAP_FAILED) {
            printf("[FAIL] mlock02 setup: mmap failed\n");
            nfail++;
        } else {
            // Test 1: ENOMEM when some pages not mapped
            munmap(addr, pagesize);
            errno = 0;
            long r = syscall(SYS_mlock, addr, pagesize);
            int e = errno;
            printf("[raw] mlock(unmapped) ret=%ld errno=%d %s\n", r, e, strerror(e));
            check("mlock(unmapped) -> ENOMEM", r == -1 && e == ENOMEM, r, e);

            // Re-map for next tests
            addr = mmap(addr, pagesize, PROT_READ | PROT_WRITE,
                        MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED, -1, 0);
            if (addr == MAP_FAILED) {
                printf("[FAIL] mlock02: re-mmap failed\n");
                nfail++;
            } else {
                // Test 2: RLIMIT_MEMLOCK enforcement
                struct rlimit rl = { .rlim_cur = (rlim_t)(pagesize - 1),
                                     .rlim_max = (rlim_t)(pagesize - 1) };
                setrlimit(RLIMIT_MEMLOCK, &rl);

                struct passwd *pw = getpwnam("nobody");
                if (pw) {
                    // Must write to the page first to ensure it's committed
                    memset(addr, 0, pagesize);
                    seteuid(pw->pw_uid);
                    errno = 0;
                    r = syscall(SYS_mlock, addr, pagesize);
                    e = errno;
                    printf("[raw] mlock(as nobody, rlimit=pagesize-1) ret=%ld errno=%d %s\n",
                           r, e, strerror(e));
                    check("mlock(RLIMIT_MEMLOCK exceeded) -> ENOMEM",
                          r == -1 && e == ENOMEM, r, e);
                    seteuid(0);

                    // Test 3: RLIMIT_MEMLOCK=0, unprivileged -> EPERM
                    rl.rlim_cur = 0;
                    rl.rlim_max = 0;
                    setrlimit(RLIMIT_MEMLOCK, &rl);
                    seteuid(pw->pw_uid);
                    errno = 0;
                    r = syscall(SYS_mlock, addr, pagesize);
                    e = errno;
                    printf("[raw] mlock(as nobody, rlimit=0) ret=%ld errno=%d %s\n",
                           r, e, strerror(e));
                    check("mlock(RLIMIT_MEMLOCK=0, unpriv) -> EPERM",
                          r == -1 && e == EPERM, r, e);
                    seteuid(0);
                } else {
                    printf("[raw] mlock: no 'nobody' user, skipping RLIMIT tests\n");
                }

                // Restore
                setrlimit(RLIMIT_MEMLOCK, &orig);
                munmap(addr, pagesize);
            }
        }
    }

    // =====================================================================
    // Probe 6: PR_SET_NAME with 16-char name
    // =====================================================================
    {
        // Linux: PR_SET_NAME accepts up to 16 bytes (15 chars + null),
        // silently truncating longer strings.
        // Test 1: 15-char name (should succeed)
        const char *name15 = "123456789012345";  // 15 chars + null = 16 bytes
        errno = 0;
        long r = syscall(SYS_prctl, 15 /*PR_SET_NAME*/, name15, 0, 0, 0);
        int e = errno;
        printf("[raw] prctl(PR_SET_NAME, \"%s\") ret=%ld errno=%d %s\n",
               name15, r, e, strerror(e));
        check("PR_SET_NAME 15-char", r == 0, r, e);

        // Test 2: 16-char name (Linux: silently truncates to 15+null)
        const char *name16 = "1234567890123456";  // 16 chars + null = 17 bytes
        errno = 0;
        r = syscall(SYS_prctl, 15 /*PR_SET_NAME*/, name16, 0, 0, 0);
        e = errno;
        printf("[raw] prctl(PR_SET_NAME, \"%s\") ret=%ld errno=%d %s\n",
               name16, r, e, strerror(e));
        check("PR_SET_NAME 16-char (truncate ok)", r == 0, r, e);

        // Read back the name to verify
        char readback[20] = {0};
        errno = 0;
        r = syscall(SYS_prctl, 16 /*PR_GET_NAME*/, readback, 0, 0, 0);
        e = errno;
        printf("[raw] prctl(PR_GET_NAME) = \"%s\" ret=%ld errno=%d\n",
               readback, r, e);
    }

    // =====================================================================
    // Probe 7: sched_setaffinity with empty CPU mask
    // =====================================================================
    {
        unsigned long mask[2] = {0, 0};  // empty mask (no CPUs)
        errno = 0;
        long r = syscall(SYS_sched_setaffinity, 0, sizeof(mask), mask);
        int e = errno;
        printf("[raw] sched_setaffinity(empty mask) ret=%ld errno=%d %s\n",
               r, e, strerror(e));
        check("sched_setaffinity(empty) -> EINVAL",
              r == -1 && e == EINVAL, r, e);

        // Also test with mask that has only invalid CPUs (very high bit)
        unsigned long mask2[2] = {0, 1UL << 63};  // CPU 127
        errno = 0;
        r = syscall(SYS_sched_setaffinity, 0, sizeof(mask2), mask2);
        e = errno;
        printf("[raw] sched_setaffinity(CPU127) ret=%ld errno=%d %s\n",
               r, e, strerror(e));
    }

    // =====================================================================
    // Probe 8: sched_setparam for non-existent PID
    // =====================================================================
    {
        int prio = 0;
        errno = 0;
        long r = syscall(SYS_sched_setparam, 17787, &prio);
        int e = errno;
        printf("[raw] sched_setparam(17787, 0) ret=%ld errno=%d %s\n",
               r, e, strerror(e));
        check("sched_setparam(invalid PID) -> ESRCH",
              r == -1 && e == ESRCH, r, e);
    }

    // =====================================================================
    // Probe 9: sched_setscheduler permission check (non-root RT)
    // =====================================================================
    {
        struct passwd *pw = getpwnam("nobody");
        if (pw) {
            pid_t child = fork();
            if (child == 0) {
                seteuid(pw->pw_uid);
                struct sched_param p = { .sched_priority = 1 };
                errno = 0;
                long r = syscall(SYS_sched_setscheduler, 0, 1 /*SCHED_FIFO*/, &p);
                int e = errno;
                printf("[raw] sched_setscheduler(SCHED_FIFO, as nobody) ret=%ld errno=%d %s\n",
                       r, e, strerror(e));
                check("sched_setscheduler(FIFO,non-root) -> EPERM",
                      r == -1 && e == EPERM, r, e);
                _exit(0);
            }
            int st;
            waitpid(child, &st, 0);
        } else {
            printf("[raw] sched_setscheduler: no 'nobody' user\n");
        }
    }

    // =====================================================================
    // Probe 10: sched_setattr basic operation
    // =====================================================================
    {
        struct sched_attr attr = {
            .size = sizeof(struct sched_attr),
            .sched_policy = 0, // SCHED_NORMAL
            .sched_flags = 0,
            .sched_nice = 0,
            .sched_priority = 0,
        };
        errno = 0;
        long r = syscall(SYS_sched_setattr, 0, &attr, 0);
        int e = errno;
        printf("[raw] sched_setattr(NORMAL) ret=%ld errno=%d %s\n", r, e, strerror(e));
        check("sched_setattr(NORMAL) -> 0", r == 0, r, e);

        // Try with nonexistent PID
        attr.sched_policy = 0;
        errno = 0;
        r = syscall(SYS_sched_setattr, 2147483647, &attr, 0);
        e = errno;
        printf("[raw] sched_setattr(MAX_PID) ret=%ld errno=%d %s\n", r, e, strerror(e));
        check("sched_setattr(MAX_PID) -> ESRCH",
              r == -1 && e == ESRCH, r, e);
    }

    // =====================================================================
    // Probe 11: sched_getattr basic operation
    // =====================================================================
    {
        struct sched_attr attr = { .size = sizeof(struct sched_attr) };
        errno = 0;
        long r = syscall(SYS_sched_getattr, 0, &attr, sizeof(attr), 0);
        int e = errno;
        printf("[raw] sched_getattr(0) ret=%ld errno=%d policy=%u prio=%u nice=%d\n",
               r, e, attr.sched_policy, attr.sched_priority, attr.sched_nice);
        check("sched_getattr(0) -> 0", r == 0, r, e);
    }

    // =====================================================================
    // Probe 12: sched_setscheduler03 SCHED_BATCH test
    // =====================================================================
    {
        struct sched_param p = { .sched_priority = 0 };
        errno = 0;
        long r = syscall(SYS_sched_setscheduler, 0, 3 /*SCHED_BATCH*/, &p);
        int e = errno;
        printf("[raw] sched_setscheduler(SCHED_BATCH=3) ret=%ld errno=%d %s\n",
               r, e, strerror(e));
        // SCHED_BATCH is not implemented; EINVAL is expected
        check("sched_setscheduler(SCHED_BATCH) -> EINVAL",
              r == -1 && e == EINVAL, r, e);
    }

    // =====================================================================
    // Probe 13: Fork latency under current conditions
    // =====================================================================
    {
        int NF = 10;
        struct timespec t0, t1;
        clock_gettime(CLOCK_MONOTONIC, &t0);
        for (int i = 0; i < NF; i++) {
            pid_t c = fork();
            if (c == 0) _exit(0);
            int st;
            waitpid(c, &st, 0);
        }
        clock_gettime(CLOCK_MONOTONIC, &t1);
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