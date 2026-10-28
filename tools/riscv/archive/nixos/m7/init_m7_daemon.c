// SPDX-License-Identifier: MPL-2.0
//
// M7 daemon driver: start nix-daemon and complete a multi-user nix build
// through it, then prove the build ran as a non-root build user.
//
// Flow:
//   1. mount the pseudo filesystems, prepare /nix/store + the daemon socket dir
//   2. fork + exec /usr/sbin/nix-daemon (root)
//   3. poll for /nix/var/nix/daemon-socket/socket
//   4. fork a client that setgid/setuid to uid 1000 ("alice") and runs three
//      builds over NIX_REMOTE=daemon:
//        - trivial: shell builder writes a fixed string to $out
//        - whoami:  builder writes `id -un` to $out (proves the daemon dropped
//          privileges to a nixbld build user, not root)
//        - hello:   installs a prebuilt riscv64 hello and runs it
//
// Static glibc, same pattern as M1-M6.

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#define DAEMON_SOCK "/nix/var/nix/daemon-socket/socket"

static void say(const char *s) {
    (void)write(1, s, strlen(s));
}

static const char CLIENT_SCRIPT[] =
    "echo __M7_CLIENT_START__\n"
    "id\n"
    "echo __M7_TRIVIAL_START__\n"
    "out=$(nix build --no-link --print-out-paths --impure "
        "--expr 'import /m7/trivial.nix')\n"
    "echo trivial_out=[$out]\n"
    "echo trivial_result=[$(cat \"$out\" 2>/dev/null)]\n"
    "echo __M7_TRIVIAL_DONE__\n"
    "echo __M7_WHOAMI_START__\n"
    "wout=$(nix build --no-link --print-out-paths --impure "
        "--expr 'import /m7/whoami.nix')\n"
    "echo whoami_out=[$wout]\n"
    "echo whoami_result=[$(cat \"$wout\" 2>/dev/null)]\n"
    "echo __M7_WHOAMI_DONE__\n"
    "echo __M7_HELLO_START__\n"
    "hout=$(nix build --no-link --print-out-paths --impure "
        "--expr 'import /m7/hello.nix')\n"
    "echo hello_out=[$hout]\n"
    "echo hello_result=[$($hout/bin/hello 2>&1)]\n"
    "echo __M7_HELLO_DONE__\n";

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

    say(">>> M7 init: nix-daemon multi-user build <<<\n");

    (void)mount("proc", "/proc", "proc", 0, NULL);
    (void)mount("sysfs", "/sys", "sysfs", 0, NULL);
    (void)mount("tmpfs", "/tmp", "tmpfs", 0, NULL);
    (void)mount("devtmpfs", "/dev", "devtmpfs", 0, NULL);

    (void)mkdir("/root", 0755);
    (void)mkdir("/nix", 0755);
    (void)mkdir("/nix/store", 0755);
    (void)mkdir("/nix/var", 0755);
    (void)mkdir("/nix/var/nix", 0755);
    (void)mkdir("/nix/var/nix/daemon-socket", 0755);

    (void)setenv("HOME", "/root", 1);
    (void)setenv("USER", "root", 1);
    (void)setenv("LOGNAME", "root", 1);
    (void)setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin", 1);
    (void)setenv("TERM", "vt100", 1);

    /* 2. Start the daemon (root). */
    pid_t dp = fork();
    if (dp < 0) {
        say("__M7_DAEMON_FORK_FAIL__\n");
        return 1;
    }
    if (dp == 0) {
        char *const argv[] = { "/usr/sbin/nix-daemon", NULL };
        (void)execv("/usr/sbin/nix-daemon", argv);
        printf("__M7_DAEMON_EXEC_FAIL__ errno=%d (%s)\n", errno, strerror(errno));
        _exit(127);
    }
    say(">>> M7 init: nix-daemon started <<<\n");

    /* 3. Wait for the daemon socket to appear. */
    struct stat st;
    for (int i = 0; i < 200; i++) {
        if (stat(DAEMON_SOCK, &st) == 0)
            break;
        usleep(50000);
    }
    if (stat(DAEMON_SOCK, &st) == 0)
        say("__M7_SOCKET_READY__\n");
    else
        say("__M7_SOCKET_MISSING__\n");

    /* 4. Client build as uid 1000 ("alice"), via the daemon. */
    pid_t cp = fork();
    if (cp == 0) {
        (void)setgid(1000);
        (void)setgroups(0, NULL);
        (void)setuid(1000);
        (void)setenv("NIX_REMOTE", "daemon", 1);
        (void)setenv("HOME", "/tmp", 1);
        (void)setenv("USER", "alice", 1);
        (void)setenv("LOGNAME", "alice", 1);
        char *const argv[] = { "/bin/sh", "-c", (char *)CLIENT_SCRIPT, NULL };
        (void)execv("/bin/sh", argv);
        _exit(127);
    }

    int status = 0;
    (void)waitpid(cp, &status, 0);
    printf("__M7_CLIENT_EXIT__ status=%d\n", WEXITSTATUS(status));

    say(">>> M7 daemon build done <<<\n");
    for (;;)
        (void)pause();
    return 0;
}
