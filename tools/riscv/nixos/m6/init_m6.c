// SPDX-License-Identifier: MPL-2.0
//
// M6 /init: drive `nix build` of a real derivation on Asterinas RISC-V.
//
// Static glibc (same pattern as M1-M5): mount the pseudo filesystems, prepare
// the /nix store layout, seed the environment Nix expects, then hand off to
// busybox `sh -c` which runs two builds with fixed markers so the QEMU driver
// can attribute a crash to the exact command:
//   1. trivial — a builtins.derivation whose builder writes a fixed string to
//      $out; proves the /nix/store write path end-to-end.
//   2. hello — installs a prebuilt riscv64 hello and runs it (path B).
//
// Interactive shell is deliberately avoided (termios gap, see M1-report.md).

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
    "echo __M6_TRIVIAL_DONE__\n"
    "echo __M6_HELLO_START__\n"
    "hout=$(nix build --no-link --print-out-paths --impure "
        "--expr 'import /m6/hello.nix')\n"
    "echo hello_out_path=[$hout]\n"
    "hr=$($hout/bin/hello 2>&1)\n"
    "echo hello_result=[$hr]\n"
    "echo __M6_HELLO_DONE__\n";

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

    say(">>> M6 init: running nix build smoke script <<<\n");

    char *const argv[] = { "/bin/sh", "-c", (char *)SMOKE_SCRIPT, NULL };
    (void)execv("/bin/sh", argv);

    say("init: exec /bin/sh failed\n");
    for (;;)
        (void)pause();
    return 0;
}
