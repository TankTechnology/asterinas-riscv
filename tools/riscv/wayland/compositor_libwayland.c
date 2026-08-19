// SPDX-License-Identifier: MPL-2.0
//
// Minimal Wayland compositor using libwayland-server, for the Asterinas RISC-V
// framebuffer chain. Maps /dev/fb0, advertises wl_compositor + wl_shm, forks
// the demo client, and blits the client's committed wl_shm buffer to the screen.

#define _GNU_SOURCE
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

#include <wayland-server.h>
#include <wayland-server-protocol.h>

#include "protocol.h"

#define SOCK_PATH "/tmp/wayland-demo.sock"

static struct wl_display *display;

static unsigned char *fb;
static const int fb_stride = DISP_W * 4;

/* Single-client demo state. */
static unsigned char *shm_map;
static size_t shm_size;
static int surface_has_buffer;
static uint32_t buffer_offset;
static uint32_t buffer_stride;
static uint32_t buffer_width;
static uint32_t buffer_height;

int client_main(void); /* defined in client.c or client_libwayland.c */

static void die(const char *msg) {
    perror(msg);
    exit(1);
}

static void tty_log(const char *s) {
    int fd = open("/dev/ttyS0", O_WRONLY);
    if (fd >= 0) {
        write(fd, s, strlen(s));
        write(fd, "\n", 1);
        close(fd);
    } else {
        fprintf(stderr, "%s\n", s);
    }
}

static void init_fb(void) {
    int fd = open("/dev/fb0", O_RDWR);
    if (fd < 0) {
        fb = malloc((size_t)DISP_W * DISP_H * 4);
        if (!fb) {
            die("malloc fb");
        }
        return;
    }
    fb = mmap(NULL, (size_t)DISP_W * DISP_H * 4, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (fb == MAP_FAILED) {
        die("mmap fb0");
    }
    close(fd);
}

static void render_buffer(void) {
    for (uint32_t y = 0; y < buffer_height; y++) {
        for (uint32_t x = 0; x < buffer_width; x++) {
            const unsigned char *src =
                shm_map + buffer_offset + (size_t)y * buffer_stride + (size_t)x * 4;
            unsigned char *dst = fb + (size_t)y * fb_stride + (size_t)x * 4;
            /* XRGB8888 (R,G,B,X) -> x8r8g8b8 (B,G,R,X) */
            dst[0] = src[2];
            dst[1] = src[1];
            dst[2] = src[0];
            dst[3] = 0;
        }
    }
    tty_log("compositor: rendered buffer to /dev/fb0");
}

/* -------------------------------------------------------------------------
 * wl_surface
 * ------------------------------------------------------------------------- */

static void surface_destroy(struct wl_client *client, struct wl_resource *resource) {
    (void)client;
    wl_resource_destroy(resource);
}

static void surface_attach(struct wl_client *client, struct wl_resource *resource,
                           struct wl_resource *buffer, int32_t x, int32_t y) {
    (void)client;
    (void)resource;
    (void)buffer;
    (void)x;
    (void)y;
}

static void surface_damage(struct wl_client *client, struct wl_resource *resource,
                           int32_t x, int32_t y, int32_t width, int32_t height) {
    (void)client;
    (void)resource;
    (void)x;
    (void)y;
    (void)width;
    (void)height;
}

static void surface_frame(struct wl_client *client, struct wl_resource *resource, uint32_t callback) {
    (void)client;
    (void)resource;
    (void)callback;
}

static void surface_commit(struct wl_client *client, struct wl_resource *resource) {
    (void)client;
    (void)resource;
    if (surface_has_buffer && shm_map) {
        render_buffer();
    }
}

static const struct wl_surface_interface surface_impl = {
    .destroy = surface_destroy,
    .attach = surface_attach,
    .damage = surface_damage,
    .frame = surface_frame,
    .commit = surface_commit,
};

/* -------------------------------------------------------------------------
 * wl_compositor
 * ------------------------------------------------------------------------- */

static void compositor_create_surface(struct wl_client *client,
                                      struct wl_resource *resource, uint32_t id) {
    (void)resource;
    struct wl_resource *surface =
        wl_resource_create(client, &wl_surface_interface, wl_resource_get_version(resource), id);
    wl_resource_set_implementation(surface, &surface_impl, NULL, NULL);
    tty_log("compositor: created surface");
}

static void compositor_create_region(struct wl_client *client,
                                     struct wl_resource *resource, uint32_t id) {
    (void)client;
    (void)resource;
    (void)id;
}

static void compositor_release(struct wl_client *client, struct wl_resource *resource) {
    (void)client;
    wl_resource_destroy(resource);
}

static const struct wl_compositor_interface compositor_impl = {
    .create_surface = compositor_create_surface,
    .create_region = compositor_create_region,
    .release = compositor_release,
};

static void compositor_bind(struct wl_client *client, void *data, uint32_t version, uint32_t id) {
    (void)data;
    struct wl_resource *resource = wl_resource_create(client, &wl_compositor_interface, version, id);
    wl_resource_set_implementation(resource, &compositor_impl, NULL, NULL);
}

/* -------------------------------------------------------------------------
 * wl_shm / wl_shm_pool / wl_buffer
 * ------------------------------------------------------------------------- */

static void buffer_destroy(struct wl_client *client, struct wl_resource *resource) {
    (void)client;
    wl_resource_destroy(resource);
}

static const struct wl_buffer_interface buffer_impl = {
    .destroy = buffer_destroy,
};

static void shm_pool_create_buffer(struct wl_client *client, struct wl_resource *resource,
                                   uint32_t id, int32_t offset, int32_t width, int32_t height,
                                   int32_t stride, uint32_t format) {
    (void)client;
    (void)format;
    buffer_offset = (uint32_t)offset;
    buffer_width = (uint32_t)width;
    buffer_height = (uint32_t)height;
    buffer_stride = (uint32_t)stride;
    surface_has_buffer = 1;
    struct wl_resource *buffer =
        wl_resource_create(client, &wl_buffer_interface, wl_resource_get_version(resource), id);
    wl_resource_set_implementation(buffer, &buffer_impl, NULL, NULL);
}

static void shm_pool_destroy(struct wl_client *client, struct wl_resource *resource) {
    (void)client;
    wl_resource_destroy(resource);
}

static const struct wl_shm_pool_interface shm_pool_impl = {
    .create_buffer = shm_pool_create_buffer,
    .destroy = shm_pool_destroy,
};

static void shm_create_pool(struct wl_client *client, struct wl_resource *resource,
                            uint32_t id, int32_t fd, int32_t size) {
    (void)client;
    (void)resource;
    shm_map = mmap(NULL, (size_t)size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (shm_map == MAP_FAILED) {
        tty_log("compositor: mmap shm pool failed");
        shm_map = NULL;
    } else {
        shm_size = (size_t)size;
        tty_log("compositor: received shm pool");
    }
    close(fd);
    /* Create the wl_shm_pool resource so the client's object id stays valid. */
    struct wl_resource *pool =
        wl_resource_create(client, &wl_shm_pool_interface, wl_resource_get_version(resource), id);
    wl_resource_set_implementation(pool, &shm_pool_impl, NULL, NULL);
}

static void shm_release(struct wl_client *client, struct wl_resource *resource) {
    (void)client;
    wl_resource_destroy(resource);
}

static const struct wl_shm_interface shm_impl = {
    .create_pool = shm_create_pool,
    .release = shm_release,
};

static void shm_bind(struct wl_client *client, void *data, uint32_t version, uint32_t id) {
    (void)data;
    struct wl_resource *resource = wl_resource_create(client, &wl_shm_interface, version, id);
    wl_resource_set_implementation(resource, &shm_impl, NULL, NULL);
}

int main(void) {
    int marker_fd = open("/dev/ttyS0", O_WRONLY);
    if (marker_fd >= 0) {
        const char *marker = ">>> Hello from RISC-V userspace on Asterinas! <<<\n";
        write(marker_fd, marker, strlen(marker));
        close(marker_fd);
    }

    init_fb();
    tty_log("compositor: framebuffer mapped");

    display = wl_display_create();
    if (!display) {
        die("wl_display_create");
    }
    if (wl_display_add_socket(display, SOCK_PATH) < 0) {
        die("wl_display_add_socket");
    }
    wl_global_create(display, &wl_compositor_interface, 1, NULL, compositor_bind);
    wl_global_create(display, &wl_shm_interface, 1, NULL, shm_bind);
    tty_log("compositor: listening");

    pid_t pid = fork();
    if (pid == 0) {
        return client_main();
    }

    wl_display_run(display);
    wl_display_destroy(display);
    return 0;
}
