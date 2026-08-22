// SPDX-License-Identifier: MPL-2.0
//
// M3 /init: bring up a minimal rootfs for Nix and run the smoke test.
//
// Static glibc (same pattern as M1/M2): mounts the proc/sys/tmp pseudo
// filesystems, prepares the /nix store layout, seeds the environment Nix
// expects (HOME/USER/PATH), then hands off to busybox `sh -c` which runs each
// Nix command with a fixed marker so the QEMU driver can attribute a crash to
// the exact command. Interactive shell is deliberately avoided (termios gap,
// see M1-report.md).

#define _GNU_SOURCE
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <unistd.h>

static void say(const char *s) {
    (void)write(1, s, strlen(s));
}

static const char SMOKE_SCRIPT[] =
    "echo __M3_VERSION_START__\n"
    "nix --version 2>&1\n"
    "echo __M3_VERSION_DONE__\n"
    "echo __M3_EVAL_START__\n"
    "r=$(nix eval --expr '1 + 1' 2>/dev/null); echo eval_result=[$r]\n"
    "echo __M3_EVAL_DONE__\n"
    "echo __M3_HELLO_START__\n"
    "r=$(nix eval --raw --expr '\"hello\"' 2>/dev/null); echo hello_result=[$r]\n"
    "echo __M3_HELLO_DONE__\n";

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

    say(">>> M3 init: Nix on Asterinas RISC-V <<<\n");

    (void)mount("proc", "/proc", "proc", 0, NULL);
    (void)mount("sysfs", "/sys", "sysfs", 0, NULL);
    (void)mount("tmpfs", "/tmp", "tmpfs", 0, NULL);
    (void)mount("devtmpfs", "/dev", "devtmpfs", 0, NULL);

    (void)mkdir("/root", 0755);
    (void)mkdir("/nix", 0755);
    (void)mkdir("/nix/store", 0755);
    (void)mkdir("/nix/var", 0755);
    (void)mkdir("/nix/var/nix", 0755);

    (void)setenv("HOME", "/root", 1);
    (void)setenv("USER", "root", 1);
    (void)setenv("LOGNAME", "root", 1);
    (void)setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin", 1);
    (void)setenv("TERM", "vt100", 1);

    say(">>> M3 init: running nix smoke script <<<\n");

    char *const argv[] = { "/bin/sh", "-c", (char *)SMOKE_SCRIPT, NULL };
    (void)execv("/bin/sh", argv);

    say("init: exec /bin/sh failed\n");
    for (;;)
        (void)pause();
    return 0;
}
