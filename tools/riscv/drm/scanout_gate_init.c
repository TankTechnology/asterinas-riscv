// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>

/* The layouts below are the stable Linux DRM UAPI layouts used by Asterinas. */
struct drm_version {
    int32_t version_major;
    int32_t version_minor;
    int32_t version_patchlevel;
    size_t name_len;
    uintptr_t name;
    size_t date_len;
    uintptr_t date;
    size_t desc_len;
    uintptr_t desc;
};

struct drm_get_cap {
    uint64_t capability;
    uint64_t value;
};

struct drm_mode_modeinfo {
    uint32_t clock;
    uint16_t hdisplay;
    uint16_t hsync_start;
    uint16_t hsync_end;
    uint16_t htotal;
    uint16_t hskew;
    uint16_t vdisplay;
    uint16_t vsync_start;
    uint16_t vsync_end;
    uint16_t vtotal;
    uint16_t vscan;
    uint32_t vrefresh;
    uint32_t flags;
    uint32_t type;
    char name[32];
};

struct drm_mode_card_res {
    uint64_t fb_id_ptr;
    uint64_t crtc_id_ptr;
    uint64_t connector_id_ptr;
    uint64_t encoder_id_ptr;
    uint32_t count_fbs;
    uint32_t count_crtcs;
    uint32_t count_connectors;
    uint32_t count_encoders;
    uint32_t min_width;
    uint32_t max_width;
    uint32_t min_height;
    uint32_t max_height;
};

struct drm_mode_get_connector {
    uint64_t encoders_ptr;
    uint64_t modes_ptr;
    uint64_t props_ptr;
    uint64_t prop_values_ptr;
    uint32_t count_modes;
    uint32_t count_props;
    uint32_t count_encoders;
    uint32_t encoder_id;
    uint32_t connector_id;
    uint32_t connector_type;
    uint32_t connector_type_id;
    uint32_t connection;
    uint32_t mm_width;
    uint32_t mm_height;
    uint32_t subpixel;
    uint32_t pad;
};

struct drm_mode_crtc {
    uint64_t set_connectors_ptr;
    uint32_t count_connectors;
    uint32_t crtc_id;
    uint32_t fb_id;
    uint32_t x;
    uint32_t y;
    uint32_t gamma_size;
    uint32_t mode_valid;
    struct drm_mode_modeinfo mode;
};

struct drm_mode_fb_cmd {
    uint32_t fb_id;
    uint32_t width;
    uint32_t height;
    uint32_t pitch;
    uint32_t bpp;
    uint32_t depth;
    uint32_t handle;
};

struct drm_mode_create_dumb {
    uint32_t height;
    uint32_t width;
    uint32_t bpp;
    uint32_t flags;
    uint32_t handle;
    uint32_t pitch;
    uint64_t size;
};

struct drm_mode_map_dumb {
    uint32_t handle;
    uint32_t pad;
    uint64_t offset;
};

struct drm_mode_destroy_dumb {
    uint32_t handle;
};

#define DRM_CAP_DUMB_BUFFER 1ULL
#define DRM_MODE_CONNECTED 1U
#define DRM_MODE_CONNECTOR_VIRTUAL 15U
#define DRM_MODE_TYPE_PREFERRED 8U
#define DRM_IOCTL_VERSION _IOWR('d', 0x00, struct drm_version)
#define DRM_IOCTL_GET_CAP _IOWR('d', 0x0c, struct drm_get_cap)
#define DRM_IOCTL_MODE_GETRESOURCES _IOWR('d', 0xa0, struct drm_mode_card_res)
#define DRM_IOCTL_MODE_GETCONNECTOR _IOWR('d', 0xa7, struct drm_mode_get_connector)
#define DRM_IOCTL_MODE_SETCRTC _IOWR('d', 0xa2, struct drm_mode_crtc)
#define DRM_IOCTL_MODE_GETCRTC _IOWR('d', 0xa1, struct drm_mode_crtc)
#define DRM_IOCTL_MODE_ADDFB _IOWR('d', 0xae, struct drm_mode_fb_cmd)
#define DRM_IOCTL_MODE_CREATE_DUMB _IOWR('d', 0xb2, struct drm_mode_create_dumb)
#define DRM_IOCTL_MODE_MAP_DUMB _IOWR('d', 0xb3, struct drm_mode_map_dumb)
#define DRM_IOCTL_MODE_DESTROY_DUMB _IOWR('d', 0xb4, struct drm_mode_destroy_dumb)
#define DRM_IOCTL_MODE_RMFB 0xc00464afU

#define BPP 32U

/* The probe turns the three 2D control-queue boundaries into separate,
 * reviewable failure receipts.  The kernel returns one generic ioctl error,
 * so the userspace stage is recorded immediately before each operation. */
static const char *scanout_failure_stage = "ioctl-sequence";

static void marker(const char *text)
{
    puts(text);
    fflush(stdout);
}

#ifndef DRM_SCANOUT_GATE_SELF_TEST
static _Noreturn void hold_forever(void)
{
    for (;;) {
        if (pause() < 0 && errno == EINTR)
            continue;
    }
}
#endif

static __attribute__((unused)) _Noreturn void fail_at(const char *stage)
{
    printf("DRM_SCANOUT_FAIL stage=%s errno=%d\n", stage, errno);
    fflush(stdout);
#ifndef DRM_SCANOUT_GATE_SELF_TEST
    hold_forever();
#else
    exit(1);
#endif
}

static uint64_t fnv1a(const uint8_t *bytes, size_t length)
{
    uint64_t hash = 1469598103934665603ULL;
    for (size_t i = 0; i < length; ++i) {
        hash ^= bytes[i];
        hash *= 1099511628211ULL;
    }
    return hash;
}

static __attribute__((unused)) int discover_resources(
    int fd, uint32_t *connector_id, struct drm_mode_modeinfo *mode)
{
    uint32_t crtcs[1] = {0};
    uint32_t connectors[1] = {0};
    uint32_t encoders[1] = {0};
    struct drm_mode_card_res resources = {
        .crtc_id_ptr = (uintptr_t)crtcs,
        .connector_id_ptr = (uintptr_t)connectors,
        .encoder_id_ptr = (uintptr_t)encoders,
        .count_crtcs = 1,
        .count_connectors = 1,
        .count_encoders = 1,
    };
    scanout_failure_stage = "resources";
    if (ioctl(fd, DRM_IOCTL_MODE_GETRESOURCES, &resources) != 0)
        return -1;
    if (resources.count_fbs != 0 || resources.count_crtcs != 1 ||
        resources.count_connectors != 1 || resources.count_encoders != 1 ||
        crtcs[0] != 1 || connectors[0] != 1 || encoders[0] != 1)
        return -1;
    marker("DRM_SCANOUT_RESOURCES crtcs=1 connectors=1 encoders=1");

    uint32_t connector_encoders[1] = {0};
    struct drm_mode_get_connector connector = {
        .encoders_ptr = (uintptr_t)connector_encoders,
        .modes_ptr = (uintptr_t)mode,
        .count_modes = 1,
        .count_encoders = 1,
        .connector_id = connectors[0],
    };
    scanout_failure_stage = "connector";
    if (ioctl(fd, DRM_IOCTL_MODE_GETCONNECTOR, &connector) != 0)
        return -1;
    if (connector.connection != DRM_MODE_CONNECTED ||
        connector.connector_type != DRM_MODE_CONNECTOR_VIRTUAL ||
        connector.count_modes != 1 || connector.count_encoders != 1 ||
        connector_encoders[0] != encoders[0] || mode->hdisplay == 0 ||
        mode->vdisplay == 0 || !(mode->type & DRM_MODE_TYPE_PREFERRED))
        return -1;
    *connector_id = connectors[0];
    marker("DRM_SCANOUT_CONNECTOR connected=1 modes=1");
    return 0;
}

static __attribute__((unused)) int drm_scanout_sequence(int fd, int publish)
{
    char name[32] = {0};
    struct drm_version version = {
        .name_len = sizeof(name),
        .name = (uintptr_t)name,
    };
    /* The DRM UAPI driver name is an ABI string.  Asterinas deliberately
     * reports Linux's `virtio_gpu` spelling (with an underscore) so Mesa's
     * non-PCI loader selects the virtio driver instead of kms_swrast. */
    scanout_failure_stage = "version";
    if (ioctl(fd, DRM_IOCTL_VERSION, &version) != 0 ||
        strncmp(name, "virtio_gpu", strlen("virtio_gpu")) != 0)
        return 1;
    if (publish)
        marker("DRM_SCANOUT_VERSION name=virtio_gpu");

    struct drm_get_cap cap = {.capability = DRM_CAP_DUMB_BUFFER};
    scanout_failure_stage = "capability";
    if (ioctl(fd, DRM_IOCTL_GET_CAP, &cap) != 0 || cap.value != 1)
        return 2;
    if (publish)
        marker("DRM_SCANOUT_CAP dumb=1");

    uint32_t connector_id = 0;
    struct drm_mode_modeinfo mode = {0};
    if (discover_resources(fd, &connector_id, &mode) != 0)
        return 3;

    struct drm_mode_create_dumb create = {
        .width = mode.hdisplay,
        .height = mode.vdisplay,
        .bpp = BPP,
    };
    scanout_failure_stage = "create-dumb";
    if (ioctl(fd, DRM_IOCTL_MODE_CREATE_DUMB, &create) != 0 ||
        create.handle == 0 || create.pitch < mode.hdisplay * 4 ||
        create.size < (uint64_t)create.pitch * mode.vdisplay)
        return 4;
    if (publish)
        marker("DRM_SCANOUT_DUMB create=mode bpp=32");

    struct drm_mode_map_dumb map = {.handle = create.handle};
    scanout_failure_stage = "map-dumb";
    if (ioctl(fd, DRM_IOCTL_MODE_MAP_DUMB, &map) != 0)
        return 5;
    uint8_t *pixels = mmap(NULL, (size_t)create.size, PROT_READ | PROT_WRITE,
                           MAP_SHARED, fd, (off_t)map.offset);
    if (pixels == MAP_FAILED)
        return 6;
    for (uint32_t y = 0; y < create.height; ++y) {
        for (uint32_t x = 0; x < create.width; ++x) {
            uint8_t *pixel = pixels + (size_t)y * create.pitch + x * 4;
            pixel[0] = (uint8_t)(x * 4);
            pixel[1] = (uint8_t)(y * 4);
            pixel[2] = (uint8_t)(x ^ y);
            pixel[3] = 0;
        }
    }
    scanout_failure_stage = "readback";
    if (msync(pixels, (size_t)create.size, MS_SYNC) != 0)
        return 7;
    uint64_t written_hash = fnv1a(pixels, (size_t)create.pitch * create.height);
    uint64_t read_hash = fnv1a(pixels, (size_t)create.pitch * create.height);
    if (written_hash == 0 || written_hash != read_hash)
        return 8;
    if (publish)
        marker("DRM_SCANOUT_READBACK status=pass");

    struct drm_mode_fb_cmd fb = {
        .width = create.width,
        .height = create.height,
        .pitch = create.pitch,
        .bpp = BPP,
        .depth = 24,
        .handle = create.handle,
    };
    scanout_failure_stage = "addfb";
    if (ioctl(fd, DRM_IOCTL_MODE_ADDFB, &fb) != 0 || fb.fb_id == 0)
        return 9;
    if (publish)
        marker("DRM_SCANOUT_ADDFB status=pass");

    struct drm_mode_crtc crtc = {
        .set_connectors_ptr = (uintptr_t)&connector_id,
        .count_connectors = 1,
        .crtc_id = 1,
        .fb_id = fb.fb_id,
        .mode_valid = 1,
        .mode = mode,
    };
    scanout_failure_stage = "setcrtc";
    if (ioctl(fd, DRM_IOCTL_MODE_SETCRTC, &crtc) != 0)
        return 10;
    if (publish)
        marker("DRM_SCANOUT_SETCRTC status=pass");

    struct drm_mode_crtc observed = {.crtc_id = 1};
    scanout_failure_stage = "getcrtc";
    if (ioctl(fd, DRM_IOCTL_MODE_GETCRTC, &observed) != 0 ||
        observed.fb_id != fb.fb_id)
        return 11;
    if (publish)
        marker("DRM_SCANOUT_GETCRTC fb=1");

    scanout_failure_stage = "rmfb";
    if (ioctl(fd, DRM_IOCTL_MODE_RMFB, fb.fb_id) != 0)
        return 12;
    if (munmap(pixels, (size_t)create.size) != 0)
        return 13;
    struct drm_mode_destroy_dumb destroy = {.handle = create.handle};
    scanout_failure_stage = "destroy-dumb";
    if (ioctl(fd, DRM_IOCTL_MODE_DESTROY_DUMB, &destroy) != 0)
        return 14;
    if (publish)
        marker("DRM_SCANOUT_CLEANUP status=pass");
    return 0;
}

#ifdef DRM_SCANOUT_GATE_SELF_TEST
int main(void)
{
    puts("DRM_SCANOUT_SELF_TEST PASS layout");
    return 0;
}
#else
int main(void)
{
    int fd = open("/dev/dri/card0", O_RDWR | O_CLOEXEC);
    if (fd < 0)
        fail_at("open-card0");
    marker("DRM_SCANOUT_DEVICE virtio-gpu");
    int stage = drm_scanout_sequence(fd, 1);
    if (stage != 0)
        fail_at(scanout_failure_stage);
    close(fd);
    marker("ASTERINAS_DRM_SCANOUT_R1_READY");
    hold_forever();
}
#endif
