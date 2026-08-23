// DRM-M20: PRIME fd<->handle round-trip verification (static musl, runs as /init).
// Verifies DRM_IOCTL_PRIME_HANDLE_TO_FD and PRIME_FD_TO_HANDLE against /dev/dri/card0.
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#define DRM_IOCTL_BASE 'd'
#define DRM_IOWR(nr, type) _IOWR(DRM_IOCTL_BASE, nr, type)

#define DRM_IOCTL_MODE_CREATE_DUMB DRM_IOWR(0xb2, struct drm_mode_create_dumb)
#define DRM_IOCTL_MODE_MAP_DUMB DRM_IOWR(0xb3, struct drm_mode_map_dumb)
#define DRM_IOCTL_MODE_DESTROY_DUMB DRM_IOWR(0xb4, struct drm_mode_destroy_dumb)
#define DRM_IOCTL_PRIME_HANDLE_TO_FD DRM_IOWR(0x2d, struct drm_prime_handle)
#define DRM_IOCTL_PRIME_FD_TO_HANDLE DRM_IOWR(0x2e, struct drm_prime_handle)

struct drm_mode_create_dumb {
    uint32_t height, width, bpp, flags;
    uint32_t handle, pitch;
    uint64_t size;
};
struct drm_mode_map_dumb {
    uint32_t handle, pad;
    uint64_t offset;
};
struct drm_mode_destroy_dumb {
    uint32_t handle;
};
struct drm_prime_handle {
    uint32_t handle;
    uint32_t flags;
    int32_t fd;
};

int main(void) {
    printf("M20_PRIME_BEGIN\n");
    int card = open("/dev/dri/card0", O_RDWR);
    if (card < 0) { printf("M20_PRIME_FAIL open card0 errno=%d\n", errno); return 1; }

    struct drm_mode_create_dumb cdb = {0};
    cdb.width = 64; cdb.height = 64; cdb.bpp = 32;
    if (ioctl(card, DRM_IOCTL_MODE_CREATE_DUMB, &cdb) != 0) {
        printf("M20_PRIME_FAIL CREATE_DUMB errno=%d\n", errno); return 1;
    }
    printf("M20_PRIME_CREATE_DUMB handle=%u size=%llu\n", cdb.handle, (unsigned long long)cdb.size);

    struct drm_prime_handle export = { .handle = cdb.handle, .flags = 0, .fd = -1 };
    if (ioctl(card, DRM_IOCTL_PRIME_HANDLE_TO_FD, &export) != 0) {
        printf("M20_PRIME_FAIL HANDLE_TO_FD errno=%d\n", errno); return 1;
    }
    if (export.fd < 0) { printf("M20_PRIME_FAIL bad fd\n"); return 1; }
    printf("M20_PRIME_HANDLE_TO_FD fd=%d\n", export.fd);

    struct drm_prime_handle import = { .handle = 0, .flags = 0, .fd = export.fd };
    if (ioctl(card, DRM_IOCTL_PRIME_FD_TO_HANDLE, &import) != 0) {
        printf("M20_PRIME_FAIL FD_TO_HANDLE errno=%d\n", errno); return 1;
    }
    if (import.handle == 0) { printf("M20_PRIME_FAIL import handle 0\n"); return 1; }
    printf("M20_PRIME_FD_TO_HANDLE handle=%u\n", import.handle);

    close(export.fd);
    struct drm_mode_destroy_dumb ddb = { .handle = cdb.handle };
    ioctl(card, DRM_IOCTL_MODE_DESTROY_DUMB, &ddb);
    close(card);
    printf("M20_PRIME_PASS\n");
    return 0;
}
