// SPDX-License-Identifier: MPL-2.0
//
// Minimal Wayland compositor for the Asterinas RISC-V framebuffer chain.
//
// The compositor maps /dev/fb0, listens on an AF_UNIX socket, forks the demo
// client, then handles the tiny Wayland-core + wl_shm protocol needed to
// receive a shared-memory buffer (via SCM_RIGHTS) and blit it to the screen.

#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include "protocol.h"
#include "wire.h"

#define SOCK_PATH "/tmp/wayland-demo.sock"

static unsigned char *fb;
static const int fb_stride = DISP_W * 4;

/* The client's shared-memory buffer. */
static int shm_fd = -1;
static unsigned char *shm_map;
static size_t shm_size;

/* The surface's attached buffer (offset/stride/dims within the pool). */
static int surface_has_buffer;
static uint32_t buffer_offset;
static uint32_t buffer_stride;
static uint32_t buffer_width;
static uint32_t buffer_height;

int client_main(void); /* defined in client.c */

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
        /* No framebuffer (e.g. host-side test): use a private buffer. */
        fb = malloc((size_t)DISP_W * DISP_H * 4);
        if (!fb) {
            die("malloc fb");
        }
        return;
    }
    fb = mmap(NULL, DISP_W * DISP_H * 4, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (fb == MAP_FAILED) {
        die("mmap fb0");
    }
    close(fd);
}

/* Announce a global object to the client's registry. */
static void send_global(int fd, uint32_t name, const char *iface, uint32_t version) {
    WlWriter w;
    wl_writer_init(&w);
    wl_put_u32(&w, name);      /* name */
    wl_put_str(&w, iface);     /* interface */
    wl_put_u32(&w, version);   /* version */
    wl_put_header(&w, OBJ_REGISTRY, EVT_REGISTRY_GLOBAL);
    wl_send_msg(fd, wl_writer_data(&w), wl_writer_len(&w), -1);
}

/* Send a wl_callback.done event (completes a display.sync round-trip). */
static void send_callback_done(int fd, uint32_t callback_id) {
    WlWriter w;
    wl_writer_init(&w);
    wl_put_u32(&w, 0); /* callback data (unused) */
    wl_put_header(&w, callback_id, EVT_CALLBACK_DONE);
    wl_send_msg(fd, wl_writer_data(&w), wl_writer_len(&w), -1);
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

static void handle_msg(int fd, WlReader *r, int rcv_fd) {
    uint32_t object_id = wl_get_u32(r);
    uint32_t sz_op = wl_get_u32(r);
    uint16_t opcode = (uint16_t)(sz_op >> 16);

    if (object_id == WL_DISPLAY_ID) {
        if (opcode == REQ_DISPLAY_GET_REGISTRY) {
            /* new_id wl_registry — client allocated OBJ_REGISTRY. */
            (void)wl_get_u32(r);
            send_global(fd, OBJ_COMPOSITOR, WL_IFACE_COMPOSITOR, 1);
            send_global(fd, OBJ_SHM, WL_IFACE_SHM, 1);
        } else if (opcode == REQ_DISPLAY_SYNC) {
            /* new_id wl_callback */
            uint32_t callback_id = wl_get_u32(r);
            send_callback_done(fd, callback_id);
        }
    } else if (object_id == OBJ_REGISTRY && opcode == REQ_REGISTRY_BIND) {
        /* name, interface, version, new_id — ignore, we announce fixed ids. */
        (void)wl_get_u32(r);
        (void)wl_get_str(r);
        (void)wl_get_u32(r);
        (void)wl_get_u32(r);
    } else if (object_id == OBJ_SHM && opcode == REQ_SHM_CREATE_POOL) {
        /* new_id wl_shm_pool, size, fd */
        (void)wl_get_u32(r);
        uint32_t size = wl_get_u32(r);
        (void)wl_get_u32(r); /* fd placeholder */
        if (rcv_fd >= 0) {
            shm_fd = rcv_fd;
            shm_size = size;
            shm_map = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, shm_fd, 0);
            if (shm_map == MAP_FAILED) {
                tty_log("compositor: mmap shm pool failed");
                shm_map = NULL;
            } else {
                tty_log("compositor: received shm pool");
            }
        } else {
            tty_log("compositor: create_pool without fd");
        }
    } else if (object_id == OBJ_SHM_POOL && opcode == REQ_SHM_POOL_CREATE_BUFFER) {
        /* new_id wl_buffer, offset, width, height, stride, format */
        (void)wl_get_u32(r);
        buffer_offset = wl_get_u32(r);
        buffer_width = wl_get_u32(r);
        buffer_height = wl_get_u32(r);
        buffer_stride = wl_get_u32(r);
        (void)wl_get_u32(r); /* format */
        surface_has_buffer = 1;
    } else if (object_id == OBJ_SURFACE && opcode == REQ_SURFACE_ATTACH) {
        /* buffer (object or null), x, y */
        (void)wl_get_u32(r);
        (void)wl_get_u32(r);
        (void)wl_get_u32(r);
    } else if (object_id == OBJ_SURFACE && opcode == REQ_SURFACE_COMMIT) {
        if (surface_has_buffer && shm_map) {
            render_buffer();
        }
    }
}

static void compositor_loop(int listen_fd) {
    int client_fd = accept(listen_fd, NULL, NULL);
    if (client_fd < 0) {
        die("accept");
    }
    tty_log("compositor: client connected");

    for (;;) {
        WlReader r;
        int rcv_fd;
        if (wl_recv_msg(client_fd, &r, &rcv_fd) < 0) {
            break;
        }
        handle_msg(client_fd, &r, rcv_fd);
    }
}

int main(void) {
    /* Smoke-test marker so the U-Boot booti flow reports userspace readiness. */
    int marker_fd = open("/dev/ttyS0", O_WRONLY);
    if (marker_fd >= 0) {
        const char *marker = ">>> Hello from RISC-V userspace on Asterinas! <<<\n";
        write(marker_fd, marker, strlen(marker));
        close(marker_fd);
    }

    init_fb();
    tty_log("compositor: framebuffer mapped");

    unlink(SOCK_PATH);
    int listen_fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (listen_fd < 0) {
        die("socket");
    }
    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, SOCK_PATH, sizeof(addr.sun_path) - 1);
    if (bind(listen_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        die("bind");
    }
    if (listen(listen_fd, 1) < 0) {
        die("listen");
    }
    tty_log("compositor: listening");

    pid_t pid = fork();
    if (pid == 0) {
        close(listen_fd);
        return client_main();
    }

    compositor_loop(listen_fd);
    return 0;
}
