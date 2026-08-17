// SPDX-License-Identifier: MPL-2.0
//
// BusyBox initramfs /init for the Asterinas RISC-V NixOS track (M1).
//
// Runs as pid 1: attaches the serial console to stdio, best-effort mounts the
// proc/sys/tmp pseudo-filesystems, then runs the M1 smoke test non-interactively
// through `busybox sh -c <script>`. Each command prints a fixed marker so the
// QEMU driver can attribute a crash to the exact command. A final interactive
// shell is intentionally NOT launched here: the interactive path depends on
// termios, which is a known M1 gap (see M1-report.md).

#define _GNU_SOURCE
#include <fcntl.h>
#include <string.h>
#include <sys/mount.h>
#include <unistd.h>

static void say(const char *s) {
    (void)write(1, s, strlen(s));
}

static const char SMOKE_SCRIPT[] =
    "echo __M1_SH_OK__\n"
    "ls /\n"
    "echo __M1_LS_DONE__\n"
    "echo hello-m1 > /tmp/m1.txt\n"
    "cat /tmp/m1.txt\n"
    "echo __M1_CAT_DONE__\n"
    "mount\n"
    "echo __M1_MOUNT_DONE__\n"
    "ps\n"
    "echo __M1_PS_DONE__\n"
    "uname -a\n"
    "echo __M1_UNAME_DONE__\n"
    "stat /bin/busybox\n"
    "echo __M1_STAT_DONE__\n"
    "df\n"
    "echo __M1_DF_DONE__\n"
    "free\n"
    "echo __M1_FREE_DONE__\n"
    "cat /proc/cpuinfo\n"
    "echo __M1_CPUINFO_DONE__\n"
    "readlink /proc/self\n"
    "echo __M1_SELF_DONE__\n"
    "dd if=/dev/zero of=/tmp/z bs=4096 count=2 2>/dev/null\n"
    "echo __M1_DD_DONE__\n";

int main(void) {
    // Attach the console as stdin/stdout/stderr.
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

    say(">>> M1 busybox init: mounting /proc /sys /tmp <<<\n");

    if (mount("proc", "/proc", "proc", 0, NULL) != 0)
        say("init: mount /proc failed\n");
    if (mount("sysfs", "/sys", "sysfs", 0, NULL) != 0)
        say("init: mount /sys failed\n");
    if (mount("tmpfs", "/tmp", "tmpfs", 0, NULL) != 0)
        say("init: mount /tmp failed\n");

    say(">>> M1 busybox init: running smoke script <<<\n");

    char *const argv[] = { "/bin/sh", "-c", (char *)SMOKE_SCRIPT, NULL };
    (void)execv("/bin/sh", argv);

    say("init: exec /bin/sh failed\n");
    for (;;)
        (void)pause();
    return 0;
}
