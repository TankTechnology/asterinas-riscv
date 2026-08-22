// DRM-M16: Pure software-rendered rotating triangle via DRM KMS dumb buffers.
//
// Uses only the 2D DRM KMS path (CREATE_DUMB, MAP_DUMB, ADDFB,
// SETCRTC, PAGE_FLIP) that is already verified working.  Every frame is
// CPU-rasterised into the dumb buffer and presented via PAGE_FLIP.
//
// No libdrm needed — self-contained raw ioctl() calls.

#define _GNU_SOURCE
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>
#include <math.h>

/* ---- DRM ioctl defs ---- */
#define DRM_IOCTL_BASE 'd'
#define DRM_IOR(nr,type)  _IOR(DRM_IOCTL_BASE, nr, type)
#define DRM_IOW(nr,type)  _IOW(DRM_IOCTL_BASE, nr, type)
#define DRM_IOWR(nr,type) _IOWR(DRM_IOCTL_BASE, nr, type)

struct drm_mode_modeinfo {
    uint32_t clock;
    uint16_t hdisplay, hsync_start, hsync_end, htotal, hskew;
    uint16_t vdisplay, vsync_start, vsync_end, vtotal, vscan;
    uint32_t vrefresh;
    uint32_t flags;
    uint32_t type;
    char     name[32];
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
    uint32_t fb_id;
    uint32_t width, height;
    uint32_t pitch;
    uint32_t bpp;
    uint32_t depth;
    uint32_t handle;
};

struct drm_mode_crtc_page_flip {
    uint32_t crtc_id;
    uint32_t fb_id;
    uint32_t flags;
    uint32_t reserved;
    uint64_t user_data;
};

struct drm_mode_destroy_dumb {
    uint32_t handle;
};

#define DRM_IOCTL_VERSION          DRM_IOWR(0x00, struct drm_version)
#define DRM_IOCTL_MODE_CREATE_DUMB DRM_IOWR(0xb2, struct drm_mode_create_dumb)
#define DRM_IOCTL_MODE_MAP_DUMB    DRM_IOWR(0xb3, struct drm_mode_map_dumb)
#define DRM_IOCTL_MODE_ADDFB       DRM_IOWR(0xae, struct drm_mode_fb_cmd)
#define DRM_IOCTL_MODE_SETCRTC     DRM_IOWR(0xa2, struct drm_mode_crtc)
#define DRM_IOCTL_MODE_PAGE_FLIP   DRM_IOWR(0xb0, struct drm_mode_crtc_page_flip)
#define DRM_IOCTL_MODE_DESTROY_DUMB DRM_IOWR(0xb4, struct drm_mode_destroy_dumb)

#define SCREEN_W 640
#define SCREEN_H 480
#define BPP 32

/* ---- triangle vertex data ---- */
typedef struct { float x, y, r, g, b; } Vertex;

static const Vertex TRI[3] = {
    {  0.0f,  0.8f, 1.0f, 0.0f, 0.0f },  /* top: red */
    { -0.8f, -0.6f, 0.0f, 1.0f, 0.0f },  /* bottom-left: green */
    {  0.8f, -0.6f, 0.0f, 0.0f, 1.0f },  /* bottom-right: blue */
};

static inline float clampf(float v, float lo, float hi) {
    return v < lo ? lo : (v > hi ? hi : v);
}

static inline uint32_t pack_rgba(int r, int g, int b) {
    return ((uint32_t)b) | ((uint32_t)g << 8) | ((uint32_t)r << 16) | 0xff000000u;
}

/* ---- edge-function rasteriser ---- */
static void draw_triangle(uint32_t *fb, int w, int h, float angle, int frame) {
    /* rotate vertices */
    float c = cosf(angle), s = sinf(angle);
    float x[3], y[3], cr[3], cg[3], cb[3];
    for (int i = 0; i < 3; i++) {
        x[i]  = TRI[i].x * c - TRI[i].y * s;
        y[i]  = TRI[i].x * s + TRI[i].y * c;
        cr[i] = TRI[i].r;
        cg[i] = TRI[i].g;
        cb[i] = TRI[i].b;
    }

    /* NDC -> screen */
    float sx[3], sy[3];
    for (int i = 0; i < 3; i++) {
        sx[i] = (0.5f + x[i] * 0.45f) * (float)w;
        sy[i] = (0.5f - y[i] * 0.45f) * (float)h;  /* flip Y */
    }

    /* bounding box */
    int minx = (int)clampf(fminf(fminf(sx[0], sx[1]), sx[2]), 0, w-1);
    int maxx = (int)clampf(fmaxf(fmaxf(sx[0], sx[1]), sx[2]), 0, w-1);
    int miny = (int)clampf(fminf(fminf(sy[0], sy[1]), sy[2]), 0, h-1);
    int maxy = (int)clampf(fmaxf(fmaxf(sy[0], sy[1]), sy[2]), 0, h-1);

    /* edge function area */
    float area = (sx[1]-sx[0])*(sy[2]-sy[0]) - (sx[2]-sx[0])*(sy[1]-sy[0]);
    if (fabsf(area) < 0.5f) return;
    float inv_area = 1.0f / area;

    for (int py = miny; py <= maxy; py++) {
        for (int px = minx; px <= maxx; px++) {
            float w0 = ((sx[1]-sx[2])*(py-sy[2]) + (sy[2]-sy[1])*(px-sx[2])) * inv_area;
            float w1 = ((sx[2]-sx[0])*(py-sy[0]) + (sy[0]-sy[2])*(px-sx[0])) * inv_area;
            float w2 = 1.0f - w0 - w1;
            if (w0 < -0.001f || w1 < -0.001f || w2 < -0.001f) continue;

            int r = (int)((cr[0]*w0 + cr[1]*w1 + cr[2]*w2) * 255.0f);
            int g = (int)((cg[0]*w0 + cg[1]*w1 + cg[2]*w2) * 255.0f);
            int b = (int)((cb[0]*w0 + cb[1]*w1 + cb[2]*w2) * 255.0f);
            fb[py * w + px] = pack_rgba(r, g, b);
        }
    }
}

int main(void) {
    setvbuf(stdout, NULL, _IOLBF, 0);

    int fd = open("/dev/dri/card0", O_RDWR);
    if (fd < 0) { perror("open /dev/dri/card0"); return 1; }
    printf("M16_DRM_OPEN_OK fd=%d\n", fd);

    /* ---- create dumb buffer ---- */
    struct drm_mode_create_dumb cdumb = {0};
    cdumb.width  = SCREEN_W;
    cdumb.height = SCREEN_H;
    cdumb.bpp    = BPP;
    if (ioctl(fd, DRM_IOCTL_MODE_CREATE_DUMB, &cdumb) < 0) {
        perror("CREATE_DUMB"); return 1;
    }
    printf("M16_DUMB_CREATED w=%u h=%u bpp=%u pitch=%u size=%llu handle=%u\n",
           cdumb.width, cdumb.height, cdumb.bpp, cdumb.pitch,
           (unsigned long long)cdumb.size, cdumb.handle);

    /* ---- map dumb buffer ---- */
    struct drm_mode_map_dumb mdumb = { .handle = cdumb.handle };
    if (ioctl(fd, DRM_IOCTL_MODE_MAP_DUMB, &mdumb) < 0) {
        perror("MAP_DUMB"); return 1;
    }
    uint32_t *fb = mmap(NULL, cdumb.size, PROT_READ|PROT_WRITE,
                        MAP_SHARED, fd, mdumb.offset);
    if (fb == MAP_FAILED) { perror("mmap"); return 1; }
    printf("M16_DUMB_MAPPED addr=%p\n", (void*)fb);

    /* ---- add framebuffer ---- */
    struct drm_mode_fb_cmd fcmd = {0};
    fcmd.width  = SCREEN_W;
    fcmd.height = SCREEN_H;
    fcmd.pitch  = cdumb.pitch;
    fcmd.bpp    = BPP;
    fcmd.depth  = 24;
    fcmd.handle = cdumb.handle;
    if (ioctl(fd, DRM_IOCTL_MODE_ADDFB, &fcmd) < 0) {
        perror("ADDFB"); return 1;
    }
    printf("M16_FB_ADDED fb_id=%u\n", fcmd.fb_id);

    /* ---- set CRTC ---- */
    struct drm_mode_crtc crtc = {0};
    crtc.crtc_id = 1;   /* our single CRTC (kernel CRTC_ID) */
    crtc.fb_id   = fcmd.fb_id;
    crtc.mode_valid = 1;
    crtc.mode.hdisplay = SCREEN_W;
    crtc.mode.vdisplay = SCREEN_H;
    if (ioctl(fd, DRM_IOCTL_MODE_SETCRTC, &crtc) < 0) {
        perror("SETCRTC"); return 1;
    }
    printf("M16_CRTC_SET\n");

    /* ---- render loop: 720 frames (12s @ 60fps) ---- */
    printf("M16_FRAME_START\n");
    for (int frame = 0; frame < 720; frame++) {
        float angle = (float)frame * 0.02792527f;  /* ~1.6 deg per frame */

        /* clear to dark gray */
        for (int i = 0; i < (int)(cdumb.size/4); i++)
            fb[i] = 0xff202020u;

        draw_triangle(fb, SCREEN_W, (int)cdumb.height, angle, frame);

        /* page flip */
        struct drm_mode_crtc_page_flip pf = {0};
        pf.crtc_id = 1;
        pf.fb_id   = fcmd.fb_id;
        if (ioctl(fd, DRM_IOCTL_MODE_PAGE_FLIP, &pf) < 0) {
            perror("PAGE_FLIP"); return 1;
        }

        if (frame % 60 == 0) {
            printf("M16_FRAME %d angle=%.1f\n", frame, angle * 180.0f / M_PI);
        }

        usleep(16667); /* ~60fps */
    }
    printf("M16_FRAME_END nframes=720\n");

    /* ---- cleanup ---- */
    struct drm_mode_destroy_dumb dd = { .handle = cdumb.handle };
    ioctl(fd, DRM_IOCTL_MODE_DESTROY_DUMB, &dd);
    close(fd);

    printf("M16_DONE\n");
    return 0;
}
