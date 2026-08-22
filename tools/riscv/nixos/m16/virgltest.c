// DRM-M16/M17: raw virtio-gpu 3D (virgl) ioctl verification client.
//
// Alpine's Mesa does not ship the virgl gallium driver (any arch), so the
// kernel's 3D ioctl surface cannot be exercised through EGL/GBM. This
// self-contained test drives the raw ioctls instead:
//
//   GETPARAM 3D_FEATURES / SUPPORTED_CAPSET_IDS
//   GET_CAPS virgl capset (cap_set_ver 0 -> newest)
//   CREATE_DUMB + MAP_DUMB + mmap
//   RESOURCE_CREATE (3D, with backing) + TRANSFER_TO_HOST + TRANSFER_FROM_HOST
//   EXECBUFFER with a single VIRGL_CMD_NOP dword
//   WAIT
//   pixel data round-trip comparison through the host virglrenderer
//
// Prints M16_VIRGL_* evidence lines; M16_VIRGL_RAW_PASS on success.

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <unistd.h>

#define DRM_IOCTL_BASE 'd'
#define DRM_IOWR(nr, type) _IOWR(DRM_IOCTL_BASE, nr, type)

#define DRM_IOCTL_MODE_CREATE_DUMB DRM_IOWR(0xb2, struct drm_mode_create_dumb)
#define DRM_IOCTL_MODE_MAP_DUMB DRM_IOWR(0xb3, struct drm_mode_map_dumb)
#define DRM_IOCTL_VIRTGPU_MAP DRM_IOWR(0x41, struct drm_virtgpu_map)
#define DRM_IOCTL_VIRTGPU_EXECBUFFER DRM_IOWR(0x42, struct drm_virtgpu_execbuffer)
#define DRM_IOCTL_VIRTGPU_GETPARAM DRM_IOWR(0x43, struct drm_virtgpu_getparam)
#define DRM_IOCTL_VIRTGPU_RESOURCE_CREATE DRM_IOWR(0x44, struct drm_virtgpu_resource_create)
#define DRM_IOCTL_VIRTGPU_RESOURCE_INFO DRM_IOWR(0x45, struct drm_virtgpu_resource_info)
#define DRM_IOCTL_VIRTGPU_TRANSFER_FROM_HOST DRM_IOWR(0x46, struct drm_virtgpu_3d_transfer)
#define DRM_IOCTL_VIRTGPU_TRANSFER_TO_HOST DRM_IOWR(0x47, struct drm_virtgpu_3d_transfer)
#define DRM_IOCTL_VIRTGPU_WAIT DRM_IOWR(0x48, struct drm_virtgpu_3d_wait)
#define DRM_IOCTL_VIRTGPU_GET_CAPS DRM_IOWR(0x49, struct drm_virtgpu_get_caps)

#define VIRTGPU_PARAM_3D_FEATURES 1
#define VIRTGPU_PARAM_SUPPORTED_CAPSET_IDS 7

#define VIRTIO_GPU_CAPSET_VIRGL 1
#define VIRTIO_GPU_CAPSET_VIRGL2 2

#define PIPE_TEXTURE_2D 2
#define PIPE_FORMAT_B8G8R8X8_UNORM 1
#define PIPE_BIND_RENDER_TARGET 2

struct drm_mode_create_dumb {
    uint32_t height, width, bpp, flags;
    uint32_t handle, pitch;
    uint64_t size;
};

struct drm_mode_map_dumb {
    uint32_t handle;
    uint32_t pad;
    uint64_t offset;
};

struct drm_virtgpu_map {
    uint64_t offset;
    uint32_t handle;
    uint32_t pad;
};

struct drm_virtgpu_getparam {
    uint64_t param;
    uint64_t value; /* userspace pointer to u64 */
};

struct drm_virtgpu_get_caps {
    uint64_t cap_set_id;
    uint64_t cap_set_ver;
    uint64_t addr;
    uint32_t size;
};

struct drm_virtgpu_resource_create {
    uint32_t target;
    uint32_t format;
    uint32_t bind;
    uint32_t width;
    uint32_t height;
    uint32_t depth;
    uint32_t array_size;
    uint32_t last_level;
    uint32_t nr_samples;
    uint32_t flags;
    uint32_t bo_handle;
    uint32_t res_handle;
    uint32_t size;
    uint32_t stride;
};

struct drm_virtgpu_resource_info {
    uint32_t bo_handle;
    uint32_t res_handle;
    uint32_t size;
    uint32_t blob_mem;
};

struct drm_virtgpu_3d_box {
    uint32_t x, y, z, w, h, d;
};

struct drm_virtgpu_3d_transfer {
    uint32_t bo_handle;
    struct drm_virtgpu_3d_box box;
    uint32_t level;
    uint32_t offset;
    uint32_t stride;
    uint32_t layer_stride;
};

struct drm_virtgpu_3d_wait {
    uint32_t handle;
    uint32_t flags;
};

struct drm_virtgpu_execbuffer {
    uint32_t flags;
    uint32_t size;
    uint64_t command;
    uint64_t bo_handles;
    uint32_t num_bo_handles;
    int32_t fence_fd;
    uint32_t ring_idx;
    uint32_t syncobj_stride;
    uint32_t num_in_syncobjs;
    uint32_t num_out_syncobjs;
    uint64_t in_syncobjs;
    uint64_t out_syncobjs;
};

static int failures;

#define CHECK(cond, ...) do { \
    if (cond) { printf("M16_VIRGL_PASS " __VA_ARGS__); printf("\n"); } \
    else { printf("M16_VIRGL_FAIL " __VA_ARGS__); printf("\n"); failures++; } \
} while (0)

#define TEX_W 64
#define TEX_H 64

int main(void) {
    setvbuf(stdout, NULL, _IOLBF, 0);
    printf("M16 virgl raw ioctl verification\n");

    /* Runs as an early userspace step; ensure dev nodes exist. */
    mkdir("/dev", 0755);
    mount("devtmpfs", "/dev", "devtmpfs", 0, NULL);

    int fd = open("/dev/dri/renderD128", O_RDWR);
    if (fd < 0) fd = open("/dev/dri/card0", O_RDWR);
    CHECK(fd >= 0, "open drm device");
    if (fd < 0) return 1;

    /* GETPARAM: value is a userspace pointer. */
    uint64_t val = 0;
    struct drm_virtgpu_getparam gp = {
        .param = VIRTGPU_PARAM_3D_FEATURES,
        .value = (uint64_t)&val,
    };
    int rc = ioctl(fd, DRM_IOCTL_VIRTGPU_GETPARAM, &gp);
    CHECK(rc == 0 && val == 1, "GETPARAM 3D_FEATURES=%llu", (unsigned long long)val);

    val = 0;
    gp.param = VIRTGPU_PARAM_SUPPORTED_CAPSET_IDS;
    gp.value = (uint64_t)&val;
    rc = ioctl(fd, DRM_IOCTL_VIRTGPU_GETPARAM, &gp);
    CHECK(rc == 0 && (val & ((1 << VIRTIO_GPU_CAPSET_VIRGL) | (1 << VIRTIO_GPU_CAPSET_VIRGL2))) != 0,
          "GETPARAM CAPSETS=0x%llx", (unsigned long long)val);

    /* GET_CAPS: virgl capset. Try version 0 (=newest) and explicit v1/v2. */
    uint8_t caps[512];
    for (uint64_t ver = 0; ver <= 2; ver++) {
        memset(caps, 0, sizeof(caps));
        struct drm_virtgpu_get_caps gc2 = {
            .cap_set_id = VIRTIO_GPU_CAPSET_VIRGL,
            .cap_set_ver = ver,
            .addr = (uint64_t)caps,
            .size = sizeof(caps),
        };
        rc = ioctl(fd, DRM_IOCTL_VIRTGPU_GET_CAPS, &gc2);
        int nonzero = 0;
        for (int i = 0; i < (int)sizeof(caps); i++) nonzero += caps[i] != 0;
        printf("M16_VIRGL_CAPS_DUMP ver=%llu rc=%d size=%u nonzero=%d first16=",
               (unsigned long long)ver, rc, gc2.size, nonzero);
        for (int i = 0; i < 16; i++) printf("%02x", caps[i]);
        printf("\n");
        if (ver == 1) {
            /* virgl capset v1 is the data Mesa's virgl driver consumes;
             * v2 comes back zeroed from this host virglrenderer (1.3.0). */
            CHECK(rc == 0 && gc2.size > 0 && nonzero > 0,
                  "GET_CAPS virgl v1 size=%u nonzero=%d", gc2.size, nonzero);
        }
    }

    /* Dumb buffer as backing store. */
    struct drm_mode_create_dumb cd = { .width = TEX_W, .height = TEX_H, .bpp = 32 };
    CHECK(ioctl(fd, DRM_IOCTL_MODE_CREATE_DUMB, &cd) == 0, "CREATE_DUMB");
    struct drm_mode_map_dumb md = { .handle = cd.handle };
    CHECK(ioctl(fd, DRM_IOCTL_MODE_MAP_DUMB, &md) == 0, "MAP_DUMB");
    uint32_t *pixels = mmap(NULL, cd.size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, md.offset);
    CHECK(pixels != MAP_FAILED, "mmap dumb");

    /* Fill with a known pattern. */
    for (int i = 0; i < TEX_W * TEX_H; i++)
        pixels[i] = 0xa5000000u | (uint32_t)i;

    /* 3D resource backed by the dumb buffer. */
    struct drm_virtgpu_resource_create rc3d = {
        .target = PIPE_TEXTURE_2D,
        .format = PIPE_FORMAT_B8G8R8X8_UNORM,
        .bind = PIPE_BIND_RENDER_TARGET,
        .width = TEX_W,
        .height = TEX_H,
        .depth = 1,
        .array_size = 1,
        .last_level = 1,
        .nr_samples = 0,
        .flags = 0,
        .bo_handle = cd.handle,
    };
    rc = ioctl(fd, DRM_IOCTL_VIRTGPU_RESOURCE_CREATE, &rc3d);
    CHECK(rc == 0 && rc3d.res_handle != 0 && rc3d.size >= (uint32_t)cd.size,
          "RESOURCE_CREATE res=%u size=%u stride=%u", rc3d.res_handle, rc3d.size, rc3d.stride);

    struct drm_virtgpu_resource_info ri = { .bo_handle = cd.handle };
    rc = ioctl(fd, DRM_IOCTL_VIRTGPU_RESOURCE_INFO, &ri);
    CHECK(rc == 0 && ri.size >= (uint32_t)cd.size, "RESOURCE_INFO size=%u", ri.size);

    /* Upload the pattern to the host. */
    struct drm_virtgpu_3d_transfer up = {
        .bo_handle = cd.handle,
        .box = { .x = 0, .y = 0, .z = 0, .w = TEX_W, .h = TEX_H, .d = 1 },
        .level = 0,
        .offset = 0,
        .stride = cd.pitch,
        .layer_stride = 0,
    };
    CHECK(ioctl(fd, DRM_IOCTL_VIRTGPU_TRANSFER_TO_HOST, &up) == 0, "TRANSFER_TO_HOST");

    /* A minimal execbuffer: one VIRGL_CMD_NOP dword (len 0, cmd 0). */
    uint32_t nop = 0;
    struct drm_virtgpu_execbuffer eb = {
        .flags = 0,
        .size = 4,
        .command = (uint64_t)&nop,
        .fence_fd = -1,
    };
    CHECK(ioctl(fd, DRM_IOCTL_VIRTGPU_EXECBUFFER, &eb) == 0, "EXECBUFFER nop");

    /* Wipe the local copy, then download from the host. */
    memset(pixels, 0, cd.size);
    struct drm_virtgpu_3d_transfer down = up;
    CHECK(ioctl(fd, DRM_IOCTL_VIRTGPU_TRANSFER_FROM_HOST, &down) == 0, "TRANSFER_FROM_HOST");

    struct drm_virtgpu_3d_wait wt = { .handle = cd.handle };
    CHECK(ioctl(fd, DRM_IOCTL_VIRTGPU_WAIT, &wt) == 0, "WAIT");

    int mismatch = 0;
    for (int i = 0; i < TEX_W * TEX_H; i++) {
        uint32_t expect = 0xa5000000u | (uint32_t)i;
        if (pixels[i] != expect) {
            if (mismatch < 4)
                printf("M16_VIRGL_MISMATCH px=%d got=%08x want=%08x\n", i, pixels[i], expect);
            mismatch++;
        }
    }
    CHECK(mismatch == 0, "pixel round-trip mismatch=%d", mismatch);

    close(fd);
    if (failures == 0) {
        printf("M16_VIRGL_RAW_PASS\n");
        return 0;
    }
    printf("M16_VIRGL_RAW_FAILED count=%d\n", failures);
    return 1;
}
