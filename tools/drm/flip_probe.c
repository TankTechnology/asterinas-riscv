// SPDX-License-Identifier: MPL-2.0

// Acceptance probe for page-flip completion events and the vblank surface.
//
//   1. establishes a mode with legacy SETCRTC,
//   2. flips to a second framebuffer with DRM_MODE_PAGE_FLIP_EVENT, checks the
//      ioctl returned before the event was queued (the flip is deferred to
//      the next scanout), polls the DRM file, and reads a
//      DRM_EVENT_FLIP_COMPLETE record,
//   3. verifies GETCRTC reports the flipped framebuffer,
//   4. performs an ASYNC flip back without an event,
//   5. reads the vblank sequence with WAIT_VBLANK (absolute) and requests a
//      vblank event (DRM_VBLANK_EVENT) that arrives through poll/read,
//   6. checks the negative cases (unknown flip flags, missing framebuffer).
//
// As PID 1 it mounts devtmpfs first, so it can be used as a one-file initramfs
// `/init`.

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
#include <time.h>
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

struct drm_wait_vblank {
    uint32_t type;
    uint32_t sequence;
    uint64_t value1;
    uint64_t value2;
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
#define DRM_IOCTL_WAIT_VBLANK _IOWR(DRM_IOCTL_BASE, 0x3a, struct drm_wait_vblank)

#define DRM_MODE_PAGE_FLIP_EVENT 0x01
#define DRM_MODE_PAGE_FLIP_ASYNC 0x02
#define DRM_VBLANK_ABSOLUTE (1u << 0)
#define DRM_VBLANK_EVENT (1u << 26)
#define DRM_EVENT_FLIP_COMPLETE 0x02
#define DRM_EVENT_VBLANK 0x01

static int card = -1;

static void fail(const char *step) {
    printf("FLIPPROBE_FAIL %s errno=%d\n", step, errno);
    exit(1);
}

static struct drm_mode_crtc get_crtc(uint32_t crtc_id) {
    struct drm_mode_crtc crtc = {.crtc_id = crtc_id};
    if (ioctl(card, DRM_IOCTL_MODE_GETCRTC, &crtc) != 0) {
        fail("getcrtc");
    }
    return crtc;
}

static uint32_t create_fb(uint32_t width, uint32_t height, uint32_t *pitch,
                          uint32_t *size, uint8_t **map) {
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
    *pitch = dumb.pitch;
    *size = dumb.size;
    *map = mapped;
    return addfb.fb_id;
}

static void fill(uint8_t *map, uint32_t pitch, uint32_t width, uint32_t height,
                 uint32_t color) {
    for (uint32_t y = 0; y < height; y++) {
        uint32_t *line = (uint32_t *)(map + (size_t)y * pitch);
        for (uint32_t x = 0; x < width; x++) {
            line[x] = color;
        }
    }
}

static int poll_readable(void) {
    struct pollfd pfd = {.fd = card, .events = POLLIN};
    return poll(&pfd, 1, 1000);
}

static int read_event(struct drm_event_vblank *event) {
    ssize_t count = read(card, event, sizeof(*event));
    return count == (ssize_t)sizeof(*event) ? 0 : -1;
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
    if (ioctl(card, DRM_IOCTL_MODE_GETRESOURCES, &res) != 0 || res.count_crtcs == 0 ||
        res.count_connectors == 0) {
        fail("getresources");
    }
    uint32_t crtc_id = crtc_ids[0];
    uint32_t connector_id = connector_ids[0];

    struct drm_mode_get_connector connector = {.connector_id = connector_id};
    if (ioctl(card, DRM_IOCTL_MODE_GETCONNECTOR, &connector) != 0) {
        fail("getconnector-count");
    }
    struct drm_mode_modeinfo modes[16];
    memset(&connector, 0, sizeof(connector));
    connector.connector_id = connector_id;
    connector.modes_ptr = (uint64_t)(uintptr_t)modes;
    connector.count_modes = 16;
    if (ioctl(card, DRM_IOCTL_MODE_GETCONNECTOR, &connector) != 0 ||
        connector.count_modes == 0) {
        fail("getconnector-modes");
    }
    struct drm_mode_modeinfo mode = modes[0];

    uint32_t size_a = 0, size_b = 0, pitch_a = 0, pitch_b = 0;
    uint8_t *map_a = NULL, *map_b = NULL;
    uint32_t fb_a = create_fb(mode.hdisplay, mode.vdisplay, &pitch_a, &size_a, &map_a);
    uint32_t fb_b = create_fb(mode.hdisplay, mode.vdisplay, &pitch_b, &size_b, &map_b);
    (void)size_a;
    (void)size_b;
    fill(map_a, pitch_a, mode.hdisplay, mode.vdisplay, 0x00ff0000);
    fill(map_b, pitch_b, mode.hdisplay, mode.vdisplay, 0x000000ff);

    uint32_t connectors[1] = {connector_id};
    struct drm_mode_crtc setcrtc = {
        .set_connectors_ptr = (uint64_t)(uintptr_t)connectors,
        .count_connectors = 1,
        .crtc_id = crtc_id,
        .fb_id = fb_a,
        .mode_valid = 1,
        .mode = mode,
    };
    if (ioctl(card, DRM_IOCTL_MODE_SETCRTC, &setcrtc) != 0) {
        fail("setcrtc");
    }
    uint32_t sequence_before = 0;
    struct drm_wait_vblank vblank = {
        .type = DRM_VBLANK_ABSOLUTE,
        .sequence = 0,
    };
    if (ioctl(card, DRM_IOCTL_WAIT_VBLANK, &vblank) != 0) {
        fail("wait-vblank-initial");
    }
    sequence_before = vblank.sequence;
    printf("FLIPPROBE_VBLANK sequence=%u\n", sequence_before);

    // A relative wait for one scanout completes from the 60 Hz software tick:
    // no flip is issued between the two calls.
    struct timespec start, end;
    clock_gettime(CLOCK_MONOTONIC, &start);
    struct drm_wait_vblank relative = {
        .type = 0,
        .sequence = 1,
    };
    if (ioctl(card, DRM_IOCTL_WAIT_VBLANK, &relative) != 0) {
        fail("wait-vblank-relative");
    }
    clock_gettime(CLOCK_MONOTONIC, &end);
    double elapsed_ms = (double)(end.tv_sec - start.tv_sec) * 1000.0 +
                        (double)(end.tv_nsec - start.tv_nsec) / 1e6;
    if (elapsed_ms < 5.0 || relative.sequence <= sequence_before) {
        printf("FLIPPROBE_FAIL vblank-relative ms=%.2f sequence=%u\n",
               elapsed_ms, relative.sequence);
        return 1;
    }
    printf("FLIPPROBE_VBLANK_RELATIVE ms=%.2f sequence=%u\n", elapsed_ms,
           relative.sequence);
    sequence_before = relative.sequence;

    // Flip with an event and read the completion. A page flip is asynchronous:
    // the ioctl queues the request and returns before the scanout, and the
    // completion event only appears after the next vblank. The relative wait
    // above returned at a tick, so the next one is a full period away.
    // A slow scheduler can lose the race against the next tick, so the
    // "not yet delivered" check is retried: a synchronous implementation
    // fails every attempt, a deferred one only loses an attempt when the
    // tick lands between the ioctl and the poll.
    int deferred = 0;
    double flip_ms = 0.0;
    for (int attempt = 0; attempt < 5 && !deferred; attempt++) {
        struct drm_wait_vblank sync_wait = {.type = 0, .sequence = 1};
        if (ioctl(card, DRM_IOCTL_WAIT_VBLANK, &sync_wait) != 0) {
            fail("wait-before-flip");
        }
        clock_gettime(CLOCK_MONOTONIC, &start);
        if (page_flip(crtc_id, fb_b, DRM_MODE_PAGE_FLIP_EVENT, 0xabcdu) != 0) {
            fail("page-flip-event");
        }
        clock_gettime(CLOCK_MONOTONIC, &end);
        flip_ms = (double)(end.tv_sec - start.tv_sec) * 1000.0 +
                  (double)(end.tv_nsec - start.tv_nsec) / 1e6;
        struct pollfd immediate = {.fd = card, .events = POLLIN};
        if (poll(&immediate, 1, 0) == 0) {
            deferred = 1;
            break;
        }
        struct drm_event_vblank raced;
        if (read_event(&raced) != 0) {
            fail("read-raced-event");
        }
    }
    if (!deferred) {
        printf("FLIPPROBE_FAIL event-delivered-synchronously\n");
        return 1;
    }
    printf("FLIPPROBE_FLIP_DEFERRED ioctl_ms=%.2f\n", flip_ms);
    if (poll_readable() <= 0) {
        fail("poll-flip-event");
    }
    struct drm_event_vblank event;
    if (read_event(&event) != 0) {
        fail("read-flip-event");
    }
    if (event.base.type != DRM_EVENT_FLIP_COMPLETE ||
        event.base.length != sizeof(event) || event.user_data != 0xabcdu ||
        event.crtc_id != crtc_id || event.sequence <= sequence_before) {
        printf("FLIPPROBE_FAIL flip-event type=%u len=%u user=0x%llx seq=%u\n",
               event.base.type, event.base.length,
               (unsigned long long)event.user_data, event.sequence);
        return 1;
    }
    printf("FLIPPROBE_FLIP_EVENT sequence=%u\n", event.sequence);
    uint32_t sequence_after = event.sequence;
    if (get_crtc(crtc_id).fb_id != fb_b) {
        fail("flip-state");
    }
    printf("FLIPPROBE_FLIP_STATE ok\n");

    // An ASYNC flip without an event comes back to fb_a. It is applied on a
    // later scanout, so poll GETCRTC until the worker publishes it.
    if (page_flip(crtc_id, fb_a, DRM_MODE_PAGE_FLIP_ASYNC, 0) != 0) {
        fail("page-flip-async");
    }
    int applied = 0;
    for (int attempt = 0; attempt < 200; attempt++) {
        if (get_crtc(crtc_id).fb_id == fb_a) {
            applied = 1;
            break;
        }
        usleep(1000);
    }
    if (!applied) {
        fail("flip-async-state");
    }
    printf("FLIPPROBE_FLIP_ASYNC ok\n");

    // The sequence must have advanced and an absolute wait for it returns now.
    vblank.type = DRM_VBLANK_ABSOLUTE;
    vblank.sequence = sequence_after;
    if (ioctl(card, DRM_IOCTL_WAIT_VBLANK, &vblank) != 0 ||
        vblank.sequence < sequence_after) {
        fail("wait-vblank-absolute");
    }
    printf("FLIPPROBE_VBLANK_WAIT sequence=%u\n", vblank.sequence);

    // Request a vblank event: it is delivered through the same event queue.
    struct drm_wait_vblank event_request = {
        .type = DRM_VBLANK_EVENT,
        .sequence = 1,
        .value1 = 0x77u,
    };
    if (ioctl(card, DRM_IOCTL_WAIT_VBLANK, &event_request) != 0) {
        fail("wait-vblank-event");
    }
    if (poll_readable() <= 0) {
        fail("poll-vblank-event");
    }
    if (read_event(&event) != 0) {
        fail("read-vblank-event");
    }
    if (event.base.type != DRM_EVENT_VBLANK ||
        event.base.length != sizeof(event) || event.user_data != 0x77u) {
        printf("FLIPPROBE_FAIL vblank-event type=%u user=0x%llx\n",
               event.base.type, (unsigned long long)event.user_data);
        return 1;
    }
    printf("FLIPPROBE_VBLANK_EVENT ok\n");

    // Negative cases.
    errno = 0;
    if (page_flip(crtc_id, fb_a, 0x80, 0) == 0 || errno != EINVAL) {
        fail("flip-unknown-flags");
    }
    errno = 0;
    if (page_flip(crtc_id, 0xffff, DRM_MODE_PAGE_FLIP_EVENT, 0) == 0 ||
        errno != ENOENT) {
        fail("flip-missing-fb");
    }
    printf("FLIPPROBE_ERRORS ok\n");

    printf("FLIPPROBE_PASS\n");

    if (getpid() == 1) {
        sleep(3600);
    }
    return 0;
}
