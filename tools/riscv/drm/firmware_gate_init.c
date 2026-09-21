// SPDX-License-Identifier: MPL-2.0

/* Proves the firmware-framebuffer scanout path end to end, from userspace.
 *
 * The machine this runs on has a bochs display and **no virtio-gpu**, and
 * U-Boot has written a `simple-framebuffer` node into the device tree. So the
 * driver has nothing to hand pixels to except the region firmware left active,
 * and the only way a picture can appear is by copying into it.
 *
 * The test is deliberately two-sided. Pixels go in through DRM -- create a dumb
 * buffer, map it, fill it, register it as a framebuffer, set the CRTC -- and
 * come back out through **fbdev**, which is a separate character device with
 * its own ioctl surface and its own mmap path. Both reach the same physical
 * memory, so agreement between them is not the driver agreeing with itself: it
 * is two interfaces, written independently, describing the same bytes.
 *
 * What each stage rules out:
 *   - the driver name rules out the virtio path being selected by accident,
 *     which would make everything below pass without testing anything new;
 *   - the reported maximum rules out advertising modes a fixed scanout cannot
 *     set, which is the failure a client would hit later and elsewhere;
 *   - the pixel comparison rules out a copy that is skipped, mis-strided, or
 *     written to the wrong offset -- a whole-frame copy that lands one row off
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

/* `drm_mode_crtc` needs a mode, and the driver ignores everything in it except
 * the geometry it re-derives, so only the fields it reads are filled. */
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

#define DRM_IOCTL_VERSION _IOWR('d', 0x00, struct drm_version)
#define DRM_IOCTL_MODE_GETRESOURCES _IOWR('d', 0xa0, struct drm_mode_card_res)
#define DRM_IOCTL_MODE_CREATE_DUMB _IOWR('d', 0xb2, struct drm_mode_create_dumb)
#define DRM_IOCTL_MODE_MAP_DUMB _IOWR('d', 0xb3, struct drm_mode_map_dumb)
#define DRM_IOCTL_MODE_ADDFB _IOWR('d', 0xae, struct drm_mode_fb_cmd)
#define DRM_IOCTL_MODE_SETCRTC _IOWR('d', 0xa2, struct drm_mode_crtc)

/* The single CRTC the driver exposes; the same id its resource enumeration
 * reports. */
#define CRTC_ID 1U

/* The name `dri.rs` reports when a firmware framebuffer is what presents --
 * `simpledrm`, which is Linux's name for a display a bootloader left running.
 * If this is anything else, every check below is being made against the wrong
 * backend and the run proves nothing about the firmware path. */
static const char EXPECTED_DRIVER[] = "simpledrm";

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

/* The pattern a pixel at (x, y) must have. Chosen so that a row or column
 * landing in the wrong place changes the value: the x term catches a stride
 * error, and the y term catches an offset error. The top-left pixel is
 * non-zero, so a framebuffer nothing was ever copied into fails immediately
 * rather than matching a zeroed region. */
static uint32_t expected_pixel(uint32_t x, uint32_t y)
{
    return 0xff000000U | (y << 12) | x;
}

static void fill_pattern(uint32_t *pixels, uint32_t pitch_pixels)
{
    for (uint32_t y = 0; y < MODE_HEIGHT; ++y)
        for (uint32_t x = 0; x < MODE_WIDTH; ++x)
            pixels[(size_t)y * pitch_pixels + x] = expected_pixel(x, y);
}

/* Samples spread across the frame rather than a few neighbouring pixels: a
 * copy that stops early, or that is right in one corner, has to fail here. */
static int pixels_agree(const uint32_t *pixels, uint32_t pitch_pixels)
{
    const uint32_t xs[] = {0U, 1U, MODE_WIDTH / 2U, MODE_WIDTH - 1U};
    const uint32_t ys[] = {0U, 1U, MODE_HEIGHT / 2U, MODE_HEIGHT - 1U};
    for (size_t i = 0; i < sizeof(xs) / sizeof(xs[0]); ++i) {
        for (size_t j = 0; j < sizeof(ys) / sizeof(ys[0]); ++j) {
            uint32_t x = xs[i];
            uint32_t y = ys[j];
            uint32_t found = pixels[(size_t)y * pitch_pixels + x];
            uint32_t want = expected_pixel(x, y);
            if (found != want) {
                printf("DRM_FIRMWARE_MISMATCH x=%u y=%u found=%08x want=%08x\n",
                       x, y, found, want);
                fflush(stdout);
                return -1;
            }
        }
    }
    return 0;
}

static void check_driver_name(int fd)
{
    char name[64] = {0};
    struct drm_version version = {
        .name_len = sizeof(name),
        .name = name,
    };
    if (ioctl(fd, DRM_IOCTL_VERSION, &version) != 0)
        fail_and_hold("version");
    /* The kernel NUL-terminates within the length it reports. */
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

static uint32_t present_one_frame(int fd)
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

    struct drm_mode_crtc crtc = {
        .crtc_id = CRTC_ID,
        .fb_id = fb.fb_id,
        .mode_valid = 1,
        .mode = {.hdisplay = MODE_WIDTH, .vdisplay = MODE_HEIGHT},
    };
    if (ioctl(fd, DRM_IOCTL_MODE_SETCRTC, &crtc) != 0)
        fail_and_hold("setcrtc");
    publish("DRM_FIRMWARE_PRESENT PASS");

    /* Filled after the present, so the copy that this gate observes cannot be
     * one the driver made from a buffer that was already correct. */
    fill_pattern(pixels, create.pitch / 4U);
    if (ioctl(fd, DRM_IOCTL_MODE_SETCRTC, &crtc) != 0)
        fail_and_hold("setcrtc-pattern");

    munmap(pixels, (size_t)create.size);
    publish("DRM_FIRMWARE_PATTERN PASS");
    return fb.fb_id;
}

/* Reads the same memory through fbdev, which knows nothing about DRM. */
static void check_fbdev(void)
{
    int fd = open("/dev/fb0", O_RDONLY | O_CLOEXEC);
    if (fd < 0)
        fail_and_hold("open-fb0");

    size_t length = (size_t)MODE_PITCH * MODE_HEIGHT;
    void *mapping = mmap(NULL, length, PROT_READ, MAP_SHARED, fd, 0);
    if (mapping == MAP_FAILED)
        fail_and_hold("mmap-fb0");

    if (pixels_agree(mapping, MODE_PITCH / 4U) != 0)
        fail_and_hold("pixels");

    munmap(mapping, length);
    close(fd);
    publish("DRM_FIRMWARE_FBDEV PASS");
}

int main(void)
{
    int fd = open("/dev/dri/card0", O_RDWR | O_CLOEXEC);
    if (fd < 0)
        fail_and_hold("open-card0");

    check_driver_name(fd);
    check_max_mode(fd);
    present_one_frame(fd);
    check_fbdev();

    publish("ASTERINAS_DRM_FIRMWARE_R1_READY");
    hold_forever();
}
