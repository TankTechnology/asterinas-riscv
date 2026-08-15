// SPDX-License-Identifier: MPL-2.0
//
// DRM-M14 KMS mode-switch smoke test: drives the `DRM_IOCTL_MODE_SETCRTC` path
// on /dev/dri/card0 to switch the virtio-gpu scanout between two *different*
// resolutions and back, verifying that
//
//   1. `SETCRTC` with a differently-sized framebuffer succeeds (the kernel's
//      `set_crtc` presents the new fb, which re-runs the virtio-gpu
//      RESOURCE_CREATE_2D / ATTACH_BACKING / SET_SCANOUT / TRANSFER_TO_HOST_2D /
//      FLUSH pipeline at the new dimensions);
//   2. `GETCRTC` reflects the new framebuffer id + mode (the switch actually
//      took effect at the KMS layer);
//   3. switching *back* to the original resolution leaves the CRTC usable.
//
// The Xorg modesetting driver exercises `SETCRTC` at startup but always sets the
// device's single preferred mode; this test is the first to switch between two
// *different* modes on the same CRTC. It runs as a bare pid-1 /init (no Xorg),
// so it owns /dev/dri/card0 uncontended.
//
// Results are written to both stdout and /dev/ttyS0 so a boot harness can read
// the `__MODESWITCH_*__` markers from the serial log.

#include <drm/drm.h>
#include <drm/drm_mode.h>

#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>

#define DEV "/dev/dri/card0"

static int failures = 0;
static FILE *ser;

static void log_line(const char *line) {
    fputs(line, stdout);
    fputc('\n', stdout);
    fflush(stdout);
    if (ser) {
        fputs(line, ser);
        fputc('\n', ser);
        fflush(ser);
    }
}

static void ok(const char *name, const char *detail) {
    char buf[256];
    snprintf(buf, sizeof buf, "[MODESWITCH] %-16s OK   %s", name, detail);
    log_line(buf);
}

static void fail(const char *name, const char *detail) {
    failures++;
    char buf[256];
    snprintf(buf, sizeof buf, "[MODESWITCH] %-16s FAIL %s (errno=%d)",
             name, detail, errno);
    log_line(buf);
}

/* Round a dimension down to a multiple of 8 (mode timings like that). */
static unsigned snap8(unsigned v) {
    unsigned r = v & ~7u;
    return r < 64 ? 64 : r;
}

/* Build a preferred-mode `drm_mode_modeinfo` for width x height @60Hz. */
static void fill_mode(struct drm_mode_modeinfo *m, uint16_t w, uint16_t h) {
    memset(m, 0, sizeof *m);
    char name[32];
    snprintf(name, sizeof name, "%ux%u", w, h);
    memcpy(m->name, name, strlen(name) + 1);
    m->clock = (uint32_t)w * h * 60 / 1000;
    m->hdisplay = w;
    m->hsync_start = w + 16;
    m->hsync_end = w + 32;
    m->htotal = w + 48;
    m->vdisplay = h;
    m->vsync_start = h + 1;
    m->vsync_end = h + 2;
    m->vtotal = h + 4;
    m->vrefresh = 60;
    m->type = DRM_MODE_TYPE_PREFERRED;
}

/* Create a dumb buffer, map it, fill it with a solid colour, and add an fb. */
static int make_fb(int fd, uint32_t w, uint32_t h, uint32_t *fb_id_out) {
    struct drm_mode_create_dumb create = {0};
    create.width = w;
    create.height = h;
    create.bpp = 32;
    if (ioctl(fd, DRM_IOCTL_MODE_CREATE_DUMB, &create) != 0) {
        fail("create_dumb", "CREATE_DUMB failed");
        return -1;
    }

    struct drm_mode_map_dumb map = {.handle = create.handle};
    if (ioctl(fd, DRM_IOCTL_MODE_MAP_DUMB, &map) != 0) {
        fail("map_dumb", "MAP_DUMB failed");
        return -1;
    }

    /* Fill via mmap so the switch is visible and the mmap path is exercised.
     * Non-fatal: a zeroed buffer still presents correctly. */
    void *mapped = mmap(NULL, create.size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, map.offset);
    if (mapped != MAP_FAILED) {
        uint32_t *px = mapped;
        size_t n = create.size / 4;
        uint32_t colour = 0xff0000ffu; /* red in X8R8G8B8 */
        for (size_t i = 0; i < n; i++)
            px[i] = colour;
        munmap(mapped, create.size);
    }

    struct drm_mode_fb_cmd fb = {0};
    fb.width = w;
    fb.height = h;
    fb.pitch = create.pitch;
    fb.bpp = 32;
    fb.depth = 24;
    fb.handle = create.handle;
    if (ioctl(fd, DRM_IOCTL_MODE_ADDFB, &fb) != 0) {
        fail("addfb", "ADDFB failed");
        return -1;
    }

    *fb_id_out = fb.fb_id;
    return 0;
}

static int do_setcrtc(int fd, uint32_t crtc_id, uint32_t fb_id, uint16_t w, uint16_t h) {
    struct drm_mode_crtc crtc = {0};
    crtc.crtc_id = crtc_id;
    crtc.fb_id = fb_id;
    crtc.x = 0;
    crtc.y = 0;
    crtc.gamma_size = 0;
    crtc.mode_valid = 1;
    fill_mode(&crtc.mode, w, h);
    if (ioctl(fd, DRM_IOCTL_MODE_SETCRTC, &crtc) != 0) {
        fail("setcrtc", "SETCRTC failed");
        return -1;
    }
    return 0;
}

static int check_crtc(int fd, uint32_t crtc_id, uint32_t expect_fb, uint16_t expect_w,
                      uint16_t expect_h, const char *tag) {
    struct drm_mode_crtc crtc = {.crtc_id = crtc_id};
    if (ioctl(fd, DRM_IOCTL_MODE_GETCRTC, &crtc) != 0) {
        fail("getcrtc", "GETCRTC failed");
        return -1;
    }
    char detail[128];
    snprintf(detail, sizeof detail,
             "fb_id=%u mode=%ux%u%s", crtc.fb_id, crtc.mode.hdisplay, crtc.mode.vdisplay,
             crtc.mode_valid ? "" : " (mode_valid=0)");
    if (crtc.fb_id == expect_fb && crtc.mode.hdisplay == expect_w &&
        crtc.mode.vdisplay == expect_h && crtc.mode_valid) {
        ok(tag, detail);
        return 0;
    }
    char msg[160];
    snprintf(msg, sizeof msg,
             "expected fb_id=%u %ux%u, got %ux%u", expect_fb, expect_w, expect_h,
             crtc.mode.hdisplay, crtc.mode.vdisplay);
    fail(tag, msg);
    return -1;
}

int main(void) {
    ser = fopen("/dev/ttyS0", "w");

    int fd = open(DEV, O_RDWR);
    if (fd < 0) {
        fail("open", "cannot open " DEV);
        goto done;
    }
    ok("open", DEV);

    /* --- enumerate resources / connector -> current mode ------------------ */
    struct drm_mode_card_res res = {0};
    if (ioctl(fd, DRM_IOCTL_MODE_GETRESOURCES, &res) != 0) {
        fail("getresources", "GETRESOURCES failed");
        goto close_fd;
    }
    uint32_t crtc_id = 0;
    memset(&res, 0, sizeof res);
    if (ioctl(fd, DRM_IOCTL_MODE_GETRESOURCES, &res) != 0 || res.count_crtcs < 1) {
        fail("getresources", "GETRESOURCES failed / no CRTC");
        goto close_fd;
    }
    res.crtc_id_ptr = (uint64_t)(uintptr_t)&crtc_id;
    if (ioctl(fd, DRM_IOCTL_MODE_GETRESOURCES, &res) != 0 || crtc_id == 0) {
        fail("getresources", "cannot obtain CRTC id");
        goto close_fd;
    }
    {
        char buf[64];
        snprintf(buf, sizeof buf, "crtc_id=%u (count_crtcs=%u)", crtc_id, res.count_crtcs);
        ok("getresources", buf);
    }

    struct drm_mode_modeinfo cur_mode = {0};
    struct drm_mode_get_connector conn = {0};
    conn.connector_id = 1;
    conn.modes_ptr = (uint64_t)(uintptr_t)&cur_mode;
    conn.count_modes = 1;
    if (ioctl(fd, DRM_IOCTL_MODE_GETCONNECTOR, &conn) != 0) {
        fail("getconnector", "GETCONNECTOR failed");
        goto close_fd;
    }
    uint16_t cur_w = cur_mode.hdisplay;
    uint16_t cur_h = cur_mode.vdisplay;
    if (cur_w == 0 || cur_h == 0) {
        /* fall back to the reported connected mode */
        cur_w = conn.count_modes ? cur_mode.hdisplay : 1024;
        cur_h = conn.count_modes ? cur_mode.vdisplay : 768;
    }
    {
        char buf[64];
        snprintf(buf, sizeof buf, "current mode %ux%u", cur_w, cur_h);
        ok("getconnector", buf);
    }

    /* --- switch to a different (smaller) resolution ----------------------- */
    uint16_t tgt_w = (uint16_t)snap8(cur_w / 2);
    uint16_t tgt_h = (uint16_t)snap8(cur_h / 2);
    {
        char buf[64];
        snprintf(buf, sizeof buf, "target mode %ux%u", tgt_w, tgt_h);
        ok("target", buf);
    }

    uint32_t fb_tgt = 0, fb_orig = 0;
    if (make_fb(fd, tgt_w, tgt_h, &fb_tgt) != 0)
        goto close_fd;
    {
        char buf[64];
        snprintf(buf, sizeof buf, "fb_id=%u (%ux%u)", fb_tgt, tgt_w, tgt_h);
        ok("addfb-target", buf);
    }

    if (do_setcrtc(fd, crtc_id, fb_tgt, tgt_w, tgt_h) != 0)
        goto close_fd;
    ok("setcrtc-target", "switched to smaller mode");

    if (check_crtc(fd, crtc_id, fb_tgt, tgt_w, tgt_h, "verify-target") != 0)
        goto close_fd;

    /* --- switch back to the original resolution --------------------------- */
    if (make_fb(fd, cur_w, cur_h, &fb_orig) != 0)
        goto close_fd;
    {
        char buf[64];
        snprintf(buf, sizeof buf, "fb_id=%u (%ux%u)", fb_orig, cur_w, cur_h);
        ok("addfb-orig", buf);
    }

    if (do_setcrtc(fd, crtc_id, fb_orig, cur_w, cur_h) != 0)
        goto close_fd;
    ok("setcrtc-orig", "switched back to original mode");

    if (check_crtc(fd, crtc_id, fb_orig, cur_w, cur_h, "verify-orig") != 0)
        goto close_fd;

close_fd:
    close(fd);
done:
    {
        char buf[64];
        snprintf(buf, sizeof buf, "__MODESWITCH_DONE__ %s",
                 failures ? "__MODESWITCH_FAIL__" : "__MODESWITCH_PASS__");
        log_line(buf);
    }
    if (ser)
        fclose(ser);
    return failures ? 1 : 0;
}
