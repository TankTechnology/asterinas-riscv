// SPDX-License-Identifier: MPL-2.0

// A static legacy-KMS probe for the Asterinas DRM core.
//
// It speaks the same ioctl subset Xorg's modesetting driver uses to bring a
// scanout up: GETRESOURCES, GETCONNECTOR, GETENCODER, CREATE_DUMB, MAP_DUMB,
// a direct `mmap` of the dumb buffer, ADDFB, SETCRTC and DIRTYFB. The four
// colour bars it paints are the acceptance image for a monitor screendump.
//
// It is also usable as an initramfs `/init`: when PID 1 runs it, it mounts
// devtmpfs first so `/dev/dri/card0` exists. When run from a normal shell it
// probes the existing `/dev/dri/card0` and leaves the system mounted state
// alone.
//
// Build with tools/drm/build_probe.sh; the binary is self-contained.

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#define DRM_IOCTL_BASE 'd'

struct drm_mode_modeinfo {
    uint32_t clock;
    uint16_t hdisplay, hsync_start, hsync_end, htotal, hskew;
    uint16_t vdisplay, vsync_start, vsync_end, vtotal, vscan;
    uint32_t vrefresh;
    uint32_t flags;
    uint32_t type;
    char name[32];
};

struct drm_mode_card_res {
    uint64_t fb_id_ptr, crtc_id_ptr, connector_id_ptr, encoder_id_ptr;
    uint32_t count_fbs, count_crtcs, count_connectors, count_encoders;
    uint32_t min_width, max_width, min_height, max_height;
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

struct drm_mode_get_encoder {
    uint32_t encoder_id;
    uint32_t encoder_type;
    uint32_t crtc_id;
    uint32_t possible_crtcs;
    uint32_t possible_clones;
};

struct drm_mode_crtc {
    uint64_t set_connectors_ptr;
    uint32_t count_connectors;
    uint32_t crtc_id;
    uint32_t fb_id;
    uint32_t x, y;
    uint32_t gamma_size;
    uint32_t mode_valid;
    struct drm_mode_modeinfo mode;
};

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

struct drm_mode_fb_cmd {
    uint32_t fb_id, width, height, pitch, bpp, depth, handle;
};

struct drm_mode_fb_dirty_cmd {
    uint32_t fb_id, flags, color, num_clips;
    uint64_t clips_ptr;
};

#define DRM_IOCTL_MODE_GETRESOURCES \
    _IOWR(DRM_IOCTL_BASE, 0xa0, struct drm_mode_card_res)
#define DRM_IOCTL_MODE_SETCRTC \
    _IOWR(DRM_IOCTL_BASE, 0xa2, struct drm_mode_crtc)
#define DRM_IOCTL_MODE_GETENCODER \
    _IOWR(DRM_IOCTL_BASE, 0xa6, struct drm_mode_get_encoder)
#define DRM_IOCTL_MODE_GETCONNECTOR \
    _IOWR(DRM_IOCTL_BASE, 0xa7, struct drm_mode_get_connector)
#define DRM_IOCTL_MODE_ADDFB \
    _IOWR(DRM_IOCTL_BASE, 0xae, struct drm_mode_fb_cmd)
#define DRM_IOCTL_MODE_DIRTYFB \
    _IOWR(DRM_IOCTL_BASE, 0xb1, struct drm_mode_fb_dirty_cmd)
#define DRM_IOCTL_MODE_CREATE_DUMB \
    _IOWR(DRM_IOCTL_BASE, 0xb2, struct drm_mode_create_dumb)
#define DRM_IOCTL_MODE_MAP_DUMB \
    _IOWR(DRM_IOCTL_BASE, 0xb3, struct drm_mode_map_dumb)

#define DRM_MODE_CONNECTED 1

static int fd = -1;

static void fail(const char *step) {
    printf("DRMPROBE_FAIL %s errno=%d (%s)\n", step, errno, strerror(errno));
    fflush(stdout);
    exit(1);
}

static void hold_for(unsigned seconds) {
    for (unsigned i = 0; i < seconds; i += 60) {
        sleep(seconds - i > 60 ? 60 : seconds - i);
    }
}

static void mount_devtmpfs(void) {
    mkdir("/dev", 0755);
    if (mount("devtmpfs", "/dev", "devtmpfs", 0, NULL) != 0 && errno != EBUSY) {
        // Some images already have a devtmpfs; anything else is fatal because
        // the DRM node will never appear.
        fail("mount devtmpfs");
    }
}

static void open_card(void) {
    for (int i = 0; i < 100; i++) {
        fd = open("/dev/dri/card0", O_RDWR | O_CLOEXEC);
        if (fd >= 0) {
            printf("DRMPROBE_DEVICE /dev/dri/card0\n");
            return;
        }
        usleep(100 * 1000);
    }
    fail("open /dev/dri/card0");
}

static uint32_t mode_width(const struct drm_mode_modeinfo *mode) {
    return mode->hdisplay;
}

static uint32_t mode_height(const struct drm_mode_modeinfo *mode) {
    return mode->vdisplay;
}

int main(int argc, char **argv) {
    unsigned hold_seconds = getpid() == 1 ? 3600 : 0;
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--hold") == 0 && i + 1 < argc) {
            hold_seconds = (unsigned)strtoul(argv[++i], NULL, 10);
        } else if (strcmp(argv[i], "--no-mount") != 0) {
            // Unknown argument; keep the default behavior.
        }
    }

    setvbuf(stdout, NULL, _IONBF, 0);

    if (getpid() == 1) {
        mount_devtmpfs();
    }

    open_card();

    struct drm_mode_card_res res = {0};
    if (ioctl(fd, DRM_IOCTL_MODE_GETRESOURCES, &res) != 0) {
        fail("GETRESOURCES(counts)");
    }

    uint32_t crtc_ids[16], connector_ids[16], encoder_ids[16];
    memset(crtc_ids, 0, sizeof(crtc_ids));
    memset(connector_ids, 0, sizeof(connector_ids));
    memset(encoder_ids, 0, sizeof(encoder_ids));
    memset(&res, 0, sizeof(res));
    res.crtc_id_ptr = (uint64_t)(uintptr_t)crtc_ids;
    res.count_crtcs = 16;
    res.connector_id_ptr = (uint64_t)(uintptr_t)connector_ids;
    res.count_connectors = 16;
    res.encoder_id_ptr = (uint64_t)(uintptr_t)encoder_ids;
    res.count_encoders = 16;
    if (ioctl(fd, DRM_IOCTL_MODE_GETRESOURCES, &res) != 0) {
        fail("GETRESOURCES(ids)");
    }
    printf("DRMPROBE_RESOURCES crtcs=%u connectors=%u encoders=%u\n",
           res.count_crtcs, res.count_connectors, res.count_encoders);
    printf("DRMPROBE_IDS crtc=%u connector=%u encoder=%u\n", crtc_ids[0],
           connector_ids[0], encoder_ids[0]);

    struct drm_mode_get_connector connector = {
        .connector_id = connector_ids[0],
    };
    if (ioctl(fd, DRM_IOCTL_MODE_GETCONNECTOR, &connector) != 0) {
        fail("GETCONNECTOR(counts)");
    }
    struct drm_mode_modeinfo modes[16];
    memset(&connector, 0, sizeof(connector));
    connector.connector_id = connector_ids[0];
    connector.modes_ptr = (uint64_t)(uintptr_t)modes;
    connector.count_modes = 16;
    if (ioctl(fd, DRM_IOCTL_MODE_GETCONNECTOR, &connector) != 0) {
        fail("GETCONNECTOR(modes)");
    }
    if (connector.connection != DRM_MODE_CONNECTED || connector.count_modes == 0) {
        printf("DRMPROBE_FAIL connector not connected (connection=%u modes=%u)\n",
               connector.connection, connector.count_modes);
        return 1;
    }
    struct drm_mode_modeinfo mode = modes[0];
    printf("DRMPROBE_MODE %ux%u %s@%u\n", mode_width(&mode), mode_height(&mode),
           mode.name, mode.vrefresh);

    struct drm_mode_get_encoder encoder = {
        .encoder_id = connector.encoder_id,
    };
    printf("DRMPROBE_CONNECTOR encoder_id=%u type=%u status=%u\n",
           connector.encoder_id, connector.connector_type, connector.connection);
    if (encoder.encoder_id == 0) {
        encoder.encoder_id = encoder_ids[0];
    }
    if (ioctl(fd, DRM_IOCTL_MODE_GETENCODER, &encoder) != 0) {
        fail("GETENCODER");
    }
    // A connector created by the shared KMS object store starts without a
    // current encoder, so `encoder_id` may be zero and the encoder may not
    // name a current CRTC yet. Xorg's modesetting driver falls back to the
    // first CRTC in the encoder's `possible_crtcs` mask; do the same.
    uint32_t crtc_id = encoder.crtc_id;
    if (crtc_id == 0) {
        uint32_t possible = encoder.possible_crtcs;
        if (possible == 0 || (possible & 1) == 0) {
            printf("DRMPROBE_FAIL encoder possible_crtcs=0x%x\n", possible);
            return 1;
        }
        crtc_id = crtc_ids[0];
    }
    printf("DRMPROBE_CRTC %u\n", crtc_id);

    struct drm_mode_create_dumb dumb = {
        .width = mode_width(&mode),
        .height = mode_height(&mode),
        .bpp = 32,
    };
    if (ioctl(fd, DRM_IOCTL_MODE_CREATE_DUMB, &dumb) != 0) {
        fail("CREATE_DUMB");
    }
    printf("DRMPROBE_DUMB handle=%u pitch=%u size=%llu\n", dumb.handle,
           dumb.pitch, (unsigned long long)dumb.size);

    struct drm_mode_map_dumb map = {.handle = dumb.handle};
    if (ioctl(fd, DRM_IOCTL_MODE_MAP_DUMB, &map) != 0) {
        fail("MAP_DUMB");
    }
    uint8_t *fb = mmap(NULL, dumb.size, PROT_READ | PROT_WRITE, MAP_SHARED, fd,
                       (off_t)map.offset);
    if (fb == MAP_FAILED) {
        fail("mmap dumb");
    }

    // Four vertical bars: red, green, blue, white.
    static const uint32_t bars[4] = {0x00ff0000, 0x0000ff00, 0x000000ff,
                                     0x00ffffff};
    uint32_t width = mode_width(&mode);
    uint32_t height = mode_height(&mode);
    for (uint32_t y = 0; y < height; y++) {
        uint32_t *line = (uint32_t *)(fb + (size_t)y * dumb.pitch);
        for (uint32_t x = 0; x < width; x++) {
            line[x] = bars[(size_t)x * 4 / width];
        }
    }
    printf("DRMPROBE_FILLED\n");

    struct drm_mode_fb_cmd addfb = {
        .width = width,
        .height = height,
        .pitch = dumb.pitch,
        .bpp = 32,
        .depth = 24,
        .handle = dumb.handle,
    };
    if (ioctl(fd, DRM_IOCTL_MODE_ADDFB, &addfb) != 0) {
        fail("ADDFB");
    }
    printf("DRMPROBE_ADDFB fb=%u\n", addfb.fb_id);

    struct drm_mode_crtc setcrtc = {
        .set_connectors_ptr = (uint64_t)(uintptr_t)&connector_ids[0],
        .count_connectors = 1,
        .crtc_id = crtc_id,
        .fb_id = addfb.fb_id,
        .mode_valid = 1,
        .mode = mode,
    };
    if (ioctl(fd, DRM_IOCTL_MODE_SETCRTC, &setcrtc) != 0) {
        fail("SETCRTC");
    }
    printf("DRMPROBE_SETCRTC ok\n");

    struct drm_mode_fb_dirty_cmd dirty = {
        .fb_id = addfb.fb_id,
        .flags = 0,
    };
    if (ioctl(fd, DRM_IOCTL_MODE_DIRTYFB, &dirty) != 0) {
        fail("DIRTYFB");
    }
    printf("DRMPROBE_DIRTYFB ok\n");
    printf("DRMPROBE_PASS\n");

    if (hold_seconds > 0) {
        hold_for(hold_seconds);
    }
    return 0;
}
