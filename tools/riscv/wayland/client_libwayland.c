// SPDX-License-Identifier: MPL-2.0
//
// Real Wayland client using libwayland-client (cross-compiled), connecting to
// the demo compositor. Replaces the hand-written wire client in client.c.

#define _GNU_SOURCE
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

#include <wayland-client.h>
#include <wayland-client-protocol.h>

#include "protocol.h"

#define SOCK_PATH "/tmp/wayland-demo.sock"

static struct wl_compositor *compositor;
static struct wl_shm *shm;

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

static void registry_global(void *data, struct wl_registry *registry,
                            uint32_t name, const char *interface, uint32_t version) {
    (void)data;
    (void)version;
    if (strcmp(interface, wl_compositor_interface.name) == 0) {
        compositor = wl_registry_bind(registry, name, &wl_compositor_interface, 1);
        tty_log("client: bound wl_compositor");
    } else if (strcmp(interface, wl_shm_interface.name) == 0) {
        shm = wl_registry_bind(registry, name, &wl_shm_interface, 1);
        tty_log("client: bound wl_shm");
    }
}

static void registry_global_remove(void *data, struct wl_registry *registry, uint32_t name) {
    (void)data;
    (void)registry;
    (void)name;
}

static const struct wl_registry_listener registry_listener = {
    registry_global,
    registry_global_remove,
};

static void fill_pattern(unsigned char *buf) {
    for (int y = 0; y < DISP_H; y++) {
        unsigned char r, g, b;
        if (y < DISP_H / 3) {
            r = 255; g = 0;   b = 0;
        } else if (y < DISP_H * 2 / 3) {
            r = 0;   g = 255; b = 0;
        } else {
            r = 0;   g = 0;   b = 255;
        }
        for (int x = 0; x < DISP_W; x++) {
            unsigned char *p = buf + (size_t)y * DISP_W * 4 + (size_t)x * 4;
            p[0] = r;
            p[1] = g;
            p[2] = b;
            p[3] = 0;
        }
    }
}

int client_main(void) {
    struct wl_display *display = wl_display_connect(SOCK_PATH);
    if (!display) {
        tty_log("client: wl_display_connect failed");
        return 1;
    }
    tty_log("client: connected via libwayland");

    tty_log("client: before get_registry");
    struct wl_registry *registry = wl_display_get_registry(display);
    tty_log("client: after get_registry");
    wl_registry_add_listener(registry, &registry_listener, NULL);
    tty_log("client: before roundtrip");
    wl_display_roundtrip(display);
    tty_log("client: registry roundtrip done");

    if (!compositor || !shm) {
        tty_log("client: missing compositor/shm global");
        return 1;
    }

    size_t buf_size = (size_t)DISP_W * DISP_H * 4;
    int fd = memfd_create("wayland-buffer", MFD_CLOEXEC);
    if (fd < 0) {
        tty_log("client: memfd_create failed");
        return 1;
    }
    ftruncate(fd, (off_t)buf_size);
    unsigned char *buf = mmap(NULL, buf_size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (buf == MAP_FAILED) {
        tty_log("client: mmap buffer failed");
        return 1;
    }
    fill_pattern(buf);

    struct wl_shm_pool *pool = wl_shm_create_pool(shm, fd, (int32_t)buf_size);
    struct wl_buffer *buffer =
        wl_shm_pool_create_buffer(pool, 0, DISP_W, DISP_H, DISP_W * 4, WL_SHM_FORMAT_XRGB8888);
    struct wl_surface *surface = wl_compositor_create_surface(compositor);
    wl_surface_attach(surface, buffer, 0, 0);
    wl_surface_commit(surface);
    tty_log("client: surface committed");

    wl_display_roundtrip(display);
    tty_log("client: roundtrip after commit");

    sleep(30);
    return 0;
}
