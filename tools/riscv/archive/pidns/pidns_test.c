// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <sched.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

#define CHECK(cond, msg) do { if (!(cond)) { printf("FAIL: %s (errno=%d %s)\n", msg, errno, strerror(errno)); _exit(1); } printf("ok: %s\n", msg); } while (0)

static int count_proc_numeric(void)
{
    DIR *d = opendir("/proc");
    if (!d) return -1;
    int n = 0;
    struct dirent *de;
    while ((de = readdir(d))) {
        if (de->d_name[0] >= '0' && de->d_name[0] <= '9') n++;
    }
    closedir(d);
    return n;
}

/* Runs as the init (PID 1) of the new PID namespace. */
static void ns_init(void)
{
    CHECK(getpid() == 1, "ns init getpid()==1");
    CHECK(getppid() == 0, "ns init getppid()==0 (parent outside ns)");

    pid_t g = fork();
    if (g == 0) {
        /* grandchild: PID 2 in the ns */
        CHECK(getpid() == 2, "grandchild getpid()==2");
        CHECK(getppid() == 1, "grandchild getppid()==1");
        int n = count_proc_numeric();
        printf("numeric /proc entries in ns: %d\n", n);
        CHECK(n == 2, "procfs shows only ns members");
        _exit(0);
    }
    int st;
    /* waitpid must return the virtual PID */
    pid_t r = waitpid(2, &st, 0);
    CHECK(r == 2 && WIFEXITED(st) && WEXITSTATUS(st) == 0, "waitpid(2) reaps grandchild with vpid");

    /* kill by virtual PID */
    g = fork();
    if (g == 0) { for (;;) pause(); }
    CHECK(kill(3, SIGTERM) == 0, "kill(3, SIGTERM) by vpid");
    r = waitpid(3, &st, 0);
    CHECK(r == 3 && WIFSIGNALED(st) && WTERMSIG(st) == SIGTERM, "grandchild died of SIGTERM");

    /* ns-init death must SIGKILL the whole ns: spawn a sleeper and exit. */
    g = fork();
    if (g == 0) { for (;;) pause(); }
    printf("ns init exiting; pid %d should be SIGKILLed\n", g);
    _exit(0);
}

int main(void)
{
    mkdir("/proc", 0555);
    if (mount("proc", "/proc", "proc", 0, NULL) != 0) { printf("FAIL: mount proc\n"); return 1; }

    pid_t t = fork();
    if (t == 0) {
        CHECK(unshare(CLONE_NEWUSER) == 0, "unshare(NEWUSER)");
        CHECK(unshare(CLONE_NEWPID) == 0, "unshare(NEWPID)");
        /* Deferred semantics: the caller itself keeps its old PID. */
        CHECK(getpid() != 1, "unshare(NEWPID) does not move the caller");
        pid_t c = fork();
        if (c == 0) ns_init();  /* _exit inside */
        int st;
        waitpid(c, &st, 0);
        CHECK(WIFEXITED(st) && WEXITSTATUS(st) == 0, "ns init exited cleanly");
        _exit(0);
    }

    /* Reap everything; the ns sleeper should appear as SIGKILLed. */
    int killed_by_ns = 0, st;
    pid_t r;
    while ((r = waitpid(-1, &st, 0)) > 0) {
        printf("reaped pid %d: exited=%d sig=%d\n", r, WIFEXITED(st), WIFSIGNALED(st) ? WTERMSIG(st) : 0);
        if (WIFSIGNALED(st) && WTERMSIG(st) == SIGKILL) killed_by_ns = 1;
    }
    CHECK(killed_by_ns, "sleeper in dead ns was SIGKILLed");
    printf("PIDNS_TEST_PASS\n");
    return 0;
}
