// DRM-M18: page-flip event (vsync) verification client.
//
// Exercises the M18 kernel surface with raw ioctls (no libdrm):
//   MODE_PAGE_FLIP with/without DRM_MODE_PAGE_FLIP_EVENT
//   MODE_ATOMIC with DRM_MODE_PAGE_FLIP_EVENT
//   read() delivering drm_event_vblank (type/length/user_data/sequence/crtc)
//   poll() POLLIN signaling, EAGAIN on empty queue, EINVAL on short buffer
//
// Prints M18_* evidence lines; exits 0 and prints M18_ALL_PASS on success.

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <unistd.h>

#define DRM_IOCTL_BASE 'd'
#define DRM_IO(nr)        _IO(DRM_IOCTL_BASE, nr)
#define DRM_IOW(nr, type) _IOW(DRM_IOCTL_BASE, nr, type)
#define DRM_IOWR(nr, type) _IOWR(DRM_IOCTL_BASE, nr, type)

#define DRM_IOCTL_SET_MASTER DRM_IO(0x1e)
#define DRM_IOCTL_MODE_CREATE_DUMB DRM_IOWR(0xb2, struct drm_mode_create_dumb)
#define DRM_IOCTL_MODE_MAP_DUMB DRM_IOWR(0xb3, struct drm_mode_map_dumb)
#define DRM_IOCTL_MODE_PAGE_FLIP DRM_IOWR(0xb0, struct drm_mode_crtc_page_flip)
#define DRM_IOCTL_MODE_ADDFB2 DRM_IOWR(0xb8, struct drm_mode_fb_cmd2)
#define DRM_IOCTL_MODE_ATOMIC DRM_IOWR(0xbc, struct drm_mode_atomic)
#define DRM_IOCTL_MODE_OBJ_GETPROPERTIES DRM_IOWR(0xb9, struct drm_mode_obj_get_properties)
#define DRM_IOCTL_MODE_GETPROPERTY DRM_IOWR(0xaa, struct drm_mode_get_property)

#define DRM_MODE_PAGE_FLIP_EVENT 0x01
#define DRM_MODE_ATOMIC_ALLOW_MODESET 0x0400
#define DRM_MODE_OBJECT_PLANE 0xeeeeeeee
#define DRM_EVENT_FLIP_COMPLETE 0x02

struct drm_mode_create_dumb {
    uint32_t height, width, bpp, flags;
    uint32_t handle, pitch;
    uint64_t size;
};

struct drm_mode_fb_cmd2 {
    uint32_t fb_id;
    uint32_t width, height;
    uint32_t pixel_format;
    uint32_t flags;
    uint32_t handles[4];
    uint32_t pitches[4];
    uint32_t offsets[4];
    uint32_t pad;
    uint64_t modifier[4];
};

struct drm_mode_crtc_page_flip {
    uint32_t crtc_id;
    uint32_t fb_id;
    uint32_t flags;
    uint32_t reserved;
    uint64_t user_data;
};

struct drm_event_vblank {
    uint32_t type;
    uint32_t length;
    uint64_t user_data;
    uint32_t tv_sec;
    uint32_t tv_usec;
    uint32_t sequence;
    uint32_t crtc_id;
};

struct drm_mode_atomic {
    uint32_t flags;
    uint32_t count_props;
    uint64_t objs_ptr;
    uint64_t count_props_ptr;
    uint64_t props_ptr;
    uint64_t prop_values_ptr;
    uint64_t blob_id;
    uint64_t user_data;
    uint64_t reserved;
    uint64_t reserved_ptr;
};

struct drm_mode_obj_get_properties {
    uint64_t props_ptr;
    uint64_t prop_values_ptr;
    uint32_t count_props;
    uint32_t obj_id;
    uint32_t obj_type;
    uint32_t pad;
};

struct drm_mode_get_property {
    uint64_t values_ptr;
    uint64_t enum_blob_ptr;
    uint32_t prop_id;
    uint32_t flags;
    char name[32];
    uint32_t count_values;
    uint32_t count_enum_blobs;
};

static int failures;

#define CHECK(cond, ...) do { \
    if (cond) { printf("M18_PASS " __VA_ARGS__); printf("\n"); } \
    else { printf("M18_FAIL " __VA_ARGS__); printf("\n"); failures++; } \
} while (0)

static uint32_t find_prop(int fd, const char *name) {
    struct drm_mode_obj_get_properties q = { .obj_id = 1, .obj_type = DRM_MODE_OBJECT_PLANE };
    if (ioctl(fd, DRM_IOCTL_MODE_OBJ_GETPROPERTIES, &q) < 0) return 0;
    uint32_t ids[32] = {0};
    uint64_t vals[32] = {0};
    q.props_ptr = (uint64_t)ids;
    q.prop_values_ptr = (uint64_t)vals;
    if (q.count_props > 32) return 0;
    if (ioctl(fd, DRM_IOCTL_MODE_OBJ_GETPROPERTIES, &q) < 0) return 0;
    for (uint32_t i = 0; i < q.count_props; i++) {
        struct drm_mode_get_property gp = { .prop_id = ids[i] };
        if (ioctl(fd, DRM_IOCTL_MODE_GETPROPERTY, &gp) == 0 &&
            strncmp(gp.name, name, 32) == 0)
            return ids[i];
    }
    return 0;
}

static uint32_t make_fb(int fd, uint32_t seed) {
    struct drm_mode_create_dumb cd = { .width = 64, .height = 64, .bpp = 32 };
    if (ioctl(fd, DRM_IOCTL_MODE_CREATE_DUMB, &cd) < 0) return 0;
    struct drm_mode_fb_cmd2 fb2 = {0};
    fb2.width = 64; fb2.height = 64;
    fb2.pixel_format = 0x34325258; /* XR24 */
    fb2.handles[0] = cd.handle;
    fb2.pitches[0] = cd.pitch;
    if (ioctl(fd, DRM_IOCTL_MODE_ADDFB2, &fb2) < 0) return 0;
    (void)seed;
    return fb2.fb_id;
}

static int read_one_event(int fd, struct drm_event_vblank *ev) {
    return read(fd, ev, sizeof(*ev));
}

int main(void) {
    setvbuf(stdout, NULL, _IOLBF, 0);
    printf("M18 page-flip event verification\n");

    mkdir("/dev", 0755);
    mount("devtmpfs", "/dev", "devtmpfs", 0, NULL);

    int fd = open("/dev/dri/card0", O_RDWR | O_NONBLOCK);
    CHECK(fd >= 0, "open card0");
    if (fd < 0) return 1;
    CHECK(ioctl(fd, DRM_IOCTL_SET_MASTER, 0) == 0, "SET_MASTER");

    uint32_t fb1 = make_fb(fd, 1);
    uint32_t fb2 = make_fb(fd, 2);
    CHECK(fb1 != 0 && fb2 != 0, "ADDFB2 fb1=%u fb2=%u", fb1, fb2);

    /* Flip without the EVENT flag: no event must be queued. */
    struct drm_mode_crtc_page_flip flip = {
        .crtc_id = 1, .fb_id = fb1, .flags = 0,
    };
    CHECK(ioctl(fd, DRM_IOCTL_MODE_PAGE_FLIP, &flip) == 0, "PAGE_FLIP no-event");

    struct drm_event_vblank ev = {0};
    int rc = read_one_event(fd, &ev);
    CHECK(rc < 0 && errno == EAGAIN, "read without event -> EAGAIN (rc=%d errno=%d)", rc, errno);

    struct pollfd pfd = { .fd = fd, .events = POLLIN };
    rc = poll(&pfd, 1, 0);
    CHECK(rc == 0, "poll empty queue -> timeout");

    /* Flip with the EVENT flag. */
    flip.fb_id = fb2;
    flip.flags = DRM_MODE_PAGE_FLIP_EVENT;
    flip.user_data = 0xdeadbeef12345678ull;
    CHECK(ioctl(fd, DRM_IOCTL_MODE_PAGE_FLIP, &flip) == 0, "PAGE_FLIP with EVENT");

    rc = poll(&pfd, 1, 1000);
    CHECK(rc == 1 && (pfd.revents & POLLIN), "poll -> POLLIN");

    memset(&ev, 0, sizeof(ev));
    rc = read_one_event(fd, &ev);
    CHECK(rc == (int)sizeof(ev) &&
          ev.type == DRM_EVENT_FLIP_COMPLETE &&
          ev.length == sizeof(ev) &&
          ev.user_data == 0xdeadbeef12345678ull &&
          ev.crtc_id == 1,
          "read event: rc=%d type=%u len=%u ud=%llx seq=%u crtc=%u tv=%u.%06u",
          rc, ev.type, ev.length, (unsigned long long)ev.user_data,
          ev.sequence, ev.crtc_id, ev.tv_sec, ev.tv_usec);

    uint32_t seq1 = ev.sequence;

    /* Second flip: sequence must increase. */
    flip.fb_id = fb1;
    flip.user_data = 2;
    CHECK(ioctl(fd, DRM_IOCTL_MODE_PAGE_FLIP, &flip) == 0, "PAGE_FLIP #2");
    memset(&ev, 0, sizeof(ev));
    rc = read_one_event(fd, &ev);
    CHECK(rc == (int)sizeof(ev) && ev.sequence == seq1 + 1 && ev.user_data == 2,
          "event #2 seq=%u (prev=%u) ud=%llu",
          ev.sequence, seq1, (unsigned long long)ev.user_data);

    /* Short buffer -> EINVAL. */
    flip.fb_id = fb2;
    flip.user_data = 3;
    CHECK(ioctl(fd, DRM_IOCTL_MODE_PAGE_FLIP, &flip) == 0, "PAGE_FLIP #3");
    char tiny[8] = {0};
    rc = read(fd, tiny, sizeof(tiny));
    CHECK(rc < 0 && errno == EINVAL, "short read -> EINVAL (rc=%d errno=%d)", rc, errno);
    /* Drain the leftover event. */
    memset(&ev, 0, sizeof(ev));
    rc = read_one_event(fd, &ev);
    CHECK(rc == (int)sizeof(ev) && ev.user_data == 3, "drain event #3");

    /* Unknown flags are rejected. */
    flip.flags = 0x8000;
    rc = ioctl(fd, DRM_IOCTL_MODE_PAGE_FLIP, &flip);
    CHECK(rc < 0 && errno == EINVAL, "PAGE_FLIP unknown flags -> EINVAL");

    /* Atomic commit with an event. */
    uint32_t p_fb_id = find_prop(fd, "FB_ID");
    CHECK(p_fb_id != 0, "find FB_ID prop=%u", p_fb_id);
    uint32_t objs[1] = { 1 };
    uint32_t props[1] = { p_fb_id };
    uint64_t vals[1] = { fb1 };
    struct drm_mode_atomic at = {
        .flags = DRM_MODE_PAGE_FLIP_EVENT,
        .count_props = 1,
        .objs_ptr = (uint64_t)objs,
        .props_ptr = (uint64_t)props,
        .prop_values_ptr = (uint64_t)vals,
        .user_data = 0xcafef00d,
    };
    CHECK(ioctl(fd, DRM_IOCTL_MODE_ATOMIC, &at) == 0, "ATOMIC commit with EVENT");

    memset(&ev, 0, sizeof(ev));
    rc = read_one_event(fd, &ev);
    CHECK(rc == (int)sizeof(ev) && ev.type == DRM_EVENT_FLIP_COMPLETE &&
          ev.user_data == 0xcafef00d,
          "atomic event: rc=%d type=%u ud=%llx seq=%u",
          rc, ev.type, (unsigned long long)ev.user_data, ev.sequence);

    /* Unknown atomic flags are rejected. */
    at.flags = 0x2000;
    rc = ioctl(fd, DRM_IOCTL_MODE_ATOMIC, &at);
    CHECK(rc < 0 && errno == EINVAL, "ATOMIC unknown flags -> EINVAL");

    /* Render node: no events. */
    int rfd = open("/dev/dri/renderD128", O_RDWR | O_NONBLOCK);
    CHECK(rfd >= 0, "open renderD128");
    if (rfd >= 0) {
        memset(&ev, 0, sizeof(ev));
        rc = read(rfd, &ev, sizeof(ev));
        CHECK(rc < 0 && errno == EAGAIN, "render node read -> EAGAIN");
        close(rfd);
    }

    close(fd);
    if (failures == 0) {
        printf("M18_ALL_PASS\n");
        return 0;
    }
    printf("M18_FAILED count=%d\n", failures);
    return 1;
}
