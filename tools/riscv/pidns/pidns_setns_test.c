#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
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

static int read_link(const char *path, char *buf, size_t size)
{
    ssize_t n = readlink(path, buf, size - 1);
    if (n < 0) return -1;
    buf[n] = 0;
    return 0;
}

/* Reads the NSpid line of /proc/<pid>/status into buf. */
static int read_nspid(pid_t pid, char *buf, size_t size)
{
    char path[64];
    snprintf(path, sizeof(path), "/proc/%d/status", pid);
    FILE *f = fopen(path, "r");
    if (!f) return -1;
    char line[256];
    int found = -1;
    while (fgets(line, sizeof(line), f)) {
        if (strncmp(line, "NSpid:", 6) == 0) {
            strncpy(buf, line, size - 1);
            buf[size - 1] = 0;
            found = 0;
            break;
        }
    }
    fclose(f);
    return found;
}

int main(void)
{
    mkdir("/proc", 0555);
    if (mount("proc", "/proc", "proc", 0, NULL) != 0) { printf("FAIL: mount proc\n"); return 1; }

    char self_pid[64], self_pfc[64];
    CHECK(read_link("/proc/self/ns/pid", self_pid, sizeof(self_pid)) == 0, "readlink /proc/self/ns/pid");
    CHECK(read_link("/proc/self/ns/pid_for_children", self_pfc, sizeof(self_pfc)) == 0, "readlink /proc/self/ns/pid_for_children");
    printf("self ns: pid=%s pid_for_children=%s\n", self_pid, self_pfc);
    CHECK(strcmp(self_pid, self_pfc) == 0, "initially pid == pid_for_children");

    /* Negative (privileged): as init-user-ns root, after unshare(NEWPID)
       the current children namespace is the new child; joining its ancestor
       must fail with EINVAL. */
    CHECK(unshare(CLONE_NEWPID) == 0, "unshare(NEWPID) as root");
    {
        int init_ns_fd0 = open("/proc/self/ns/pid", O_RDONLY);
        CHECK(init_ns_fd0 >= 0, "open own (initial) pid ns");
        errno = 0;
        CHECK(setns(init_ns_fd0, CLONE_NEWPID) == -1 && errno == EINVAL,
              "privileged setns into ancestor pid ns fails with EINVAL");
        close(init_ns_fd0);
    }

    CHECK(unshare(CLONE_NEWUSER) == 0, "unshare(NEWUSER)");
    CHECK(unshare(CLONE_NEWPID) == 0, "unshare(NEWPID)");

    /* pid_for_children must have switched to the new namespace; pid must not. */
    char new_pfc[64], self_pid2[64];
    CHECK(read_link("/proc/self/ns/pid_for_children", new_pfc, sizeof(new_pfc)) == 0, "readlink pid_for_children after unshare");
    CHECK(read_link("/proc/self/ns/pid", self_pid2, sizeof(self_pid2)) == 0, "readlink pid after unshare");
    printf("after unshare: pid=%s pid_for_children=%s\n", self_pid2, new_pfc);
    CHECK(strcmp(new_pfc, self_pfc) != 0, "unshare(NEWPID) switched pid_for_children");
    CHECK(strcmp(self_pid2, self_pid) == 0, "unshare(NEWPID) kept pid namespace");

    /* Fork the namespace init. */
    pid_t init_child = fork();
    if (init_child == 0) {
        CHECK(getpid() == 1, "ns init getpid()==1");
        for (;;) pause();
    }
    printf("ns init global pid: %d\n", init_child);

    /* Open the namespace init's `pid` entry and join it via setns. */
    char ns_path[64];
    snprintf(ns_path, sizeof(ns_path), "/proc/%d/ns/pid", init_child);
    int ns_fd = open(ns_path, O_RDONLY);
    CHECK(ns_fd >= 0, "open /proc/<init>/ns/pid");
    CHECK(setns(ns_fd, CLONE_NEWPID) == 0, "setns(CLONE_NEWPID) into child ns");

    /* Deferred semantics: our own pid namespace is unchanged... */
    char after_pid[64], after_pfc[64];
    CHECK(read_link("/proc/self/ns/pid", after_pid, sizeof(after_pid)) == 0, "readlink pid after setns");
    CHECK(read_link("/proc/self/ns/pid_for_children", after_pfc, sizeof(after_pfc)) == 0, "readlink pid_for_children after setns");
    CHECK(strcmp(after_pid, self_pid) == 0, "setns kept own pid namespace");
    CHECK(strcmp(after_pfc, new_pfc) == 0, "setns switched pid_for_children to target");

    /* ...and a forked child joins the target namespace as a member. */
    pid_t member = fork();
    if (member == 0) {
        CHECK(getpid() == 2, "setns-joined child getpid()==2 (ns init is 1)");
        CHECK(getppid() == 0, "setns-joined child getppid()==0 (parent outside ns)");
        _exit(0);
    }
    int st;
    CHECK(waitpid(member, &st, 0) == member && WEXITSTATUS(st) == 0, "setns-joined child passed its checks");

    /* NSpid: the namespace init's status, read from the parent (init ns),
       shows both the global PID and the vpid 1. */
    char nspid[128], expect[64];
    CHECK(read_nspid(init_child, nspid, sizeof(nspid)) == 0, "read NSpid of ns init");
    snprintf(expect, sizeof(expect), "NSpid:\t%d\t1\t1\n", init_child);
    printf("NSpid line: %s", nspid);
    CHECK(strcmp(nspid, expect) == 0, "NSpid shows <global>\t1");

    /* pidfd path: a PID file from pidfd_open is usable with setns. */
    int pidfd = syscall(SYS_pidfd_open, init_child, 0);
    CHECK(pidfd >= 0, "pidfd_open(<ns init>)");
    /* Rejoining the same namespace from a pid file must succeed. */
    CHECK(setns(pidfd, CLONE_NEWPID) == 0, "setns(pidfd, CLONE_NEWPID)");
    close(pidfd);

    /* Negative: joining an ancestor namespace is rejected (EPERM from the
       capability check since we are in a child user namespace; EINVAL from
       the ancestor rule when privileged). */
    int init_ns_fd = open("/proc/1/ns/pid", O_RDONLY);
    CHECK(init_ns_fd >= 0, "open /proc/1/ns/pid");
    errno = 0;
    CHECK(setns(init_ns_fd, CLONE_NEWPID) == -1 && (errno == EINVAL || errno == EPERM),
          "setns into ancestor pid ns is rejected");
    close(init_ns_fd);

    /* Kill the namespace init; wait for it. */
    kill(init_child, SIGKILL);
    waitpid(init_child, &st, 0);

    printf("SETNS_TEST_PASS\n");
    return 0;
}
