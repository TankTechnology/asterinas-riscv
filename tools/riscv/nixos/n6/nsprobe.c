// SPDX-License-Identifier: MPL-2.0
//
// NIXOS-N6 namespace probe: replays the exact clone() flag combinations the
// nix build sandbox uses and reports which one fails, isolating the EINVAL
// seen in the sandbox=true gate run.

#define _GNU_SOURCE
#include <errno.h>
#include <sched.h>
#include <stdio.h>
#include <string.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

static void try_clone(const char *name, int flags) {
    errno = 0;
    // Raw clone with a NULL stack (fork semantics); the child must not
    // return from this function, so it _exit()s immediately.
    pid_t r = syscall(SYS_clone, flags | SIGCHLD, NULL, NULL, NULL, 0);
    if (r == 0)
        _exit(0);
    if (r < 0) {
        printf("__NSPROBE__ %s flags=0x%x -> errno=%d (%s)\n", name, flags,
               errno, strerror(errno));
        return;
    }
    int status;
    waitpid(r, &status, 0);
    printf("__NSPROBE__ %s flags=0x%x -> ok (child exit=%d)\n", name, flags,
           WEXITSTATUS(status));
}

int main(void) {
    setbuf(stdout, NULL);
    printf(">>> N6 nsprobe start <<<\n");

    try_clone("newns", CLONE_NEWNS);
    try_clone("newuser", CLONE_NEWUSER);
    try_clone("newpid", CLONE_NEWPID);
    try_clone("newnet", CLONE_NEWNET);
    try_clone("newns-newpid", CLONE_NEWNS | CLONE_NEWPID);
    try_clone("no-newuser", CLONE_NEWNS | CLONE_NEWPID | CLONE_NEWIPC |
                  CLONE_NEWUTS | CLONE_NEWNET);
    try_clone("all", CLONE_NEWNS | CLONE_NEWPID | CLONE_NEWIPC | CLONE_NEWUTS |
                  CLONE_NEWUSER | CLONE_NEWNET);
    try_clone("all-newcgroup", CLONE_NEWNS | CLONE_NEWPID | CLONE_NEWIPC |
                  CLONE_NEWUTS | CLONE_NEWUSER | CLONE_NEWNET | CLONE_NEWCGROUP);

    // The exact nix sandbox helper flag set includes CLONE_PARENT, which is
    // rejected for PID 1 itself; probe it from a forked child instead.
    pid_t r = fork();
    if (r == 0) {
        try_clone("nix-helper-no-newnet",
                  CLONE_NEWPID | CLONE_NEWNS | CLONE_NEWIPC | CLONE_NEWUTS |
                      CLONE_PARENT | CLONE_NEWUSER);
        try_clone("nix-helper",
                  CLONE_NEWPID | CLONE_NEWNS | CLONE_NEWIPC | CLONE_NEWUTS |
                      CLONE_PARENT | CLONE_NEWNET | CLONE_NEWUSER);
        _exit(0);
    }
    int status;
    waitpid(r, &status, 0);

    printf(">>> N6 nsprobe done <<<\n");
    return 0;
}
