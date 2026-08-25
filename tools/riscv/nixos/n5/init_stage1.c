// SPDX-License-Identifier: MPL-2.0
//
// NIXOS-N5 stage-1 init: mounts the persistent ext2 root disk, carries /dev
// over with a bind mount, chroots into it, and runs the stage-2 script as
// PID 1 (which does the nix persistence check and then exec()s systemd).

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <unistd.h>

static void say(const char *s) {
    (void)write(1, s, strlen(s));
}

static const char STAGE2_SCRIPT[] =
    "mount -t proc proc /proc\n"
    "mount -t sysfs sysfs /sys\n"
    "mount -t tmpfs tmpfs /tmp\n"
    "echo __N5_STAGE1_OK__\n"
    "export PATH=/nix/store/355b1vblxfwy4iw3kbglqavshjlav14z-nix-riscv64-unknown-linux-gnu-2.30.2/bin:/bin\n"
    "export HOME=/root TMPDIR=/tmp\n"
    // R1-B: the profile lives on the persistent disk. First boot installs it;
    // any later boot must find it already there.
    "if test -x /nix/var/nix/profiles/default/bin/busybox; then\n"
    "  echo __N5_PROFILE_PERSISTED__\n"
    "  /nix/var/nix/profiles/default/bin/sh -c 'echo __N5_PROFILE_RUNS__'\n"
    "else\n"
    "  echo __N5_FIRST_BOOT__\n"
    "  nix-store --load-db < /nix/.reginfo\n"
    "  echo loaddb-rc=$?\n"
    "  mkdir -p /nix/var/nix/daemon-socket\n"
    "  nix-daemon --daemon >/tmp/daemon.log 2>&1 &\n"
    "  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do\n"
    "    test -S /nix/var/nix/daemon-socket/socket && break\n"
    "    sleep 1\n"
    "  done\n"
    "  NIX_REMOTE=daemon nix profile add --profile /nix/var/nix/profiles/default \\\n"
    "    /nix/store/7g4f0sx5kcf62d1qnc3sl6ijn5mgn978-busybox-riscv64-unknown-linux-gnu-1.36.1 2>&1\n"
    "  echo __N5_INSTALL_RC__=$?\n"
    "  sync\n"
    "fi\n"
    "echo __N5_STAGE2_SYSTEMD__\n"
    "exec /init\n";

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

    say(">>> N5 stage1: mounting persistent root (/dev/vdb, ext2) <<<\n");

    if (mkdir("/newroot", 0755) != 0 && errno != EEXIST) {
        printf("stage1: mkdir /newroot failed: %s\n", strerror(errno));
    }
    if (mount("/dev/vdb", "/newroot", "ext2", 0, NULL) != 0) {
        printf(">>> N5 stage1: mount /dev/vdb FAILED: %s <<<\n", strerror(errno));
        for (;;)
            (void)pause();
    }

    // Carry the registry-provided device nodes into the new root.
    (void)mkdir("/newroot/dev", 0755);
    if (mount("/dev", "/newroot/dev", NULL, MS_BIND, NULL) != 0)
        printf("stage1: bind /dev failed: %s\n", strerror(errno));

    if (chroot("/newroot") != 0) {
        printf(">>> N5 stage1: chroot FAILED: %s <<<\n", strerror(errno));
        for (;;)
            (void)pause();
    }
    (void)chdir("/");

    say(">>> N5 stage1: switched to persistent root, starting stage2 <<<\n");

    char *const argv[] = { "/bin/sh", "-c", (char *)STAGE2_SCRIPT, NULL };
    (void)execv("/bin/sh", argv);

    printf("stage1: exec /bin/sh failed: %s\n", strerror(errno));
    for (;;)
        (void)pause();
    return 0;
}
