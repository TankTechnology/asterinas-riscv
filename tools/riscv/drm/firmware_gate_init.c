// SPDX-License-Identifier: MPL-2.0

/* Proves the firmware-framebuffer scanout path end to end, from userspace.
 *
 * The machine this runs on has a bochs display and **no virtio-gpu**, and
 * U-Boot has written a `simple-framebuffer` node into the device tree. So the
 * driver has nothing to hand pixels to except the region firmware left active,
 * and the only way a picture can appear is by copying into it.
 *
 * The test is deliberately two-sided. Pixels go in through DRM -- create dumb
 * buffers, map them, fill them, register them as framebuffers, drive the CRTC --
 * and come back out through **fbdev**, which is a separate character device
 * with its own ioctl surface and its own mmap path. Both reach the same
 * physical memory, so agreement between them is not the driver agreeing with
 * itself: it is two interfaces, written independently, describing the same
 * bytes.
 *
 * Three present paths are exercised, because they are three different ioctls
 * that reach the same place by different routes, and covering only one would
 * leave the other two to be discovered broken later:
 *
 *   - `MODE_SETCRTC`, the ordinary "show this now";
 *   - `MODE_PAGE_FLIP`, which the X server uses for every frame after the
 *     first and which must show the *new* buffer;
 *   - `MODE_DIRTYFB`, which says "only this part of the buffer moved" -- and
 *     is the one that can quietly be an expensive lie, so it is tested in a
 *     way that fails if the whole frame is re-presented (see below).
 *
 * What each stage rules out:
 *   - the driver name rules out the virtio path being selected by accident,
 *     which would make everything below pass without testing anything new;
 *   - the reported maximum rules out advertising modes a fixed scanout cannot
 *     set, which is the failure a client would hit later and elsewhere;
 *   - the pixel comparisons rule out a copy that is skipped, mis-strided, or
 *     written to the wrong offset -- a whole-frame copy landing one row off
 *     still "succeeds" at every ioctl.
 */

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

/* The mode U-Boot wrote into the device tree (`BOCHS_XRGB8888`): 1280x1024 at
 * a stride of 1280 32-bit pixels, so the visible row and the stride are the
 * same length here. A padded stride is legal and the driver accepts it, but
 * this gate pins the layout it was given rather than one it chose. */
#define MODE_WIDTH 1280U
#define MODE_HEIGHT 1024U
#define MODE_BPP 32U
#define MODE_PITCH (MODE_WIDTH * 4U)
#define MODE_SIZE ((uint64_t)MODE_PITCH * MODE_HEIGHT)

/* The single CRTC the driver exposes; the same id its resource enumeration
 * reports. */
#define CRTC_ID 1U

/* The damaged region the `MODE_DIRTYFB` stage works in. Deliberately off the
 * edges and not square, so a rectangle transposed or off by an edge shows up
 * as the wrong pixels rather than as the same ones. */
#define DAMAGE_X1 101U
#define DAMAGE_Y1 57U
#define DAMAGE_X2 613U
#define DAMAGE_Y2 401U

/* The name `dri.rs` reports when a firmware framebuffer is what presents --
 * `simpledrm`, which is Linux's name for a display a bootloader left running.
 * If this is anything else, every check below is being made against the wrong
 * backend and the run proves nothing about the firmware path. */
static const char EXPECTED_DRIVER[] = "simpledrm";

struct drm_mode_modeinfo {
    uint32_t clock;
    uint16_t hdisplay, hsync_start, hsync_end, htotal, hskew;
    uint16_t vdisplay, vsync_start, vsync_end, vtotal, vscan;
    uint32_t vrefresh;
    uint32_t flags;
    uint32_t type;
    char name[32];
};

struct drm_version {
    int version_major;
    int version_minor;
    int version_patchlevel;
    size_t name_len;
    char *name;
    size_t date_len;
    char *date;
    size_t desc_len;
    char *desc;
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

struct drm_mode_fb_cmd {
    uint32_t fb_id;
    uint32_t width;
    uint32_t height;
    uint32_t pitch;
    uint32_t bpp;
    uint32_t depth;
    uint32_t handle;
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

/* A page flip has its own argument struct, and it is *not* `drm_mode_crtc`:
 * 24 bytes against 104. The command number is
 * `direction | size | type | number`, so using the wrong struct here would
 * encode a different number and the kernel would answer ENOTTY -- the same
 * shape of mistake that had `GEM_OPEN` declared as `RM_MAP` in this tree's
 * driver and in two of its probes. `/usr/include/drm/drm.h:1217`. */
struct drm_mode_crtc_page_flip {
    uint32_t crtc_id;
    uint32_t fb_id;
    uint32_t flags;
    uint32_t reserved;
    uint64_t user_data;
};

struct drm_mode_fb_dirty_cmd {
    uint32_t fb_id;
    uint32_t flags;
    uint32_t color;
    uint32_t num_clips;
    uint64_t clips_ptr;
};

/* Half-open, as the kernel reads it: the rect covers x1 up to but not
 * including x2. */
struct drm_clip_rect {
    uint16_t x1, y1, x2, y2;
};

#define DRM_IOCTL_VERSION _IOWR('d', 0x00, struct drm_version)
#define DRM_IOCTL_MODE_GETRESOURCES _IOWR('d', 0xa0, struct drm_mode_card_res)
#define DRM_IOCTL_MODE_GETCRTC _IOWR('d', 0xa1, struct drm_mode_crtc)
#define DRM_IOCTL_MODE_SETCRTC _IOWR('d', 0xa2, struct drm_mode_crtc)
#define DRM_IOCTL_MODE_ADDFB _IOWR('d', 0xae, struct drm_mode_fb_cmd)
#define DRM_IOCTL_MODE_DIRTYFB _IOWR('d', 0xb1, struct drm_mode_fb_dirty_cmd)
#define DRM_IOCTL_MODE_CREATE_DUMB _IOWR('d', 0xb2, struct drm_mode_create_dumb)
#define DRM_IOCTL_MODE_MAP_DUMB _IOWR('d', 0xb3, struct drm_mode_map_dumb)
#define DRM_IOCTL_MODE_PAGE_FLIP _IOWR('d', 0xb0, struct drm_mode_crtc_page_flip)

/* One dumb buffer, mapped, and the framebuffer that names it. */
struct buffer {
    uint32_t handle;
    uint32_t fb_id;
    uint32_t pitch;
    uint64_t size;
    uint32_t *pixels;
};

static void publish(const char *marker)
{
    puts(marker);
    fflush(stdout);
}

static _Noreturn void hold_forever(void)
{
    for (;;)
        pause();
}

static _Noreturn void fail_and_hold(const char *stage)
{
    printf("DRM_FIRMWARE_FAIL stage=%s errno=%d\n", stage, errno);
    fflush(stdout);
    hold_forever();
}

static _Noreturn void fail_with(const char *stage, const char *detail)
{
    printf("DRM_FIRMWARE_FAIL stage=%s detail=%s\n", stage, detail);
    fflush(stdout);
    hold_forever();
}

/* The three patterns are distinguished by their green channel, which is the
 * one a byte-order mistake changes least confusingly: `A` and `Z` differ in
 * every pixel, and `B` differs from both. Chosen so that a copy landing a row
 * or a column out changes the value, and so that the top-left pixel is never
 * zero -- a framebuffer nothing was copied into must not match. */
static uint32_t pattern_a(uint32_t x, uint32_t y)
{
    return 0xff000000U | (y << 12) | x;
}

static uint32_t pattern_b(uint32_t x, uint32_t y)
{
    return 0xff000000U | (0x80000U) | ((y & 0xffU) << 4) | (x & 0xfU);
}

static uint32_t pattern_z(uint32_t x, uint32_t y)
{
    (void)x;
    (void)y;
    return 0xff00ff00U;
}

static int in_damage(uint32_t x, uint32_t y)
{
    return x >= DAMAGE_X1 && x < DAMAGE_X2 && y >= DAMAGE_Y1 && y < DAMAGE_Y2;
}

/* Samples spread across the frame rather than a few neighbouring pixels: a
 * copy that stops early, or that is right in one corner, has to fail here. */
static const uint32_t SAMPLE_X[] = {0U, 1U, MODE_WIDTH / 2U, MODE_WIDTH - 1U};
static const uint32_t SAMPLE_Y[] = {0U, 1U, MODE_HEIGHT / 2U, MODE_HEIGHT - 1U};
#define SAMPLE_COUNT (sizeof(SAMPLE_X) / sizeof(SAMPLE_X[0]))

/* The fbdev mapping, opened once and kept: every stage reads the scanout
 * through it, which is the point -- it is not the DRM side. */
static int fbdev_fd = -1;
static const uint32_t *fbdev_pixels;
static size_t fbdev_length;

static void open_fbdev(void)
{
    fbdev_fd = open("/dev/fb0", O_RDONLY | O_CLOEXEC);
    if (fbdev_fd < 0)
        fail_and_hold("open-fb0");

    fbdev_length = (size_t)MODE_PITCH * MODE_HEIGHT;
    void *mapping = mmap(NULL, fbdev_length, PROT_READ, MAP_SHARED, fbdev_fd, 0);
    if (mapping == MAP_FAILED)
        fail_and_hold("mmap-fb0");
    fbdev_pixels = mapping;
}

static const uint32_t *fbdev_at(uint32_t x, uint32_t y)
{
    return &fbdev_pixels[(size_t)y * (MODE_PITCH / 4U) + x];
}

/* Every sample must hold `want`. `stage` names the failure, and `where` says
 * whether the sample was inside or outside the damaged region, because those
 * two failing mean opposite things. */
static void require_samples(uint32_t (*want)(uint32_t, uint32_t), const char *stage,
                            const char *where)
{
    for (size_t i = 0; i < SAMPLE_COUNT; ++i) {
        for (size_t j = 0; j < SAMPLE_COUNT; ++j) {
            uint32_t x = SAMPLE_X[i];
            uint32_t y = SAMPLE_Y[j];
            uint32_t found = *fbdev_at(x, y);
            uint32_t expected = want(x, y);
            if (found != expected) {
                printf("DRM_FIRMWARE_MISMATCH x=%u y=%u found=%08x want=%08x "
                       "stage=%s where=%s\n",
                       x, y, found, expected, stage, where);
                fflush(stdout);
                fail_with(stage, "pixels");
            }
        }
    }
}

/* Every sample *outside* the damaged region must hold `want`.
 *
 * Samples inside it are skipped rather than avoided by choosing coordinates
 * that happen not to land there. That arrangement holds today and would stop
 * holding after any edit to the rectangle, at which point this check would
 * start failing for the right reason at the wrong time. At least one sample
 * must be outside, so it cannot pass by checking nothing.
 */
static void require_samples_outside_damage(uint32_t (*want)(uint32_t, uint32_t),
                                           const char *stage)
{
    size_t checked = 0;
    for (size_t i = 0; i < SAMPLE_COUNT; ++i) {
        for (size_t j = 0; j < SAMPLE_COUNT; ++j) {
            uint32_t x = SAMPLE_X[i];
            uint32_t y = SAMPLE_Y[j];
            if (in_damage(x, y))
                continue;
            ++checked;
            uint32_t found = *fbdev_at(x, y);
            uint32_t expected = want(x, y);
            if (found != expected) {
                printf("DRM_FIRMWARE_MISMATCH x=%u y=%u found=%08x want=%08x "
                       "stage=%s where=outside\n",
                       x, y, found, expected, stage);
                fflush(stdout);
                fail_with(stage, "pixels");
            }
        }
    }
    if (checked == 0)
        fail_with(stage, "no-sample-outside-damage");
}

static void check_driver_name(int fd)
{
    char name[64] = {0};
    struct drm_version version = {.name_len = sizeof(name), .name = name};
    if (ioctl(fd, DRM_IOCTL_VERSION, &version) != 0)
        fail_and_hold("version");
    if (version.name_len < sizeof(name))
        name[version.name_len] = '\0';
    else
        name[sizeof(name) - 1] = '\0';

    printf("DRM_FIRMWARE_DRIVER name=%s\n", name);
    fflush(stdout);
    if (strcmp(name, EXPECTED_DRIVER) != 0)
        fail_and_hold("driver-name");
    publish("DRM_FIRMWARE_DRIVER PASS");
}

/* A fixed scanout must not advertise modes it cannot set: a client that picked
 * one would be refused later, at SETCRTC, with nothing pointing back here. */
static void check_max_mode(int fd)
{
    struct drm_mode_card_res res = {0};
    if (ioctl(fd, DRM_IOCTL_MODE_GETRESOURCES, &res) != 0)
        fail_and_hold("getresources");
    printf("DRM_FIRMWARE_MAX width=%u height=%u\n", res.max_width, res.max_height);
    fflush(stdout);
    if (res.max_width != MODE_WIDTH || res.max_height != MODE_HEIGHT)
        fail_and_hold("max-mode");
    publish("DRM_FIRMWARE_MAX PASS");
}

static void create_buffer(int fd, struct buffer *buffer)
{
    struct drm_mode_create_dumb create = {
        .width = MODE_WIDTH,
        .height = MODE_HEIGHT,
        .bpp = MODE_BPP,
    };
    if (ioctl(fd, DRM_IOCTL_MODE_CREATE_DUMB, &create) != 0)
        fail_and_hold("create-dumb");
    if (create.pitch != MODE_PITCH || create.size < MODE_SIZE) {
        printf("DRM_FIRMWARE_GEOMETRY pitch=%u size=%llu\n", create.pitch,
               (unsigned long long)create.size);
        fflush(stdout);
        fail_and_hold("dumb-geometry");
    }

    struct drm_mode_map_dumb map = {.handle = create.handle};
    if (ioctl(fd, DRM_IOCTL_MODE_MAP_DUMB, &map) != 0)
        fail_and_hold("map-dumb");

    void *pixels = mmap(NULL, (size_t)create.size, PROT_READ | PROT_WRITE,
                        MAP_SHARED, fd, (off_t)map.offset);
    if (pixels == MAP_FAILED)
        fail_and_hold("mmap-dumb");

    struct drm_mode_fb_cmd fb = {
        .width = MODE_WIDTH,
        .height = MODE_HEIGHT,
        .pitch = create.pitch,
        .bpp = MODE_BPP,
        .depth = MODE_BPP,
        .handle = create.handle,
    };
    if (ioctl(fd, DRM_IOCTL_MODE_ADDFB, &fb) != 0)
        fail_and_hold("addfb");

    buffer->handle = create.handle;
    buffer->fb_id = fb.fb_id;
    buffer->pitch = create.pitch;
    buffer->size = create.size;
    buffer->pixels = pixels;
}

static void set_crtc(int fd, uint32_t fb_id)
{
    struct drm_mode_crtc crtc = {
        .crtc_id = CRTC_ID,
        .fb_id = fb_id,
        .mode_valid = 1,
        .mode = {.hdisplay = MODE_WIDTH, .vdisplay = MODE_HEIGHT},
    };
    if (ioctl(fd, DRM_IOCTL_MODE_SETCRTC, &crtc) != 0)
        fail_and_hold("setcrtc");
}

static void fill(struct buffer *buffer, uint32_t (*pattern)(uint32_t, uint32_t))
{
    uint32_t pitch_pixels = buffer->pitch / 4U;
    for (uint32_t y = 0; y < MODE_HEIGHT; ++y)
        for (uint32_t x = 0; x < MODE_WIDTH; ++x)
            buffer->pixels[(size_t)y * pitch_pixels + x] = pattern(x, y);
}

/* The ordinary present: fill a buffer, tell the CRTC to show it, read it back
 * through fbdev. */
static void present_setcrtc(int fd, struct buffer *buffer)
{
    set_crtc(fd, buffer->fb_id);
    /* Filled after the present, so what fbdev is about to show cannot be a
     * copy the driver made from a buffer that was already correct -- and so
     * the pattern reaching the screen is the work of the next call, not of
     * this one. */
    fill(buffer, pattern_a);
    set_crtc(fd, buffer->fb_id);

    require_samples(pattern_a, "setcrtc", "whole-frame");
    publish("DRM_FIRMWARE_SETCRTC PASS");
}

/* `MODE_PAGE_FLIP` is what the X server uses for every frame after the first,
 * and it must land on the *new* buffer. Two buffers, so a flip that does
 * nothing shows the old pattern and fails. */
static void page_flip(int fd, struct buffer *first, struct buffer *second)
{
    struct drm_mode_crtc_page_flip flip = {
        .crtc_id = CRTC_ID,
        .fb_id = second->fb_id,
    };
    fill(second, pattern_z);
    if (ioctl(fd, DRM_IOCTL_MODE_PAGE_FLIP, &flip) != 0)
        fail_and_hold("page-flip");

    require_samples(pattern_z, "page-flip", "whole-frame");

    /* And back, so a flip that only ever accepts one buffer is caught. */
    fill(first, pattern_a);
    flip.fb_id = first->fb_id;
    if (ioctl(fd, DRM_IOCTL_MODE_PAGE_FLIP, &flip) != 0)
        fail_and_hold("page-flip-back");
    require_samples(pattern_a, "page-flip", "whole-frame");

    publish("DRM_FIRMWARE_PAGEFLIP PASS");
}

/* The stage that can distinguish an incremental present from a whole-frame one.
 *
 * Filling the source buffer and then dirtying a rectangle would pass either
 * way: if only the rectangle changed in the source, re-presenting everything
 * and re-presenting the rectangle produce the same screen. So the source is
 * poisoned *outside* the rectangle first. A backend that copies the whole
 * frame puts the poison on screen and fails; one that copies the rectangle
 * leaves what was there before, which is the actual claim being made.
 */
static void dirty_rect(int fd, struct buffer *buffer)
{
    /* 1. The screen holds `A` everywhere, from the setcrtc stage. */
    require_samples(pattern_a, "dirtyfb", "before");

    /* 2. The source now disagrees with the screen everywhere, and the region
     *    that is about to be dirtied carries `B`. */
    fill(buffer, pattern_z);
    uint32_t pitch_pixels = buffer->pitch / 4U;
    for (uint32_t y = DAMAGE_Y1; y < DAMAGE_Y2; ++y)
        for (uint32_t x = DAMAGE_X1; x < DAMAGE_X2; ++x)
            buffer->pixels[(size_t)y * pitch_pixels + x] = pattern_b(x, y);

    /* 3. Say that only that rectangle moved, and give the driver words it
     *    cannot act on to be sure it is the rectangles doing the work: a
     *    kernel that ignored the list would copy everything. */
    struct drm_clip_rect clip = {
        .x1 = DAMAGE_X1,
        .y1 = DAMAGE_Y1,
        .x2 = DAMAGE_X2,
        .y2 = DAMAGE_Y2,
    };
    struct drm_mode_fb_dirty_cmd dirty = {
        .fb_id = buffer->fb_id,
        .num_clips = 1,
        .clips_ptr = (uint64_t)(uintptr_t)&clip,
    };
    if (ioctl(fd, DRM_IOCTL_MODE_DIRTYFB, &dirty) != 0)
        fail_and_hold("dirtyfb");

    /* 4. The rectangle carries `B`... */
    for (uint32_t y = DAMAGE_Y1; y < DAMAGE_Y2; y += 16U) {
        for (uint32_t x = DAMAGE_X1; x < DAMAGE_X2; x += 16U) {
            uint32_t found = *fbdev_at(x, y);
            uint32_t expected = pattern_b(x, y);
            if (found != expected) {
                printf("DRM_FIRMWARE_MISMATCH x=%u y=%u found=%08x want=%08x "
                       "stage=dirtyfb where=inside\n",
                       x, y, found, expected);
                fflush(stdout);
                fail_with("dirtyfb", "rect-not-copied");
            }
        }
    }

    /* ...and everywhere else still carries `A`, the poison having reached the
     * source but not the screen. */
    require_samples_outside_damage(pattern_a, "dirtyfb");

    publish("DRM_FIRMWARE_DIRTYFB PASS");
}

int main(void)
{
    int fd = open("/dev/dri/card0", O_RDWR | O_CLOEXEC);
    if (fd < 0)
        fail_and_hold("open-card0");

    open_fbdev();

    check_driver_name(fd);
    check_max_mode(fd);

    struct buffer first;
    struct buffer second;
    create_buffer(fd, &first);
    create_buffer(fd, &second);

    present_setcrtc(fd, &first);
    page_flip(fd, &first, &second);
    dirty_rect(fd, &first);

    publish("ASTERINAS_DRM_FIRMWARE_R1_READY");
    hold_forever();
}
