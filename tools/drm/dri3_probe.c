// SPDX-License-Identifier: MPL-2.0

// A DRI3 buffer-exchange probe.
//
// It connects to an X server, requires the DRI3 extension, exports a
// DRM dumb buffer as a dma-buf, creates a pixmap from that descriptor with
// `DRI3PixmapFromBuffer`, and verifies with `GetGeometry` that the server
// really imported the buffer. That is the server side of the DRI3 handshake
// that a compositor performs; no libX11 or libxcb is involved.
//
// Usage: dri3-probe [DISPLAY_NUMBER]
// Prints DRI3PROBE_* markers and exits 0 on success.

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <unistd.h>

#define DRM_IOCTL_BASE 'd'

struct drm_mode_create_dumb {
    uint32_t height, width, bpp, flags;
    uint32_t handle, pitch;
    uint64_t size;
};

struct drm_prime_handle {
    uint32_t handle;
    uint32_t flags;
    int32_t fd;
};

#define DRM_IOCTL_MODE_CREATE_DUMB \
    _IOWR(DRM_IOCTL_BASE, 0xb2, struct drm_mode_create_dumb)
#define DRM_IOCTL_PRIME_HANDLE_TO_FD \
    _IOWR(DRM_IOCTL_BASE, 0x2d, struct drm_prime_handle)

#define DRM_CLOEXEC 0x1

#define X11_QUERY_EXTENSION 98
#define X11_GET_GEOMETRY 14
#define DRI3_PIXMAP_FROM_BUFFER 2

#define PIXMAP_WIDTH 256
#define PIXMAP_HEIGHT 256

static int xfd = -1;
static unsigned resource_base = 0;

static void on_alarm(int unused) {
    (void)unused;
    printf("DRI3PROBE_TIMEOUT\n");
    _exit(3);
}

static int read_exact(int fd, void *buffer, size_t length) {
    size_t got = 0;
    while (got < length) {
        ssize_t count = read(fd, (char *)buffer + got, length - got);
        if (count <= 0) {
            return -1;
        }
        got += (size_t)count;
    }
    return 0;
}

static int write_exact(int fd, const void *buffer, size_t length) {
    size_t sent = 0;
    while (sent < length) {
        ssize_t count = write(fd, (const char *)buffer + sent, length - sent);
        if (count <= 0) {
            return -1;
        }
        sent += (size_t)count;
    }
    return 0;
}

static void put_u32(unsigned char *p, uint32_t value) {
    p[0] = (unsigned char)value;
    p[1] = (unsigned char)(value >> 8);
    p[2] = (unsigned char)(value >> 16);
    p[3] = (unsigned char)(value >> 24);
}

static uint32_t get_u32(const unsigned char *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
           ((uint32_t)p[3] << 24);
}

static void fail(const char *step) {
    printf("DRI3PROBE_FAIL %s errno=%d\n", step, errno);
    exit(1);
}

// Waits for a reply with the given sequence number; any X error fails.
static int wait_for_reply(uint16_t sequence, unsigned char *reply) {
    for (;;) {
        unsigned char packet[32];
        if (read_exact(xfd, packet, sizeof(packet)) != 0) {
            return -1;
        }
        unsigned type = packet[0] & 0x7f;
        uint16_t packet_sequence = (uint16_t)(packet[2] | (packet[3] << 8));
        if (type == 0) {
            printf("DRI3PROBE_XERROR code=%u seq=%u\n", packet[1],
                   packet_sequence);
            return -1;
        }
        if (type == 1 && packet_sequence == sequence) {
            memcpy(reply, packet, sizeof(packet));
            return 0;
        }
    }
}

int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IONBF, 0);
    signal(SIGALRM, on_alarm);
    alarm(60);

    unsigned display = argc > 1 ? (unsigned)strtoul(argv[1], NULL, 10) : 0;

    // --- X connection setup ---
    xfd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (xfd < 0) {
        fail("socket");
    }
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    snprintf(address.sun_path, sizeof(address.sun_path), "/tmp/.X11-unix/X%u",
             display);
    if (connect(xfd, (struct sockaddr *)&address, sizeof(address)) != 0) {
        fail("connect");
    }
    unsigned char setup_request[12] = {'l', 0, 11, 0, 0, 0, 0, 0, 0, 0, 0, 0};
    if (write_exact(xfd, setup_request, sizeof(setup_request)) != 0) {
        fail("setup-write");
    }
    unsigned char header[8];
    if (read_exact(xfd, header, sizeof(header)) != 0 || header[0] != 1) {
        fail("setup-reply");
    }
    size_t add_length = ((size_t)header[6] | ((size_t)header[7] << 8)) * 4;
    unsigned char *additional = calloc(1, add_length);
    if (additional == NULL || read_exact(xfd, additional, add_length) != 0) {
        fail("setup-data");
    }
    resource_base = get_u32(additional + 4);
    unsigned vendor_length = additional[16] | (additional[17] << 8);
    unsigned formats = additional[21];
    size_t screen_offset =
        32 + (((size_t)vendor_length + 3) & ~(size_t)3) + (size_t)formats * 8;
    uint32_t root = get_u32(additional + screen_offset);
    uint32_t pixmap = resource_base + 1;
    printf("DRI3PROBE_SETUP root=0x%x pixmap=0x%x\n", root, pixmap);

    // --- DRI3 presence and version ---
    unsigned char query[12] = {X11_QUERY_EXTENSION, 0, 3, 0, 4, 0, 0, 0,
                               'D', 'R', 'I', '3'};
    if (write_exact(xfd, query, sizeof(query)) != 0) {
        fail("query-extension");
    }
    unsigned char reply[32];
    if (wait_for_reply(1, reply) != 0) {
        fail("query-extension-reply");
    }
    unsigned present = reply[8];
    unsigned major_opcode = reply[9];
    if (!present) {
        printf("DRI3PROBE_FAIL extension-absent\n");
        return 1;
    }

    unsigned char version[12] = {0, 0, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0};
    version[0] = (unsigned char)major_opcode;
    put_u32(version + 4, 1);
    put_u32(version + 8, 2);
    if (write_exact(xfd, version, sizeof(version)) != 0) {
        fail("query-version");
    }
    if (wait_for_reply(2, reply) != 0) {
        fail("query-version-reply");
    }
    unsigned server_major = get_u32(reply + 8);
    unsigned server_minor = get_u32(reply + 12);
    printf("DRI3PROBE_VERSION %u.%u opcode=%u\n", server_major, server_minor,
           major_opcode);

    // --- Export a dma-buf from the DRM node ---
    int card = open("/dev/dri/card0", O_RDWR | O_CLOEXEC);
    if (card < 0) {
        fail("open-card");
    }
    struct drm_mode_create_dumb dumb = {
        .width = PIXMAP_WIDTH,
        .height = PIXMAP_HEIGHT,
        .bpp = 32,
    };
    if (ioctl(card, DRM_IOCTL_MODE_CREATE_DUMB, &dumb) != 0) {
        fail("create-dumb");
    }
    struct drm_prime_handle prime = {
        .handle = dumb.handle,
        .flags = DRM_CLOEXEC,
    };
    if (ioctl(card, DRM_IOCTL_PRIME_HANDLE_TO_FD, &prime) != 0) {
        fail("prime-export");
    }
    printf("DRI3PROBE_EXPORT pitch=%u fd=%d\n", dumb.pitch, prime.fd);

    // --- DRI3PixmapFromBuffer ---
    // Header (4) + pixmap (4) + drawable (4) + size (4) + width/height/stride
    // (2 each) + depth/bpp (1 each) = 24 bytes, length 6. The descriptor
    // travels in SCM_RIGHTS and takes no room in the request body; Xorg's
    // handler size-checks the request against `xDRI3PixmapFromBufferReq`.
    unsigned char from_buffer[24] = {0};
    from_buffer[0] = (unsigned char)major_opcode;
    from_buffer[1] = DRI3_PIXMAP_FROM_BUFFER;
    from_buffer[2] = 6;
    put_u32(from_buffer + 4, pixmap);
    put_u32(from_buffer + 8, root);
    put_u32(from_buffer + 12, PIXMAP_HEIGHT * dumb.pitch); // size
    from_buffer[16] = (unsigned char)PIXMAP_WIDTH;
    from_buffer[17] = (unsigned char)(PIXMAP_WIDTH >> 8);
    from_buffer[18] = (unsigned char)PIXMAP_HEIGHT;
    from_buffer[19] = (unsigned char)(PIXMAP_HEIGHT >> 8);
    from_buffer[20] = (unsigned char)dumb.pitch;
    from_buffer[21] = (unsigned char)(dumb.pitch >> 8);
    from_buffer[22] = 24; // depth
    from_buffer[23] = 32; // bpp

    {
        struct msghdr msg = {0};
        struct iovec iov = {.iov_base = from_buffer, .iov_len = sizeof(from_buffer)};
        msg.msg_iov = &iov;
        msg.msg_iovlen = 1;
        union {
            struct cmsghdr header;
            char buffer[CMSG_SPACE(sizeof(int))];
        } control;
        memset(&control, 0, sizeof(control));
        msg.msg_control = control.buffer;
        msg.msg_controllen = sizeof(control.buffer);
        struct cmsghdr *cmsg = CMSG_FIRSTHDR(&msg);
        cmsg->cmsg_level = SOL_SOCKET;
        cmsg->cmsg_type = SCM_RIGHTS;
        cmsg->cmsg_len = CMSG_LEN(sizeof(int));
        memcpy(CMSG_DATA(cmsg), &prime.fd, sizeof(prime.fd));
        if (sendmsg(xfd, &msg, 0) < 0) {
            fail("pixmap-from-buffer-send");
        }
    }
    close(prime.fd);
    close(card);

    // --- Verify the pixmap exists with the buffer's geometry ---
    unsigned char geometry[8] = {X11_GET_GEOMETRY, 0, 2, 0, 0, 0, 0, 0};
    put_u32(geometry + 4, pixmap);
    if (write_exact(xfd, geometry, sizeof(geometry)) != 0) {
        fail("get-geometry");
    }
    if (wait_for_reply(4, reply) != 0) {
        printf("DRI3PROBE_FAIL pixmap-not-imported\n");
        return 1;
    }
    unsigned depth = reply[1];
    unsigned width = (unsigned)(reply[16] | (reply[17] << 8));
    unsigned height = (unsigned)(reply[18] | (reply[19] << 8));
    printf("DRI3PROBE_PIXMAP %ux%u depth=%u\n", width, height, depth);
    if (width != PIXMAP_WIDTH || height != PIXMAP_HEIGHT || depth != 24) {
        printf("DRI3PROBE_FAIL geometry\n");
        return 1;
    }

    printf("DRI3PROBE_PASS\n");
    return 0;
}
