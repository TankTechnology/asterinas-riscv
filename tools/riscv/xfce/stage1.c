// SPDX-License-Identifier: MPL-2.0

// Mounts the persistent Xfce root disk and starts its existing init launcher.

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <unistd.h>

static void stop(const char *operation) {
    dprintf(2, "xfce-drm stage1: %s failed: %s\n", operation, strerror(errno));
    for (;;)
        pause();
}

int main(void) {
    int console = open("/dev/console", O_RDWR);
    if (console < 0)
        console = open("/dev/ttyS0", O_RDWR);
    if (console >= 0) {
        (void)dup2(console, STDIN_FILENO);
        (void)dup2(console, STDOUT_FILENO);
        (void)dup2(console, STDERR_FILENO);
        if (console > STDERR_FILENO)
            (void)close(console);
    }

    puts(">>> XFCE-DRM stage1: mounting /dev/vdb <<<");
    if (mkdir("/newroot", 0755) != 0 && errno != EEXIST)
        stop("mkdir /newroot");
    if (mount("/dev/vdb", "/newroot", "ext2", 0, NULL) != 0)
        stop("mount /dev/vdb");

    if (mkdir("/newroot/dev", 0755) != 0 && errno != EEXIST)
        stop("mkdir /newroot/dev");
    if (mount("/dev", "/newroot/dev", NULL, MS_BIND, NULL) != 0)
        stop("bind /dev");
    if (chroot("/newroot") != 0)
        stop("chroot");
    if (chdir("/") != 0)
        stop("chdir /");

    puts(">>> XFCE-DRM stage1: starting persistent root init <<<");
    char *const argv[] = { "/init", NULL };
    execv(argv[0], argv);
    stop("exec /init");
}
