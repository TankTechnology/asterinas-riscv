// SPDX-License-Identifier: MPL-2.0
//
// DRM-M1 smoke test: open the virtio-gpu DRM node, query the driver version,
// and report the result. Runs as pid 1 on Asterinas RISC-V. The kernel-side
// virtio-gpu driver renders the test pattern during boot; this user-space test
// only proves the `/dev/dri/card0` interface is present and functional.
//
// The host side verifies the 2D pipeline independently by taking a QEMU
// `screendump` and checking the rendered gradient.

#include <errno.h>
#include <fcntl.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

#define DEV "/dev/dri/card0"

/* _IOWR('d', 0x00, struct drm_version) */
#define DRM_IOCTL_VERSION 0xc0406400UL

struct drm_version {
    int version_major;
    int version_minor;
    int version_patchlevel;
    size_t name_len;
    char *name;
    size_t date_len;
    char *date;
    size_t desc_len;
    char *desc;
};

static int failures = 0;

static void ok(const char *name) {
    printf("[DRM] %s: OK  __DRM_%s_OK__\n", name, name);
}

static void fail(const char *name, const char *msg) {
    failures++;
    printf("[DRM] %s: FAIL (%s) __DRM_%s_FAIL__\n", name, name, msg);
}

int main(void) {
    int fd = open(DEV, O_RDWR);
    if (fd < 0) {
        fail("open", "cannot open DRM node");
        printf("[DRM] errno=%d\n", errno);
        printf("__DRM_DONE__ __DRM_FAIL__\n");
        return 1;
    }
    ok("open");

    char name[64];
    char date[64];
    char desc[128];
    struct drm_version version = {
        .version_major = 0,
        .version_minor = 0,
        .version_patchlevel = 0,
        .name_len = sizeof(name),
        .name = name,
        .date_len = sizeof(date),
        .date = date,
        .desc_len = sizeof(desc),
        .desc = desc,
    };

    if (ioctl(fd, DRM_IOCTL_VERSION, &version) == 0) {
        printf("[DRM] version %d.%d.%d name='%s' date='%s' __DRM_version_OK__\n",
               version.version_major, version.version_minor,
               version.version_patchlevel, version.name, version.date);
        ok("version");
    } else {
        fail("version", "DRM_IOCTL_VERSION failed");
        printf("[DRM] ioctl errno=%d\n", errno);
    }

    close(fd);

    printf("__DRM_DONE__ %s\n", failures ? "__DRM_FAIL__" : "__DRM_PASS__");
    return failures ? 1 : 0;
}
