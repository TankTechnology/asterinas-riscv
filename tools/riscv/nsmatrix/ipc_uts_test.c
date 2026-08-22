#define _GNU_SOURCE
#include <errno.h>
#include <sched.h>
#include <stdio.h>
#include <string.h>
#include <sys/ipc.h>
#include <sys/mount.h>
#include <sys/shm.h>
#include <sys/stat.h>
#include <sys/utsname.h>
#include <sys/wait.h>
#include <unistd.h>

#define CHECK(cond, msg) do { if (!(cond)) { printf("FAIL: %s (errno=%d %s)\n", msg, errno, strerror(errno)); _exit(1); } printf("ok: %s\n", msg); } while (0)

#define SHM_KEY 0x4b454d35 /* "KEM5" */

int main(void)
{
    /* Parent (initial IPC namespace) owns a shared memory segment. */
    int parent_shm = shmget(SHM_KEY, 4096, IPC_CREAT | 0600);
    CHECK(parent_shm >= 0, "parent shmget(IPC_CREAT)");

    CHECK(unshare(CLONE_NEWUSER) == 0, "unshare(NEWUSER)");

    pid_t c = fork();
    if (c == 0) {
        /* Child: new IPC + UTS namespaces. */
        CHECK(unshare(CLONE_NEWIPC) == 0, "child unshare(NEWIPC)");
        CHECK(unshare(CLONE_NEWUTS) == 0, "child unshare(NEWUTS)");

        /* The parent's segment is invisible in the new IPC namespace. */
        errno = 0;
        CHECK(shmget(SHM_KEY, 4096, 0) == -1 && errno == ENOENT,
              "parent shm invisible in new ipc ns");

        /* The child can create its own segment under the same key. */
        int child_shm = shmget(SHM_KEY, 4096, IPC_CREAT | IPC_EXCL | 0600);
        CHECK(child_shm >= 0, "child creates own shm under same key");
        CHECK(shmctl(child_shm, IPC_RMID, NULL) == 0, "child removes its shm");

        /* UTS isolation: hostname changes stay inside the namespace. */
        CHECK(sethostname("kem5-sandbox", 12) == 0, "child sethostname");
        struct utsname uts;
        CHECK(uname(&uts) == 0 && strcmp(uts.nodename, "kem5-sandbox") == 0,
              "child uname shows new hostname");
        _exit(0);
    }

    int st;
    CHECK(waitpid(c, &st, 0) == c && WIFEXITED(st) && WEXITSTATUS(st) == 0,
          "child ipc/uts checks passed");

    /* The parent's segment and hostname are untouched. */
    errno = 0;
    CHECK(shmget(SHM_KEY, 4096, 0) == parent_shm, "parent shm still reachable by key");
    struct utsname uts;
    CHECK(uname(&uts) == 0 && strcmp(uts.nodename, "kem5-sandbox") != 0,
          "parent hostname unchanged");
    CHECK(shmctl(parent_shm, IPC_RMID, NULL) == 0, "parent removes its shm");

    printf("IPC_UTS_TEST_PASS\n");
    return 0;
}
