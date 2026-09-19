// SPDX-License-Identifier: MPL-2.0
//
// Ask GBM, from inside the guest, which path it takes for the buffers a
// compositor needs.
//
// The question this exists to answer is not "does GBM work" -- it is *which*
// Mesa backend served a buffer. Mesa creates a GBM buffer one of two ways:
//
//   - through the DRI image extension, which for virgl means
//     `VIRTGPU_RESOURCE_CREATE` reaches the kernel; or
//   - through `create_dumb()`, which issues `MODE_CREATE_DUMB` instead.
//
// `gbm_dri_bo_create()` chooses between them on `dri->has_dmabuf_export`, and
// that field is private to the library. It is nevertheless observable from
// outside, because the two paths disagree about formats: `create_dumb()`
// only accepts a scanout buffer whose format is XRGB8888 or XBGR8888, and
// returns EINVAL for ARGB8888 with no ioctl at all, while the image path has
// no such rule. So a format matrix separates them without a debugger.
//
// Everything is reached through `dlopen` so this needs no Mesa headers and no
// link-time dependency on the guest's libraries -- only the binary itself.
//
// Build (host cross toolchain, no sysroot beyond the compiler's own):
//   riscv64-linux-gnu-gcc -O2 -o drm-gbm-probe drm-gbm-probe.c -ldl

#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

/* gbm/backend flags, from `gbm.h`. */
#define GBM_BO_USE_SCANOUT (1 << 0)
#define GBM_BO_USE_CURSOR_64X64 (1 << 1)
#define GBM_BO_USE_RENDERING (1 << 2)

/* fourcc("AR24"), fourcc("XR24"), fourcc("XB24"). */
#define GBM_FORMAT_ARGB8888 0x34325241u
#define GBM_FORMAT_XRGB8888 0x34325258u
#define GBM_FORMAT_XBGR8888 0x34324258u

struct gbm_device;
struct gbm_bo;

static void *(*p_gbm_create_device)(int fd);
static void (*p_gbm_device_destroy)(struct gbm_device *dev);
static const char *(*p_gbm_device_get_backend_name)(struct gbm_device *dev);
static struct gbm_bo *(*p_gbm_bo_create)(struct gbm_device *dev, uint32_t width,
                                         uint32_t height, uint32_t format,
                                         uint32_t usage);
static void (*p_gbm_bo_destroy)(struct gbm_bo *bo);
static uint32_t (*p_gbm_bo_get_stride)(struct gbm_bo *bo);
static int (*p_gbm_bo_get_fd)(struct gbm_bo *bo);

static int load_gbm(void) {
    void *h = dlopen("libgbm.so.1", RTLD_NOW | RTLD_LOCAL);
    if (!h) {
        printf("GBM_PROBE dlopen-failed %s\n", dlerror());
        return -1;
    }
    p_gbm_create_device = dlsym(h, "gbm_create_device");
    p_gbm_device_destroy = dlsym(h, "gbm_device_destroy");
    p_gbm_device_get_backend_name = dlsym(h, "gbm_device_get_backend_name");
    p_gbm_bo_create = dlsym(h, "gbm_bo_create");
    p_gbm_bo_destroy = dlsym(h, "gbm_bo_destroy");
    p_gbm_bo_get_stride = dlsym(h, "gbm_bo_get_stride");
    p_gbm_bo_get_fd = dlsym(h, "gbm_bo_get_fd");
    if (!p_gbm_create_device || !p_gbm_bo_create) {
        printf("GBM_PROBE dlsym-failed\n");
        return -1;
    }
    return 0;
}

struct case_ {
    const char *label;
    uint32_t format;
    uint32_t usage;
};

static void run_case(struct gbm_device *dev, const struct case_ *c, int w,
                     int h) {
    struct gbm_bo *bo;

    errno = 0;
    bo = p_gbm_bo_create(dev, w, h, c->format, c->usage);
    if (!bo) {
        printf("GBM_PROBE %s FAILED errno=%d (%s)\n", c->label, errno,
               strerror(errno));
        return;
    }

    printf("GBM_PROBE %s ok stride=%u", c->label, p_gbm_bo_get_stride(bo));
    if (p_gbm_bo_get_fd) {
        int fd = p_gbm_bo_get_fd(bo);
        printf(" dmabuf_fd=%d errno=%d", fd, fd < 0 ? errno : 0);
        if (fd >= 0)
            close(fd);
    }
    printf("\n");
    p_gbm_bo_destroy(bo);
}

static void probe_node(const char *node, int w, int h) {
    static const struct case_ cases[] = {
        /* What Xorg's `drmmode_create_bo` asks for, with depth 24. */
        {"argb8888+scanout+render", GBM_FORMAT_ARGB8888,
         GBM_BO_USE_SCANOUT | GBM_BO_USE_RENDERING},
        /* The same request in a format `create_dumb` accepts. */
        {"xrgb8888+scanout+render", GBM_FORMAT_XRGB8888,
         GBM_BO_USE_SCANOUT | GBM_BO_USE_RENDERING},
        {"xbgr8888+scanout+render", GBM_FORMAT_XBGR8888,
         GBM_BO_USE_SCANOUT | GBM_BO_USE_RENDERING},
        /* `create_dumb` accepts this one whatever the export capability is. */
        {"argb8888+cursor", GBM_FORMAT_ARGB8888, GBM_BO_USE_CURSOR_64X64},
        /* No SCANOUT: `create_dumb` would refuse, the image path would not. */
        {"argb8888+render-only", GBM_FORMAT_ARGB8888, GBM_BO_USE_RENDERING},
    };
    struct gbm_device *dev;
    int fd, i;

    fd = open(node, O_RDWR | O_CLOEXEC);
    if (fd < 0) {
        printf("GBM_PROBE node=%s open-failed errno=%d\n", node, errno);
        return;
    }

    errno = 0;
    dev = p_gbm_create_device(fd);
    if (!dev) {
        printf("GBM_PROBE node=%s gbm_create_device FAILED errno=%d (%s)\n", node,
               errno, strerror(errno));
        close(fd);
        return;
    }

    printf("GBM_PROBE node=%s backend=%s\n", node,
           p_gbm_device_get_backend_name
               ? p_gbm_device_get_backend_name(dev)
               : "(no symbol)");

    for (i = 0; i < (int)(sizeof(cases) / sizeof(cases[0])); i++)
        run_case(dev, &cases[i], w, h);

    p_gbm_device_destroy(dev);
    close(fd);
}

int main(int argc, char **argv) {
    int w = argc > 1 ? atoi(argv[1]) : 1280;
    int h = argc > 2 ? atoi(argv[2]) : 800;

    if (load_gbm() != 0)
        return 1;

    printf("GBM_PROBE begin %dx%d\n", w, h);
    probe_node("/dev/dri/card0", w, h);
    probe_node("/dev/dri/renderD128", w, h);
    printf("GBM_PROBE end\n");
    return 0;
}
