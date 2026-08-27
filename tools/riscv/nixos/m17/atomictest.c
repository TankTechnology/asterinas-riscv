// DRM-M17: atomic modesetting verification client.
//
// Exercises the M17 kernel surface with raw ioctls (no libdrm):
//   MODE_GETPLANERESOURCES / MODE_GETPLANE
//   MODE_OBJ_GETPROPERTIES / MODE_GETPROPERTY / MODE_GETPROPBLOB
//   MODE_CREATEPROPBLOB / MODE_DESTROYPROPBLOB
//   MODE_ADDFB2
//   MODE_ATOMIC (TEST_ONLY + ALLOW_MODESET commit)
//
// Prints M17_* evidence lines; exits 0 and prints M17_ALL_PASS on success.

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stddef.h>
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

#define DRM_IOCTL_SET_CLIENT_CAP DRM_IOW(0x0d, struct drm_set_client_cap)
#define DRM_IOCTL_SET_MASTER DRM_IO(0x1e)
#define DRM_IOCTL_MODE_GETRESOURCES DRM_IOWR(0xa0, struct drm_mode_card_res)
#define DRM_IOCTL_MODE_CREATE_DUMB DRM_IOWR(0xb2, struct drm_mode_create_dumb)
#define DRM_IOCTL_MODE_GETPLANERESOURCES DRM_IOWR(0xb5, struct drm_mode_get_plane_res)
#define DRM_IOCTL_MODE_GETPLANE DRM_IOWR(0xb6, struct drm_mode_get_plane)
#define DRM_IOCTL_MODE_GETPROPERTY DRM_IOWR(0xaa, struct drm_mode_get_property)
#define DRM_IOCTL_MODE_GETPROPBLOB DRM_IOWR(0xac, struct drm_mode_get_blob)
#define DRM_IOCTL_MODE_ADDFB2 DRM_IOWR(0xb8, struct drm_mode_fb_cmd2)
#define DRM_IOCTL_MODE_OBJ_GETPROPERTIES DRM_IOWR(0xb9, struct drm_mode_obj_get_properties)
#define DRM_IOCTL_MODE_ATOMIC DRM_IOWR(0xbc, struct drm_mode_atomic)
#define DRM_IOCTL_MODE_CREATEPROPBLOB DRM_IOWR(0xbd, struct drm_mode_create_blob)
#define DRM_IOCTL_MODE_DESTROYPROPBLOB DRM_IOWR(0xbe, struct drm_mode_destroy_blob)

#define DRM_MODE_OBJECT_CRTC 0xcccccccc
#define DRM_MODE_OBJECT_CONNECTOR 0xc0c0c0c0
#define DRM_MODE_OBJECT_PLANE 0xeeeeeeee

#define DRM_MODE_ATOMIC_TEST_ONLY 0x0100
#define DRM_MODE_ATOMIC_NONBLOCK 0x0200
#define DRM_MODE_ATOMIC_ALLOW_MODESET 0x0400

#define DRM_CLIENT_CAP_UNIVERSAL_PLANES 2
#define DRM_CLIENT_CAP_ATOMIC 3
#define DRM_CLIENT_CAP_WRITEBACK_CONNECTORS 5

struct drm_set_client_cap { uint64_t capability; uint64_t value; };

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
    uint32_t height, width, bpp, flags;
    uint32_t handle, pitch;
    uint64_t size;
};

struct drm_mode_get_plane_res {
    uint64_t plane_id_ptr;
    uint32_t count_planes;
    uint32_t pad;
};

struct drm_mode_get_plane {
    uint32_t plane_id;
    uint32_t crtc_id;
    uint32_t fb_id;
    uint32_t possible_crtcs;
    uint32_t gamma_size;
    uint32_t count_format_types;
    uint64_t format_type_ptr;
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

struct drm_mode_property_enum {
    uint64_t value;
    char name[32];
};

struct drm_mode_get_blob {
    uint32_t blob_id;
    uint32_t length;
    uint64_t data;
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

struct drm_mode_create_blob {
    uint64_t data;
    uint32_t length;
    uint32_t blob_id;
};

struct drm_mode_destroy_blob {
    uint32_t blob_id;
    uint32_t pad;
};

struct drm_mode_atomic {
    uint32_t flags;
    uint32_t count_objs;
    uint64_t objs_ptr;
    uint64_t count_props_ptr;
    uint64_t props_ptr;
    uint64_t prop_values_ptr;
    uint64_t reserved;
    uint64_t user_data;
};

_Static_assert(sizeof(struct drm_mode_atomic) == 56,
               "drm_mode_atomic must match the Linux UAPI");
_Static_assert(offsetof(struct drm_mode_atomic, reserved) == 40,
               "drm_mode_atomic.reserved offset must match Linux");
_Static_assert(DRM_IOCTL_MODE_ATOMIC == 0xc03864bcUL,
               "DRM_IOCTL_MODE_ATOMIC must use the Linux command number");

struct drm_mode_modeinfo {
    uint32_t clock;
    uint16_t hdisplay, hsync_start, hsync_end, htotal, hskew;
    uint16_t vdisplay, vsync_start, vsync_end, vtotal, vscan;
    uint32_t vrefresh;
    uint32_t flags;
    uint32_t type;
    char name[32];
};

_Static_assert(sizeof(struct drm_mode_modeinfo) == 68,
               "drm_mode_modeinfo must match the Linux UAPI");

static int failures;

#define CHECK(cond, ...) do { \
    if (cond) { printf("M17_PASS " __VA_ARGS__); printf("\n"); } \
    else { printf("M17_FAIL " __VA_ARGS__); printf("\n"); failures++; } \
} while (0)

/* Look up a property id by name on an object, or 0 if absent. */
static uint32_t find_prop(int fd, uint32_t obj_id, uint32_t obj_type, const char *name) {
    struct drm_mode_obj_get_properties q = { .obj_id = obj_id, .obj_type = obj_type };
    if (ioctl(fd, DRM_IOCTL_MODE_OBJ_GETPROPERTIES, &q) < 0) return 0;
    uint32_t ids[32] = {0};
    uint64_t vals[32] = {0};
    if (q.count_props > 32) return 0;
    q.props_ptr = (uint64_t)ids;
    q.prop_values_ptr = (uint64_t)vals;
    if (ioctl(fd, DRM_IOCTL_MODE_OBJ_GETPROPERTIES, &q) < 0) return 0;
    for (uint32_t i = 0; i < q.count_props; i++) {
        struct drm_mode_get_property gp = { .prop_id = ids[i] };
        if (ioctl(fd, DRM_IOCTL_MODE_GETPROPERTY, &gp) == 0 &&
            strncmp(gp.name, name, 32) == 0)
            return ids[i];
    }
    return 0;
}

static uint64_t get_prop_value(int fd, uint32_t obj_id, uint32_t obj_type,
                               uint32_t wanted_prop) {
    struct drm_mode_obj_get_properties q = { .obj_id = obj_id, .obj_type = obj_type };
    if (ioctl(fd, DRM_IOCTL_MODE_OBJ_GETPROPERTIES, &q) < 0 || q.count_props > 32)
        return UINT64_MAX;
    uint32_t ids[32] = {0};
    uint64_t vals[32] = {0};
    q.props_ptr = (uint64_t)ids;
    q.prop_values_ptr = (uint64_t)vals;
    if (ioctl(fd, DRM_IOCTL_MODE_OBJ_GETPROPERTIES, &q) < 0)
        return UINT64_MAX;
    for (uint32_t i = 0; i < q.count_props; i++) {
        if (ids[i] == wanted_prop)
            return vals[i];
    }
    return UINT64_MAX;
}

int main(void) {
    setvbuf(stdout, NULL, _IOLBF, 0);
    printf("M17 atomic modesetting verification\n");

    /* Runs as /init: bring up the device nodes first. */
    mkdir("/dev", 0755);
    mkdir("/proc", 0755);
    mount("devtmpfs", "/dev", "devtmpfs", 0, NULL);
    mount("proc", "/proc", "proc", 0, NULL);

    int fd = open("/dev/dri/card0", O_RDWR);
    CHECK(fd >= 0, "open card0");
    if (fd < 0) return 1;
    CHECK(ioctl(fd, DRM_IOCTL_SET_MASTER, 0) == 0, "SET_MASTER");

    struct drm_mode_atomic no_cap_atomic = {0};
    errno = 0;
    CHECK(ioctl(fd, DRM_IOCTL_MODE_ATOMIC, &no_cap_atomic) < 0 &&
          errno == EOPNOTSUPP, "ATOMIC requires client capability errno=%d", errno);
    struct drm_mode_get_plane_res no_cap_planes = {0};
    CHECK(ioctl(fd, DRM_IOCTL_MODE_GETPLANERESOURCES, &no_cap_planes) == 0 &&
          no_cap_planes.count_planes == 0,
          "primary plane hidden until UNIVERSAL_PLANES is enabled");

    struct drm_set_client_cap cap = { .capability = DRM_CLIENT_CAP_UNIVERSAL_PLANES, .value = 1 };
    CHECK(ioctl(fd, DRM_IOCTL_SET_CLIENT_CAP, &cap) == 0, "SET_CLIENT_CAP UNIVERSAL_PLANES");
    cap.capability = DRM_CLIENT_CAP_ATOMIC;
    CHECK(ioctl(fd, DRM_IOCTL_SET_CLIENT_CAP, &cap) == 0, "SET_CLIENT_CAP ATOMIC");
    cap.capability = DRM_CLIENT_CAP_WRITEBACK_CONNECTORS;
    errno = 0;
    CHECK(ioctl(fd, DRM_IOCTL_SET_CLIENT_CAP, &cap) < 0 && errno == EINVAL,
          "SET_CLIENT_CAP rejects unimplemented writeback errno=%d", errno);

    /* --- globally unique KMS object discovery --- */
    struct drm_mode_card_res resources = {0};
    CHECK(ioctl(fd, DRM_IOCTL_MODE_GETRESOURCES, &resources) == 0 &&
          resources.count_crtcs == 1 && resources.count_connectors == 1 &&
          resources.count_encoders == 1,
          "GETRESOURCES crtcs=%u connectors=%u encoders=%u",
          resources.count_crtcs, resources.count_connectors, resources.count_encoders);
    uint32_t crtc_id = 0, connector_id = 0, encoder_id = 0;
    resources.crtc_id_ptr = (uint64_t)&crtc_id;
    resources.connector_id_ptr = (uint64_t)&connector_id;
    resources.encoder_id_ptr = (uint64_t)&encoder_id;
    CHECK(ioctl(fd, DRM_IOCTL_MODE_GETRESOURCES, &resources) == 0 &&
          crtc_id != 0 && connector_id != 0 && encoder_id != 0 &&
          crtc_id != connector_id && crtc_id != encoder_id && connector_id != encoder_id,
          "unique core ids crtc=%u connector=%u encoder=%u",
          crtc_id, connector_id, encoder_id);

    /* --- planes --- */
    struct drm_mode_get_plane_res pres = {0};
    CHECK(ioctl(fd, DRM_IOCTL_MODE_GETPLANERESOURCES, &pres) == 0 && pres.count_planes == 1,
          "GETPLANERESOURCES count=%u", pres.count_planes);
    uint32_t plane_id = 0xdeadbeef;
    pres.plane_id_ptr = (uint64_t)&plane_id;
    pres.count_planes = 0;
    CHECK(ioctl(fd, DRM_IOCTL_MODE_GETPLANERESOURCES, &pres) == 0 &&
          pres.count_planes == 1 && plane_id == 0xdeadbeef,
          "GETPLANERESOURCES respects zero capacity");
    plane_id = 0;
    pres.count_planes = 1;
    CHECK(ioctl(fd, DRM_IOCTL_MODE_GETPLANERESOURCES, &pres) == 0 && plane_id != 0,
          "GETPLANERESOURCES plane_id=%u", plane_id);
    CHECK(plane_id != crtc_id && plane_id != connector_id && plane_id != encoder_id,
          "unique plane id=%u", plane_id);

    uint32_t formats[4] = {0xdeadbeef, 0, 0, 0};
    struct drm_mode_get_plane pl = {
        .plane_id = plane_id, .format_type_ptr = (uint64_t)formats
    };
    CHECK(ioctl(fd, DRM_IOCTL_MODE_GETPLANE, &pl) == 0 &&
          pl.count_format_types == 2 && formats[0] == 0xdeadbeef,
          "GETPLANE capacity query formats=%u", pl.count_format_types);
    formats[0] = 0;
    pl.format_type_ptr = (uint64_t)formats;
    pl.count_format_types = 4;
    CHECK(ioctl(fd, DRM_IOCTL_MODE_GETPLANE, &pl) == 0 &&
          pl.possible_crtcs == 1 && pl.count_format_types == 2 &&
          formats[0] == 0x34325258 /* XR24 */,
          "GETPLANE crtcs=%u formats=%u first=%08x",
          pl.possible_crtcs, pl.count_format_types, formats[0]);

    /* --- property enumeration --- */
    struct drm_mode_obj_get_properties op = {
        .obj_id = crtc_id, .obj_type = DRM_MODE_OBJECT_CRTC
    };
    CHECK(ioctl(fd, DRM_IOCTL_MODE_OBJ_GETPROPERTIES, &op) == 0 && op.count_props == 2,
          "CRTC props count=%u", op.count_props);
    op.obj_id = connector_id;
    op.obj_type = DRM_MODE_OBJECT_CONNECTOR;
    CHECK(ioctl(fd, DRM_IOCTL_MODE_OBJ_GETPROPERTIES, &op) == 0 && op.count_props == 1,
          "CONNECTOR props count=%u", op.count_props);
    op.obj_id = plane_id;
    op.obj_type = DRM_MODE_OBJECT_PLANE;
    CHECK(ioctl(fd, DRM_IOCTL_MODE_OBJ_GETPROPERTIES, &op) == 0 && op.count_props == 11,
          "PLANE props count=%u", op.count_props);

    uint32_t p_active = find_prop(fd, crtc_id, DRM_MODE_OBJECT_CRTC, "ACTIVE");
    uint32_t p_mode_id = find_prop(fd, crtc_id, DRM_MODE_OBJECT_CRTC, "MODE_ID");
    uint32_t p_conn_crtc = find_prop(fd, connector_id, DRM_MODE_OBJECT_CONNECTOR, "CRTC_ID");
    uint32_t p_fb_id = find_prop(fd, plane_id, DRM_MODE_OBJECT_PLANE, "FB_ID");
    uint32_t p_plane_crtc = find_prop(fd, plane_id, DRM_MODE_OBJECT_PLANE, "CRTC_ID");
    uint32_t p_type = find_prop(fd, plane_id, DRM_MODE_OBJECT_PLANE, "type");
    CHECK(p_active && p_mode_id && p_conn_crtc && p_fb_id && p_plane_crtc && p_type,
          "property discovery active=%u mode_id=%u conn_crtc=%u fb=%u plane_crtc=%u type=%u",
          p_active, p_mode_id, p_conn_crtc, p_fb_id, p_plane_crtc, p_type);

    /* --- GETPROPERTY details --- */
    uint64_t range[2] = {UINT64_MAX, UINT64_MAX};
    struct drm_mode_get_property gp = {
        .prop_id = p_active, .values_ptr = (uint64_t)range, .count_values = 1
    };
    CHECK(ioctl(fd, DRM_IOCTL_MODE_GETPROPERTY, &gp) == 0 &&
          gp.count_values == 2 && range[0] == 0 && range[1] == UINT64_MAX,
          "GETPROPERTY respects partial value capacity");
    gp.count_values = 2;
    CHECK(ioctl(fd, DRM_IOCTL_MODE_GETPROPERTY, &gp) == 0 &&
          strcmp(gp.name, "ACTIVE") == 0 && gp.count_values == 2 &&
          range[0] == 0 && range[1] == 1 && (gp.flags & 0x2) /* RANGE */,
          "GETPROPERTY ACTIVE name=%s range=[%llu,%llu] flags=0x%x",
          gp.name, (unsigned long long)range[0], (unsigned long long)range[1], gp.flags);

    struct drm_mode_property_enum enums[3] = {0};
    memset(&gp, 0, sizeof(gp));
    gp.prop_id = p_type;
    gp.enum_blob_ptr = (uint64_t)enums;
    gp.count_enum_blobs = 3;
    CHECK(ioctl(fd, DRM_IOCTL_MODE_GETPROPERTY, &gp) == 0 &&
          strcmp(gp.name, "type") == 0 && gp.count_enum_blobs == 3 &&
          strcmp(enums[1].name, "Primary") == 0 && enums[1].value == 1 &&
          (gp.flags & 0x8) /* ENUM */ && (gp.flags & 0x4) /* IMMUTABLE */,
          "GETPROPERTY type name=%s enums=%u primary=%s flags=0x%x",
          gp.name, gp.count_enum_blobs, enums[1].name, gp.flags);

    /* --- dumb buffer + ADDFB2 --- */
    struct drm_mode_create_dumb cdumb = { .width = 640, .height = 480, .bpp = 32 };
    CHECK(ioctl(fd, DRM_IOCTL_MODE_CREATE_DUMB, &cdumb) == 0, "CREATE_DUMB");
    struct drm_mode_fb_cmd2 fb2 = {0};
    fb2.width = 640; fb2.height = 480;
    fb2.pixel_format = 0x34325258; /* XR24 */
    fb2.handles[0] = cdumb.handle;
    fb2.pitches[0] = cdumb.pitch;
    CHECK(ioctl(fd, DRM_IOCTL_MODE_ADDFB2, &fb2) == 0 && fb2.fb_id != 0,
          "ADDFB2 fb_id=%u", fb2.fb_id);

    /* --- property blob round-trip --- */
    struct drm_mode_modeinfo mode = {0};
    mode.hdisplay = 640; mode.vdisplay = 480;
    mode.htotal = 800; mode.vtotal = 525;
    mode.clock = 25175; mode.vrefresh = 60;
    strcpy(mode.name, "640x480");
    struct drm_mode_create_blob cb = {
        .data = (uint64_t)&mode, .length = sizeof(mode),
    };
    CHECK(ioctl(fd, DRM_IOCTL_MODE_CREATEPROPBLOB, &cb) == 0 && cb.blob_id != 0,
          "CREATEPROPBLOB id=%u", cb.blob_id);
    uint8_t tiny_mode = 0;
    struct drm_mode_create_blob tiny_cb = {
        .data = (uint64_t)&tiny_mode, .length = sizeof(tiny_mode),
    };
    CHECK(ioctl(fd, DRM_IOCTL_MODE_CREATEPROPBLOB, &tiny_cb) == 0,
          "CREATEPROPBLOB tiny validation fixture id=%u", tiny_cb.blob_id);

    struct drm_mode_create_blob huge = {
        .data = (uint64_t)&mode, .length = UINT32_MAX,
    };
    errno = 0;
    CHECK(ioctl(fd, DRM_IOCTL_MODE_CREATEPROPBLOB, &huge) < 0 && errno == EINVAL,
          "CREATEPROPBLOB rejects oversized allocation errno=%d", errno);

    struct drm_mode_modeinfo mode2 = {0};
    struct drm_mode_get_blob gb = { .blob_id = cb.blob_id,
        .data = (uint64_t)&mode2, .length = sizeof(mode2) };
    CHECK(ioctl(fd, DRM_IOCTL_MODE_GETPROPBLOB, &gb) == 0 &&
          gb.length == sizeof(mode) &&
          mode2.hdisplay == 640 && mode2.vdisplay == 480,
          "GETPROPBLOB len=%u %ux%u", gb.length, mode2.hdisplay, mode2.vdisplay);

    /* --- atomic commit: TEST_ONLY then real --- */
    uint32_t objs[3] = { crtc_id, connector_id, plane_id };
    uint32_t prop_counts[3] = { 2, 1, 2 };
    uint32_t props[6] = { p_mode_id, p_active, p_conn_crtc, p_fb_id, p_plane_crtc, 0 };
    uint64_t pvals[6] = { cb.blob_id, 1, crtc_id, fb2.fb_id, crtc_id, 0 };
    /* SRC_W/SRC_H: 16.16 fixed point */
    uint32_t p_src_w = find_prop(fd, plane_id, DRM_MODE_OBJECT_PLANE, "SRC_W");
    uint32_t p_src_h = find_prop(fd, plane_id, DRM_MODE_OBJECT_PLANE, "SRC_H");
    props[4] = p_plane_crtc;
    if (p_src_w && p_src_h) {
        props[5] = p_src_w; pvals[5] = 640ull << 16;
        prop_counts[2] = 3;
        /* keep it simple: SRC_W only; kernel accepts partial state */
    }
    struct drm_mode_atomic at = {0};
    at.flags = DRM_MODE_ATOMIC_TEST_ONLY | DRM_MODE_ATOMIC_ALLOW_MODESET;
    at.count_objs = 3;
    at.objs_ptr = (uint64_t)objs;
    at.count_props_ptr = (uint64_t)prop_counts;
    at.props_ptr = (uint64_t)props;
    at.prop_values_ptr = (uint64_t)pvals;
    CHECK(ioctl(fd, DRM_IOCTL_MODE_ATOMIC, &at) == 0, "ATOMIC TEST_ONLY");
    CHECK(get_prop_value(fd, crtc_id, DRM_MODE_OBJECT_CRTC, p_mode_id) == 0,
          "ATOMIC TEST_ONLY leaves MODE_ID unchanged");

    at.flags = DRM_MODE_ATOMIC_TEST_ONLY;
    errno = 0;
    CHECK(ioctl(fd, DRM_IOCTL_MODE_ATOMIC, &at) < 0 && errno == EINVAL,
          "ATOMIC TEST_ONLY validates ALLOW_MODESET errno=%d", errno);
    at.flags = DRM_MODE_ATOMIC_TEST_ONLY | DRM_MODE_ATOMIC_ALLOW_MODESET;
    pvals[0] = tiny_cb.blob_id;
    errno = 0;
    CHECK(ioctl(fd, DRM_IOCTL_MODE_ATOMIC, &at) < 0 && errno == EINVAL,
          "ATOMIC TEST_ONLY validates mode blob size errno=%d", errno);
    pvals[0] = cb.blob_id;
    pvals[1] = 0;
    errno = 0;
    CHECK(ioctl(fd, DRM_IOCTL_MODE_ATOMIC, &at) < 0 && errno == EOPNOTSUPP,
          "ATOMIC rejects unsupported pipeline disable errno=%d", errno);
    pvals[1] = 1;

    struct drm_mode_destroy_blob tiny_db = { .blob_id = tiny_cb.blob_id };
    ioctl(fd, DRM_IOCTL_MODE_DESTROYPROPBLOB, &tiny_db);

    /* A property must be rejected when paired with the wrong object type. */
    uint32_t bad_obj = crtc_id, bad_count = 1, bad_prop = p_fb_id;
    uint64_t bad_value = fb2.fb_id;
    struct drm_mode_atomic bad = {
        .flags = DRM_MODE_ATOMIC_TEST_ONLY,
        .count_objs = 1,
        .objs_ptr = (uint64_t)&bad_obj,
        .count_props_ptr = (uint64_t)&bad_count,
        .props_ptr = (uint64_t)&bad_prop,
        .prop_values_ptr = (uint64_t)&bad_value,
    };
    errno = 0;
    CHECK(ioctl(fd, DRM_IOCTL_MODE_ATOMIC, &bad) < 0 && errno == EINVAL,
          "ATOMIC rejects property/object mismatch errno=%d", errno);

    bad.count_objs = 0;
    bad.reserved = 1;
    errno = 0;
    CHECK(ioctl(fd, DRM_IOCTL_MODE_ATOMIC, &bad) < 0 && errno == EINVAL,
          "ATOMIC rejects nonzero reserved errno=%d", errno);

    bad.reserved = 0;
    bad.flags = DRM_MODE_ATOMIC_NONBLOCK;
    errno = 0;
    CHECK(ioctl(fd, DRM_IOCTL_MODE_ATOMIC, &bad) < 0 && errno == EOPNOTSUPP,
          "ATOMIC rejects unsupported NONBLOCK errno=%d", errno);

    at.flags = DRM_MODE_ATOMIC_ALLOW_MODESET;
    CHECK(ioctl(fd, DRM_IOCTL_MODE_ATOMIC, &at) == 0, "ATOMIC commit ALLOW_MODESET");

    /* Values must now be visible via OBJ_GETPROPERTIES. */
    {
        struct drm_mode_obj_get_properties vq = {
            .obj_id = crtc_id, .obj_type = DRM_MODE_OBJECT_CRTC
        };
        ioctl(fd, DRM_IOCTL_MODE_OBJ_GETPROPERTIES, &vq);
        uint32_t ids[8] = {0}; uint64_t vals[8] = {0};
        vq.props_ptr = (uint64_t)ids;
        vq.prop_values_ptr = (uint64_t)vals;
        ioctl(fd, DRM_IOCTL_MODE_OBJ_GETPROPERTIES, &vq);
        uint64_t mode_id_val = 0, active_val = 0;
        for (uint32_t i = 0; i < vq.count_props; i++) {
            if (ids[i] == p_mode_id) mode_id_val = vals[i];
            if (ids[i] == p_active) active_val = vals[i];
        }
        CHECK(mode_id_val == cb.blob_id && active_val == 1,
              "readback MODE_ID=%llu ACTIVE=%llu",
              (unsigned long long)mode_id_val, (unsigned long long)active_val);
    }

    /* A peer file cannot destroy this file's blob. */
    struct drm_mode_destroy_blob db = { .blob_id = cb.blob_id };
    int peer_fd = open("/dev/dri/card0", O_RDWR);
    errno = 0;
    CHECK(peer_fd >= 0 && ioctl(peer_fd, DRM_IOCTL_MODE_DESTROYPROPBLOB, &db) < 0 &&
          errno == EACCES, "peer blob destroy rejected errno=%d", errno);
    if (peer_fd >= 0) close(peer_fd);

    /* --- destroy blob; committed MODE_ID keeps it alive --- */
    CHECK(ioctl(fd, DRM_IOCTL_MODE_DESTROYPROPBLOB, &db) == 0, "DESTROYPROPBLOB");
    memset(&mode2, 0, sizeof(mode2));
    gb.length = sizeof(mode2);
    gb.data = (uint64_t)&mode2;
    CHECK(ioctl(fd, DRM_IOCTL_MODE_GETPROPBLOB, &gb) == 0 &&
          mode2.hdisplay == 640 && mode2.vdisplay == 480,
          "committed MODE_ID retains destroyed blob");

    /* --- render node must reject KMS ioctls --- */
    int rfd = open("/dev/dri/renderD128", O_RDWR);
    CHECK(rfd >= 0, "open renderD128");
    if (rfd >= 0) {
        struct drm_mode_get_plane_res rp = {0};
        int rc = ioctl(rfd, DRM_IOCTL_MODE_GETPLANERESOURCES, &rp);
        CHECK(rc < 0 && errno == EOPNOTSUPP, "render node rejects GETPLANERESOURCES errno=%d", errno);
        struct drm_mode_atomic rat = {0};
        rc = ioctl(rfd, DRM_IOCTL_MODE_ATOMIC, &rat);
        CHECK(rc < 0 && errno == EOPNOTSUPP, "render node rejects ATOMIC errno=%d", errno);
        close(rfd);
    }

    close(fd);
    if (failures == 0) {
        printf("M17_ALL_PASS\n");
        return 0;
    }
    printf("M17_FAILED count=%d\n", failures);
    return 1;
}
