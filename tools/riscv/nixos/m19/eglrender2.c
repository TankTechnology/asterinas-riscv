// SPDX-License-Identifier: MPL-2.0

// DRM-M19: kmscube-style end-to-end virgl verification client.
//
// Real-client flow (no pbuffer — the GBM/DRM platform has none):
//   open card0 -> KMS discovery -> GBM surface -> EGL window surface
//   render N frames -> eglSwapBuffers -> lock front buffer -> ADDFB2
//   -> atomic commit with PAGE_FLIP_EVENT -> read flip event
//   -> glReadPixels checksum per frame -> PPM dump of the last frame
//
// Evidence on stdout: M19_* markers, M19_EGL_DONE on success.

#define _GNU_SOURCE
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES2/gl2.h>
#include <gbm.h>

#include <errno.h>
#include <fcntl.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <poll.h>
#include <unistd.h>

#define NUM_FRAMES 4
#define FENCE_POLL_TIMEOUT_MS 5000

/* ---- raw KMS ioctl bits (no libdrm needed) ---- */
#define DRM_IOCTL_BASE 'd'
#define DRM_IOW(nr, type) _IOW(DRM_IOCTL_BASE, nr, type)
#define DRM_IOWR(nr, type) _IOWR(DRM_IOCTL_BASE, nr, type)

#define DRM_IOCTL_SET_MASTER _IO('d', 0x1e)
#define DRM_IOCTL_SET_CLIENT_CAP DRM_IOW(0x0d, struct drm_set_client_cap)
#define DRM_IOCTL_MODE_GETRESOURCES DRM_IOWR(0xa0, struct drm_mode_card_res)
#define DRM_IOCTL_MODE_GETCONNECTOR DRM_IOWR(0xa7, struct drm_mode_get_connector)
#define DRM_IOCTL_MODE_GETPLANERESOURCES DRM_IOWR(0xb5, struct drm_mode_get_plane_res)
#define DRM_IOCTL_MODE_GETPROPERTY DRM_IOWR(0xaa, struct drm_mode_get_property)
#define DRM_IOCTL_MODE_OBJ_GETPROPERTIES DRM_IOWR(0xb9, struct drm_mode_obj_get_properties)
#define DRM_IOCTL_MODE_ADDFB2 DRM_IOWR(0xb8, struct drm_mode_fb_cmd2)
#define DRM_IOCTL_MODE_ATOMIC DRM_IOWR(0xbc, struct drm_mode_atomic)
#define DRM_IOCTL_MODE_CREATEPROPBLOB DRM_IOWR(0xbd, struct drm_mode_create_blob)

#define DRM_MODE_OBJECT_CRTC 0xcccccccc
#define DRM_MODE_OBJECT_CONNECTOR 0xc0c0c0c0
#define DRM_MODE_OBJECT_PLANE 0xeeeeeeee
#define DRM_MODE_ATOMIC_ALLOW_MODESET 0x0400
#define DRM_MODE_ATOMIC_TEST_ONLY 0x0100
#define DRM_MODE_ATOMIC_NONBLOCK 0x0200
#define DRM_MODE_PAGE_FLIP_EVENT 0x01
#define DRM_EVENT_FLIP_COMPLETE 0x02
#define DRM_CLIENT_CAP_UNIVERSAL_PLANES 2
#define DRM_CLIENT_CAP_ATOMIC 3

struct drm_set_client_cap { uint64_t capability, value; };

struct drm_mode_modeinfo {
    uint32_t clock;
    uint16_t hdisplay, hsync_start, hsync_end, htotal, hskew;
    uint16_t vdisplay, vsync_start, vsync_end, vtotal, vscan;
    uint32_t vrefresh, flags, type;
    char name[32];
};

struct drm_mode_card_res {
    uint64_t fb_id_ptr, crtc_id_ptr, connector_id_ptr, encoder_id_ptr;
    uint32_t count_fbs, count_crtcs, count_connectors, count_encoders;
    uint32_t min_width, max_width, min_height, max_height;
};

struct drm_mode_get_plane_res {
    uint64_t plane_id_ptr;
    uint32_t count_planes, pad;
};

struct drm_mode_get_connector {
    uint64_t encoders_ptr, modes_ptr, props_ptr, prop_values_ptr;
    uint32_t count_modes, count_props, count_encoders;
    uint32_t encoder_id, connector_id, connector_type, connector_type_id;
    uint32_t connection, mm_width, mm_height, subpixel, pad;
};

struct drm_mode_obj_get_properties {
    uint64_t props_ptr, prop_values_ptr;
    uint32_t count_props, obj_id, obj_type, pad;
};

struct drm_mode_get_property {
    uint64_t values_ptr, enum_blob_ptr;
    uint32_t prop_id, flags;
    char name[32];
    uint32_t count_values, count_enum_blobs;
};

struct drm_mode_fb_cmd2 {
    uint32_t fb_id, width, height, pixel_format, flags;
    uint32_t handles[4], pitches[4], offsets[4], pad;
    uint64_t modifier[4];
};

struct drm_mode_atomic {
    uint32_t flags, count_objs;
    uint64_t objs_ptr, count_props_ptr, props_ptr, prop_values_ptr;
    uint64_t reserved, user_data;
};

struct drm_mode_create_blob {
    uint64_t data;
    uint32_t length, blob_id;
};

struct drm_event_vblank {
    uint32_t type, length;
    uint64_t user_data;
    uint32_t tv_sec, tv_usec, sequence, crtc_id;
};

static void fail(const char *stage) {
    printf("M19_EGL_FAIL %s (errno=%d)\n", stage, errno);
    fflush(stdout);
    exit(1);
}

/* Find a KMS property id by name on an object. */
static uint32_t find_prop(int fd, uint32_t obj_id, uint32_t obj_type,
                          const char *name) {
    struct drm_mode_obj_get_properties q = { .obj_id = obj_id, .obj_type = obj_type };
    if (ioctl(fd, DRM_IOCTL_MODE_OBJ_GETPROPERTIES, &q) < 0) return 0;
    uint32_t ids[32] = {0};
    uint64_t vals[32] = {0};
    q.props_ptr = (uint64_t)ids;
    q.prop_values_ptr = (uint64_t)vals;
    if (q.count_props > 32 || ioctl(fd, DRM_IOCTL_MODE_OBJ_GETPROPERTIES, &q) < 0) return 0;
    for (uint32_t i = 0; i < q.count_props; i++) {
        struct drm_mode_get_property gp = { .prop_id = ids[i] };
        if (ioctl(fd, DRM_IOCTL_MODE_GETPROPERTY, &gp) == 0 &&
            strncmp(gp.name, name, 32) == 0)
            return ids[i];
    }
    return 0;
}

static const char *VERT_SRC =
    "attribute vec2 pos;\n"
    "attribute vec3 col;\n"
    "varying vec3 vcol;\n"
    "void main() { vcol = col; gl_Position = vec4(pos, 0.0, 1.0); }\n";
static const char *FRAG_SRC =
    "precision mediump float;\n"
    "varying vec3 vcol;\n"
    "void main() { gl_FragColor = vec4(vcol, 1.0); }\n";

static GLuint make_shader(GLenum type, const char *src) {
    GLuint sh = glCreateShader(type);
    glShaderSource(sh, 1, &src, NULL);
    glCompileShader(sh);
    GLint ok = 0;
    glGetShaderiv(sh, GL_COMPILE_STATUS, &ok);
    if (!ok) fail("shader");
    return sh;
}

/* Read back the current render target and print probe pixels + a checksum.
 * `tag` identifies the read point within a frame (preread / postrender /
 * clearprobe) so a single boot distinguishes buffer-tracking bugs from
 * data-sync bugs. */
static uint32_t probe(const char *tag, int frame, uint8_t *px,
                      uint32_t width, uint32_t height) {
    glReadPixels(0, 0, width, height, GL_RGBA, GL_UNSIGNED_BYTE, px);
    uint32_t csum = 0;
    for (uint32_t i = 0; i < width * height * 4; i += 97)
        csum = csum * 31 + px[i];
    printf("M19_PROBE %s frame=%d csum=%08x bg=(%u,%u,%u) center=(%u,%u,%u)\n",
           tag, frame, csum,
           px[0], px[1], px[2],
           px[(height / 2 * width + width / 2) * 4],
           px[(height / 2 * width + width / 2) * 4 + 1],
           px[(height / 2 * width + width / 2) * 4 + 2]);
    return csum;
}

int main(void) {
    setvbuf(stdout, NULL, _IOLBF, 0);

    int fd = open("/dev/dri/card0", O_RDWR);
    if (fd < 0) fail("open card0");
    if (ioctl(fd, DRM_IOCTL_SET_MASTER, 0) < 0) fail("set_master");
    struct drm_set_client_cap cap = {
        .capability = DRM_CLIENT_CAP_UNIVERSAL_PLANES,
        .value = 1,
    };
    if (ioctl(fd, DRM_IOCTL_SET_CLIENT_CAP, &cap) < 0) fail("cap planes");
    cap.capability = DRM_CLIENT_CAP_ATOMIC;
    if (ioctl(fd, DRM_IOCTL_SET_CLIENT_CAP, &cap) < 0) fail("cap atomic");
    printf("M19_KMS_CAPS_OK\n");

    /* Discover global KMS object ids instead of assuming that every object is
     * id 1. DRM objects share one namespace, and Asterinas deliberately gives
     * the CRTC, connector, encoder, and plane distinct ids. */
    struct drm_mode_card_res resources = {0};
    if (ioctl(fd, DRM_IOCTL_MODE_GETRESOURCES, &resources) < 0)
        fail("get_resources");
    uint32_t crtc_ids[8] = {0}, connector_ids[8] = {0};
    if (resources.count_crtcs > 8 || resources.count_connectors > 8)
        fail("too_many_kms_objects");
    resources.crtc_id_ptr = (uint64_t)crtc_ids;
    resources.connector_id_ptr = (uint64_t)connector_ids;
    if (ioctl(fd, DRM_IOCTL_MODE_GETRESOURCES, &resources) < 0 ||
        resources.count_crtcs < 1 || resources.count_connectors < 1)
        fail("get_resource_ids");

    struct drm_mode_get_plane_res plane_resources = {0};
    if (ioctl(fd, DRM_IOCTL_MODE_GETPLANERESOURCES, &plane_resources) < 0)
        fail("get_plane_resources");
    uint32_t plane_ids[8] = {0};
    if (plane_resources.count_planes > 8)
        fail("too_many_planes");
    plane_resources.plane_id_ptr = (uint64_t)plane_ids;
    if (ioctl(fd, DRM_IOCTL_MODE_GETPLANERESOURCES, &plane_resources) < 0 ||
        plane_resources.count_planes < 1)
        fail("get_plane_ids");

    const uint32_t crtc_id = crtc_ids[0];
    const uint32_t connector_id = connector_ids[0];
    const uint32_t plane_id = plane_ids[0];
    printf("M19_KMS_IDS crtc=%u connector=%u plane=%u\n",
           crtc_id, connector_id, plane_id);

    /* Connector mode. */
    struct drm_mode_get_connector conn = { .connector_id = connector_id };
    struct drm_mode_modeinfo mode = {0};
    if (ioctl(fd, DRM_IOCTL_MODE_GETCONNECTOR, &conn) < 0) fail("get_connector");
    conn.modes_ptr = (uint64_t)&mode;
    conn.count_modes = 1;
    if (ioctl(fd, DRM_IOCTL_MODE_GETCONNECTOR, &conn) < 0) fail("get_connector mode");
    if (conn.count_modes < 1) fail("no modes");
    uint32_t width = mode.hdisplay, height = mode.vdisplay;
    printf("M19_MODE %ux%u %s\n", width, height, mode.name);

    struct gbm_device *gbm = gbm_create_device(fd);
    if (!gbm) fail("gbm_create_device");
    printf("M19_GBM_BACKEND %s\n", gbm_device_get_backend_name(gbm));

    struct gbm_surface *gsurf = gbm_surface_create(
        gbm, width, height, GBM_FORMAT_XRGB8888,
        GBM_BO_USE_SCANOUT | GBM_BO_USE_RENDERING);
    if (!gsurf) fail("gbm_surface_create");

    EGLDisplay dpy = eglGetPlatformDisplay(EGL_PLATFORM_GBM_KHR, gbm, NULL);
    if (dpy == EGL_NO_DISPLAY) fail("get_platform_display");
    EGLint major = 0, minor = 0;
    if (!eglInitialize(dpy, &major, &minor)) fail("eglInitialize");
    printf("M19_EGL_DISPLAY_OK version=%d.%d vendor=%s\n", major, minor,
           eglQueryString(dpy, EGL_VENDOR));

    if (!eglBindAPI(EGL_OPENGL_ES_API)) fail("bind_api");

    EGLint cfg_attrs[] = {
        EGL_SURFACE_TYPE, EGL_WINDOW_BIT,
        EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT,
        EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8, EGL_ALPHA_SIZE, 8,
        EGL_NONE,
    };
    EGLConfig cfg;
    EGLint ncfg = 0;
    if (!eglChooseConfig(dpy, cfg_attrs, &cfg, 1, &ncfg) || ncfg < 1)
        fail("choose_config");
    printf("M19_EGL_CONFIGS_OK\n");

    EGLContext ctx = eglCreateContext(dpy, cfg, EGL_NO_CONTEXT,
        (EGLint[]){ EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE });
    if (ctx == EGL_NO_CONTEXT) fail("create_context");
    EGLSurface surf = eglCreateWindowSurface(dpy, cfg, (EGLNativeWindowType)gsurf, NULL);
    if (surf == EGL_NO_SURFACE) fail("create_window_surface");
    if (!eglMakeCurrent(dpy, surf, surf, ctx)) fail("make_current");
    printf("M19_EGL_CTX_OK\n");
    printf("M19_GL_VENDOR %s\n", glGetString(GL_VENDOR));
    printf("M19_GL_RENDERER %s\n", glGetString(GL_RENDERER));
    printf("M19_GL_VERSION %s\n", glGetString(GL_VERSION));

    GLuint vs = make_shader(GL_VERTEX_SHADER, VERT_SRC);
    GLuint fs = make_shader(GL_FRAGMENT_SHADER, FRAG_SRC);
    GLuint prog = glCreateProgram();
    glAttachShader(prog, vs);
    glAttachShader(prog, fs);
    glLinkProgram(prog);
    GLint linked = 0;
    glGetProgramiv(prog, GL_LINK_STATUS, &linked);
    if (!linked) fail("link");
    glUseProgram(prog);

    /* KMS property ids. */
    uint32_t p_conn_crtc = find_prop(fd, connector_id, DRM_MODE_OBJECT_CONNECTOR, "CRTC_ID");
    uint32_t p_crtc_active = find_prop(fd, crtc_id, DRM_MODE_OBJECT_CRTC, "ACTIVE");
    uint32_t p_crtc_mode = find_prop(fd, crtc_id, DRM_MODE_OBJECT_CRTC, "MODE_ID");
    uint32_t p_out_fence = find_prop(fd, crtc_id, DRM_MODE_OBJECT_CRTC, "OUT_FENCE_PTR");
    uint32_t p_plane_fb = find_prop(fd, plane_id, DRM_MODE_OBJECT_PLANE, "FB_ID");
    uint32_t p_plane_crtc = find_prop(fd, plane_id, DRM_MODE_OBJECT_PLANE, "CRTC_ID");
    uint32_t p_src_x = find_prop(fd, plane_id, DRM_MODE_OBJECT_PLANE, "SRC_X");
    uint32_t p_src_y = find_prop(fd, plane_id, DRM_MODE_OBJECT_PLANE, "SRC_Y");
    uint32_t p_src_w = find_prop(fd, plane_id, DRM_MODE_OBJECT_PLANE, "SRC_W");
    uint32_t p_src_h = find_prop(fd, plane_id, DRM_MODE_OBJECT_PLANE, "SRC_H");
    uint32_t p_crtc_x = find_prop(fd, plane_id, DRM_MODE_OBJECT_PLANE, "CRTC_X");
    uint32_t p_crtc_y = find_prop(fd, plane_id, DRM_MODE_OBJECT_PLANE, "CRTC_Y");
    uint32_t p_crtc_w = find_prop(fd, plane_id, DRM_MODE_OBJECT_PLANE, "CRTC_W");
    uint32_t p_crtc_h = find_prop(fd, plane_id, DRM_MODE_OBJECT_PLANE, "CRTC_H");
    uint32_t p_in_fence = find_prop(fd, plane_id, DRM_MODE_OBJECT_PLANE, "IN_FENCE_FD");
    if (!p_conn_crtc || !p_crtc_active || !p_crtc_mode || !p_out_fence ||
        !p_plane_fb || !p_plane_crtc || !p_in_fence)
        fail("props");
    printf("M19_EXPLICIT_SYNC_PROPS in=%u out=%u\n", p_in_fence, p_out_fence);

    struct drm_mode_create_blob blob = {
        .data = (uint64_t)&mode, .length = sizeof(mode),
    };
    if (ioctl(fd, DRM_IOCTL_MODE_CREATEPROPBLOB, &blob) < 0) fail("propblob");

    /* Render + present loop. */
    if (width == 0 || height == 0 || width > SIZE_MAX / height ||
        (size_t)width * height > SIZE_MAX / 4)
        fail("pixel_buffer_size");
    size_t pixel_buffer_size = (size_t)width * height * 4;
    uint8_t *pixels = malloc(pixel_buffer_size);
    if (!pixels) fail("pixels");
    uint32_t csums[NUM_FRAMES];
    uint32_t previous_sequence = 0;
    bool have_previous_sequence = false;
    struct gbm_bo *pending_bo = NULL;
    int previous_out_fence = -1;

    for (int frame = 0; frame < NUM_FRAMES; frame++) {
        float t = (float)frame * 0.7f;
        float tri[3][5] = {
            { -0.6f + t * 0.05f, -0.4f, 1, 0, 0 },
            {  0.6f - t * 0.03f, -0.4f, 0, 1, 0 },
            {  0.0f,  0.5f + t * 0.07f, 0, 0, 1 },
        };

        /* PreRead: what does the back buffer still hold from its last use?
         * With double buffering, frame N reuses frame N-2's buffer, so a
         * correct pipeline shows frame N-2's pixels here. */
        glFinish();
        probe("preread", frame, pixels, width, height);

        glViewport(0, 0, width, height);
        glClearColor(0.1f, 0.1f, 0.12f + t * 0.02f, 1.0f);
        glClear(GL_COLOR_BUFFER_BIT);
        glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 5 * sizeof(float), &tri[0][0]);
        glVertexAttribPointer(1, 3, GL_FLOAT, GL_FALSE, 5 * sizeof(float), &tri[0][2]);
        glEnableVertexAttribArray(0);
        glEnableVertexAttribArray(1);
        glDrawArrays(GL_TRIANGLES, 0, 3);
        glFinish();

        /* PostRender: the rendered frame (this is the official checksum). */
        csums[frame] = probe("postrender", frame, pixels, width, height);

        /* ClearProbe: overwrite the whole target with a frame-unique solid
         * color and read back immediately. If this shows the solid color,
         * glReadPixels tracks the current render target correctly (so any
         * staleness above is a data-sync problem in the kernel/host path).
         * If it shows old content, the read path is bound to a stale buffer
         * (a Mesa/GBM buffer-tracking problem). */
        glClearColor(0.2f + 0.2f * frame, 0.05f + 0.2f * frame, 0.3f, 1.0f);
        glClear(GL_COLOR_BUFFER_BIT);
        glFinish();
        probe("clearprobe", frame, pixels, width, height);

        /* Re-render the real frame so the presented image is the triangle. */
        glClearColor(0.1f, 0.1f, 0.12f + t * 0.02f, 1.0f);
        glClear(GL_COLOR_BUFFER_BIT);
        glDrawArrays(GL_TRIANGLES, 0, 3);
        glFinish();

        if (!eglSwapBuffers(dpy, surf)) fail("swap_buffers");

        struct gbm_bo *bo = gbm_surface_lock_front_buffer(gsurf);
        if (!bo) fail("lock_front_buffer");
        uint32_t handle = gbm_bo_get_handle(bo).u32;
        uint32_t stride = gbm_bo_get_stride(bo);
        printf("M19_BO frame=%d handle=%u stride=%u\n", frame, handle, stride);

        struct drm_mode_fb_cmd2 fb2 = {0};
        fb2.width = width; fb2.height = height;
        fb2.pixel_format = 0x34325258; /* XR24 */
        fb2.handles[0] = handle;
        fb2.pitches[0] = stride;
        if (ioctl(fd, DRM_IOCTL_MODE_ADDFB2, &fb2) < 0) fail("addfb2");

        /* Atomic commit: full state on frame 0, FB-only flips after. */
        uint32_t objs[3], counts[3], props[16];
        uint64_t vals[16];
        uint32_t obj_count = 0, prop_count = 0;
        uint32_t out_fence_value_index = UINT32_MAX;
        uint32_t in_fence_value_index = UINT32_MAX;
        uint32_t fb_value_index = UINT32_MAX;
        uint32_t aflags = DRM_MODE_PAGE_FLIP_EVENT | DRM_MODE_ATOMIC_NONBLOCK;
        int out_fence = -1;
        if (frame == 0) {
            aflags |= DRM_MODE_ATOMIC_ALLOW_MODESET;
            objs[obj_count] = connector_id; counts[obj_count++] = 1;
            props[prop_count] = p_conn_crtc; vals[prop_count++] = crtc_id;

            objs[obj_count] = crtc_id; counts[obj_count++] = 3;
            props[prop_count] = p_crtc_active; vals[prop_count++] = 1;
            props[prop_count] = p_crtc_mode; vals[prop_count++] = blob.blob_id;
            props[prop_count] = p_out_fence;
            out_fence_value_index = prop_count;
            vals[prop_count++] = (uint64_t)&out_fence;

            objs[obj_count] = plane_id; counts[obj_count++] = 11;
            props[prop_count] = p_plane_crtc; vals[prop_count++] = crtc_id;
            props[prop_count] = p_src_x; vals[prop_count++] = 0;
            props[prop_count] = p_src_y; vals[prop_count++] = 0;
            props[prop_count] = p_src_w; vals[prop_count++] = (uint64_t)width << 16;
            props[prop_count] = p_src_h; vals[prop_count++] = (uint64_t)height << 16;
            props[prop_count] = p_crtc_x; vals[prop_count++] = 0;
            props[prop_count] = p_crtc_y; vals[prop_count++] = 0;
            props[prop_count] = p_crtc_w; vals[prop_count++] = width;
            props[prop_count] = p_crtc_h; vals[prop_count++] = height;
            props[prop_count] = p_plane_fb;
            fb_value_index = prop_count;
            vals[prop_count++] = fb2.fb_id;
            props[prop_count] = p_in_fence;
            in_fence_value_index = prop_count;
            vals[prop_count++] = (uint64_t)-1;
        } else {
            objs[obj_count] = crtc_id; counts[obj_count++] = 1;
            props[prop_count] = p_out_fence;
            out_fence_value_index = prop_count;
            vals[prop_count++] = (uint64_t)&out_fence;

            objs[obj_count] = plane_id; counts[obj_count++] = 2;
            props[prop_count] = p_plane_fb;
            fb_value_index = prop_count;
            vals[prop_count++] = fb2.fb_id;
            props[prop_count] = p_in_fence;
            in_fence_value_index = prop_count;
            vals[prop_count++] = (uint64_t)previous_out_fence;
        }

        struct drm_mode_atomic at = {
            .flags = aflags,
            .count_objs = obj_count,
            .objs_ptr = (uint64_t)objs,
            .count_props_ptr = (uint64_t)counts,
            .props_ptr = (uint64_t)props,
            .prop_values_ptr = (uint64_t)vals,
            .user_data = (uint64_t)frame + 100,
        };

        if (frame == 0) {
            uint32_t test_flags = (aflags & ~(DRM_MODE_PAGE_FLIP_EVENT |
                                              DRM_MODE_ATOMIC_NONBLOCK)) |
                                  DRM_MODE_ATOMIC_TEST_ONLY;
            at.flags = test_flags;

            vals[out_fence_value_index] = 1;
            errno = 0;
            if (ioctl(fd, DRM_IOCTL_MODE_ATOMIC, &at) >= 0 || errno != EFAULT)
                fail("atomic_bad_out_pointer");
            vals[out_fence_value_index] = (uint64_t)&out_fence;

            vals[fb_value_index] = UINT32_MAX;
            out_fence = 123;
            errno = 0;
            if (ioctl(fd, DRM_IOCTL_MODE_ATOMIC, &at) >= 0 || errno != EINVAL ||
                out_fence != -1)
                fail("atomic_failed_commit_out_fence");
            vals[fb_value_index] = fb2.fb_id;

            vals[in_fence_value_index] = INT32_MAX;
            out_fence = 123;
            errno = 0;
            if (ioctl(fd, DRM_IOCTL_MODE_ATOMIC, &at) >= 0 || errno != EBADF ||
                out_fence != -1)
                fail("atomic_bad_in_fence");
            vals[in_fence_value_index] = (uint64_t)-1;

            out_fence = 123;
            if (ioctl(fd, DRM_IOCTL_MODE_ATOMIC, &at) < 0 || out_fence != -1)
                fail("atomic_fence_test_only");
            printf("M19_EXPLICIT_SYNC_NEGATIVE_OK\n");
            at.flags = aflags;
        }
        if (ioctl(fd, DRM_IOCTL_MODE_ATOMIC, &at) < 0) fail("atomic_commit");
        if (out_fence < 0) fail("atomic_out_fence");
        if (previous_out_fence >= 0)
            close(previous_out_fence);

        struct pollfd fence_poll = { .fd = out_fence, .events = POLLIN };
        int poll_rc = poll(&fence_poll, 1, FENCE_POLL_TIMEOUT_MS);
        if (poll_rc != 1 || !(fence_poll.revents & POLLIN) ||
            (fence_poll.revents & (POLLERR | POLLNVAL)))
            fail("atomic_out_fence_poll");
        printf("M19_EXPLICIT_SYNC frame=%d fd=%d revents=%x\n",
               frame, out_fence, fence_poll.revents);
        previous_out_fence = out_fence;

        /* Wait for the flip event. */
        struct drm_event_vblank ev = {0};
        int rc = read(fd, &ev, sizeof(ev));
        if (rc != (int)sizeof(ev) || ev.type != DRM_EVENT_FLIP_COMPLETE ||
            ev.length != sizeof(ev) || ev.user_data != (uint64_t)frame + 100 ||
            ev.crtc_id != crtc_id ||
            (have_previous_sequence && ev.sequence <= previous_sequence))
            fail("flip_event");
        previous_sequence = ev.sequence;
        have_previous_sequence = true;

        printf("M19_FRAME %d csum=%08x fb=%u seq=%u\n", frame, csums[frame], fb2.fb_id,
               ev.sequence);

        /* Release the previous front buffer now that its flip completed. */
        if (pending_bo) {
            gbm_surface_release_buffer(gsurf, pending_bo);
        }
        pending_bo = bo;
    }

    if (previous_out_fence >= 0)
        close(previous_out_fence);

    for (int i = 0; i < NUM_FRAMES; i++)
        for (int j = i + 1; j < NUM_FRAMES; j++)
            if (csums[i] == csums[j])
                fail("frame_checksums");
    printf("M19_FRAMES_DISTINCT %d\n", NUM_FRAMES);

    printf("M19_EGL_DONE\n");
    return 0;
}
