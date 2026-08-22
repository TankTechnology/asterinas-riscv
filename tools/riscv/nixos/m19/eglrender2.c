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
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

#define NUM_FRAMES 4

/* ---- raw KMS ioctl bits (no libdrm needed) ---- */
#define DRM_IOCTL_BASE 'd'
#define DRM_IOW(nr, type) _IOW(DRM_IOCTL_BASE, nr, type)
#define DRM_IOWR(nr, type) _IOWR(DRM_IOCTL_BASE, nr, type)

#define DRM_IOCTL_SET_MASTER _IO('d', 0x1e)
#define DRM_IOCTL_SET_CLIENT_CAP DRM_IOW(0x0d, struct drm_set_client_cap)
#define DRM_IOCTL_MODE_GETRESOURCES DRM_IOWR(0xa0, struct drm_mode_card_res)
#define DRM_IOCTL_MODE_GETCONNECTOR DRM_IOWR(0xa7, struct drm_mode_get_connector)
#define DRM_IOCTL_MODE_GETPROPERTY DRM_IOWR(0xaa, struct drm_mode_get_property)
#define DRM_IOCTL_MODE_OBJ_GETPROPERTIES DRM_IOWR(0xb9, struct drm_mode_obj_get_properties)
#define DRM_IOCTL_MODE_ADDFB2 DRM_IOWR(0xb8, struct drm_mode_fb_cmd2)
#define DRM_IOCTL_MODE_ATOMIC DRM_IOWR(0xbc, struct drm_mode_atomic)
#define DRM_IOCTL_MODE_CREATEPROPBLOB DRM_IOWR(0xbd, struct drm_mode_create_blob)

#define DRM_MODE_OBJECT_CRTC 0xcccccccc
#define DRM_MODE_OBJECT_CONNECTOR 0xc0c0c0c0
#define DRM_MODE_OBJECT_PLANE 0xeeeeeeee
#define DRM_MODE_ATOMIC_ALLOW_MODESET 0x0400
#define DRM_MODE_PAGE_FLIP_EVENT 0x01
#define DRM_EVENT_FLIP_COMPLETE 0x02

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
    uint32_t flags, count_props;
    uint64_t objs_ptr, count_props_ptr, props_ptr, prop_values_ptr;
    uint64_t blob_id, user_data, reserved, reserved_ptr;
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
static uint32_t find_prop(int fd, uint32_t obj_type, const char *name) {
    struct drm_mode_obj_get_properties q = { .obj_id = 1, .obj_type = obj_type };
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

int main(void) {
    setvbuf(stdout, NULL, _IOLBF, 0);

    int fd = open("/dev/dri/card0", O_RDWR);
    if (fd < 0) fail("open card0");
    if (ioctl(fd, DRM_IOCTL_SET_MASTER, 0) < 0) fail("set_master");
    struct drm_set_client_cap cap = { .capability = 2, .value = 1 }; /* UNIVERSAL_PLANES */
    if (ioctl(fd, DRM_IOCTL_SET_CLIENT_CAP, &cap) < 0) fail("cap planes");
    cap.capability = 3; /* ATOMIC */
    if (ioctl(fd, DRM_IOCTL_SET_CLIENT_CAP, &cap) < 0) fail("cap atomic");
    printf("M19_KMS_CAPS_OK\n");

    /* Connector mode. */
    struct drm_mode_get_connector conn = { .connector_id = 1 };
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
    uint32_t p_conn_crtc = find_prop(fd, DRM_MODE_OBJECT_CONNECTOR, "CRTC_ID");
    uint32_t p_crtc_active = find_prop(fd, DRM_MODE_OBJECT_CRTC, "ACTIVE");
    uint32_t p_crtc_mode = find_prop(fd, DRM_MODE_OBJECT_CRTC, "MODE_ID");
    uint32_t p_plane_fb = find_prop(fd, DRM_MODE_OBJECT_PLANE, "FB_ID");
    uint32_t p_plane_crtc = find_prop(fd, DRM_MODE_OBJECT_PLANE, "CRTC_ID");
    uint32_t p_src_x = find_prop(fd, DRM_MODE_OBJECT_PLANE, "SRC_X");
    uint32_t p_src_y = find_prop(fd, DRM_MODE_OBJECT_PLANE, "SRC_Y");
    uint32_t p_src_w = find_prop(fd, DRM_MODE_OBJECT_PLANE, "SRC_W");
    uint32_t p_src_h = find_prop(fd, DRM_MODE_OBJECT_PLANE, "SRC_H");
    uint32_t p_crtc_x = find_prop(fd, DRM_MODE_OBJECT_PLANE, "CRTC_X");
    uint32_t p_crtc_y = find_prop(fd, DRM_MODE_OBJECT_PLANE, "CRTC_Y");
    uint32_t p_crtc_w = find_prop(fd, DRM_MODE_OBJECT_PLANE, "CRTC_W");
    uint32_t p_crtc_h = find_prop(fd, DRM_MODE_OBJECT_PLANE, "CRTC_H");
    if (!p_conn_crtc || !p_crtc_active || !p_crtc_mode || !p_plane_fb || !p_plane_crtc)
        fail("props");

    struct drm_mode_create_blob blob = {
        .data = (uint64_t)&mode, .length = sizeof(mode),
    };
    if (ioctl(fd, DRM_IOCTL_MODE_CREATEPROPBLOB, &blob) < 0) fail("propblob");

    /* Render + present loop. */
    uint8_t *pixels = malloc(width * height * 4);
    uint32_t csums[NUM_FRAMES];
    struct gbm_bo *pending_bo = NULL;
    uint32_t pending_fb = 0;

    for (int frame = 0; frame < NUM_FRAMES; frame++) {
        float t = (float)frame * 0.7f;
        float tri[3][5] = {
            { -0.6f + t * 0.05f, -0.4f, 1, 0, 0 },
            {  0.6f - t * 0.03f, -0.4f, 0, 1, 0 },
            {  0.0f,  0.5f + t * 0.07f, 0, 0, 1 },
        };
        glViewport(0, 0, width, height);
        glClearColor(0.1f, 0.1f, 0.12f + t * 0.02f, 1.0f);
        glClear(GL_COLOR_BUFFER_BIT);
        glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 5 * sizeof(float), &tri[0][0]);
        glVertexAttribPointer(1, 3, GL_FLOAT, GL_FALSE, 5 * sizeof(float), &tri[0][2]);
        glEnableVertexAttribArray(0);
        glEnableVertexAttribArray(1);
        glDrawArrays(GL_TRIANGLES, 0, 3);
        glFinish();

        glReadPixels(0, 0, width, height, GL_RGBA, GL_UNSIGNED_BYTE, pixels);
        uint32_t csum = 0;
        for (uint32_t i = 0; i < width * height * 4; i += 97)
            csum = csum * 31 + pixels[i];
        csums[frame] = csum;

        if (!eglSwapBuffers(dpy, surf)) fail("swap_buffers");

        struct gbm_bo *bo = gbm_surface_lock_front_buffer(gsurf);
        if (!bo) fail("lock_front_buffer");
        uint32_t handle = gbm_bo_get_handle(bo).u32;
        uint32_t stride = gbm_bo_get_stride(bo);

        struct drm_mode_fb_cmd2 fb2 = {0};
        fb2.width = width; fb2.height = height;
        fb2.pixel_format = 0x34325258; /* XR24 */
        fb2.handles[0] = handle;
        fb2.pitches[0] = stride;
        if (ioctl(fd, DRM_IOCTL_MODE_ADDFB2, &fb2) < 0) fail("addfb2");

        /* Atomic commit: full state on frame 0, FB-only flips after. */
        uint32_t objs[14], props[14];
        uint64_t vals[14];
        uint32_t n = 0;
        uint32_t aflags = DRM_MODE_PAGE_FLIP_EVENT;
        if (frame == 0) {
            aflags |= DRM_MODE_ATOMIC_ALLOW_MODESET;
            objs[n] = 1; props[n] = p_conn_crtc; vals[n] = 1; n++;
            objs[n] = 1; props[n] = p_crtc_active; vals[n] = 1; n++;
            objs[n] = 1; props[n] = p_crtc_mode; vals[n] = blob.blob_id; n++;
            objs[n] = 1; props[n] = p_plane_crtc; vals[n] = 1; n++;
            objs[n] = 1; props[n] = p_src_x; vals[n] = 0; n++;
            objs[n] = 1; props[n] = p_src_y; vals[n] = 0; n++;
            objs[n] = 1; props[n] = p_src_w; vals[n] = (uint64_t)width << 16; n++;
            objs[n] = 1; props[n] = p_src_h; vals[n] = (uint64_t)height << 16; n++;
            objs[n] = 1; props[n] = p_crtc_x; vals[n] = 0; n++;
            objs[n] = 1; props[n] = p_crtc_y; vals[n] = 0; n++;
            objs[n] = 1; props[n] = p_crtc_w; vals[n] = width; n++;
            objs[n] = 1; props[n] = p_crtc_h; vals[n] = height; n++;
        }
        objs[n] = 1; props[n] = p_plane_fb; vals[n] = fb2.fb_id; n++;

        struct drm_mode_atomic at = {
            .flags = aflags,
            .count_props = n,
            .objs_ptr = (uint64_t)objs,
            .props_ptr = (uint64_t)props,
            .prop_values_ptr = (uint64_t)vals,
            .user_data = (uint64_t)frame + 100,
        };
        if (ioctl(fd, DRM_IOCTL_MODE_ATOMIC, &at) < 0) fail("atomic_commit");

        /* Wait for the flip event. */
        struct drm_event_vblank ev = {0};
        int rc = read(fd, &ev, sizeof(ev));
        if (rc != (int)sizeof(ev) || ev.type != DRM_EVENT_FLIP_COMPLETE ||
            ev.user_data != (uint64_t)frame + 100)
            fail("flip_event");

        printf("M19_FRAME %d csum=%08x fb=%u seq=%u\n", frame, csum, fb2.fb_id,
               ev.sequence);

        /* Release the previous front buffer now that its flip completed. */
        if (pending_bo) {
            gbm_surface_release_buffer(gsurf, pending_bo);
        }
        pending_bo = bo;
        pending_fb = fb2.fb_id;

        if (frame == NUM_FRAMES - 1) {
            FILE *out = fopen("/m19_frame.ppm", "wb");
            if (out) {
                fprintf(out, "P6\n%u %u\n255\n", width, height);
                for (uint32_t y = 0; y < height; y++)
                    for (uint32_t x = 0; x < width; x++) {
                        uint8_t *p = pixels + ((height - 1 - y) * width + x) * 4;
                        fputc(p[0], out); fputc(p[1], out); fputc(p[2], out);
                    }
                fclose(out);
                printf("M19_FRAME_SAVED /m19_frame.ppm %u\n", width * height * 3 + 17);
            }
        }
    }

    int distinct = 0;
    for (int i = 1; i < NUM_FRAMES; i++)
        if (csums[i] != csums[0]) distinct++;
    printf("M19_FRAMES_DISTINCT %d\n", distinct);

    printf("M19_EGL_DONE\n");
    return 0;
}
