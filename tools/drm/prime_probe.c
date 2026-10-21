// SPDX-License-Identifier: MPL-2.0

// Acceptance probe for DRM PRIME / dma-buf support.
//
//   * exports a dumb buffer as a dma-buf descriptor,
//   * maps the descriptor and sees the bytes written through the GEM mapping,
//   * imports the descriptor into a second DRM file and maps that handle,
//   * passes the descriptor to a child process over a unix socket
//     (`SCM_RIGHTS`) and has the child import and map it,
//   * checks the negative cases: a non-dma-buf fd and a closed fd.
//
// As PID 1 it mounts devtmpfs first, so it can be used as a one-file
// initramfs `/init`, like `virtio_gpu_probe.c`.

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/mount.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#define DRM_IOCTL_BASE 'd'

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

struct drm_prime_handle {
    uint32_t handle;
    uint32_t flags;
    int32_t fd;
};

struct drm_get_cap {
    uint64_t capability;
    uint64_t value;
};

#define DRM_IOCTL_GET_CAP _IOWR(DRM_IOCTL_BASE, 0x0c, struct drm_get_cap)
#define DRM_IOCTL_MODE_CREATE_DUMB \
    _IOWR(DRM_IOCTL_BASE, 0xb2, struct drm_mode_create_dumb)
#define DRM_IOCTL_MODE_MAP_DUMB \
    _IOWR(DRM_IOCTL_BASE, 0xb3, struct drm_mode_map_dumb)
#define DRM_IOCTL_PRIME_HANDLE_TO_FD \
    _IOWR(DRM_IOCTL_BASE, 0x2d, struct drm_prime_handle)
#define DRM_IOCTL_PRIME_FD_TO_HANDLE \
    _IOWR(DRM_IOCTL_BASE, 0x2e, struct drm_prime_handle)

#define DRM_CAP_PRIME 0x5
#define DRM_PRIME_CAP_IMPORT 0x1
#define DRM_PRIME_CAP_EXPORT 0x2
#define DRM_CLOEXEC 0x1

#define WIDTH 64
#define HEIGHT 64

static int open_card(void) {
    int fd = open("/dev/dri/card0", O_RDWR | O_CLOEXEC);
    if (fd < 0) {
        printf("PRIMEPROBE_FAIL open-card errno=%d\n", errno);
        exit(1);
    }
    return fd;
}

static int check_pattern(const uint8_t *buffer, size_t size) {
    for (size_t i = 0; i < size; i += 4096) {
        uint8_t expected = (uint8_t)(0x40 + (i / 4096) % 0x3f);
        if (buffer[i] != expected) {
            printf("PRIMEPROBE_PATTERN_MISMATCH page=%zu got=0x%02x want=0x%02x\n",
                   i / 4096, buffer[i], expected);
            return -1;
        }
    }
    return 0;
}

static void fill_pattern(uint8_t *buffer, size_t size) {
    for (size_t i = 0; i < size; i += 4096) {
        buffer[i] = (uint8_t)(0x40 + (i / 4096) % 0x3f);
    }
}

static uint8_t *map_handle(int fd, uint32_t handle, size_t size) {
    struct drm_mode_map_dumb map = {.handle = handle};
    if (ioctl(fd, DRM_IOCTL_MODE_MAP_DUMB, &map) != 0) {
        return MAP_FAILED;
    }
    return mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd,
                (off_t)map.offset);
}

static int send_fd(int socket_fd, int fd) {
    struct msghdr msg = {0};
    struct iovec iov;
    char byte = 'x';
    iov.iov_base = &byte;
    iov.iov_len = 1;
    msg.msg_iov = &iov;
    msg.msg_iovlen = 1;

    union {
        struct cmsghdr header;
        char buffer[CMSG_SPACE(sizeof(int))];
    } control;
    memset(&control, 0, sizeof(control));
    msg.msg_control = control.buffer;
    msg.msg_controllen = sizeof(control.buffer);

    struct cmsghdr *header = CMSG_FIRSTHDR(&msg);
    header->cmsg_level = SOL_SOCKET;
    header->cmsg_type = SCM_RIGHTS;
    header->cmsg_len = CMSG_LEN(sizeof(int));
    memcpy(CMSG_DATA(header), &fd, sizeof(fd));

    return sendmsg(socket_fd, &msg, 0) < 0 ? -1 : 0;
}

static int receive_fd(int socket_fd) {
    struct msghdr msg = {0};
    struct iovec iov;
    char byte;
    iov.iov_base = &byte;
    iov.iov_len = 1;
    msg.msg_iov = &iov;
    msg.msg_iovlen = 1;

    union {
        struct cmsghdr header;
        char buffer[CMSG_SPACE(sizeof(int))];
    } control;
    memset(&control, 0, sizeof(control));
    msg.msg_control = control.buffer;
    msg.msg_controllen = sizeof(control.buffer);

    if (recvmsg(socket_fd, &msg, 0) < 0) {
        return -1;
    }
    struct cmsghdr *header = CMSG_FIRSTHDR(&msg);
    if (header == NULL || header->cmsg_type != SCM_RIGHTS) {
        return -1;
    }
    int fd;
    memcpy(&fd, CMSG_DATA(header), sizeof(fd));
    return fd;
}

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);

    if (getpid() == 1) {
        mkdir("/dev", 0755);
        if (mount("devtmpfs", "/dev", "devtmpfs", 0, NULL) != 0 && errno != EBUSY) {
            printf("PRIMEPROBE_FAIL mount-devtmpfs errno=%d\n", errno);
            return 1;
        }
    }

    int exporter = open_card();

    struct drm_get_cap cap = {.capability = DRM_CAP_PRIME};
    if (ioctl(exporter, DRM_IOCTL_GET_CAP, &cap) != 0) {
        printf("PRIMEPROBE_FAIL get-cap errno=%d\n", errno);
        return 1;
    }
    printf("PRIMEPROBE_CAP prime=0x%llx\n", (unsigned long long)cap.value);
    if (cap.value != (DRM_PRIME_CAP_IMPORT | DRM_PRIME_CAP_EXPORT)) {
        printf("PRIMEPROBE_FAIL cap value\n");
        return 1;
    }

    struct drm_mode_create_dumb dumb = {
        .width = WIDTH,
        .height = HEIGHT,
        .bpp = 32,
    };
    if (ioctl(exporter, DRM_IOCTL_MODE_CREATE_DUMB, &dumb) != 0) {
        printf("PRIMEPROBE_FAIL create-dumb errno=%d\n", errno);
        return 1;
    }
    printf("PRIMEPROBE_DUMB handle=%u pitch=%u size=%llu\n", dumb.handle,
           dumb.pitch, (unsigned long long)dumb.size);

    uint8_t *gem = map_handle(exporter, dumb.handle, dumb.size);
    if (gem == MAP_FAILED) {
        printf("PRIMEPROBE_FAIL map-dumb errno=%d\n", errno);
        return 1;
    }
    fill_pattern(gem, dumb.size);

    struct drm_prime_handle prime = {
        .handle = dumb.handle,
        .flags = DRM_CLOEXEC,
    };
    if (ioctl(exporter, DRM_IOCTL_PRIME_HANDLE_TO_FD, &prime) != 0) {
        printf("PRIMEPROBE_FAIL handle-to-fd errno=%d\n", errno);
        return 1;
    }
    printf("PRIMEPROBE_EXPORT fd=%d\n", prime.fd);

    // The descriptor maps the same bytes the GEM mapping wrote.
    uint8_t *shared = mmap(NULL, dumb.size, PROT_READ | PROT_WRITE, MAP_SHARED,
                           prime.fd, 0);
    if (shared == MAP_FAILED) {
        printf("PRIMEPROBE_FAIL mmap-dmabuf errno=%d\n", errno);
        return 1;
    }
    if (check_pattern(shared, dumb.size) != 0) {
        return 1;
    }
    printf("PRIMEPROBE_MMAP_SHARED ok\n");

    // Import into a second DRM file: the handle belongs to that file now.
    int importer = open_card();
    struct drm_prime_handle imported = {.fd = prime.fd};
    if (ioctl(importer, DRM_IOCTL_PRIME_FD_TO_HANDLE, &imported) != 0) {
        printf("PRIMEPROBE_FAIL fd-to-handle errno=%d\n", errno);
        return 1;
    }
    printf("PRIMEPROBE_IMPORT handle=%u\n", imported.handle);
    uint8_t *imported_map = map_handle(importer, imported.handle, dumb.size);
    if (imported_map == MAP_FAILED) {
        printf("PRIMEPROBE_FAIL map-imported errno=%d\n", errno);
        return 1;
    }
    if (check_pattern(imported_map, dumb.size) != 0) {
        return 1;
    }
    printf("PRIMEPROBE_IMPORT_MMAP ok\n");

    // The exporting file can go away; the descriptor and the imported handle
    // keep the buffer alive.
    close(exporter);

    // Pass the descriptor to a child and have it import and map the buffer.
    int sockets[2];
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) != 0) {
        printf("PRIMEPROBE_FAIL socketpair errno=%d\n", errno);
        return 1;
    }
    pid_t child = fork();
    if (child < 0) {
        printf("PRIMEPROBE_FAIL fork errno=%d\n", errno);
        return 1;
    }
    if (child == 0) {
        close(sockets[0]);
        int received = receive_fd(sockets[1]);
        if (received < 0) {
            printf("PRIMEPROBE_CHILD_FAIL receive errno=%d\n", errno);
            _exit(2);
        }
        int child_card = open("/dev/dri/card0", O_RDWR | O_CLOEXEC);
        struct drm_prime_handle child_prime = {.fd = received};
        if (child_card < 0 ||
            ioctl(child_card, DRM_IOCTL_PRIME_FD_TO_HANDLE, &child_prime) != 0) {
            printf("PRIMEPROBE_CHILD_FAIL import errno=%d\n", errno);
            _exit(2);
        }
        uint8_t *child_map =
            map_handle(child_card, child_prime.handle, dumb.size);
        if (child_map == MAP_FAILED || check_pattern(child_map, dumb.size) != 0) {
            printf("PRIMEPROBE_CHILD_FAIL map errno=%d\n", errno);
            _exit(2);
        }
        printf("PRIMEPROBE_CHILD_PASS\n");
        _exit(0);
    }
    close(sockets[1]);
    if (send_fd(sockets[0], prime.fd) != 0) {
        printf("PRIMEPROBE_FAIL sendmsg errno=%d\n", errno);
        return 1;
    }
    int status = 0;
    waitpid(child, &status, 0);
    if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
        printf("PRIMEPROBE_FAIL child status=%d\n", status);
        return 1;
    }
    printf("PRIMEPROBE_SCM_RIGHTS ok\n");

    // A descriptor that is not a dma-buf is rejected with EINVAL.
    int pipe_fds[2];
    if (pipe(pipe_fds) != 0) {
        printf("PRIMEPROBE_FAIL pipe errno=%d\n", errno);
        return 1;
    }
    struct drm_prime_handle wrong = {.fd = pipe_fds[0]};
    errno = 0;
    if (ioctl(importer, DRM_IOCTL_PRIME_FD_TO_HANDLE, &wrong) == 0 ||
        errno != EINVAL) {
        printf("PRIMEPROBE_FAIL non-dmabuf accepted errno=%d\n", errno);
        return 1;
    }
    printf("PRIMEPROBE_REJECT_NON_DMABUF ok\n");
    close(pipe_fds[0]);
    close(pipe_fds[1]);

    // A closed descriptor is rejected with EBADF.
    close(prime.fd);
    struct drm_prime_handle closed = {.fd = prime.fd};
    errno = 0;
    if (ioctl(importer, DRM_IOCTL_PRIME_FD_TO_HANDLE, &closed) == 0 ||
        errno != EBADF) {
        printf("PRIMEPROBE_FAIL closed fd errno=%d\n", errno);
        return 1;
    }
    printf("PRIMEPROBE_REJECT_CLOSED_FD ok\n");

    printf("PRIMEPROBE_PASS\n");

    if (getpid() == 1) {
        sleep(3600);
    }
    return 0;
}
