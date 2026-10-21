// SPDX-License-Identifier: MPL-2.0

// Acceptance probe for the minimal atomic KMS surface.
//
// The probe:
//   1. builds a 1024x768 dumb framebuffer and a MODE_ID blob,
//   2. checks that an atomic commit is refused before DRM_CLIENT_CAP_ATOMIC,
//   3. commits with DRM_MODE_ATOMIC_TEST_ONLY and verifies GETCRTC still
//      reports the CRTC as disabled,
//   4. commits the same state for real and verifies GETCRTC reports the mode
//      and framebuffer,
//   5. re-runs legacy SETCRTC with the same state (legacy path regression),
//   6. checks that a commit with a destroyed MODE_ID blob and one with a
//      page-flip-event flag are refused.
//
// As PID 1 it mounts devtmpfs first, so it can be used as a one-file initramfs
// `/init`.

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

struct drm_mode_create_blob {
    uint64_t data;
    uint32_t length;
    uint32_t blob_id;
};

struct drm_mode_destroy_blob {
    uint32_t blob_id;
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

struct drm_wait_vblank {
    uint32_t type;
    uint32_t sequence;
    uint64_t value1;
    uint64_t value2;
};

struct drm_set_client_cap {
    uint64_t capability;
    uint64_t value;
};

#define DRM_IOCTL_SET_CLIENT_CAP \
    _IOW(DRM_IOCTL_BASE, 0x0d, struct drm_set_client_cap)
#define DRM_IOCTL_MODE_GETRESOURCES \
    _IOWR(DRM_IOCTL_BASE, 0xa0, struct drm_mode_card_res)
#define DRM_IOCTL_MODE_GETCRTC _IOWR(DRM_IOCTL_BASE, 0xa1, struct drm_mode_crtc)
#define DRM_IOCTL_MODE_SETCRTC _IOWR(DRM_IOCTL_BASE, 0xa2, struct drm_mode_crtc)
#define DRM_IOCTL_MODE_GETCONNECTOR \
    _IOWR(DRM_IOCTL_BASE, 0xa7, struct drm_mode_get_connector)
#define DRM_IOCTL_MODE_GETPROPERTY \
    _IOWR(DRM_IOCTL_BASE, 0xaa, struct drm_mode_get_property)
#define DRM_IOCTL_MODE_ADDFB _IOWR(DRM_IOCTL_BASE, 0xae, struct drm_mode_fb_cmd)
#define DRM_IOCTL_MODE_CREATE_DUMB \
    _IOWR(DRM_IOCTL_BASE, 0xb2, struct drm_mode_create_dumb)
#define DRM_IOCTL_MODE_MAP_DUMB \
    _IOWR(DRM_IOCTL_BASE, 0xb3, struct drm_mode_map_dumb)
#define DRM_IOCTL_MODE_GETPLANERESOURCES \
    _IOWR(DRM_IOCTL_BASE, 0xb5, struct drm_mode_get_plane_res)
#define DRM_IOCTL_MODE_GETPLANE _IOWR(DRM_IOCTL_BASE, 0xb6, struct drm_mode_get_plane)
#define DRM_IOCTL_MODE_OBJ_GETPROPERTIES \
    _IOWR(DRM_IOCTL_BASE, 0xb9, struct drm_mode_obj_get_properties)
#define DRM_IOCTL_MODE_ATOMIC _IOWR(DRM_IOCTL_BASE, 0xbc, struct drm_mode_atomic)
#define DRM_IOCTL_MODE_CREATEPROPBLOB \
    _IOWR(DRM_IOCTL_BASE, 0xbd, struct drm_mode_create_blob)
#define DRM_IOCTL_MODE_DESTROYPROPBLOB \
    _IOWR(DRM_IOCTL_BASE, 0xbe, struct drm_mode_destroy_blob)

#define DRM_CLIENT_CAP_UNIVERSAL_PLANES 2
#define DRM_CLIENT_CAP_ATOMIC 3
#define DRM_MODE_ATOMIC_PAGE_FLIP_EVENT 0x0001
#define DRM_MODE_ATOMIC_TEST_ONLY 0x0100
#define DRM_MODE_ATOMIC_ALLOW_MODESET 0x0400
#define DRM_MODE_OBJECT_CRTC 0xcccccccc
#define DRM_MODE_OBJECT_CONNECTOR 0xc0c0c0c0
#define DRM_MODE_OBJECT_PLANE 0xeeeeeeee
#define DRM_IOCTL_WAIT_VBLANK _IOWR(DRM_IOCTL_BASE, 0x3a, struct drm_wait_vblank)
#define DRM_PLANE_TYPE_PRIMARY 1

static int card = -1;

static void fail(const char *step) {
    printf("ATOMICPROBE_FAIL %s errno=%d\n", step, errno);
    exit(1);
}

static uint32_t find_property(uint32_t object_id, uint32_t object_type,
                              const char *name) {
    struct drm_mode_obj_get_properties props = {
        .obj_id = object_id,
        .obj_type = object_type,
    };
    if (ioctl(card, DRM_IOCTL_MODE_OBJ_GETPROPERTIES, &props) != 0) {
        fail("obj-getprops-count");
    }
    uint32_t ids[64] = {0};
    uint64_t values[64] = {0};
    uint32_t count = props.count_props < 64 ? props.count_props : 64;
    props.props_ptr = (uint64_t)(uintptr_t)ids;
    props.prop_values_ptr = (uint64_t)(uintptr_t)values;
    props.count_props = count;
    if (ioctl(card, DRM_IOCTL_MODE_OBJ_GETPROPERTIES, &props) != 0) {
        fail("obj-getprops");
    }
    for (uint32_t i = 0; i < count; i++) {
        struct drm_mode_get_property property = {.prop_id = ids[i]};
        if (ioctl(card, DRM_IOCTL_MODE_GETPROPERTY, &property) != 0) {
            fail("getproperty");
        }
        if (strncmp(property.name, name, sizeof(property.name)) == 0) {
            return ids[i];
        }
    }
    printf("ATOMICPROBE_FAIL property-%s-missing\n", name);
    exit(1);
}

static struct drm_mode_crtc get_crtc(uint32_t crtc_id) {
    struct drm_mode_crtc crtc = {.crtc_id = crtc_id};
    if (ioctl(card, DRM_IOCTL_MODE_GETCRTC, &crtc) != 0) {
        fail("getcrtc");
    }
    return crtc;
}

// Sends an atomic request with no properties. The kernel checks the client
// capability before it parses any object, so this is enough to prove that a
// client without DRM_CLIENT_CAP_ATOMIC is refused. (Atomic properties are
// hidden from such clients, so they cannot be looked up first.)
static int atomic_without_props(uint32_t crtc_id) {
    uint32_t objects[1] = {crtc_id};
    uint32_t counts[1] = {0};
    struct drm_mode_atomic atomic = {
        .flags = DRM_MODE_ATOMIC_ALLOW_MODESET,
        .count_objs = 1,
        .objs_ptr = (uint64_t)(uintptr_t)objects,
        .count_props_ptr = (uint64_t)(uintptr_t)counts,
    };
    return ioctl(card, DRM_IOCTL_MODE_ATOMIC, &atomic);
}

static int atomic_commit(uint32_t flags, uint32_t crtc_id,
                         uint32_t connector_id, uint32_t plane_id,
                         uint32_t fb_id, uint32_t blob_id,
                         const struct drm_mode_modeinfo *mode) {
    uint32_t crtc_active = find_property(crtc_id, DRM_MODE_OBJECT_CRTC, "ACTIVE");
    uint32_t crtc_mode = find_property(crtc_id, DRM_MODE_OBJECT_CRTC, "MODE_ID");
    uint32_t connector_crtc =
        find_property(connector_id, DRM_MODE_OBJECT_CONNECTOR, "CRTC_ID");
    uint32_t plane_fb = find_property(plane_id, DRM_MODE_OBJECT_PLANE, "FB_ID");
    uint32_t plane_crtc = find_property(plane_id, DRM_MODE_OBJECT_PLANE, "CRTC_ID");
    uint32_t plane_src_x = find_property(plane_id, DRM_MODE_OBJECT_PLANE, "SRC_X");
    uint32_t plane_src_y = find_property(plane_id, DRM_MODE_OBJECT_PLANE, "SRC_Y");
    uint32_t plane_src_w = find_property(plane_id, DRM_MODE_OBJECT_PLANE, "SRC_W");
    uint32_t plane_src_h = find_property(plane_id, DRM_MODE_OBJECT_PLANE, "SRC_H");

    uint32_t objects[3] = {crtc_id, connector_id, plane_id};
    uint32_t counts[3] = {2, 1, 6};
    uint32_t props[9] = {
        crtc_active, crtc_mode,
        connector_crtc,
        plane_fb, plane_crtc, plane_src_x, plane_src_y, plane_src_w, plane_src_h,
    };
    uint64_t values[9] = {
        1, blob_id,
        crtc_id,
        fb_id, crtc_id,
        0, 0,
        (uint64_t)mode->hdisplay << 16, (uint64_t)mode->vdisplay << 16,
    };

    struct drm_mode_atomic atomic = {
        .flags = flags,
        .count_objs = 3,
        .objs_ptr = (uint64_t)(uintptr_t)objects,
        .count_props_ptr = (uint64_t)(uintptr_t)counts,
        .props_ptr = (uint64_t)(uintptr_t)props,
        .prop_values_ptr = (uint64_t)(uintptr_t)values,
    };
    return ioctl(card, DRM_IOCTL_MODE_ATOMIC, &atomic);
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
    printf("ATOMICPROBE_MODE %ux%u\n", mode.hdisplay, mode.vdisplay);

    struct drm_set_client_cap planes_cap = {
        .capability = DRM_CLIENT_CAP_UNIVERSAL_PLANES,
        .value = 1,
    };
    if (ioctl(card, DRM_IOCTL_SET_CLIENT_CAP, &planes_cap) != 0) {
        fail("set-client-cap-universal-planes");
    }

    struct drm_mode_get_plane_res plane_res = {0};
    if (ioctl(card, DRM_IOCTL_MODE_GETPLANERESOURCES, &plane_res) != 0) {
        fail("getplaneres-count");
    }
    uint32_t plane_ids[8];
    plane_res.plane_id_ptr = (uint64_t)(uintptr_t)plane_ids;
    plane_res.count_planes = 8;
    if (ioctl(card, DRM_IOCTL_MODE_GETPLANERESOURCES, &plane_res) != 0 ||
        plane_res.count_planes == 0) {
        fail("getplaneres");
    }
    // The virtio-gpu device exposes one plane, its primary. Confirm it is
    // addressable through GETPLANE before using it in the commit.
    uint32_t plane_id = plane_ids[0];
    struct drm_mode_get_plane plane = {.plane_id = plane_id};
    if (ioctl(card, DRM_IOCTL_MODE_GETPLANE, &plane) != 0) {
        fail("getplane");
    }
    printf("ATOMICPROBE_PLANE %u\n", plane_id);

    struct drm_mode_create_dumb dumb = {
        .width = mode.hdisplay,
        .height = mode.vdisplay,
        .bpp = 32,
    };
    if (ioctl(card, DRM_IOCTL_MODE_CREATE_DUMB, &dumb) != 0) {
        fail("create-dumb");
    }
    struct drm_mode_map_dumb map = {.handle = dumb.handle};
    if (ioctl(card, DRM_IOCTL_MODE_MAP_DUMB, &map) != 0) {
        fail("map-dumb");
    }
    uint8_t *fb_map = mmap(NULL, dumb.size, PROT_READ | PROT_WRITE, MAP_SHARED,
                           card, (off_t)map.offset);
    if (fb_map == MAP_FAILED) {
        fail("mmap-dumb");
    }
    static const uint32_t bars[4] = {0x00ff0000, 0x0000ff00, 0x000000ff,
                                     0x00ffffff};
    for (uint32_t y = 0; y < mode.vdisplay; y++) {
        uint32_t *line = (uint32_t *)(fb_map + (size_t)y * dumb.pitch);
        for (uint32_t x = 0; x < mode.hdisplay; x++) {
            line[x] = bars[(size_t)x * 4 / mode.hdisplay];
        }
    }
    struct drm_mode_fb_cmd addfb = {
        .width = mode.hdisplay,
        .height = mode.vdisplay,
        .pitch = dumb.pitch,
        .bpp = 32,
        .depth = 24,
        .handle = dumb.handle,
    };
    if (ioctl(card, DRM_IOCTL_MODE_ADDFB, &addfb) != 0) {
        fail("addfb");
    }
    printf("ATOMICPROBE_FB %u\n", addfb.fb_id);

    struct drm_mode_create_blob blob = {
        .data = (uint64_t)(uintptr_t)&mode,
        .length = sizeof(mode),
    };
    if (ioctl(card, DRM_IOCTL_MODE_CREATEPROPBLOB, &blob) != 0) {
        fail("create-blob");
    }
    printf("ATOMICPROBE_BLOB %u\n", blob.blob_id);

    // Without DRM_CLIENT_CAP_ATOMIC the atomic ioctl must be refused.
    errno = 0;
    if (atomic_without_props(crtc_id) == 0 || errno != EINVAL) {
        fail("atomic-without-cap");
    }
    struct drm_set_client_cap cap = {
        .capability = DRM_CLIENT_CAP_ATOMIC,
        .value = 1,
    };
    if (ioctl(card, DRM_IOCTL_SET_CLIENT_CAP, &cap) != 0) {
        fail("set-client-cap");
    }
    printf("ATOMICPROBE_CAP ok\n");

    // TEST_ONLY validates without changing the CRTC.
    if (atomic_commit(DRM_MODE_ATOMIC_ALLOW_MODESET | DRM_MODE_ATOMIC_TEST_ONLY,
                      crtc_id, connector_id, plane_id, addfb.fb_id, blob.blob_id,
                      &mode) != 0) {
        fail("atomic-test-only");
    }
    struct drm_mode_crtc before = get_crtc(crtc_id);
    if (before.mode_valid != 0) {
        fail("test-only-applied");
    }
    printf("ATOMICPROBE_TEST_ONLY ok\n");

    if (atomic_commit(DRM_MODE_ATOMIC_ALLOW_MODESET, crtc_id, connector_id,
                      plane_id, addfb.fb_id, blob.blob_id, &mode) != 0) {
        fail("atomic-commit");
    }
    struct drm_mode_crtc after = get_crtc(crtc_id);
    if (after.mode_valid == 0 || after.fb_id != addfb.fb_id ||
        after.mode.hdisplay != mode.hdisplay ||
        after.mode.vdisplay != mode.vdisplay) {
        fail("atomic-state");
    }
    printf("ATOMICPROBE_COMMIT mode=%ux%u fb=%u\n", after.mode.hdisplay,
           after.mode.vdisplay, after.fb_id);

    // The legacy path must keep working on the same state.
    uint32_t connectors[1] = {connector_id};
    struct drm_mode_crtc setcrtc = {
        .set_connectors_ptr = (uint64_t)(uintptr_t)connectors,
        .count_connectors = 1,
        .crtc_id = crtc_id,
        .fb_id = addfb.fb_id,
        .mode_valid = 1,
        .mode = mode,
    };
    if (ioctl(card, DRM_IOCTL_MODE_SETCRTC, &setcrtc) != 0) {
        fail("legacy-setcrtc");
    }
    printf("ATOMICPROBE_LEGACY ok\n");

    // A page-flip-event commit is not supported and must be refused.
    errno = 0;
    if (atomic_commit(DRM_MODE_ATOMIC_ALLOW_MODESET | DRM_MODE_ATOMIC_PAGE_FLIP_EVENT,
                      crtc_id, connector_id, plane_id, addfb.fb_id, blob.blob_id,
                      &mode) == 0 ||
        errno != EINVAL) {
        fail("atomic-page-flip-event");
    }
    printf("ATOMICPROBE_REJECT_PAGE_FLIP_EVENT ok\n");

    // A destroyed MODE_ID blob must not be usable.
    struct drm_mode_destroy_blob destroy = {.blob_id = blob.blob_id};
    if (ioctl(card, DRM_IOCTL_MODE_DESTROYPROPBLOB, &destroy) != 0) {
        fail("destroy-blob");
    }
    errno = 0;
    if (atomic_commit(DRM_MODE_ATOMIC_ALLOW_MODESET, crtc_id, connector_id,
                      plane_id, addfb.fb_id, blob.blob_id, &mode) == 0 ||
        errno != ENOENT) {
        fail("atomic-destroyed-blob");
    }
    printf("ATOMICPROBE_REJECT_DESTROYED_BLOB ok\n");

    // Vblank is a defined placeholder on this driver.
    struct drm_wait_vblank vblank = {.type = 0};
    errno = 0;
    if (ioctl(card, DRM_IOCTL_WAIT_VBLANK, &vblank) == 0 || errno != EOPNOTSUPP) {
        fail("wait-vblank-placeholder");
    }
    printf("ATOMICPROBE_VBLANK_PLACEHOLDER ok\n");

    printf("ATOMICPROBE_PASS\n");

    if (getpid() == 1) {
        sleep(3600);
    }
    return 0;
}
