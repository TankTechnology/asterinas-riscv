// SPDX-License-Identifier: MPL-2.0

// Acceptance probe for a second CRTC.
//
// QEMU exposes more than one scanout when started with
// `-device virtio-gpu-pci,max_outputs=2`; the driver then creates one
// CRTC/encoder/connector per scanout. The probe
//
//   1. requires at least two CRTCs and two connectors,
//   2. modesets both CRTCs onto their own connector and framebuffer,
//   3. verifies each CRTC reports its own framebuffer,
//   4. flips the second CRTC asynchronously and checks the completion event
//      names that CRTC while the first CRTC keeps its framebuffer,
//   5. flips the first CRTC back and checks the second is untouched.
//
// As PID 1 it mounts devtmpfs first, so it can be a one-file initramfs
// `/init`. Prints MULTICRTC_PASS on success.

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <poll.h>
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

struct drm_mode_crtc_page_flip {
    uint32_t crtc_id;
    uint32_t fb_id;
    uint32_t flags;
    uint32_t reserved;
    uint64_t user_data;
};

struct drm_event {
    uint32_t type;
    uint32_t length;
};

struct drm_event_vblank {
    struct drm_event base;
    uint64_t user_data;
    uint32_t tv_sec;
    uint32_t tv_usec;
    uint32_t sequence;
    uint32_t crtc_id;
};

#define DRM_IOCTL_MODE_GETRESOURCES \
    _IOWR(DRM_IOCTL_BASE, 0xa0, struct drm_mode_card_res)
#define DRM_IOCTL_MODE_GETCRTC _IOWR(DRM_IOCTL_BASE, 0xa1, struct drm_mode_crtc)
#define DRM_IOCTL_MODE_SETCRTC _IOWR(DRM_IOCTL_BASE, 0xa2, struct drm_mode_crtc)
#define DRM_IOCTL_MODE_GETCONNECTOR \
    _IOWR(DRM_IOCTL_BASE, 0xa7, struct drm_mode_get_connector)
#define DRM_IOCTL_MODE_ADDFB _IOWR(DRM_IOCTL_BASE, 0xae, struct drm_mode_fb_cmd)
#define DRM_IOCTL_MODE_PAGE_FLIP \
    _IOWR(DRM_IOCTL_BASE, 0xb0, struct drm_mode_crtc_page_flip)
#define DRM_IOCTL_MODE_CREATE_DUMB \
    _IOWR(DRM_IOCTL_BASE, 0xb2, struct drm_mode_create_dumb)
#define DRM_IOCTL_MODE_MAP_DUMB \
    _IOWR(DRM_IOCTL_BASE, 0xb3, struct drm_mode_map_dumb)

#define DRM_MODE_PAGE_FLIP_EVENT 0x01
#define DRM_EVENT_FLIP_COMPLETE 0x02

static int card = -1;

static void fail(const char *step) {
    printf("MULTICRTC_FAIL %s errno=%d (%s)\n", step, errno, strerror(errno));
    exit(1);
}

static struct drm_mode_crtc get_crtc(uint32_t crtc_id) {
    struct drm_mode_crtc crtc = {.crtc_id = crtc_id};
    if (ioctl(card, DRM_IOCTL_MODE_GETCRTC, &crtc) != 0) {
        fail("getcrtc");
    }
    return crtc;
}

static uint32_t create_fb(uint32_t width, uint32_t height, uint32_t color,
                          uint32_t *pitch_out) {
    struct drm_mode_create_dumb dumb = {
        .width = width,
        .height = height,
        .bpp = 32,
    };
    if (ioctl(card, DRM_IOCTL_MODE_CREATE_DUMB, &dumb) != 0) {
        fail("create-dumb");
    }
    struct drm_mode_map_dumb map_dumb = {.handle = dumb.handle};
    if (ioctl(card, DRM_IOCTL_MODE_MAP_DUMB, &map_dumb) != 0) {
        fail("map-dumb");
    }
    uint8_t *mapped = mmap(NULL, dumb.size, PROT_READ | PROT_WRITE, MAP_SHARED,
                           card, (off_t)map_dumb.offset);
    if (mapped == MAP_FAILED) {
        fail("mmap-dumb");
    }
    for (uint32_t y = 0; y < height; y++) {
        uint32_t *line = (uint32_t *)(mapped + (size_t)y * dumb.pitch);
        for (uint32_t x = 0; x < width; x++) {
            line[x] = color;
        }
    }
    struct drm_mode_fb_cmd addfb = {
        .width = width,
        .height = height,
        .pitch = dumb.pitch,
        .bpp = 32,
        .depth = 24,
        .handle = dumb.handle,
    };
    if (ioctl(card, DRM_IOCTL_MODE_ADDFB, &addfb) != 0) {
        fail("addfb");
    }
    *pitch_out = dumb.pitch;
    return addfb.fb_id;
}

static int page_flip(uint32_t crtc_id, uint32_t fb_id, uint32_t flags,
                     uint64_t user_data) {
    struct drm_mode_crtc_page_flip flip = {
        .crtc_id = crtc_id,
        .fb_id = fb_id,
        .flags = flags,
        .user_data = user_data,
    };
    return ioctl(card, DRM_IOCTL_MODE_PAGE_FLIP, &flip);
}

static int read_event(struct drm_event_vblank *event) {
    ssize_t count = read(card, event, sizeof(*event));
    return count == (ssize_t)sizeof(*event) ? 0 : -1;
}

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);

    if (getpid() == 1) {
        mkdir("/dev", 0755);
        if (mount("devtmpfs", "/dev", "devtmpfs", 0, NULL) != 0 && errno != EBUSY) {
            fail("mount-devtmpfs");
        }
    }

    card = open("/dev/dri/card0", O_RDWR | O_CLOEXEC);
    if (card < 0) {
        fail("open-card");
    }

    struct drm_mode_card_res res = {0};
    if (ioctl(card, DRM_IOCTL_MODE_GETRESOURCES, &res) != 0) {
        fail("getresources-count");
    }
    uint32_t crtc_ids[8], connector_ids[8], encoder_ids[8];
    memset(&res, 0, sizeof(res));
    res.crtc_id_ptr = (uint64_t)(uintptr_t)crtc_ids;
    res.count_crtcs = 8;
    res.connector_id_ptr = (uint64_t)(uintptr_t)connector_ids;
    res.count_connectors = 8;
    res.encoder_id_ptr = (uint64_t)(uintptr_t)encoder_ids;
    res.count_encoders = 8;
    if (ioctl(card, DRM_IOCTL_MODE_GETRESOURCES, &res) != 0) {
        fail("getresources");
    }
    printf("MULTICRTC_RESOURCES crtcs=%u connectors=%u encoders=%u\n",
           res.count_crtcs, res.count_connectors, res.count_encoders);
    if (res.count_crtcs < 2 || res.count_connectors < 2) {
        printf("MULTICRTC_FAIL missing-second-crtc crtcs=%u connectors=%u\n",
               res.count_crtcs, res.count_connectors);
        return 1;
    }

    uint32_t crtc[2] = {crtc_ids[0], crtc_ids[1]};
    uint32_t connector[2] = {connector_ids[0], connector_ids[1]};
    struct drm_mode_modeinfo modes[2];
    for (int i = 0; i < 2; i++) {
        struct drm_mode_get_connector query = {.connector_id = connector[i]};
        if (ioctl(card, DRM_IOCTL_MODE_GETCONNECTOR, &query) != 0) {
            fail("getconnector-count");
        }
        memset(&query, 0, sizeof(query));
        query.connector_id = connector[i];
        query.modes_ptr = (uint64_t)(uintptr_t)&modes[i];
        query.count_modes = 1;
        if (ioctl(card, DRM_IOCTL_MODE_GETCONNECTOR, &query) != 0 ||
            query.count_modes == 0) {
            fail("getconnector-mode");
        }
    }

    uint32_t pitch = 0;
    uint32_t fb0 = create_fb(modes[0].hdisplay, modes[0].vdisplay, 0x00ff0000, &pitch);
    uint32_t fb1 = create_fb(modes[1].hdisplay, modes[1].vdisplay, 0x0000ff00, &pitch);
    uint32_t fb1_next =
        create_fb(modes[1].hdisplay, modes[1].vdisplay, 0x000000ff, &pitch);

    for (int i = 0; i < 2; i++) {
        uint32_t connectors[1] = {connector[i]};
        struct drm_mode_crtc setcrtc = {
            .set_connectors_ptr = (uint64_t)(uintptr_t)connectors,
            .count_connectors = 1,
            .crtc_id = crtc[i],
            .fb_id = i == 0 ? fb0 : fb1,
            .mode_valid = 1,
            .mode = modes[i],
        };
        if (ioctl(card, DRM_IOCTL_MODE_SETCRTC, &setcrtc) != 0) {
            fail(i == 0 ? "setcrtc-0" : "setcrtc-1");
        }
    }
    if (get_crtc(crtc[0]).fb_id != fb0 || get_crtc(crtc[1]).fb_id != fb1) {
        fail("modeset-state");
    }
    printf("MULTICRTC_MODESET crtc0=%u crtc1=%u fb0=%u fb1=%u\n", crtc[0], crtc[1],
           fb0, fb1);

    // Flip the second CRTC; its event must name that CRTC and leave the first
    // CRTC's framebuffer alone.
    if (page_flip(crtc[1], fb1_next, DRM_MODE_PAGE_FLIP_EVENT, 0x0c11u) != 0) {
        fail("flip-crtc1");
    }
    struct pollfd pfd = {.fd = card, .events = POLLIN};
    if (poll(&pfd, 1, 1000) <= 0) {
        fail("poll-flip-crtc1");
    }
    struct drm_event_vblank event;
    if (read_event(&event) != 0) {
        fail("read-flip-crtc1");
    }
    if (event.base.type != DRM_EVENT_FLIP_COMPLETE || event.user_data != 0x0c11u ||
        event.crtc_id != crtc[1]) {
        printf("MULTICRTC_FAIL event type=%u user=0x%llx crtc=%u\n", event.base.type,
               (unsigned long long)event.user_data, event.crtc_id);
        return 1;
    }
    if (get_crtc(crtc[1]).fb_id != fb1_next) {
        fail("flip-crtc1-state");
    }
    if (get_crtc(crtc[0]).fb_id != fb0) {
        fail("crtc0-disturbed");
    }
    printf("MULTICRTC_FLIP crtc1 fb=%u sequence=%u\n", fb1_next, event.sequence);

    // Flip the first CRTC back to its own framebuffer; the second keeps its.
    if (page_flip(crtc[0], fb0, DRM_MODE_PAGE_FLIP_EVENT, 0x0c00u) != 0) {
        fail("flip-crtc0");
    }
    if (poll(&pfd, 1, 1000) <= 0) {
        fail("poll-flip-crtc0");
    }
    if (read_event(&event) != 0 || event.crtc_id != crtc[0] ||
        event.user_data != 0x0c00u) {
        fail("read-flip-crtc0");
    }
    if (get_crtc(crtc[0]).fb_id != fb0 || get_crtc(crtc[1]).fb_id != fb1_next) {
        fail("flip-crtc0-state");
    }
    printf("MULTICRTC_FLIP crtc0 fb=%u sequence=%u\n", fb0, event.sequence);

    printf("MULTICRTC_PASS\n");
    if (getpid() == 1) {
        sleep(3600);
    }
    return 0;
}
