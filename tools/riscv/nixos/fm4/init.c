// SPDX-License-Identifier: MPL-2.0
//
// FOUNDATION-M4 smoke test for the Asterinas RISC-V NixOS track.
//
// Exercises the security trio: fanotify (init/mark + event read), the keyring
// syscalls (add_key/request_key/keyctl), and seccomp strict mode. Runs as pid 1.
// Each test prints a `[FM4]` line plus a `__FM4_<NAME>_{OK,FAIL}__` marker so the
// QEMU driver can attribute a crash/ENOSYS to the exact syscall. A final
// `__FM4_DONE__` line ends the run.
//
// All syscalls use the riscv64 asm-generic numbers so the same binary reports
// ENOSYS before the kernel implements them and passes after.

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/fanotify.h>
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
#ifndef SYS_add_key
#define SYS_add_key 217
#endif
#ifndef SYS_request_key
#define SYS_request_key 218
#endif
#ifndef SYS_keyctl
#define SYS_keyctl 219
#endif
#ifndef SYS_fanotify_init
#define SYS_fanotify_init 262
#endif
#ifndef SYS_fanotify_mark
#define SYS_fanotify_mark 263
#endif
#ifndef SYS_seccomp
#define SYS_seccomp 277
#endif

// --- seccomp (linux/seccomp.h) ---
#define SECCOMP_SET_MODE_STRICT 0
#define SECCOMP_SET_MODE_FILTER 1

// --- keyctl (linux/keyctl.h) ---
#define KEYCTL_GET_KEYRING_ID 0
#define KEYCTL_JOIN_SESSION_KEYRING 1
#define KEY_SPEC_THREAD_KEYRING (-1)
#define KEY_SPEC_PROCESS_KEYRING (-2)
#define KEY_SPEC_SESSION_KEYRING (-3)
#define KEY_SPEC_USER_KEYRING (-4)
#define KEY_SPEC_USER_SESSION_KEYRING (-5)
#define KEY_SPEC_GROUP_KEYRING (-6)

static int failures = 0;

static void say(const char *s) { (void)write(1, s, strlen(s)); }

static void ok(const char *name) {
    char buf[128];
    int n = snprintf(buf, sizeof(buf), "[FM4] %s: OK  __FM4_%s_OK__\n", name, name);
    (void)write(1, buf, (size_t)n);
}

static void fail(const char *name, const char *why) {
    char buf[256];
    int n = snprintf(buf, sizeof(buf), "[FM4] %s: FAIL (%s)  __FM4_%s_FAIL__\n", name, why,
                     name);
    (void)write(1, buf, (size_t)n);
    failures++;
}

static void check(const char *name, int cond, const char *why) {
    if (cond)
        ok(name);
    else
        fail(name, why);
}

// ---------------------------------------------------------------------------
// Test 1: fanotify — init a group, mark a file, and observe a MODIFY event.
// ---------------------------------------------------------------------------
static void test_fanotify(void) {
    long fd = syscall(SYS_fanotify_init, FAN_CLASS_NOTIF | FAN_CLOEXEC, O_RDONLY);
    if (fd < 0) {
        char msg[128];
        snprintf(msg, sizeof(msg), "fanotify_init -> errno %d (%s)", errno, strerror(errno));
        fail("fanotify", msg);
        return;
    }

    // Create a file to mark and modify.
    int f = open("/tmp/fan.txt", O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (f < 0) {
        fail("fanotify", "open /tmp/fan.txt failed");
        (void)close((int)fd);
        return;
    }

    // Mark it for MODIFY + OPEN + CLOSE_WRITE.
    unsigned long long mask = FAN_MODIFY | FAN_OPEN | FAN_CLOSE_WRITE;
    long r = syscall(SYS_fanotify_mark, fd, FAN_MARK_ADD, mask, AT_FDCWD, "/tmp/fan.txt");
    if (r != 0) {
        char msg[128];
        snprintf(msg, sizeof(msg), "fanotify_mark -> errno %d (%s)", errno, strerror(errno));
        fail("fanotify", msg);
        (void)close((int)fd);
        (void)close(f);
        return;
    }

    // Trigger events: write + close the marked file.
    (void)write(f, "hello-fm4", 9);
    (void)close(f);

    // Read one event back. We expect FAN_MODIFY (and possibly FAN_OPEN).
    struct fanotify_event_metadata meta;
    memset(&meta, 0, sizeof(meta));
    ssize_t n = read((int)fd, &meta, sizeof(meta));
    if (n < (ssize_t)sizeof(meta)) {
        char msg[128];
        snprintf(msg, sizeof(msg), "fanotify read -> %zd bytes (%s)", n,
                 n < 0 ? strerror(errno) : "short read");
        fail("fanotify", msg);
        (void)close((int)fd);
        return;
    }

    // FAN_MODIFY must be set; the event's pid should be our own.
    int saw_modify = (meta.mask & FAN_MODIFY) != 0;
    int pid_ok = meta.pid == (int)getpid();
    check("fanotify", saw_modify && pid_ok,
          saw_modify ? "event pid != getpid()" : "no FAN_MODIFY in event mask");

    (void)close((int)fd);
}

// ---------------------------------------------------------------------------
// Test 2: keyrings — keyctl(KEYCTL_GET_KEYRING_ID), add_key, request_key.
// ---------------------------------------------------------------------------
static void test_keyctl(void) {
    long kr = syscall(SYS_keyctl, KEYCTL_GET_KEYRING_ID, KEY_SPEC_SESSION_KEYRING, 0);
    if (kr < 0) {
        char msg[128];
        snprintf(msg, sizeof(msg), "keyctl(GET_KEYRING_ID) -> errno %d (%s)", errno,
                 strerror(errno));
        fail("keyctl", msg);
        return;
    }
    if (kr == 0) {
        fail("keyctl", "session keyring serial is 0");
        return;
    }

    long key = syscall(SYS_add_key, "user", "fm4-test", "payload", 7, KEY_SPEC_SESSION_KEYRING);
    if (key <= 0) {
        char msg[128];
        snprintf(msg, sizeof(msg), "add_key -> errno %d (%s)", errno, strerror(errno));
        fail("keyctl", msg);
        return;
    }

    // request_key for a non-existent key must fail with ENOKEY (not ENOSYS).
    long rq = syscall(SYS_request_key, "user", "fm4-nonexistent", NULL, 0);
    if (rq == -1 && errno == ENOKEY) {
        ok("keyctl");
    } else if (rq == -1) {
        char msg[128];
        snprintf(msg, sizeof(msg), "request_key -> errno %d (want ENOKEY)", errno);
        fail("keyctl", msg);
    } else {
        fail("keyctl", "request_key unexpectedly succeeded");
    }
}

// ---------------------------------------------------------------------------
// Test 3: seccomp — strict mode blocks a non-allowlisted syscall with SIGSYS.
// ---------------------------------------------------------------------------
static void test_seccomp(void) {
    pid_t pid = fork();
    if (pid < 0) {
        fail("seccomp", "fork failed");
        return;
    }

    if (pid == 0) {
        // Child: enter strict mode, then attempt a forbidden syscall (getpid).
        // Strict mode permits only read/write/_exit/exit_group/sigreturn.
        long r = syscall(SYS_seccomp, SECCOMP_SET_MODE_STRICT, 0, NULL);
        if (r != 0)
            _exit(100); // seccomp() itself failed — parent reports this as fail
        (void)syscall(SYS_getpid); // forbidden -> SIGSYS -> process dies here
        _exit(101);                // should never be reached
    }

    int status = 0;
    if (wait4(pid, &status, 0, NULL) != pid) {
        fail("seccomp", "wait4 failed");
        return;
    }

    if (WIFSIGNALED(status) && WTERMSIG(status) == SIGSYS) {
        ok("seccomp");
    } else if (WIFEXITED(status)) {
        char msg[128];
        snprintf(msg, sizeof(msg), "child exited with %d (want SIGSYS)", WEXITSTATUS(status));
        fail("seccomp", msg);
    } else {
        fail("seccomp", "child neither exited nor died with SIGSYS");
    }
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

    say(">>> FOUNDATION-M4 smoke test <<<\n");

    if (mount("proc", "/proc", "proc", 0, NULL) != 0)
        say("init: mount /proc failed\n");
    if (mount("sysfs", "/sys", "sysfs", 0, NULL) != 0)
        say("init: mount /sys failed\n");
    if (mount("tmpfs", "/tmp", "tmpfs", 0, NULL) != 0)
        say("init: mount /tmp failed\n");

    test_fanotify();
    test_keyctl();
    test_seccomp();

    say(failures == 0 ? "__FM4_DONE__ __FM4_PASS__\n" : "__FM4_DONE__ __FM4_FAIL__\n");

    for (;;)
        (void)pause();
    return 0;
}
