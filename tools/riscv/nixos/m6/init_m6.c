// SPDX-License-Identifier: MPL-2.0
//
// M6 /init: drive `nix build` of a real derivation on Asterinas RISC-V.
//
// Static glibc (same pattern as M1-M5): mount the pseudo filesystems, prepare
// the /nix store layout, seed the environment Nix expects, then hand off to
// busybox `sh -c` which runs the build steps with fixed markers so the QEMU
// driver can attribute a crash to the exact command. Interactive shell is
// deliberately avoided (termios gap, see M1-report.md).
//
// Step 1 (this file): the trivial derivation (`nix build` a
// `builtins.derivation` whose builder is /bin/sh writing to $out). Step 2 adds
// the hello-from-source build (see hello.nix / hello.c).

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
    "echo __M6_TRIVIAL_START__\n"
    "out=$(nix build --no-link --print-out-paths --impure "
        "--expr 'import /m6/trivial.nix')\n"
    "echo out_path=[$out]\n"
    "val=$(cat \"$out\" 2>/dev/null)\n"
    "echo trivial_result=[$val]\n"
    "echo __M6_TRIVIAL_DONE__\n";

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

    say(">>> M6 init: nix build on Asterinas RISC-V <<<\n");

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
    (void)setenv("NIX_REMOTE", "", 1);

    say(">>> M6 init: running nix build smoke script <<<\n");

    char *const argv[] = { "/bin/sh", "-c", (char *)SMOKE_SCRIPT, NULL };
    (void)execv("/bin/sh", argv);

    say("init: exec /bin/sh failed\n");
    for (;;)
        (void)pause();
    return 0;
}
