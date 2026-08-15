// SPDX-License-Identifier: MPL-2.0
//
// DRM-M4 cursor smoke test: exercise the *hardware cursor* path of the
// virtio-gpu DRM node through the standard legacy cursor ioctls
// (MODE_CURSOR / MODE_CURSOR2). Runs as pid 1 on Asterinas RISC-V.
//
// Flow: open /dev/dri/card0 -> MODE_GETRESOURCES (crtc id) ->
// MODE_CREATE_DUMB (64x64 ARGB cursor) -> MODE_MAP_DUMB -> mmap -> draw an
// opaque white arrow -> MODE_CURSOR2 (BO|MOVE, hotspot 0,0) ->
// MODE_CURSOR (MOVE) -> MODE_CURSOR (BO, handle=0 = hide).
//
// The cursor overlay is not part of QEMU's console surface, so the host cannot
// verify it with a `screendump`; instead it checks two signals: (1) every ioctl
// returned 0 (QEMU answered OK_NODATA, i.e. the cursor commands were accepted),
// and (2) QEMU's `virtio_gpu_update_cursor` trace fired, proving the device
// actually processed the cursor command.

#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>

#define DEV "/dev/dri/card0"

/* Hardcoded DRM ioctl numbers (the cross sysroot lacks asm/ioctl.h, so the
 * uapi macros expand to 0; these are the correct _IOWR encodings). */
#define DRM_IOCTL_MODE_GETRESOURCES 0xc04064a0ULL
#define DRM_IOCTL_MODE_CREATE_DUMB  0xc02064b2ULL
#define DRM_IOCTL_MODE_MAP_DUMB     0xc01064b3ULL
#define DRM_IOCTL_MODE_CURSOR       0xc01c64a3ULL
#define DRM_IOCTL_MODE_CURSOR2      0xc02464bbULL

#define DRM_MODE_CURSOR_BO   0x01
#define DRM_MODE_CURSOR_MOVE 0x02

#define CURSOR_W 64
#define CURSOR_H 64

/* ---- uapi structs (riscv64: pointers/size_t are 8 bytes) ---- */

struct drm_mode_card_res {
    uint64_t fb_id_ptr, crtc_id_ptr, connector_id_ptr, encoder_id_ptr;
    uint32_t count_fbs, count_crtcs, count_connectors, count_encoders;
    uint32_t min_width, max_width, min_height, max_height;
};

struct drm_mode_create_dumb {
    uint32_t height, width, bpp, flags;
    uint32_t handle, pitch;
    uint64_t size;
};

struct drm_mode_map_dumb {
    uint32_t handle, pad;
    uint64_t offset;
};

/* 7 x u32 = 28 bytes. */
struct drm_mode_cursor {
    uint32_t flags;
    uint32_t crtc_id;
    int32_t x;
    int32_t y;
    uint32_t width;
    uint32_t height;
    uint32_t handle;
};

/* 9 x u32 = 36 bytes. */
struct drm_mode_cursor2 {
    uint32_t flags;
    uint32_t crtc_id;
    int32_t x;
    int32_t y;
    uint32_t width;
    uint32_t height;
    uint32_t handle;
    int32_t hot_x;
    int32_t hot_y;
};

/* ---- helpers ---- */

static int failures = 0;

static void ok(const char *name) {
    printf("[DRM] %s: OK\n", name);
}

static void fail(const char *name, const char *msg) {
    failures++;
    printf("[DRM] %s: FAIL (%s)\n", name, msg);
}

/* Draws an opaque white arrow (ARGB; little-endian memory layout is BGRA,
 * matching virtio-gpu's B8G8R8A8 cursor format). */
static void draw_arrow(uint32_t *pix, int w, int h) {
    for (int y = 0; y < h; y++) {
        for (int x = 0; x < w; x++) {
            int inside = 0;
            /* diagonal shaft, top bar, left bar */
            if (x < 44 && y < 44 && y >= x - 6 && y <= x + 6) inside = 1;
            if (y < 8 && x < 40) inside = 1;
            if (x < 8 && y < 40) inside = 1;
            uint32_t a = inside ? 0xFF : 0x00;
            uint32_t v = inside ? 0xFF : 0x00;
            pix[(size_t)y * w + x] = (a << 24) | (v << 16) | (v << 8) | v;
        }
    }
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

    /* MODE_GETRESOURCES. */
    uint32_t crtc_id = 0;
    struct drm_mode_card_res res = {0};
    res.crtc_id_ptr = (uint64_t)&crtc_id;
    if (ioctl(fd, DRM_IOCTL_MODE_GETRESOURCES, &res) != 0 || crtc_id == 0) {
        fail("get_resources", "DRM_IOCTL_MODE_GETRESOURCES failed");
        printf("__DRM_DONE__ __DRM_FAIL__\n");
        return 1;
    }
    printf("[DRM] crtc_id=%u\n", crtc_id);
    ok("get_resources");

    /* Cursor dumb buffer: 64x64 32bpp. */
    struct drm_mode_create_dumb dumb = {
        .height = CURSOR_H, .width = CURSOR_W, .bpp = 32, .flags = 0 };
    if (ioctl(fd, DRM_IOCTL_MODE_CREATE_DUMB, &dumb) != 0 || dumb.handle == 0) {
        fail("create_dumb", "DRM_IOCTL_MODE_CREATE_DUMB failed");
        printf("__DRM_DONE__ __DRM_FAIL__\n");
        return 1;
    }
    printf("[DRM] cursor handle=%u pitch=%u size=%llu\n",
           dumb.handle, dumb.pitch, (unsigned long long)dumb.size);
    ok("create_dumb");

    struct drm_mode_map_dumb map = { .handle = dumb.handle, .pad = 0 };
    if (ioctl(fd, DRM_IOCTL_MODE_MAP_DUMB, &map) != 0) {
        fail("map_dumb", "DRM_IOCTL_MODE_MAP_DUMB failed");
        printf("__DRM_DONE__ __DRM_FAIL__\n");
        return 1;
    }
    ok("map_dumb");

    uint32_t *cursor = mmap(NULL, dumb.size, PROT_READ | PROT_WRITE, MAP_SHARED,
                            fd, (off_t)map.offset);
    if (cursor == MAP_FAILED) {
        fail("mmap", "mmap failed");
        printf("[DRM] errno=%d\n", errno);
        printf("__DRM_DONE__ __DRM_FAIL__\n");
        return 1;
    }
    ok("mmap");

    draw_arrow(cursor, CURSOR_W, CURSOR_H);
    ok("draw_cursor");

    /* MODE_CURSOR2: set the cursor buffer (BO) and position (MOVE) together. */
    struct drm_mode_cursor2 set = {
        .flags = DRM_MODE_CURSOR_BO | DRM_MODE_CURSOR_MOVE,
        .crtc_id = crtc_id,
        .x = 200, .y = 150,
        .width = CURSOR_W, .height = CURSOR_H,
        .handle = dumb.handle,
        .hot_x = 0, .hot_y = 0,
    };
    if (ioctl(fd, DRM_IOCTL_MODE_CURSOR2, &set) != 0) {
        fail("set_cursor2", "DRM_IOCTL_MODE_CURSOR2 failed");
        printf("[DRM] errno=%d\n", errno);
        printf("__DRM_DONE__ __DRM_FAIL__\n");
        return 1;
    }
    ok("set_cursor2");
    printf("__DRM_CURSOR_SET_OK__\n");
    fflush(stdout);

    /* MODE_CURSOR: reposition the cursor without changing the buffer. */
    struct drm_mode_cursor move = {
        .flags = DRM_MODE_CURSOR_MOVE,
        .crtc_id = crtc_id,
        .x = 400, .y = 300,
        .width = CURSOR_W, .height = CURSOR_H,
        .handle = 0,
    };
    if (ioctl(fd, DRM_IOCTL_MODE_CURSOR, &move) != 0) {
        fail("move_cursor", "DRM_IOCTL_MODE_CURSOR(MOVE) failed");
        printf("[DRM] errno=%d\n", errno);
        printf("__DRM_DONE__ __DRM_FAIL__\n");
        return 1;
    }
    ok("move_cursor");
    printf("__DRM_CURSOR_MOVE_OK__\n");
    fflush(stdout);

    /* MODE_CURSOR: hide the cursor (BO with handle 0). */
    struct drm_mode_cursor hide = {
        .flags = DRM_MODE_CURSOR_BO,
        .crtc_id = crtc_id,
        .x = 0, .y = 0,
        .width = CURSOR_W, .height = CURSOR_H,
        .handle = 0,
    };
    if (ioctl(fd, DRM_IOCTL_MODE_CURSOR, &hide) != 0) {
        fail("hide_cursor", "DRM_IOCTL_MODE_CURSOR(BO handle=0) failed");
        printf("[DRM] errno=%d\n", errno);
        printf("__DRM_DONE__ __DRM_FAIL__\n");
        return 1;
    }
    ok("hide_cursor");
    printf("__DRM_CURSOR_HIDE_OK__\n");
    fflush(stdout);

    close(fd);
    printf("__DRM_DONE__ %s\n", failures ? "__DRM_FAIL__" : "__DRM_PASS__");
    return failures ? 1 : 0;
}
