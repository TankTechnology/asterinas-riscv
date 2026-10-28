// SPDX-License-Identifier: MPL-2.0
//
// Minimal Wayland client for the Asterinas RISC-V framebuffer chain.
//
// Connects to the demo compositor, allocates a memfd-backed shared-memory
// buffer, fills it with a color-bar test pattern, and submits it as a surface
// buffer via the tiny wl_shm protocol. The compositor then blits it to /dev/fb0.

#define _GNU_SOURCE
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

static int sock_fd;

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

/* Send a request built in `w`, without an fd. */
static void request(WlWriter *w, uint32_t object_id, uint16_t opcode) {
    wl_put_header(w, object_id, opcode);
    if (wl_send_msg(sock_fd, wl_writer_data(w), wl_writer_len(w), -1) < 0) {
        die("sendmsg");
    }
    /* Give the synchronous demo compositor time to drain each message. A real
     * Wayland stack parses a SOCK_STREAM byte stream; this demo paces the
     * client so messages do not coalesce. */
    usleep(10000);
}

static void connect_display(void) {
    sock_fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (sock_fd < 0) {
        die("socket");
    }
    struct sockaddr_un addr;
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, SOCK_PATH, sizeof(addr.sun_path) - 1);
    if (connect(sock_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        die("connect");
    }
}

/* Fill the shared buffer with three horizontal color bars (R, G, B). */
static void fill_pattern(unsigned char *buf) {
    for (int y = 0; y < DISP_H; y++) {
        unsigned char r, g, b;
        if (y < DISP_H / 3) {
            r = 255; g = 0;   b = 0;     /* red */
        } else if (y < DISP_H * 2 / 3) {
            r = 0;   g = 255; b = 0;     /* green */
        } else {
            r = 0;   g = 0;   b = 255;   /* blue */
        }
        for (int x = 0; x < DISP_W; x++) {
            unsigned char *p = buf + (size_t)y * DISP_W * 4 + (size_t)x * 4;
            p[0] = r; /* R (XRGB8888) */
            p[1] = g; /* G */
            p[2] = b; /* B */
            p[3] = 0; /* X */
        }
    }
}

int client_main(void) {
    connect_display();
    tty_log("client: connected");

    /* wl_display.get_registry(new_id = OBJ_REGISTRY) */
    WlWriter w;
    wl_writer_init(&w);
    wl_put_u32(&w, OBJ_REGISTRY);
    request(&w, WL_DISPLAY_ID, REQ_DISPLAY_GET_REGISTRY);

    /* Drain the two wl_registry.global events (compositor, shm). SOCK_STREAM
     * may coalesce the two globals into one recv, so parse message boundaries
     * from the header size field rather than assuming one recv == one message. */
    {
        unsigned char buf[WL_MSG_MAX];
        size_t buf_len = 0, buf_pos = 0;
        int globals = 0;
        while (globals < 2) {
            if (buf_pos >= buf_len) {
                ssize_t n = recv(sock_fd, buf, sizeof(buf), 0);
                if (n <= 0) {
                    die("recv global");
                }
                buf_len = (size_t)n;
                buf_pos = 0;
            }
            uint32_t sz_op;
            memcpy(&sz_op, buf + buf_pos + 4, sizeof(sz_op));
            uint16_t size = (uint16_t)(sz_op >> 16);
            if (size == 0 || buf_pos + size > buf_len) {
                die("bad global size");
            }
            globals++;
            buf_pos += size;
        }
    }
    tty_log("client: registry globals received");

    /* Bind the compositor and shm globals (ids are fixed in this demo). */
    wl_writer_init(&w);
    wl_put_u32(&w, OBJ_COMPOSITOR);
    wl_put_str(&w, WL_IFACE_COMPOSITOR);
    wl_put_u32(&w, 1);
    wl_put_u32(&w, OBJ_COMPOSITOR);
    request(&w, OBJ_REGISTRY, REQ_REGISTRY_BIND);

    wl_writer_init(&w);
    wl_put_u32(&w, OBJ_SHM);
    wl_put_str(&w, WL_IFACE_SHM);
    wl_put_u32(&w, 1);
    wl_put_u32(&w, OBJ_SHM);
    request(&w, OBJ_REGISTRY, REQ_REGISTRY_BIND);

    /* Allocate a memfd-backed shared buffer and fill it. */
    int memfd = memfd_create("wayland-buffer", MFD_CLOEXEC);
    if (memfd < 0) {
        die("memfd_create");
    }
    size_t buf_size = (size_t)DISP_W * DISP_H * 4;
    if (ftruncate(memfd, (off_t)buf_size) < 0) {
        die("ftruncate");
    }
    unsigned char *buf = mmap(NULL, buf_size, PROT_READ | PROT_WRITE, MAP_SHARED, memfd, 0);
    if (buf == MAP_FAILED) {
        die("mmap buffer");
    }
    fill_pattern(buf);
    tty_log("client: buffer filled");

    /* wl_shm.create_pool(new_id = OBJ_SHM_POOL, size, fd) */
    wl_writer_init(&w);
    wl_put_u32(&w, OBJ_SHM_POOL);
    wl_put_u32(&w, (uint32_t)buf_size);
    wl_put_fd_placeholder(&w);
    wl_put_header(&w, OBJ_SHM, REQ_SHM_CREATE_POOL);
    if (wl_send_msg(sock_fd, wl_writer_data(&w), wl_writer_len(&w), memfd) < 0) {
        die("send create_pool");
    }
    usleep(10000);
    tty_log("client: shm pool sent");

    /* wl_shm_pool.create_buffer(new_id = OBJ_BUFFER, offset, w, h, stride, format) */
    wl_writer_init(&w);
    wl_put_u32(&w, OBJ_BUFFER);
    wl_put_u32(&w, 0);                          /* offset */
    wl_put_u32(&w, DISP_W);                     /* width */
    wl_put_u32(&w, DISP_H);                     /* height */
    wl_put_u32(&w, DISP_W * 4);                 /* stride */
    wl_put_u32(&w, WL_SHM_FORMAT_XRGB8888);     /* format */
    request(&w, OBJ_SHM_POOL, REQ_SHM_POOL_CREATE_BUFFER);

    /* wl_compositor.create_surface(new_id = OBJ_SURFACE) */
    wl_writer_init(&w);
    wl_put_u32(&w, OBJ_SURFACE);
    request(&w, OBJ_COMPOSITOR, REQ_COMPOSITOR_CREATE_SURFACE);

    /* wl_surface.attach(buffer = OBJ_BUFFER, x = 0, y = 0) */
    wl_writer_init(&w);
    wl_put_u32(&w, OBJ_BUFFER);
    wl_put_u32(&w, 0);
    wl_put_u32(&w, 0);
    request(&w, OBJ_SURFACE, REQ_SURFACE_ATTACH);

    /* wl_surface.commit() */
    wl_writer_init(&w);
    request(&w, OBJ_SURFACE, REQ_SURFACE_COMMIT);

    /* wl_display.sync(new_id = OBJ_CALLBACK) and wait for done. */
    wl_writer_init(&w);
    wl_put_u32(&w, OBJ_CALLBACK);
    request(&w, WL_DISPLAY_ID, REQ_DISPLAY_SYNC);

    WlReader r;
    int rcv_fd;
    if (wl_recv_msg(sock_fd, &r, &rcv_fd) < 0) {
        die("recv callback");
    }
    tty_log("client: buffer committed and acknowledged");

    /* Keep the process alive briefly so the framebuffer can be captured. */
    sleep(30);
    return 0;
}
