// SPDX-License-Identifier: MPL-2.0
//
// DRM-M2 smoke test: drive the virtio-gpu DRM node through the *standard* KMS
// ioctl path (no private/out-of-band hooks) to draw a frame entirely from user
// space. Runs as pid 1 on Asterinas RISC-V.
//
// Flow: open /dev/dri/card0 -> GET_CAP -> SET_CLIENT_CAP -> MODE_GETRESOURCES
// -> MODE_GETCONNECTOR (fetch the native mode) -> MODE_GETENCODER ->
// MODE_CREATE_DUMB -> MODE_MAP_DUMB -> mmap -> draw -> MODE_ADDFB ->
// MODE_SETCRTC. A second pass draws at a *different* resolution to exercise
// MODESET (multi-resolution mode switching).
//
// The host verifies each phase independently by taking a QEMU `screendump`:
// phase 1 must show a green->blue gradient, phase 2 a solid red frame.

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
 * uapi macros expand to 0; these are the correct _IOWR/_IOW encodings). */
#define DRM_IOCTL_GET_CAP            0xc010640cULL
#define DRM_IOCTL_SET_CLIENT_CAP     0x4010640dULL
#define DRM_IOCTL_MODE_GETRESOURCES  0xc04064a0ULL
#define DRM_IOCTL_MODE_GETCONNECTOR  0xc05064a7ULL
#define DRM_IOCTL_MODE_GETENCODER    0xc01464a6ULL
#define DRM_IOCTL_MODE_CREATE_DUMB   0xc02064b2ULL
#define DRM_IOCTL_MODE_MAP_DUMB      0xc01064b3ULL
#define DRM_IOCTL_MODE_ADDFB         0xc01c64aeULL
#define DRM_IOCTL_MODE_SETCRTC       0xc06864a2ULL

#define DRM_CAP_DUMB_BUFFER          1ULL
#define DRM_CLIENT_CAP_UNIVERSAL_PLANES 2ULL

#define DRM_MODE_CONNECTOR_VIRTUAL   15

/* ---- uapi structs (riscv64: pointers/size_t are 8 bytes) ---- */

struct drm_get_cap {
    uint64_t capability;
    uint64_t value;
};

struct drm_set_client_cap {
    uint64_t capability;
    uint64_t value;
};

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

struct drm_mode_get_encoder {
    uint32_t encoder_id, encoder_type, crtc_id, possible_crtcs, possible_clones;
};

struct drm_mode_get_connector {
    uint64_t encoders_ptr, modes_ptr, props_ptr, prop_values_ptr;
    uint32_t count_modes, count_props, count_encoders;
    uint32_t encoder_id, connector_id, connector_type, connector_type_id;
    uint32_t connection, mm_width, mm_height, subpixel, pad;
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

struct drm_mode_fb_cmd {
    uint32_t fb_id, width, height, pitch, bpp, depth, handle;
};

/* ---- helpers ---- */

static int failures = 0;

static void ok(const char *name) {
    printf("[DRM] %s: OK  __DRM_%s_OK__\n", name, name);
}

static void fail(const char *name, const char *msg) {
    failures++;
    printf("[DRM] %s: FAIL (%s) __DRM_%s_FAIL__\n", name, name, msg);
}

/* Busy-wait so the host can screendump before the next phase redraws. */
static void pause_ms(int ms) {
    for (volatile int i = 0; i < ms * 200000; i++) {
    }
}

static struct drm_mode_modeinfo make_mode(uint32_t w, uint32_t h) {
    struct drm_mode_modeinfo m;
    memset(&m, 0, sizeof(m));
    m.clock = (uint32_t)(w * h * 60 / 1000);
    m.hdisplay = (uint16_t)w;
    m.hsync_start = (uint16_t)(w + 16);
    m.hsync_end = (uint16_t)(w + 32);
    m.htotal = (uint16_t)(w + 48);
    m.vdisplay = (uint16_t)h;
    m.vsync_start = (uint16_t)(h + 1);
    m.vsync_end = (uint16_t)(h + 2);
    m.vtotal = (uint16_t)(h + 4);
    m.vrefresh = 60;
    m.type = 8; /* DRM_MODE_TYPE_PREFERRED */
    snprintf(m.name, sizeof(m.name), "%ux%u", w, h);
    return m;
}

/* Draws a horizontal green->blue gradient into a B8G8R8X8 buffer. */
static void draw_gradient(uint8_t *map, uint32_t w, uint32_t h, uint32_t pitch) {
    for (uint32_t y = 0; y < h; y++) {
        for (uint32_t x = 0; x < w; x++) {
            uint32_t t = x * 255 / w;
            uint8_t *px = map + (size_t)y * pitch + (size_t)x * 4;
            px[0] = (uint8_t)t;        /* B */
            px[1] = (uint8_t)(255 - t); /* G */
            px[2] = 0;                  /* R */
            px[3] = 0;                  /* X */
        }
    }
}

/* Fills a B8G8R8X8 buffer with a solid red frame. */
static void fill_red(uint8_t *map, uint32_t w, uint32_t h, uint32_t pitch) {
    for (uint32_t y = 0; y < h; y++) {
        for (uint32_t x = 0; x < w; x++) {
            uint8_t *px = map + (size_t)y * pitch + (size_t)x * 4;
            px[0] = 0;    /* B */
            px[1] = 0;    /* G */
            px[2] = 255;  /* R */
            px[3] = 0;    /* X */
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

    /* GET_CAP: dumb buffers must be advertised. */
    struct drm_get_cap cap = { .capability = DRM_CAP_DUMB_BUFFER, .value = 0 };
    if (ioctl(fd, DRM_IOCTL_GET_CAP, &cap) == 0 && cap.value == 1) {
        ok("get_cap");
    } else {
        fail("get_cap", "DRM_IOCTL_GET_CAP(DUMB_BUFFER) failed");
    }

    /* SET_CLIENT_CAP: a modesetting client enables universal planes. */
    struct drm_set_client_cap ccap = {
        .capability = DRM_CLIENT_CAP_UNIVERSAL_PLANES, .value = 1 };
    if (ioctl(fd, DRM_IOCTL_SET_CLIENT_CAP, &ccap) == 0) {
        ok("set_client_cap");
    } else {
        fail("set_client_cap", "DRM_IOCTL_SET_CLIENT_CAP failed");
    }

    /* MODE_GETRESOURCES. */
    uint32_t crtc_id = 0, connector_id = 0, encoder_id = 0;
    struct drm_mode_card_res res = {0};
    res.crtc_id_ptr = (uint64_t)&crtc_id;
    res.connector_id_ptr = (uint64_t)&connector_id;
    res.encoder_id_ptr = (uint64_t)&encoder_id;
    res.count_crtcs = 1;
    res.count_connectors = 1;
    res.count_encoders = 1;
    if (ioctl(fd, DRM_IOCTL_MODE_GETRESOURCES, &res) == 0
        && res.count_crtcs == 1 && res.count_connectors == 1 && res.count_encoders == 1
        && crtc_id != 0 && connector_id != 0 && encoder_id != 0) {
        printf("[DRM] resources: crtc=%u connector=%u encoder=%u\n",
               crtc_id, connector_id, encoder_id);
        ok("get_resources");
    } else {
        fail("get_resources", "DRM_IOCTL_MODE_GETRESOURCES failed");
        printf("__DRM_DONE__ __DRM_FAIL__\n");
        return 1;
    }

    /* MODE_GETCONNECTOR: fetch the native mode. */
    struct drm_mode_modeinfo native_mode;
    uint32_t encoder_ids[1];
    struct drm_mode_get_connector conn = {0};
    conn.connector_id = connector_id;
    conn.modes_ptr = (uint64_t)&native_mode;
    conn.count_modes = 1;
    conn.encoders_ptr = (uint64_t)encoder_ids;
    conn.count_encoders = 1;
    if (ioctl(fd, DRM_IOCTL_MODE_GETCONNECTOR, &conn) == 0
        && conn.count_modes >= 1 && conn.connection == 1 /* DRM_MODE_CONNECTED */
        && native_mode.hdisplay != 0 && native_mode.vdisplay != 0) {
        printf("[DRM] connector: mode %ux%u type=%u\n",
               native_mode.hdisplay, native_mode.vdisplay,
               (unsigned)conn.connector_type);
        ok("get_connector");
    } else {
        fail("get_connector", "DRM_IOCTL_MODE_GETCONNECTOR failed");
    }

    /* MODE_GETENCODER. */
    struct drm_mode_get_encoder enc = { .encoder_id = encoder_id };
    if (ioctl(fd, DRM_IOCTL_MODE_GETENCODER, &enc) == 0 && enc.crtc_id == crtc_id) {
        ok("get_encoder");
    } else {
        fail("get_encoder", "DRM_IOCTL_MODE_GETENCODER failed");
    }

    uint32_t w = native_mode.hdisplay;
    uint32_t h = native_mode.vdisplay;

    /* ---- Phase 1: full-res green->blue gradient ---- */

    struct drm_mode_create_dumb dumb = { .height = h, .width = w, .bpp = 32, .flags = 0 };
    if (ioctl(fd, DRM_IOCTL_MODE_CREATE_DUMB, &dumb) != 0 || dumb.handle == 0) {
        fail("create_dumb1", "DRM_IOCTL_MODE_CREATE_DUMB failed");
        printf("__DRM_DONE__ __DRM_FAIL__\n");
        return 1;
    }
    ok("create_dumb1");

    struct drm_mode_map_dumb map = { .handle = dumb.handle, .pad = 0 };
    if (ioctl(fd, DRM_IOCTL_MODE_MAP_DUMB, &map) != 0) {
        fail("map_dumb1", "DRM_IOCTL_MODE_MAP_DUMB failed");
        printf("__DRM_DONE__ __DRM_FAIL__\n");
        return 1;
    }
    ok("map_dumb1");

    uint8_t *fb = mmap(NULL, dumb.size, PROT_READ | PROT_WRITE, MAP_SHARED, fd,
                       (off_t)map.offset);
    if (fb == MAP_FAILED) {
        fail("mmap1", "mmap failed");
        printf("[DRM] errno=%d\n", errno);
        printf("__DRM_DONE__ __DRM_FAIL__\n");
        return 1;
    }
    ok("mmap1");

    draw_gradient(fb, w, h, dumb.pitch);
    ok("draw1");

    struct drm_mode_fb_cmd fbc = {
        .width = w, .height = h, .pitch = dumb.pitch, .bpp = 32, .depth = 24,
        .handle = dumb.handle,
    };
    if (ioctl(fd, DRM_IOCTL_MODE_ADDFB, &fbc) != 0 || fbc.fb_id == 0) {
        fail("addfb1", "DRM_IOCTL_MODE_ADDFB failed");
        printf("__DRM_DONE__ __DRM_FAIL__\n");
        return 1;
    }
    ok("addfb1");

    struct drm_mode_crtc crtc = {0};
    crtc.crtc_id = crtc_id;
    crtc.fb_id = fbc.fb_id;
    crtc.count_connectors = 0;
    crtc.mode = make_mode(w, h);
    crtc.mode_valid = 1;
    if (ioctl(fd, DRM_IOCTL_MODE_SETCRTC, &crtc) != 0) {
        fail("setcrtc1", "DRM_IOCTL_MODE_SETCRTC failed");
        printf("[DRM] errno=%d\n", errno);
        printf("__DRM_DONE__ __DRM_FAIL__\n");
        return 1;
    }
    ok("setcrtc1");

    printf("__DRM_PHASE1_OK__\n");
    fflush(stdout);
    pause_ms(3000);

    /* ---- Phase 2: half-res solid red (multi-resolution MODESET) ---- */

    uint32_t w2 = w / 2;
    uint32_t h2 = h / 2;
    struct drm_mode_create_dumb dumb2 = { .height = h2, .width = w2, .bpp = 32, .flags = 0 };
    if (ioctl(fd, DRM_IOCTL_MODE_CREATE_DUMB, &dumb2) != 0 || dumb2.handle == 0) {
        fail("create_dumb2", "DRM_IOCTL_MODE_CREATE_DUMB failed");
        printf("__DRM_DONE__ __DRM_FAIL__\n");
        return 1;
    }

    struct drm_mode_map_dumb map2 = { .handle = dumb2.handle, .pad = 0 };
    if (ioctl(fd, DRM_IOCTL_MODE_MAP_DUMB, &map2) != 0) {
        fail("map_dumb2", "DRM_IOCTL_MODE_MAP_DUMB failed");
        printf("__DRM_DONE__ __DRM_FAIL__\n");
        return 1;
    }

    uint8_t *fb2 = mmap(NULL, dumb2.size, PROT_READ | PROT_WRITE, MAP_SHARED, fd,
                        (off_t)map2.offset);
    if (fb2 == MAP_FAILED) {
        fail("mmap2", "mmap failed");
        printf("__DRM_DONE__ __DRM_FAIL__\n");
        return 1;
    }
    fill_red(fb2, w2, h2, dumb2.pitch);

    struct drm_mode_fb_cmd fbc2 = {
        .width = w2, .height = h2, .pitch = dumb2.pitch, .bpp = 32, .depth = 24,
        .handle = dumb2.handle,
    };
    if (ioctl(fd, DRM_IOCTL_MODE_ADDFB, &fbc2) != 0 || fbc2.fb_id == 0) {
        fail("addfb2", "DRM_IOCTL_MODE_ADDFB failed");
        printf("__DRM_DONE__ __DRM_FAIL__\n");
        return 1;
    }

    struct drm_mode_crtc crtc2 = {0};
    crtc2.crtc_id = crtc_id;
    crtc2.fb_id = fbc2.fb_id;
    crtc2.mode = make_mode(w2, h2);
    crtc2.mode_valid = 1;
    if (ioctl(fd, DRM_IOCTL_MODE_SETCRTC, &crtc2) != 0) {
        fail("setcrtc2", "DRM_IOCTL_MODE_SETCRTC failed");
        printf("[DRM] errno=%d\n", errno);
        printf("__DRM_DONE__ __DRM_FAIL__\n");
        return 1;
    }
    ok("modeset2");

    printf("__DRM_PHASE2_OK__\n");
    fflush(stdout);
    pause_ms(3000);

    close(fd);
    printf("__DRM_DONE__ %s\n", failures ? "__DRM_FAIL__" : "__DRM_PASS__");
    return failures ? 1 : 0;
}
