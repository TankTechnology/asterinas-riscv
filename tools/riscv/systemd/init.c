// SPDX-License-Identifier: MPL-2.0
//
// Static PID-1 launcher for the SYSTEMD-DESKTOP milestone. The kernel hands
// control to /init; this program does the absolute minimum of early setup
// (create the mount-point directories, mount a writable tmpfs on /run so
// systemd has somewhere to place runtime state) and then exec()s the real
// systemd so that it becomes PID 1 and runs in "system mode".
//
// We deliberately do NOT mount /proc, /sys or /sys/fs/cgroup here: systemd's
// own mount_setup_early() does that, and pre-mounting them risks confusing its
// remount logic. /dev is already a kernel-populated ramfs (see
// kernel/src/device/mod.rs::init_in_first_process), so no devtmpfs mount is
// needed or available.
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

static void mkdir_p(const char *path, mode_t mode) {
    if (mkdir(path, mode) < 0 && errno != EEXIST) {
        fprintf(stderr, "[init] mkdir %s: %s\n", path, strerror(errno));
    }
}

int main(void) {
    fprintf(stderr, ">>> systemd init: launching systemd (PID 1) <<<\n");

    // Mount points + runtime state directories (systemd expects them to exist).
    mkdir_p("/proc", 0755);
    mkdir_p("/sys", 0755);
    mkdir_p("/dev", 0755);
    mkdir_p("/run", 0755);
    mkdir_p("/tmp", 0777);
    mkdir_p("/var", 0755);
    mkdir_p("/var/log", 0755);
    mkdir_p("/var/tmp", 0777);
    mkdir_p("/sys/fs/cgroup", 0755);

    // /run must be a writable tmpfs for systemd's runtime state (mount units,
    // sockets, the manager's runtime dir). If this mount is unsupported the
    // kernel returns an error and systemd will still run, just degraded.
    if (mount("tmpfs", "/run", "tmpfs", 0, "mode=0755") < 0) {
        fprintf(stderr, "[init] mount /run tmpfs: %s\n", strerror(errno));
    }

    char *const argv[] = {
        "/usr/lib/systemd/systemd",
        "--system",
        "--log-target=console",
        "--log-level=info",
        "--show-status=1",
        NULL,
    };

    execv(argv[0], argv);

    fprintf(stderr, "[init] exec systemd failed: %s\n", strerror(errno));
    fprintf(stderr, "[init] falling back to /bin/sh\n");

    // Last resort: drop to a shell so the boot is still inspectable.
    execl("/bin/sh", "/bin/sh", (char *)NULL);
    return 1;
}
