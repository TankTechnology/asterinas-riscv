// SPDX-License-Identifier: MPL-2.0
//
// FOUNDATION-M3 smoke test for the Asterinas RISC-V NixOS track.
//
// Runs as pid 1. Each test exercises one M3 item (pivot_root, /proc/self/mountinfo,
// mount propagation, openat2, membarrier, mount_setattr) and prints a fixed
// marker so the QEMU driver can attribute a crash/ENOSYS to the exact syscall.
// A final `__FM3_DONE__` line ends the run; every per-test line is prefixed
// `[FM3]` and each test prints `__FM3_<NAME>_OK__` or `__FM3_<NAME>_FAIL__`.
//
// Raw syscalls (openat2/membarrier/mount_setattr) use the riscv64 asm-generic
// numbers so the same binary works before/after the kernel implements them.

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/mount.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

// --- riscv64 asm-generic syscall numbers (not all in glibc headers) ---
#ifndef SYS_openat2
#define SYS_openat2 437
#endif
#ifndef SYS_membarrier
#define SYS_membarrier 283
#endif
#ifndef SYS_mount_setattr
#define SYS_mount_setattr 442
#endif
#ifndef SYS_pivot_root
#define SYS_pivot_root 41
#endif

// --- openat2 (linux/openat2.h) ---
struct open_how {
    unsigned long long flags;
    unsigned long long mode;
    unsigned long long resolve;
};
#define RESOLVE_NO_XDEV 0x01
#define RESOLVE_NO_MAGICLINKS 0x02
#define RESOLVE_NO_SYMLINKS 0x04
#define RESOLVE_BENEATH 0x08
#define RESOLVE_IN_ROOT 0x10
#define RESOLVE_CACHED 0x20

// --- membarrier (linux/membarrier.h) ---
#define MEMBARRIER_CMD_QUERY 0
#define MEMBARRIER_CMD_GLOBAL (1 << 0)
#define MEMBARRIER_CMD_GLOBAL_EXPEDITED (1 << 1)
#define MEMBARRIER_CMD_REGISTER_GLOBAL_EXPEDITED (1 << 2)
#define MEMBARRIER_CMD_PRIVATE_EXPEDITED (1 << 3)
#define MEMBARRIER_CMD_REGISTER_PRIVATE_EXPEDITED (1 << 4)
#define MEMBARRIER_CMD_PRIVATE_EXPEDITED_SYNC_CORE (1 << 5)
#define MEMBARRIER_CMD_REGISTER_PRIVATE_EXPEDITED_SYNC_CORE (1 << 6)
#define MEMBARRIER_CMD_GET_REGISTRATIONS (1 << 9)

// --- mount_setattr (linux/mount.h) ---
#ifndef MOUNT_ATTR_SIZE_VER0
struct mount_attr {
    unsigned long long attr_set;
    unsigned long long attr_clr;
    unsigned long long propagation;
    unsigned long long userns_fd;
};
#endif
#ifndef MOUNT_ATTR_NODEV
#define MOUNT_ATTR_NODEV 0x00000004
#endif
#ifndef MOUNT_ATTR_NOEXEC
#define MOUNT_ATTR_NOEXEC 0x00000008
#endif
#ifndef AT_RECURSIVE
#define AT_RECURSIVE 0x8000
#endif

// --- mount propagation (mount(2) flags) ---
#ifndef MS_PRIVATE
#define MS_PRIVATE (1 << 18)
#endif
#ifndef MS_SLAVE
#define MS_SLAVE (1 << 19)
#endif
#ifndef MS_SHARED
#define MS_SHARED (1 << 20)
#endif
#ifndef MS_UNBINDABLE
#define MS_UNBINDABLE (1 << 17)
#endif

static int failures = 0;

static void say(const char *s) {
    (void)write(1, s, strlen(s));
}

static void ok(const char *name) {
    char buf[128];
    int n = snprintf(buf, sizeof(buf), "[FM3] %s: OK  __FM3_%s_OK__\n", name, name);
    (void)write(1, buf, (size_t)n);
}

static void fail(const char *name, const char *why) {
    char buf[256];
    int n = snprintf(buf, sizeof(buf), "[FM3] %s: FAIL (%s)  __FM3_%s_FAIL__\n", name,
                     why, name);
    (void)write(1, buf, (size_t)n);
    failures++;
}

static void check(const char *name, int cond, const char *why) {
    if (cond) {
        ok(name);
    } else {
        fail(name, why);
    }
}

static void dump_file(const char *path) {
    int fd = open(path, O_RDONLY);
    if (fd < 0) {
        say("  (cannot open)\n");
        return;
    }
    char buf[4096];
    ssize_t n;
    while ((n = read(fd, buf, sizeof(buf))) > 0) {
        (void)write(1, buf, (size_t)n);
    }
    (void)close(fd);
}

// ---------------------------------------------------------------------------
// Test 1: /proc/self/mountinfo field format (mount ID / parent ID / major:minor)
// ---------------------------------------------------------------------------
static void test_mountinfo(void) {
    say("  --- /proc/self/mountinfo ---\n");
    dump_file("/proc/self/mountinfo");

    // Re-open and validate each line has >= 10 space-separated fields, the
    // 3rd field has a ':' (major:minor), and the 2nd is a numeric parent id.
    int fd = open("/proc/self/mountinfo", O_RDONLY);
    if (fd < 0) {
        fail("mountinfo", "open failed");
        return;
    }
    char buf[65536];
    ssize_t n = read(fd, buf, sizeof(buf) - 1);
    (void)close(fd);
    if (n <= 0) {
        fail("mountinfo", "read returned nothing");
        return;
    }
    buf[n] = '\0';

    int lines = 0, good = 1;
    char *saveptr = NULL;
    for (char *line = strtok_r(buf, "\n", &saveptr); line; line = strtok_r(NULL, "\n", &saveptr)) {
        if (*line == '\0')
            continue;
        lines++;
        char *fields[64];
        int nf = 0;
        char *sp = NULL;
        for (char *tok = strtok_r(line, " ", &sp); tok && nf < 64;
             tok = strtok_r(NULL, " ", &sp)) {
            fields[nf++] = tok;
        }
        if (nf < 10) {
            good = 0;
            char msg[128];
            snprintf(msg, sizeof(msg), "line %d has %d fields (<10)", lines, nf);
            fail("mountinfo", msg);
            return;
        }
        // field 3 must be major:minor
        if (strchr(fields[2], ':') == NULL) {
            good = 0;
            fail("mountinfo", "field 3 missing ':' (major:minor)");
            return;
        }
        // field 1 and 2 must be numeric
        for (int i = 0; i < 2; i++) {
            for (char *p = fields[i]; *p; p++) {
                if (*p < '0' || *p > '9') {
                    good = 0;
                    fail("mountinfo", "mount id / parent id not numeric");
                    return;
                }
            }
        }
    }
    check("mountinfo", lines > 0 && good, "no valid lines");
}

// ---------------------------------------------------------------------------
// Test 2: pivot_root (initramfs root -> tmpfs root)
// ---------------------------------------------------------------------------
static void test_pivot_root(void) {
    if (mkdir("/newroot", 0755) != 0 && errno != EEXIST) {
        fail("pivot_root", "mkdir /newroot");
        return;
    }
    if (mount("tmpfs", "/newroot", "tmpfs", 0, NULL) != 0) {
        fail("pivot_root", "mount tmpfs /newroot");
        return;
    }
    if (mkdir("/newroot/oldroot", 0755) != 0) {
        fail("pivot_root", "mkdir /newroot/oldroot");
        return;
    }
    // Save a marker inside the OLD root so we can prove it is now under /oldroot.
    int fd = open("/newroot/oldroot/.oldroot_marker", O_WRONLY | O_CREAT | O_TRUNC, 0644);
    // Note: writing to /newroot/oldroot actually writes into the old rootfs if
    // oldroot is not yet detached; keep it simple and just detect /oldroot/init.
    if (fd >= 0)
        (void)close(fd);

    if (chdir("/newroot") != 0) {
        fail("pivot_root", "chdir /newroot");
        return;
    }
    long r = syscall(SYS_pivot_root, ".", "oldroot");
    if (r != 0) {
        char msg[128];
        snprintf(msg, sizeof(msg), "pivot_root -> errno %d (%s)", errno, strerror(errno));
        fail("pivot_root", msg);
        return;
    }
    if (chdir("/") != 0) {
        fail("pivot_root", "chdir / after pivot");
        return;
    }

    // The old initramfs root (which contained /init, /bin) is now at /oldroot.
    struct stat st;
    int saw_oldroot = (stat("/oldroot", &st) == 0);
    int saw_oldinit = (stat("/oldroot/init", &st) == 0);
    check("pivot_root", saw_oldroot && saw_oldinit, "old root not visible at /oldroot");

    // Detach the old root (this is what a real initramfs does before freeing it).
    if (umount2("/oldroot", MNT_DETACH) != 0) {
        char msg[128];
        snprintf(msg, sizeof(msg), "umount /oldroot -> errno %d (%s)", errno, strerror(errno));
        fail("pivot_root", msg);
        return;
    }
    say("  pivot_root: old root detached OK\n");
}

// ---------------------------------------------------------------------------
// Test 3: mount propagation (MS_SHARED / MS_SLAVE / MS_PRIVATE)
// ---------------------------------------------------------------------------
static void test_mount_propagation(void) {
    // Minimal acceptance: the propagation-change syscall returns 0 instead of EINVAL.
    long r = syscall(SYS_mount, NULL, "/", NULL, MS_SHARED, NULL);
    char msg[128];
    if (r != 0) {
        snprintf(msg, sizeof(msg), "MS_SHARED on / -> errno %d (%s)", errno, strerror(errno));
        fail("mountprop_shared", msg);
    } else {
        ok("mountprop_shared");
    }

    r = syscall(SYS_mount, NULL, "/", NULL, MS_SLAVE, NULL);
    if (r != 0) {
        snprintf(msg, sizeof(msg), "MS_SLAVE on / -> errno %d (%s)", errno, strerror(errno));
        fail("mountprop_slave", msg);
    } else {
        ok("mountprop_slave");
    }

    r = syscall(SYS_mount, NULL, "/", NULL, MS_PRIVATE, NULL);
    if (r != 0) {
        snprintf(msg, sizeof(msg), "MS_PRIVATE on / -> errno %d (%s)", errno, strerror(errno));
        fail("mountprop_private", msg);
    } else {
        ok("mountprop_private");
    }

    // mountinfo should now report the optional propagation fields.
    say("  --- /proc/self/mountinfo after propagation change ---\n");
    dump_file("/proc/self/mountinfo");
}

// ---------------------------------------------------------------------------
// Test 4: openat2
// ---------------------------------------------------------------------------
static void test_openat2(void) {
    struct open_how how;
    memset(&how, 0, sizeof(how));
    how.flags = O_RDONLY;
    how.resolve = 0;

    long fd = syscall(SYS_openat2, AT_FDCWD, "/proc/self/mountinfo", &how, sizeof(how));
    char msg[128];
    if (fd < 0) {
        snprintf(msg, sizeof(msg), "openat2 -> errno %d (%s)", errno, strerror(errno));
        fail("openat2", msg);
        return;
    }
    (void)close((int)fd);

    // A bad resolve flag must be rejected with EINVAL.
    how.resolve = 0x80000000; /* unknown bit */
    long bad = syscall(SYS_openat2, AT_FDCWD, "/proc/self/mountinfo", &how, sizeof(how));
    if (bad < 0) {
        if (errno == EINVAL) {
            ok("openat2");
        } else {
            snprintf(msg, sizeof(msg), "bad resolve -> errno %d (want EINVAL)", errno);
            fail("openat2", msg);
        }
    } else {
        (void)close((int)bad);
        fail("openat2", "unknown resolve bit accepted");
    }
}

// ---------------------------------------------------------------------------
// Test 5: membarrier
// ---------------------------------------------------------------------------
static void test_membarrier(void) {
    long query = syscall(SYS_membarrier, MEMBARRIER_CMD_QUERY, 0, 0);
    char msg[128];
    if (query < 0) {
        snprintf(msg, sizeof(msg), "membarrier QUERY -> errno %d (%s)", errno, strerror(errno));
        fail("membarrier", msg);
        return;
    }
    // Query must advertise both execution and its mandatory registration.
    if ((query & MEMBARRIER_CMD_PRIVATE_EXPEDITED) == 0 ||
        (query & MEMBARRIER_CMD_REGISTER_PRIVATE_EXPEDITED) == 0) {
        fail("membarrier", "QUERY does not advertise private expedited pair");
        return;
    }
    if ((query & MEMBARRIER_CMD_GLOBAL) == 0 ||
        (query & MEMBARRIER_CMD_GLOBAL_EXPEDITED) == 0 ||
        (query & MEMBARRIER_CMD_REGISTER_GLOBAL_EXPEDITED) == 0) {
        fail("membarrier", "QUERY does not advertise global commands");
        return;
    }

    long r = syscall(SYS_membarrier, MEMBARRIER_CMD_GLOBAL, 0, 0);
    if (r != 0) {
        snprintf(msg, sizeof(msg), "GLOBAL -> errno %d (%s)", errno, strerror(errno));
        fail("membarrier", msg);
        return;
    }

    r = syscall(SYS_membarrier, MEMBARRIER_CMD_GLOBAL_EXPEDITED, 0, 0);
    if (r != 0) {
        snprintf(msg, sizeof(msg), "GLOBAL_EXPEDITED -> errno %d (%s)", errno,
                 strerror(errno));
        fail("membarrier", msg);
        return;
    }

    r = syscall(SYS_membarrier, MEMBARRIER_CMD_REGISTER_GLOBAL_EXPEDITED, 0, 0);
    if (r != 0) {
        snprintf(msg, sizeof(msg), "REGISTER_GLOBAL_EXPEDITED -> errno %d (%s)", errno,
                 strerror(errno));
        fail("membarrier", msg);
        return;
    }

    // Linux requires registration before executing a private expedited
    // barrier. The previous smoke test accidentally accepted a false-success
    // implementation that skipped both this check and the actual barrier.
    r = syscall(SYS_membarrier, MEMBARRIER_CMD_PRIVATE_EXPEDITED, 0, 0);
    if (r == 0 || errno != EPERM) {
        fail("membarrier", "unregistered PRIVATE_EXPEDITED did not return EPERM");
        return;
    }

    r = syscall(SYS_membarrier, MEMBARRIER_CMD_REGISTER_PRIVATE_EXPEDITED, 0, 0);
    if (r != 0) {
        snprintf(msg, sizeof(msg), "REGISTER_PRIVATE_EXPEDITED -> errno %d (%s)", errno,
                 strerror(errno));
        fail("membarrier", msg);
        return;
    }

    long registrations = syscall(SYS_membarrier, MEMBARRIER_CMD_GET_REGISTRATIONS, 0, 0);
    if ((registrations & MEMBARRIER_CMD_REGISTER_PRIVATE_EXPEDITED) == 0) {
        fail("membarrier", "GET_REGISTRATIONS omitted private expedited registration");
        return;
    }

    r = syscall(SYS_membarrier, MEMBARRIER_CMD_PRIVATE_EXPEDITED, 0, 0);
    if (r != 0) {
        snprintf(msg, sizeof(msg), "PRIVATE_EXPEDITED -> errno %d (%s)", errno, strerror(errno));
        fail("membarrier", msg);
        return;
    }

    // RISC-V must also expose and enforce registration for sync-core. The
    // dedicated SMP4 regression exercises its cross-hart JIT visibility.
    if ((query & MEMBARRIER_CMD_PRIVATE_EXPEDITED_SYNC_CORE) == 0 ||
        (query & MEMBARRIER_CMD_REGISTER_PRIVATE_EXPEDITED_SYNC_CORE) == 0) {
        fail("membarrier", "QUERY does not advertise sync-core pair");
        return;
    }

    errno = 0;
    r = syscall(SYS_membarrier, MEMBARRIER_CMD_PRIVATE_EXPEDITED_SYNC_CORE, 0, 0);
    if (r == 0 || errno != EPERM) {
        fail("membarrier", "unregistered sync-core did not return EPERM");
        return;
    }

    r = syscall(SYS_membarrier, MEMBARRIER_CMD_REGISTER_PRIVATE_EXPEDITED_SYNC_CORE, 0, 0);
    if (r != 0) {
        snprintf(msg, sizeof(msg), "REGISTER_SYNC_CORE -> errno %d (%s)", errno,
                 strerror(errno));
        fail("membarrier", msg);
        return;
    }

    registrations = syscall(SYS_membarrier, MEMBARRIER_CMD_GET_REGISTRATIONS, 0, 0);
    if ((registrations & MEMBARRIER_CMD_REGISTER_PRIVATE_EXPEDITED_SYNC_CORE) == 0) {
        fail("membarrier", "GET_REGISTRATIONS omitted sync-core registration");
        return;
    }

    r = syscall(SYS_membarrier, MEMBARRIER_CMD_PRIVATE_EXPEDITED_SYNC_CORE, 0, 0);
    if (r != 0) {
        snprintf(msg, sizeof(msg), "PRIVATE_EXPEDITED_SYNC_CORE -> errno %d (%s)", errno,
                 strerror(errno));
        fail("membarrier", msg);
        return;
    }

    pid_t child = fork();
    if (child < 0) {
        fail("membarrier", "fork for registration inheritance failed");
        return;
    }
    if (child == 0) {
        long inherited = syscall(SYS_membarrier, MEMBARRIER_CMD_GET_REGISTRATIONS, 0, 0);
        long expected = MEMBARRIER_CMD_REGISTER_GLOBAL_EXPEDITED |
                        MEMBARRIER_CMD_REGISTER_PRIVATE_EXPEDITED |
                        MEMBARRIER_CMD_REGISTER_PRIVATE_EXPEDITED_SYNC_CORE;
        _exit((inherited & expected) == expected ? 0 : 1);
    }

    int child_status;
    if (waitpid(child, &child_status, 0) != child || !WIFEXITED(child_status) ||
        WEXITSTATUS(child_status) != 0) {
        fail("membarrier", "fork did not inherit registrations");
        return;
    }
    ok("membarrier");
}

// ---------------------------------------------------------------------------
// Test 6: mount_setattr
// ---------------------------------------------------------------------------
static void test_mount_setattr(void) {
    struct mount_attr attr;
    memset(&attr, 0, sizeof(attr));
    attr.attr_set = MOUNT_ATTR_NOEXEC;

    long r = syscall(SYS_mount_setattr, AT_FDCWD, "/", 0, &attr, sizeof(attr));
    char msg[128];
    if (r != 0) {
        snprintf(msg, sizeof(msg), "mount_setattr -> errno %d (%s)", errno, strerror(errno));
        fail("mount_setattr", msg);
        return;
    }

    // mount_setattr propagation: make / private, then shared, then slave.
    struct mount_attr p;
    memset(&p, 0, sizeof(p));
    p.propagation = MS_PRIVATE;
    r = syscall(SYS_mount_setattr, AT_FDCWD, "/", 0, &p, sizeof(p));
    if (r != 0) {
        snprintf(msg, sizeof(msg), "mount_setattr MS_PRIVATE -> errno %d", errno);
        fail("mount_setattr", msg);
        return;
    }
    memset(&p, 0, sizeof(p));
    p.propagation = MS_SHARED;
    r = syscall(SYS_mount_setattr, AT_FDCWD, "/", 0, &p, sizeof(p));
    if (r != 0) {
        snprintf(msg, sizeof(msg), "mount_setattr MS_SHARED -> errno %d", errno);
        fail("mount_setattr", msg);
        return;
    }
    memset(&p, 0, sizeof(p));
    p.propagation = MS_SLAVE;
    r = syscall(SYS_mount_setattr, AT_FDCWD, "/", 0, &p, sizeof(p));
    if (r != 0) {
        snprintf(msg, sizeof(msg), "mount_setattr MS_SLAVE -> errno %d", errno);
        fail("mount_setattr", msg);
        return;
    }
    ok("mount_setattr");
}

int main(void) {
    int fd = open("/dev/console", O_RDWR);
    if (fd < 0)
        fd = open("/dev/ttyS0", O_RDWR);
    if (fd >= 0) {
        (void)dup2(fd, 0);
        (void)dup2(fd, 1);
        (void)dup2(fd, 2);
        if (fd > 2)
            (void)close(fd);
    }

    say(">>> FOUNDATION-M3 smoke test <<<\n");

    if (mount("proc", "/proc", "proc", 0, NULL) != 0)
        say("init: mount /proc failed\n");
    if (mount("sysfs", "/sys", "sysfs", 0, NULL) != 0)
        say("init: mount /sys failed\n");
    if (mount("tmpfs", "/tmp", "tmpfs", 0, NULL) != 0)
        say("init: mount /tmp failed\n");

    test_mountinfo();
    test_openat2();
    test_membarrier();
    test_mount_propagation();
    test_mount_setattr();
    test_pivot_root();

    say(failures == 0 ? "__FM3_DONE__ __FM3_PASS__\n" : "__FM3_DONE__ __FM3_FAIL__\n");

    for (;;)
        (void)pause();
    return 0;
}
