// SPDX-License-Identifier: MPL-2.0
//
// DRM-M8 static PID-1 launcher for the systemd desktop. Same early-setup as the
// main-chain SYSTEMD-DESKTOP init.c, plus a **runtime GPU fallback**: it probes
// for /dev/dri/card0 and picks the Xorg config accordingly —
//
//   /dev/dri/card0 present  -> /etc/xorg-modesetting.conf  (DRM modesetting)
//   /dev/dri/card0 absent   -> /etc/xorg-fbdev.conf        (bochs fbdev)
//
// Both configs are copied to /etc/xorg.conf before exec'ing systemd so the
// xorg.service unit (which runs `Xorg -config /etc/xorg.conf`) picks the right
// driver without any unit changes.
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

static int copy_file(const char *src, const char *dst) {
    FILE *in = fopen(src, "rb");
    if (!in)
        return -1;
    FILE *out = fopen(dst, "wb");
    if (!out) {
        fclose(in);
        return -1;
    }
    char buf[4096];
    size_t n;
    while ((n = fread(buf, 1, sizeof buf, in)) > 0)
        (void)fwrite(buf, 1, n, out);
    fclose(in);
    fclose(out);
    return 0;
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

    // /run must be a writable tmpfs for systemd's runtime state.
    if (mount("tmpfs", "/run", "tmpfs", 0, "mode=0755") < 0) {
        fprintf(stderr, "[init] mount /run tmpfs: %s\n", strerror(errno));
    }

    // Pick the Xorg config: DRM modesetting if /dev/dri/card0 exists, else the
    // bochs simple-framebuffer fbdev fallback.
    struct stat st;
    const char *cfg = (stat("/dev/dri/card0", &st) == 0 && S_ISCHR(st.st_mode))
        ? "/etc/xorg-modesetting.conf"
        : "/etc/xorg-fbdev.conf";
    if (copy_file(cfg, "/etc/xorg.conf") == 0)
        fprintf(stderr, "[init] Xorg config: %s\n", cfg);
    else
        fprintf(stderr, "[init] WARN: cannot install Xorg config %s\n", cfg);

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
    execl("/bin/sh", "/bin/sh", (char *)NULL);
    return 1;
}
