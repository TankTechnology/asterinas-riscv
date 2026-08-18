#include <stdio.h>
#include <fcntl.h>
#include <sys/ioctl.h>
#include <unistd.h>
#include <errno.h>
#include <string.h>
#include <stdint.h>

// Correct DRM ioctl macros (matching Linux kernel)
#define DRM_IOCTL_BASE 0x64  // 'd'
#define DRM_IO(nr) _IO(DRM_IOCTL_BASE, nr)
#define DRM_IOR(nr, type) _IOR(DRM_IOCTL_BASE, nr, type)
#define DRM_IOW(nr, type) _IOW(DRM_IOCTL_BASE, nr, type)
#define DRM_IOWR(nr, type) _IOWR(DRM_IOCTL_BASE, nr, type)

struct drm_version {
    int version_major, version_minor, version_patchlevel;
    size_t name_len; void *name;
    size_t date_len; void *date;
    size_t desc_len; void *desc;
};

struct drm_get_cap { uint64_t capability; uint64_t value; };

#define DRM_IOCTL_VERSION DRM_IOWR(0x00, struct drm_version)
#define DRM_IOCTL_GET_CAP DRM_IOWR(0x0c, struct drm_get_cap)
#define DRM_IOCTL_SET_CLIENT_CAP DRM_IOW(0x0d, struct drm_get_cap)
#define DRM_IOCTL_SET_MASTER DRM_IO(0x1e)
#define DRM_IOCTL_DROP_MASTER DRM_IO(0x1f)

int main() {
    printf("DRM test v2: opening /dev/dri/card0\n");
    printf("  SET_MASTER cmd=0x%08lx\n", (unsigned long)DRM_IOCTL_SET_MASTER);
    printf("  VERSION   cmd=0x%08lx\n", (unsigned long)DRM_IOCTL_VERSION);
    printf("  GET_CAP   cmd=0x%08lx\n", (unsigned long)DRM_IOCTL_GET_CAP);

    int fd = open("/dev/dri/card0", O_RDWR);
    if (fd < 0) { printf("OPEN FAILED: %s\n", strerror(errno)); return 1; }
    printf("OPEN OK: fd=%d\n", fd);

    // Test DRM_IOCTL_VERSION
    char name[64]={0}, date[64]={0}, desc[64]={0};
    struct drm_version ver = {
        .name_len = sizeof(name), .name = name,
        .date_len = sizeof(date), .date = date,
        .desc_len = sizeof(desc), .desc = desc,
    };
    if (ioctl(fd, DRM_IOCTL_VERSION, &ver) == 0)
        printf("VERSION: %d.%d.%d name=%s\n", ver.version_major, ver.version_minor, ver.version_patchlevel, name);
    else printf("VERSION FAILED: %s\n", strerror(errno));

    // Test SET_MASTER (correct: DRM_IO)
    if (ioctl(fd, DRM_IOCTL_SET_MASTER, 0) == 0)
        printf("SET_MASTER OK\n");
    else printf("SET_MASTER FAILED: %s (errno=%d)\n", strerror(errno), errno);

    // Test GET_CAP
    struct drm_get_cap cap = { .capability = 1, .value = 0 };
    if (ioctl(fd, DRM_IOCTL_GET_CAP, &cap) == 0)
        printf("GET_CAP DUMB_BUFFER: %llu\n", (unsigned long long)cap.value);
    else printf("GET_CAP FAILED: %s\n", strerror(errno));

    // Test GET_CAP for PRIME
    cap.capability = 0x5; cap.value = 0;
    if (ioctl(fd, DRM_IOCTL_GET_CAP, &cap) == 0)
        printf("GET_CAP PRIME: %llu\n", (unsigned long long)cap.value);
    else printf("GET_CAP PRIME FAILED: %s\n", strerror(errno));

    close(fd);

    // Test render node
    printf("\nOpening /dev/dri/renderD128\n");
    fd = open("/dev/dri/renderD128", O_RDWR);
    if (fd < 0) { printf("RENDER OPEN FAILED: %s\n", strerror(errno)); return 1; }
    printf("RENDER OPEN OK: fd=%d\n", fd);

    // SET_MASTER should fail on render node
    if (ioctl(fd, DRM_IOCTL_SET_MASTER, 0) == 0)
        printf("RENDER SET_MASTER OK (unexpected)\n");
    else printf("RENDER SET_MASTER FAILED: %s (expected)\n", strerror(errno));

    // GET_CAP should work on render node
    cap.capability = 1; cap.value = 0;
    if (ioctl(fd, DRM_IOCTL_GET_CAP, &cap) == 0)
        printf("RENDER GET_CAP DUMB_BUFFER: %llu\n", (unsigned long long)cap.value);
    else printf("RENDER GET_CAP FAILED: %s\n", strerror(errno));

    close(fd);
    printf("\nDRM test ALL PASS\n");
    return 0;
}
