// SPDX-License-Identifier: MPL-2.0
//
// NIXOS-N1 initramfs /init: mounts the pseudo-filesystems, then runs the
// netlink probe and the BusyBox `ip` applet non-interactively, printing fixed
// markers for the QEMU driver.

#define _GNU_SOURCE
#include <fcntl.h>
#include <string.h>
#include <sys/mount.h>
#include <unistd.h>

static void say(const char *s) {
    (void)write(1, s, strlen(s));
}

static const char N1_SCRIPT[] =
    "/bin/nlprobe\n"
    "echo __N1_IP_LINK__\n"
    "ip link\n"
    "echo __N1_IP_ADDR__\n"
    "ip addr\n"
    "echo __N1_DONE__\n";

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

    say(">>> N1 init: mounting /proc /sys /tmp <<<\n");

    if (mount("proc", "/proc", "proc", 0, NULL) != 0)
        say("init: mount /proc failed\n");
    if (mount("sysfs", "/sys", "sysfs", 0, NULL) != 0)
        say("init: mount /sys failed\n");
    if (mount("tmpfs", "/tmp", "tmpfs", 0, NULL) != 0)
        say("init: mount /tmp failed\n");

    say(">>> N1 init: running netlink script <<<\n");

    char *const argv[] = { "/bin/sh", "-c", (char *)N1_SCRIPT, NULL };
    (void)execv("/bin/sh", argv);

    say("init: exec /bin/sh failed\n");
    for (;;)
        (void)pause();
    return 0;
}
