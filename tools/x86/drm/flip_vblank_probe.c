// SPDX-License-Identifier: MPL-2.0
//
// Raw-ioctl DRM/KMS probe for the page-flip/vblank bring-up on a kernel that
// cannot boot a Debian userland (see
// docs/porting/2026-10-28-drm-203-split-plan.md). It lists the connector's
// modes, presents a dumb buffer with `MODE_PAGE_FLIP` + `PAGE_FLIP_EVENT`,
// reads the `DRM_EVENT_FLIP_COMPLETE` record back, checks that a flip without
// the flag leaves no event, and asks `WAIT_VBLANK` for the sequence.
//
// Usage: build it statically and use it as the `/init` of a minimal
// initramfs; it creates /dev/dri/card0 (major 226, minor 0) itself.
//
//   cc -static -O2 -o init tools/x86/drm/flip_vblank_probe.c
//   (cd <dir with init>; printf '.\ninit\n' | cpio -o -H newc) > probe.cpio
//   qemu-system-x86_64 -enable-kvm -cpu host -smp 2 -m 2G --no-reboot \
//       -display none -vga none -device virtio-gpu-pci \
//       -kernel target/osdk/aster-kernel-osdk-bin.qemu_elf -initrd probe.cpio \
//       -append "console=ttyS0 earlycon init=/init" -serial file:probe.log
//
// Expected tail:
//   DRM_PROBE_MODES count=1 connection=1 first=1280x800 1280x800
//   DRM_PROBE_FLIP_EVENT type=2 len=32 user=0x5eed
//   DRM_PROBE_VBLANK sequence=2 tv=...
//   DRM_PROBE_OK

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <unistd.h>

#define DRM_IO(nr) _IO('d', nr)
#define DRM_IOWR(nr, type) _IOWR('d', nr, type)
#define DRM_IOW(nr, type) _IOW('d', nr, type)

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
    uint64_t encoders_ptr, modes_ptr, props_ptr, prop_values_ptr;
    uint32_t count_modes, count_props, count_encoders;
    uint32_t encoder_id, connector_id, connector_type, connector_type_id;
    uint32_t connection, mm_width, mm_height, subpixel, pad;
};

struct drm_mode_get_encoder {
    uint32_t encoder_id, encoder_type, crtc_id;
    uint32_t possible_crtcs, possible_clones;
};

struct drm_mode_crtc {
    uint64_t set_connectors_ptr;
    uint32_t count_connectors, crtc_id, fb_id, x, y, gamma_size, mode_valid;
    struct drm_mode_modeinfo mode;
};

struct drm_mode_create_dumb {
    uint32_t height, width, bpp, flags, handle, pitch;
    uint64_t size;
};

struct drm_mode_map_dumb {
    uint32_t handle, pad;
    uint64_t offset;
};

struct drm_mode_fb_cmd {
    uint32_t fb_id, width, height, pitch, bpp, depth, handle;
};

struct drm_mode_crtc_page_flip {
    uint32_t crtc_id, fb_id, flags, reserved;
    uint64_t user_data;
};

struct drm_wait_vblank {
    uint32_t type, sequence;
    uint64_t value1, value2;
};

#define DRM_IOCTL_MODE_GETRESOURCES DRM_IOWR(0xA0, struct drm_mode_card_res)
#define DRM_IOCTL_MODE_GETCRTC DRM_IOWR(0xA1, struct drm_mode_crtc)
#define DRM_IOCTL_MODE_GETENCODER DRM_IOWR(0xA6, struct drm_mode_get_encoder)
#define DRM_IOCTL_MODE_GETCONNECTOR DRM_IOWR(0xA7, struct drm_mode_get_connector)
#define DRM_IOCTL_MODE_ADDFB DRM_IOWR(0xAE, struct drm_mode_fb_cmd)
#define DRM_IOCTL_MODE_PAGE_FLIP DRM_IOWR(0xB0, struct drm_mode_crtc_page_flip)
#define DRM_IOCTL_MODE_CREATE_DUMB DRM_IOWR(0xB2, struct drm_mode_create_dumb)
#define DRM_IOCTL_MODE_MAP_DUMB DRM_IOWR(0xB3, struct drm_mode_map_dumb)
#define DRM_IOCTL_WAIT_VBLANK DRM_IOWR(0x3A, struct drm_wait_vblank)

#define PAGE_FLIP_EVENT 0x01
#define DRM_EVENT_FLIP_COMPLETE 0x02

static void die(const char *what)
{
    printf("DRM_PROBE_FAIL %s errno=%d (%s)\n", what, errno, strerror(errno));
    fflush(stdout);
    for (;;)
        sleep(3600);
}

int main(void)
{
    printf("DRM_PROBE_START\n");
    fflush(stdout);

    mkdir("/dev", 0755);
    mkdir("/dev/dri", 0755);
    unlink("/dev/dri/card0");
    if (mknod("/dev/dri/card0", S_IFCHR | 0600, makedev(226, 0)) != 0 && errno != EEXIST)
        die("mknod");

    int fd = open("/dev/dri/card0", O_RDWR);
    if (fd < 0)
        die("open");

    struct drm_mode_card_res res;
    memset(&res, 0, sizeof(res));
    if (ioctl(fd, DRM_IOCTL_MODE_GETRESOURCES, &res) != 0)
        die("GETRESOURCES count");
    printf("DRM_PROBE_RES connectors=%u crtcs=%u encoders=%u fbs=%u\n",
           res.count_connectors, res.count_crtcs, res.count_encoders, res.count_fbs);

    uint32_t connector_ids[8] = {0};
    uint32_t crtc_ids[8] = {0};
    uint32_t encoder_ids[8] = {0};
    memset(&res, 0, sizeof(res));
    res.connector_id_ptr = (uint64_t)(uintptr_t)connector_ids;
    res.crtc_id_ptr = (uint64_t)(uintptr_t)crtc_ids;
    res.encoder_id_ptr = (uint64_t)(uintptr_t)encoder_ids;
    res.count_connectors = 8;
    res.count_crtcs = 8;
    res.count_encoders = 8;
    if (ioctl(fd, DRM_IOCTL_MODE_GETRESOURCES, &res) != 0)
        die("GETRESOURCES ids");
    if (res.count_connectors == 0 || res.count_crtcs == 0)
        die("no connector/crtc");

    struct drm_mode_modeinfo modes[32];
    memset(modes, 0, sizeof(modes));
    struct drm_mode_get_connector conn;
    memset(&conn, 0, sizeof(conn));
    conn.connector_id = connector_ids[0];
    if (ioctl(fd, DRM_IOCTL_MODE_GETCONNECTOR, &conn) != 0)
        die("GETCONNECTOR count");
    conn.modes_ptr = (uint64_t)(uintptr_t)modes;
    conn.count_modes = 32;
    if (ioctl(fd, DRM_IOCTL_MODE_GETCONNECTOR, &conn) != 0)
        die("GETCONNECTOR modes");
    printf("DRM_PROBE_MODES count=%u connection=%u first=%s %ux%u\n",
           conn.count_modes, conn.connection, modes[0].name,
           modes[0].hdisplay, modes[0].vdisplay);

    struct drm_mode_crtc crtc;
    memset(&crtc, 0, sizeof(crtc));
    crtc.crtc_id = crtc_ids[0];
    if (ioctl(fd, DRM_IOCTL_MODE_GETCRTC, &crtc) != 0)
        die("GETCRTC");
    printf("DRM_PROBE_CRTC id=%u fb=%u mode_valid=%u %ux%u\n", crtc.crtc_id,
           crtc.fb_id, crtc.mode_valid, crtc.mode.hdisplay, crtc.mode.vdisplay);

    struct drm_mode_create_dumb dumb;
    memset(&dumb, 0, sizeof(dumb));
    dumb.width = 640;
    dumb.height = 480;
    dumb.bpp = 32;
    if (ioctl(fd, DRM_IOCTL_MODE_CREATE_DUMB, &dumb) != 0)
        die("CREATE_DUMB");

    struct drm_mode_fb_cmd fb;
    memset(&fb, 0, sizeof(fb));
    fb.width = dumb.width;
    fb.height = dumb.height;
    fb.pitch = dumb.pitch;
    fb.bpp = 32;
    fb.depth = 24;
    fb.handle = dumb.handle;
    if (ioctl(fd, DRM_IOCTL_MODE_ADDFB, &fb) != 0)
        die("ADDFB");

    struct drm_mode_map_dumb map;
    memset(&map, 0, sizeof(map));
    map.handle = dumb.handle;
    if (ioctl(fd, DRM_IOCTL_MODE_MAP_DUMB, &map) != 0)
        die("MAP_DUMB");
    void *pixels = mmap(NULL, dumb.size, PROT_READ | PROT_WRITE, MAP_SHARED, fd,
                        (off_t)map.offset);
    if (pixels == MAP_FAILED)
        die("mmap");
    memset(pixels, 0x40, dumb.size);
    printf("DRM_PROBE_FB fb=%u handle=%u size=%llu pitch=%u\n", fb.fb_id,
           dumb.handle, (unsigned long long)dumb.size, dumb.pitch);

    struct drm_mode_crtc_page_flip flip;
    memset(&flip, 0, sizeof(flip));
    flip.crtc_id = crtc_ids[0];
    flip.fb_id = fb.fb_id;
    flip.flags = PAGE_FLIP_EVENT;
    flip.user_data = 0x5eed;
    if (ioctl(fd, DRM_IOCTL_MODE_PAGE_FLIP, &flip) != 0)
        die("PAGE_FLIP");

    unsigned char event[64];
    ssize_t n = read(fd, event, sizeof(event));
    if (n < 32)
        die("read flip event");
    uint32_t etype, elen;
    uint64_t euser;
    memcpy(&etype, event + 0, 4);
    memcpy(&elen, event + 4, 4);
    memcpy(&euser, event + 8, 8);
    if (etype != DRM_EVENT_FLIP_COMPLETE || elen != 32 || euser != 0x5eed)
        die("flip event contents");
    printf("DRM_PROBE_FLIP_EVENT type=%u len=%u user=0x%llx\n", etype, elen,
           (unsigned long long)euser);

    // A flip without EVENT must not leave a second record behind.
    flip.flags = 0;
    if (ioctl(fd, DRM_IOCTL_MODE_PAGE_FLIP, &flip) != 0)
        die("PAGE_FLIP no-event");
    if (read(fd, event, sizeof(event)) >= 0 || errno != EAGAIN)
        die("unexpected event/EAGAIN");

    struct drm_wait_vblank vblank;
    memset(&vblank, 0, sizeof(vblank));
    vblank.type = 0; // relative, 0 = just report
    if (ioctl(fd, DRM_IOCTL_WAIT_VBLANK, &vblank) != 0)
        die("WAIT_VBLANK");
    printf("DRM_PROBE_VBLANK sequence=%u tv=%llu.%06llu\n", vblank.sequence,
           (unsigned long long)vblank.value1, (unsigned long long)vblank.value2);
    if (vblank.sequence < 2)
        die("vblank sequence did not advance with the flips");

    printf("DRM_PROBE_OK\n");
    fflush(stdout);
    return 0;
}
