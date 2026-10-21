// SPDX-License-Identifier: MPL-2.0

// A minimal raw-X11 client that paints the root window a solid colour.
//
// It speaks the protocol directly so it needs no libX11 and no headers, which
// makes it usable as a static binary inside a minimal Debian image that has an
// X server but not `xsetroot` (x11-xserver-utils). The request sequence is the
// one `xsetroot -solid red` produces: `ChangeWindowAttributes` setting the root
// window's background pixel, then `ClearArea` over the whole root.
//
// Usage: x11-paint [DISPLAY_NUMBER] [PIXEL_VALUE]
// Prints XPAINT_* markers and exits 0 on success.

#define _GNU_SOURCE

#include <errno.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#define X11_RED 0x00ff0000u

static int fd;

static void on_alarm(int unused) {
    (void)unused;
    printf("XPAINT_TIMEOUT\n");
    _exit(3);
}

static int read_exact(void *buffer, size_t length) {
    size_t got = 0;
    while (got < length) {
        ssize_t count = read(fd, (char *)buffer + got, length - got);
        if (count <= 0) {
            printf("XPAINT_READ_FAIL errno=%d\n", errno);
            return -1;
        }
        got += (size_t)count;
    }
    return 0;
}

static int write_exact(const void *buffer, size_t length) {
    size_t sent = 0;
    while (sent < length) {
        ssize_t count = write(fd, (const char *)buffer + sent, length - sent);
        if (count <= 0) {
            printf("XPAINT_WRITE_FAIL errno=%d\n", errno);
            return -1;
        }
        sent += (size_t)count;
    }
    return 0;
}

static uint32_t load_le32(const unsigned char *bytes) {
    return (uint32_t)bytes[0] | ((uint32_t)bytes[1] << 8) |
           ((uint32_t)bytes[2] << 16) | ((uint32_t)bytes[3] << 24);
}

static void store_le32(unsigned char *bytes, uint32_t value) {
    bytes[0] = (unsigned char)value;
    bytes[1] = (unsigned char)(value >> 8);
    bytes[2] = (unsigned char)(value >> 16);
    bytes[3] = (unsigned char)(value >> 24);
}

int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IONBF, 0);

    unsigned display = 0;
    uint32_t pixel = X11_RED;
    if (argc > 1) {
        display = (unsigned)strtoul(argv[1], NULL, 10);
    }
    if (argc > 2) {
        pixel = (uint32_t)strtoul(argv[2], NULL, 0);
    }

    signal(SIGALRM, on_alarm);
    alarm(180);

    fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) {
        printf("XPAINT_FAIL socket errno=%d\n", errno);
        return 1;
    }
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    snprintf(address.sun_path, sizeof(address.sun_path),
             "/tmp/.X11-unix/X%u", display);
    if (connect(fd, (struct sockaddr *)&address, sizeof(address)) != 0) {
        printf("XPAINT_FAIL connect errno=%d\n", errno);
        return 1;
    }

    // Connection setup: little-endian, protocol 11.0.
    unsigned char request[12] = {'l', 0, 11, 0, 0, 0, 0, 0, 0, 0, 0, 0};
    if (write_exact(request, sizeof(request)) != 0) {
        return 1;
    }
    unsigned char header[8];
    if (read_exact(header, sizeof(header)) != 0) {
        return 1;
    }
    if (header[0] != 1) {
        printf("XPAINT_FAIL setup status=%u\n", header[0]);
        return 1;
    }
    // The additional-data length is in 4-byte units.
    size_t add_length = ((size_t)header[6] | ((size_t)header[7] << 8)) * 4;
    unsigned char *additional = calloc(1, add_length);
    if (additional == NULL || read_exact(additional, add_length) != 0) {
        printf("XPAINT_FAIL setup data\n");
        return 1;
    }

    unsigned vendor_length = additional[16] | (additional[17] << 8);
    unsigned formats = additional[21];
    size_t screen_offset =
        32 + (((size_t)vendor_length + 3) & ~(size_t)3) + (size_t)formats * 8;
    if (screen_offset + 40 > add_length) {
        printf("XPAINT_FAIL screen offset\n");
        return 1;
    }
    uint32_t root = load_le32(additional + screen_offset);
    unsigned root_depth = additional[screen_offset + 38];
    printf("XPAINT_SETUP root=0x%x depth=%u\n", root, root_depth);

    // ChangeWindowAttributes: window, value-mask CWBackPixel = 2, pixel value.
    unsigned char change[16] = {2, 0, 4, 0};
    store_le32(change + 4, root);
    store_le32(change + 8, 2);
    store_le32(change + 12, pixel);
    if (write_exact(change, sizeof(change)) != 0) {
        return 1;
    }

    // ClearArea: opcode 61, exposures = 0, length 4, window, x = y = w = h = 0.
    unsigned char clear[16] = {61, 0, 4, 0};
    store_le32(clear + 4, root);
    if (write_exact(clear, sizeof(clear)) != 0) {
        return 1;
    }

    // GetInputFocus: a round trip proves the server processed the requests.
    unsigned char focus[4] = {43, 0, 1, 0};
    if (write_exact(focus, sizeof(focus)) != 0) {
        return 1;
    }
    unsigned char reply[32];
    if (read_exact(reply, sizeof(reply)) != 0) {
        return 1;
    }
    // A reply's first byte is 1; its second byte is request-specific detail,
    // not the opcode, so only the reply type is checked here.
    if (reply[0] != 1) {
        printf("XPAINT_FAIL reply type=%u detail=%u\n", reply[0], reply[1]);
        return 1;
    }

    printf("XPAINT_DONE pixel=0x%06x\n", pixel);
    return 0;
}
