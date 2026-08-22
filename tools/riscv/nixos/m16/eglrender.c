// DRM-M16 end-to-end virgl verification client.
//
// Opens the kernel's DRM render node through GBM, creates an EGL/GLES2
// context (which on a virgl-backed virtio-gpu device makes Mesa's gallium
// `virtio_gpu` driver talk to the host virglrenderer through the kernel's
// 3D ioctls), renders a series of deterministic frames, reads the pixels
// back and emits machine-checkable evidence on stdout:
//
//   M16_EGL_DISPLAY_OK / M16_EGL_CTX_OK      - EGL bring-up milestones
//   M16_GL_VENDOR/RENDERER/VERSION           - GL string evidence (virgl)
//   M16_FRAME <n> csum=<hex> distinct=<n>    - per-frame pixel checksums
//   M16_FRAME_SAVED <path> <bytes>           - final frame dumped as PPM
//   M16_EGL_DONE                             - success sentinel
//
// Any failure exits with M16_EGL_FAIL <stage> on stdout and a nonzero code.

#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES2/gl2.h>
#include <gbm.h>

#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define WIDTH 256
#define HEIGHT 256
#define NUM_FRAMES 4

static void fail(const char *stage) {
    printf("M16_EGL_FAIL %s\n", stage);
    fflush(stdout);
    exit(1);
}

static const char *VERT_SRC =
    "attribute vec2 pos;\n"
    "attribute vec3 col;\n"
    "varying vec3 vcol;\n"
    "uniform float angle;\n"
    "void main() {\n"
    "    float c = cos(angle), s = sin(angle);\n"
    "    gl_Position = vec4(pos.x * c - pos.y * s, pos.x * s + pos.y * c, 0.0, 1.0);\n"
    "    vcol = col;\n"
    "}\n";

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
    if (!ok) {
        char log[512];
        glGetShaderInfoLog(sh, sizeof(log), NULL, log);
        fprintf(stderr, "shader compile: %s\n", log);
        fail("shader");
    }
    return sh;
}

/* Dump loaded shared libraries containing "dri" for driver identification. */
static void dump_dri_maps(const char *tag) {
    FILE *f = fopen("/proc/self/maps", "r");
    if (!f) return;
    char line[512];
    printf("M16_MAPS_BEGIN %s\n", tag);
    while (fgets(line, sizeof(line), f)) {
        if (strstr(line, ".so") && (strstr(line, "dri") || strstr(line, "gbm") || strstr(line, "pipe"))) {
            char *p = strchr(line, '/');
            if (p) printf("M16_MAP %s", p);
        }
    }
    printf("M16_MAPS_END\n");
    fclose(f);
}

int main(void) {
    setvbuf(stdout, NULL, _IOLBF, 0);

    int fd = open("/dev/dri/renderD128", O_RDWR);
    if (fd < 0) {
        perror("open renderD128, trying card0");
        fd = open("/dev/dri/card0", O_RDWR);
    }
    if (fd < 0) fail("open");

    struct gbm_device *gbm = gbm_create_device(fd);
    if (!gbm) fail("gbm_create_device");
    printf("M16_GBM_BACKEND %s\n", gbm_device_get_backend_name(gbm));
    dump_dri_maps("after_gbm");

    EGLDisplay dpy =
        eglGetPlatformDisplay(EGL_PLATFORM_GBM_KHR, gbm, NULL);
    if (dpy == EGL_NO_DISPLAY) fail("get_platform_display");
    EGLint major = 0, minor = 0;
    if (!eglInitialize(dpy, &major, &minor)) fail("eglInitialize");
    printf("M16_EGL_DISPLAY_OK version=%d.%d vendor=%s\n", major, minor,
           eglQueryString(dpy, EGL_VENDOR));
    dump_dri_maps("after_init");

    if (!eglBindAPI(EGL_OPENGL_ES_API)) fail("bind_api");

    EGLint cfg_attrs[] = {
        EGL_SURFACE_TYPE, EGL_PBUFFER_BIT,
        EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT,
        EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8, EGL_ALPHA_SIZE, 8,
        EGL_NONE,
    };
    EGLConfig cfg;
    EGLint ncfg = 0;
    if (!eglChooseConfig(dpy, cfg_attrs, &cfg, 1, &ncfg) || ncfg < 1)
        fail("choose_config");

    EGLint ctx_attrs[] = {EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE};
    EGLContext ctx = eglCreateContext(dpy, cfg, EGL_NO_CONTEXT, ctx_attrs);
    if (ctx == EGL_NO_CONTEXT) fail("create_context");

    EGLint pb_attrs[] = {EGL_WIDTH, WIDTH, EGL_HEIGHT, HEIGHT, EGL_NONE};
    EGLSurface surf = eglCreatePbufferSurface(dpy, cfg, pb_attrs);
    if (surf == EGL_NO_SURFACE) fail("create_pbuffer");
    if (!eglMakeCurrent(dpy, surf, surf, ctx)) fail("make_current");
    printf("M16_EGL_CTX_OK\n");

    printf("M16_GL_VENDOR %s\n", glGetString(GL_VENDOR));
    printf("M16_GL_RENDERER %s\n", glGetString(GL_RENDERER));
    printf("M16_GL_VERSION %s\n", glGetString(GL_VERSION));

    GLuint prog = glCreateProgram();
    glAttachShader(prog, make_shader(GL_VERTEX_SHADER, VERT_SRC));
    glAttachShader(prog, make_shader(GL_FRAGMENT_SHADER, FRAG_SRC));
    glLinkProgram(prog);
    GLint linked = 0;
    glGetProgramiv(prog, GL_LINK_STATUS, &linked);
    if (!linked) fail("link");
    glUseProgram(prog);

    /* An equilateral triangle with per-vertex RGB; rotates each frame. */
    GLfloat verts[9] = {0.0f, 0.8f, 0.0f, -0.8f, -0.6f, 0.0f, 0.8f, -0.6f, 0.0f};
    GLfloat cols[9] = {1.0f, 0.0f, 0.0f, 0.0f, 1.0f, 0.0f, 0.0f, 0.0f, 1.0f};
    GLint apos = glGetAttribLocation(prog, "pos");
    GLint acol = glGetAttribLocation(prog, "col");
    GLint uangle = glGetUniformLocation(prog, "angle");
    glEnableVertexAttribArray(apos);
    glVertexAttribPointer(apos, 2, GL_FLOAT, GL_FALSE, 0, verts);
    glEnableVertexAttribArray(acol);
    glVertexAttribPointer(acol, 3, GL_FLOAT, GL_FALSE, 0, cols);

    uint8_t *pixels = malloc(WIDTH * HEIGHT * 4);
    uint8_t *last = malloc(WIDTH * HEIGHT * 4);
    if (!pixels || !last) fail("malloc");

    for (int frame = 0; frame < NUM_FRAMES; frame++) {
        /* Background color cycles per frame: dark red/green/blue/yellow. */
        static const GLfloat bg[NUM_FRAMES][4] = {
            {0.25f, 0.0f, 0.0f, 1.0f},
            {0.0f, 0.25f, 0.0f, 1.0f},
            {0.0f, 0.0f, 0.25f, 1.0f},
            {0.25f, 0.25f, 0.0f, 1.0f},
        };
        glClearColor(bg[frame][0], bg[frame][1], bg[frame][2], bg[frame][3]);
        glClear(GL_COLOR_BUFFER_BIT);
        glUniform1f(uangle, (float)frame * 0.7853982f); /* 45 deg steps */
        glDrawArrays(GL_TRIANGLES, 0, 3);
        glFinish();

        glReadPixels(0, 0, WIDTH, HEIGHT, GL_RGBA, GL_UNSIGNED_BYTE, pixels);
        if (glGetError() != GL_NO_ERROR) fail("readpixels");

        uint64_t csum = 0;
        uint32_t distinct = 0;
        uint32_t seen[16] = {0}; /* tiny bloom-ish distinct counter */
        for (size_t i = 0; i < (size_t)WIDTH * HEIGHT * 4; i++)
            csum = csum * 131 + pixels[i];
        for (size_t i = 0; i < (size_t)WIDTH * HEIGHT; i++) {
            uint32_t px;
            memcpy(&px, pixels + i * 4, 4);
            int found = 0;
            for (uint32_t k = 0; k < distinct && k < 16; k++)
                if (seen[k] == px) { found = 1; break; }
            if (!found && distinct < 16) seen[distinct++] = px;
        }
        printf("M16_FRAME %d csum=%016llx distinct_ge=%u\n", frame,
               (unsigned long long)csum, distinct);
        if (frame == NUM_FRAMES - 1) memcpy(last, pixels, (size_t)WIDTH * HEIGHT * 4);
    }

    FILE *out = fopen("/m16_frame.ppm", "wb");
    if (!out) fail("fopen_ppm");
    fprintf(out, "P6\n%d %d\n255\n", WIDTH, HEIGHT);
    for (int y = 0; y < HEIGHT; y++)
        for (int x = 0; x < WIDTH; x++) {
            /* GL origin is bottom-left; flip for the image. */
            const uint8_t *p = last + ((size_t)(HEIGHT - 1 - y) * WIDTH + x) * 4;
            fputc(p[0], out);
            fputc(p[1], out);
            fputc(p[2], out);
        }
    fclose(out);
    printf("M16_FRAME_SAVED /m16_frame.ppm %d\n", WIDTH * HEIGHT * 3 + 17);

    eglDestroySurface(dpy, surf);
    eglDestroyContext(dpy, ctx);
    eglTerminate(dpy);
    gbm_device_destroy(gbm);
    close(fd);
    printf("M16_EGL_DONE\n");
    return 0;
}