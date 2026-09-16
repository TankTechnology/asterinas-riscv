// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE
#include <errno.h>
#include <sched.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

#define CHECK(cond, msg) do { if (!(cond)) { printf("FAIL: %s (errno=%d %s)\n", msg, errno, strerror(errno)); _exit(1); } printf("ok: %s\n", msg); } while (0)

int main(void)
{
    mkdir("/proc", 0555);
    if (mount("proc", "/proc", "proc", 0, NULL) != 0) { printf("FAIL: mount proc\n"); return 1; }

    /* clone(CLONE_NEWPID|CLONE_NEWUSER|SIGCHLD): the child becomes the
       init of the new PID namespace immediately. */
    pid_t c = syscall(SYS_clone, CLONE_NEWPID | CLONE_NEWUSER | SIGCHLD, NULL, NULL, NULL, NULL);
    if (c == 0) {
        CHECK(getpid() == 1, "clone(NEWPID|NEWUSER) child getpid()==1");
        CHECK(getppid() == 0, "child getppid()==0 (parent outside ns)");
        pid_t g = fork();
        if (g == 0) { _exit(getpid() == 2 ? 0 : 7); }
        int st;
        CHECK(waitpid(2, &st, 0) == 2 && WEXITSTATUS(st) == 0, "grandchild vpid 2 reaped");
        printf("PIDNS_CLONE_TEST_PASS\n");
        _exit(0);
    }
    CHECK(c > 0, "clone(CLONE_NEWPID|CLONE_NEWUSER|SIGCHLD) succeeds");
    int st;
    CHECK(waitpid(c, &st, 0) == c && WIFEXITED(st) && WEXITSTATUS(st) == 0, "ns init exited cleanly");
    return 0;
}
