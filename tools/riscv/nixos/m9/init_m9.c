// SPDX-License-Identifier: MPL-2.0
//
// M9 `/init` — the real lightweight-NixOS PID 1. Unlike M8 (which ran a
// one-shot install + a single login shell), this boot is:
//
//   1. mount the pseudo filesystems (/proc /sys /tmp /run) and prepare /nix
//   2. set the hostname
//   3. hand control to busybox `init`, which:
//        - runs /etc/rc as `::sysinit` (installs the nix profile, starts
//          services: syslogd + crond + the nix-managed heartbeat daemon)
//        - respawns a getty/login loop on ttyS0
//
// The /dev tree is NOT mounted here: Asterinas exposes device nodes (console,
// ttyS0, null, zero) through its device registry, not a devtmpfs mount (the
// devtmpfs fstype is not registered — M8-report.md). So /dev nodes already
// exist at boot and we only ensure a couple of standard ones are present.
//
// Static glibc, same pattern as M1-M8.

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

static void say(const char *s) {
    (void)write(1, s, strlen(s));
}

static void mkdirp(const char *p) {
    if (mkdir(p, 0755) != 0 && errno != EEXIST)
        printf("__M9_INIT__ mkdir %s failed: %d\n", p, errno);
}

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

    say(">>> M9 init: lightweight NixOS (busybox-init + nix profile) <<<\n");

    /* 1. Pseudo filesystems. devtmpfs is intentionally skipped (not a
     *    registered fstype; the device registry already created /dev nodes).
     *    /run may not exist in the unpacked rootfs, so create it first. */
    mkdirp("/run");
    if (mount("proc", "/proc", "proc", 0, NULL) != 0)
        say("__M9_INIT__ mount /proc failed\n");
    if (mount("sysfs", "/sys", "sysfs", 0, NULL) != 0)
        say("__M9_INIT__ mount /sys failed\n");
    if (mount("tmpfs", "/tmp", "tmpfs", 0, NULL) != 0)
        say("__M9_INIT__ mount /tmp failed\n");
    if (mount("tmpfs", "/run", "tmpfs", 0, NULL) != 0)
        say("__M9_INIT__ mount /run failed\n");

    /* /dev: the device registry already created console/ttyS0/null/zero at
     * boot (M8-report.md §3.1); no devtmpfs mount and no mknod needed. */

    /* 2. Runtime + nix directory layout. /nix is prepared first so that, if a
     * second virtio-blk ext2 disk (vdb) is present, it can be mounted over
     * /nix to persist the store across reboots (optional bonus). */
    mkdirp("/nix");
    /* Optional: persist /nix on a second virtio-blk ext2 disk (vdb). */
    {
        struct stat vdb;
        if (stat("/dev/vdb", &vdb) == 0) {
            if (mount("/dev/vdb", "/nix", "ext2", 0, NULL) == 0)
                say(">>> M9 init: persistent /nix on /dev/vdb (ext2) <<<\n");
            else
                printf("__M9_INIT__ mount /dev/vdb ext2 failed: errno=%d (%s) "
                       "(continuing on initramfs)\n", errno, strerror(errno));
        }
    }

    mkdirp("/run");
    mkdirp("/var");
    mkdirp("/var/log");
    mkdirp("/root");
    mkdirp("/nix/store");
    mkdirp("/nix/var");
    mkdirp("/nix/var/nix");
    mkdirp("/nix/var/nix/profiles");

    /* 3. Hostname. */
    if (sethostname("nixos-riscv", 11) != 0)
        say("__M9_INIT__ sethostname failed\n");

    /* 4. Baseline environment (the login shell will refine PATH via
     *    /etc/profile after the profile is installed by /etc/rc). */
    (void)setenv("HOME", "/root", 1);
    (void)setenv("USER", "root", 1);
    (void)setenv("LOGNAME", "root", 1);
    (void)setenv("PATH", "/nix/var/nix/profiles/default/bin:/usr/bin:/bin:/usr/sbin:/sbin", 1);
    (void)setenv("TERM", "vt100", 1);

    /* 5. Hand off to busybox init (becomes PID 1). It runs /etc/rc
     *    (sysinit) then the getty respawn loop. */
    say(">>> M9 init: execing busybox init <<<\n");
    char *const argv[] = { "/bin/busybox", "init", NULL };
    (void)execv("/bin/busybox", argv);
    printf("__M9_INIT__ exec busybox init failed: %d (%s)\n", errno, strerror(errno));
    return 1;
}
