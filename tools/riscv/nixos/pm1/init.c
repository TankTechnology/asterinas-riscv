// SPDX-License-Identifier: MPL-2.0
//
// POLISH-M1 smoke test for seccomp SECCOMP_SET_MODE_FILTER (classic BPF) on
// Asterinas RISC-V. Runs as pid 1. Each sub-test forks a child, installs a BPF
// filter, and checks that the filter's action (ERRNO / KILL) is applied to a
// targeted syscall. Prints `__PM1_<NAME>_{OK,FAIL}__` markers and a final
// `__PM1_DONE__` + `__PM1_PASS__`/`__PM1_FAIL__`.

#define _GNU_SOURCE
#include <errno.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <signal.h>
#include <stdio.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#ifndef SYS_seccomp
#define SYS_seccomp 277
#endif
#ifndef SYS_getpid
#define SYS_getpid 172
#endif

static int failures = 0;

static void ok(const char *name) {
    printf("[PM1] %s: OK  __PM1_%s_OK__\n", name, name);
}

static void fail(const char *name, const char *msg) {
    failures++;
    printf("[PM1] %s: FAIL (%s) __PM1_%s_FAIL__\n", name, msg, name);
}

// Filter that returns ERRNO(EPERM) for `getpid`, ALLOW otherwise.
static void test_filter_errno(void) {
    struct sock_filter filter[] = {
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, 0),                    // A = nr
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_getpid, 0, 1),    // nr == getpid?
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM),     // -> EPERM
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),             // -> allow
    };
    struct sock_fprog prog = { .len = 4, .filter = filter };

    pid_t pid = fork();
    if (pid < 0) { fail("seccomp_filter_errno", "fork"); return; }
    if (pid == 0) {
        if (syscall(SYS_seccomp, SECCOMP_SET_MODE_FILTER, 0, &prog) != 0)
            _exit(100); // filter install failed
        errno = 0;
        long r = syscall(SYS_getpid);
        _exit((r == -1 && errno == EPERM) ? 0 : 1);
    }

    int status = 0;
    if (waitpid(pid, &status, 0) < 0) { fail("seccomp_filter_errno", "waitpid"); return; }
    if (WIFEXITED(status) && WEXITSTATUS(status) == 0)
        ok("seccomp_filter_errno");
    else if (WIFEXITED(status) && WEXITSTATUS(status) == 100)
        fail("seccomp_filter_errno", "seccomp(SET_MODE_FILTER) failed");
    else
        fail("seccomp_filter_errno", "getpid not blocked with EPERM");
}

// Filter that returns KILL for `getpid`: the child must die with SIGSYS.
static void test_filter_kill(void) {
    struct sock_filter filter[] = {
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, 0),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_getpid, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
    };
    struct sock_fprog prog = { .len = 4, .filter = filter };

    pid_t pid = fork();
    if (pid < 0) { fail("seccomp_filter_kill", "fork"); return; }
    if (pid == 0) {
        if (syscall(SYS_seccomp, SECCOMP_SET_MODE_FILTER, 0, &prog) != 0)
            _exit(100);
        (void)syscall(SYS_getpid); // blocked -> SIGSYS
        _exit(1);                  // should not reach here
    }

    int status = 0;
    if (waitpid(pid, &status, 0) < 0) { fail("seccomp_filter_kill", "waitpid"); return; }
    if (WIFSIGNALED(status) && WTERMSIG(status) == SIGSYS)
        ok("seccomp_filter_kill");
    else
        fail("seccomp_filter_kill", "child did not die with SIGSYS");
}

// Filter that returns ERRNO(EPERM) for `getpid`; installed in a parent process,
// then a forked child must inherit it and see `getpid` blocked with EPERM.
static void test_filter_inherit(void) {
    struct sock_filter filter[] = {
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, 0),                    // A = nr
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_getpid, 0, 1),    // nr == getpid?
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM),     // -> EPERM
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),             // -> allow
    };
    struct sock_fprog prog = { .len = 4, .filter = filter };

    pid_t pid = fork();
    if (pid < 0) { fail("seccomp_filter_inherit", "fork"); return; }
    if (pid == 0) {
        // Child installs the filter, then forks a grandchild that must inherit it.
        if (syscall(SYS_seccomp, SECCOMP_SET_MODE_FILTER, 0, &prog) != 0)
            _exit(100); // filter install failed

        pid_t gc = fork();
        if (gc < 0) _exit(101); // fork failed
        if (gc == 0) {
            errno = 0;
            long r = syscall(SYS_getpid);
            _exit((r == -1 && errno == EPERM) ? 0 : 2);
        }
        int st = 0;
        if (waitpid(gc, &st, 0) < 0) _exit(102); // waitpid failed
        _exit((WIFEXITED(st) && WEXITSTATUS(st) == 0) ? 0 : 3);
    }

    int status = 0;
    if (waitpid(pid, &status, 0) < 0) { fail("seccomp_filter_inherit", "waitpid"); return; }
    if (WIFEXITED(status) && WEXITSTATUS(status) == 0)
        ok("seccomp_filter_inherit");
    else if (WIFEXITED(status) && WEXITSTATUS(status) == 100)
        fail("seccomp_filter_inherit", "seccomp(SET_MODE_FILTER) failed");
    else if (WIFEXITED(status) && WEXITSTATUS(status) == 101)
        fail("seccomp_filter_inherit", "fork of grandchild failed");
    else
        fail("seccomp_filter_inherit", "grandchild did not inherit the filter");
}

int main(void) {
    test_filter_errno();
    test_filter_kill();
    test_filter_inherit();
    printf("__PM1_DONE__ %s\n", failures ? "__PM1_FAIL__" : "__PM1_PASS__");
    return 0;
}
