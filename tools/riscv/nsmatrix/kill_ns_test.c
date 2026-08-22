#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

#define CHECK(cond, msg) do { if (!(cond)) { printf("FAIL: %s (errno=%d %s)\n", msg, errno, strerror(errno)); _exit(1); } printf("ok: %s\n", msg); } while (0)

static void *thread_fn(void *arg) { for (;;) pause(); }

int main(void)
{
    mkdir("/proc", 0555);
    if (mount("proc", "/proc", "proc", 0, NULL) != 0) { printf("FAIL: mount proc\n"); return 1; }

    /* Multithreaded unshare(CLONE_NEWPID) must fail with EINVAL. */
    pthread_t th;
    CHECK(pthread_create(&th, NULL, thread_fn, NULL) == 0, "pthread_create");
    errno = 0;
    CHECK(unshare(CLONE_NEWPID) == -1 && errno == EINVAL,
          "multithreaded unshare(NEWPID) fails with EINVAL");
    pthread_cancel(th);
    pthread_join(th, NULL);

    /* Outside member: own process group, sleeping, initial namespace. */
    pid_t outsider = fork();
    if (outsider == 0) { setpgid(0, 0); for (;;) pause(); }
    sleep(1);

    /* Set up a new pid+user namespace with an init process. */
    CHECK(unshare(CLONE_NEWUSER) == 0, "unshare(NEWUSER)");
    CHECK(unshare(CLONE_NEWPID) == 0, "unshare(NEWPID)");
    pid_t ns_init = fork();
    if (ns_init == 0) {
        CHECK(getpid() == 1, "ns init getpid()==1");

        /* kill(-pgid) of a group with no visible members: ESRCH. */
        errno = 0;
        CHECK(kill(-outsider, 0) == -1 && errno == ESRCH,
              "kill(-pgid) of invisible group fails with ESRCH");

        /* kill(-1) must reach ns members but not outside processes. */
        pid_t member = fork();
        if (member == 0) { for (;;) pause(); }
        sleep(1);
        CHECK(kill(-1, SIGTERM) == 0, "kill(-1, SIGTERM) from ns init");
        int st;
        CHECK(waitpid(member, &st, 0) == member && WIFSIGNALED(st) && WTERMSIG(st) == SIGTERM,
              "ns member killed by kill(-1)");
        _exit(0);
    }

    int st;
    CHECK(waitpid(ns_init, &st, 0) == ns_init && WEXITSTATUS(st) == 0, "ns init checks passed");

    /* The outsider in the initial namespace survived both signals. */
    errno = 0;
    CHECK(kill(outsider, 0) == 0, "outsider process unaffected by in-ns kill(-pgid)/kill(-1)");
    kill(outsider, SIGKILL);
    waitpid(outsider, &st, 0);

    printf("KILL_NS_TEST_PASS\n");
    return 0;
}
