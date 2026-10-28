// SPDX-License-Identifier: MPL-2.0
//
// M8 lightweight-route driver: prove the Alpine-style "busybox-init + nix
// profile activation" system works — no systemd, no switch_root, and no
// nix-daemon (single-user nix). Flow:
//
//   1. mount the pseudo filesystems, prepare /nix
//   2. as root in single-user mode, `nix profile install` hello into
//      /nix/var/nix/profiles/default (nix builds the derivation locally)
//   3. verify the profile's bin/hello exists
//   4. spawn a *login shell* (/bin/sh -l) which sources /etc/profile (which
//      puts the profile bin dir on PATH) and runs the bare `hello` command
//
// Static glibc, same pattern as M1-M7.

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

static void say(const char *s) {
    (void)write(1, s, strlen(s));
}

/* Step 2: install hello into the system profile (single-user, no daemon). */
static const char INSTALL_SCRIPT[] =
    "echo __M8_INSTALL_START__\n"
    "nix profile install --profile /nix/var/nix/profiles/default "
        "--impure --expr 'import /m8/hello.nix' 2>&1\n"
    "echo __M8_INSTALL_EXIT__=$?\n"
    "nix profile list --profile /nix/var/nix/profiles/default 2>&1\n"
    "echo __M8_PROFILE_BIN__=$([ -x /nix/var/nix/profiles/default/bin/hello ] "
        "&& echo OK || echo MISSING)\n";

/* Step 4: a login shell sources /etc/profile and runs the installed binary. */
static const char LOGIN_SCRIPT[] =
    "echo __M8_LOGIN_START__\n"
    "echo __M8_PATH__=$PATH\n"
    "command -v hello || echo __M8_HELLO_NOT_IN_PATH__\n"
    "echo __M8_HELLO__=$(hello 2>&1)\n"
    "echo __M8_LOGIN_DONE__\n";

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

    say(">>> M8 init: lightweight nix-profile system <<<\n");

    (void)mount("proc", "/proc", "proc", 0, NULL);
    (void)mount("sysfs", "/sys", "sysfs", 0, NULL);
    (void)mount("tmpfs", "/tmp", "tmpfs", 0, NULL);

    (void)mkdir("/root", 0755);
    (void)mkdir("/nix", 0755);
    (void)mkdir("/nix/store", 0755);
    (void)mkdir("/nix/var", 0755);
    (void)mkdir("/nix/var/nix", 0755);
    (void)mkdir("/nix/var/nix/profiles", 0755);

    (void)setenv("HOME", "/root", 1);
    (void)setenv("USER", "root", 1);
    (void)setenv("LOGNAME", "root", 1);
    (void)setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin", 1);
    (void)setenv("TERM", "vt100", 1);

    /* 2. Install hello into the system profile (single-user). */
    pid_t cp = fork();
    if (cp == 0) {
        char *const argv[] = { "/bin/sh", "-c", (char *)INSTALL_SCRIPT, NULL };
        (void)execv("/bin/sh", argv);
        _exit(127);
    }
    int status = 0;
    (void)waitpid(cp, &status, 0);
    printf("__M8_INSTALL_DONE__ exit=%d\n", WEXITSTATUS(status));

    /* 4. Login shell sources /etc/profile and runs the installed binary. */
    pid_t lp = fork();
    if (lp == 0) {
        char *const argv[] = { "/bin/sh", "-l", "-c", (char *)LOGIN_SCRIPT, NULL };
        (void)execv("/bin/sh", argv);
        _exit(127);
    }
    (void)waitpid(lp, &status, 0);
    printf("__M8_LOGIN_EXIT__=%d\n", WEXITSTATUS(status));

    say(">>> M8 lightweight system done <<<\n");
    for (;;)
        (void)pause();
    return 0;
}
