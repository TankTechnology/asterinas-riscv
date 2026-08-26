// DRM-M20: PRIME fd<->handle round-trip verification (static musl, runs as /init).
// Verifies DRM_IOCTL_PRIME_HANDLE_TO_FD and PRIME_FD_TO_HANDLE against /dev/dri/card0.
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#define DRM_IOCTL_BASE 'd'
#define DRM_IOWR(nr, type) _IOWR(DRM_IOCTL_BASE, nr, type)

#define DRM_IOCTL_MODE_CREATE_DUMB DRM_IOWR(0xb2, struct drm_mode_create_dumb)
#define DRM_IOCTL_MODE_MAP_DUMB DRM_IOWR(0xb3, struct drm_mode_map_dumb)
#define DRM_IOCTL_MODE_DESTROY_DUMB DRM_IOWR(0xb4, struct drm_mode_destroy_dumb)
#define DRM_IOCTL_PRIME_HANDLE_TO_FD DRM_IOWR(0x2d, struct drm_prime_handle)
#define DRM_IOCTL_PRIME_FD_TO_HANDLE DRM_IOWR(0x2e, struct drm_prime_handle)
#define DRM_CLOEXEC O_CLOEXEC
#define DRM_RDWR O_RDWR

struct drm_mode_create_dumb {
    uint32_t height, width, bpp, flags;
    uint32_t handle, pitch;
    uint64_t size;
};
struct drm_mode_map_dumb {
    uint32_t handle, pad;
    uint64_t offset;
};
struct drm_mode_destroy_dumb {
    uint32_t handle;
};
struct drm_prime_handle {
    uint32_t handle;
    uint32_t flags;
    int32_t fd;
};

enum {
    FIRST_GEM_HANDLE = 1,
    FAILED_CREATE_THREADS = 8,
};

struct failed_create_task {
    int card;
    struct drm_mode_create_dumb *request;
    pthread_barrier_t *barrier;
    int result;
    int error;
};

static void *run_failed_create(void *opaque) {
    struct failed_create_task *task = opaque;
    pthread_barrier_wait(task->barrier);
    errno = 0;
    task->result = ioctl(task->card, DRM_IOCTL_MODE_CREATE_DUMB, task->request);
    task->error = errno;
    return NULL;
}

int main(void) {
    printf("M20_PRIME_BEGIN\n");
    int card = open("/dev/dri/card0", O_RDWR);
    if (card < 0) { printf("M20_PRIME_FAIL open card0 errno=%d\n", errno); return 1; }

    /* A failed response copyout must not publish the reserved GEM handle or
     * consume space from the dumb-buffer bump allocator. */
    long page_size = sysconf(_SC_PAGESIZE);
    struct drm_mode_create_dumb *unpublished = mmap(
        NULL, page_size, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (unpublished == MAP_FAILED) {
        printf("M20_PRIME_FAIL unpublished mmap errno=%d\n", errno); return 1;
    }
    *unpublished = (struct drm_mode_create_dumb){ .width = 1, .height = 1, .bpp = 32 };
    if (mprotect(unpublished, page_size, PROT_READ) != 0) {
        printf("M20_PRIME_FAIL unpublished mprotect errno=%d\n", errno); return 1;
    }
    errno = 0;
    if (ioctl(card, DRM_IOCTL_MODE_CREATE_DUMB, unpublished) != -1 || errno != EFAULT) {
        printf("M20_PRIME_FAIL unpublished CREATE_DUMB result errno=%d handle=%u\n",
               errno, unpublished->handle);
        return 1;
    }
    struct drm_mode_map_dumb unpublished_map = { .handle = FIRST_GEM_HANDLE };
    errno = 0;
    if (ioctl(card, DRM_IOCTL_MODE_MAP_DUMB, &unpublished_map) != -1 || errno != EINVAL) {
        printf("M20_PRIME_FAIL unpublished handle visible errno=%d offset=%llu\n",
               errno, (unsigned long long)unpublished_map.offset);
        return 1;
    }
    munmap(unpublished, page_size);

    /* Launch several failed copyouts together. The pool cursor must remain at
     * zero even when threads share the same DRM file description. */
    pthread_t threads[FAILED_CREATE_THREADS];
    struct failed_create_task tasks[FAILED_CREATE_THREADS] = {0};
    pthread_barrier_t barrier;
    if (pthread_barrier_init(&barrier, NULL, FAILED_CREATE_THREADS + 1) != 0) {
        printf("M20_PRIME_FAIL barrier init\n"); return 1;
    }
    for (int i = 0; i < FAILED_CREATE_THREADS; i++) {
        tasks[i].card = card;
        tasks[i].barrier = &barrier;
        tasks[i].request = mmap(NULL, page_size, PROT_READ | PROT_WRITE,
                                MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (tasks[i].request == MAP_FAILED) {
            printf("M20_PRIME_FAIL concurrent mmap i=%d errno=%d\n", i, errno);
            return 1;
        }
        *tasks[i].request = (struct drm_mode_create_dumb){
            .width = 1, .height = 1, .bpp = 32
        };
        if (mprotect(tasks[i].request, page_size, PROT_READ) != 0 ||
            pthread_create(&threads[i], NULL, run_failed_create, &tasks[i]) != 0) {
            printf("M20_PRIME_FAIL concurrent setup i=%d errno=%d\n", i, errno);
            return 1;
        }
    }
    pthread_barrier_wait(&barrier);
    for (int i = 0; i < FAILED_CREATE_THREADS; i++) {
        pthread_join(threads[i], NULL);
        if (tasks[i].result != -1 || tasks[i].error != EFAULT) {
            printf("M20_PRIME_FAIL concurrent CREATE_DUMB i=%d result=%d errno=%d\n",
                   i, tasks[i].result, tasks[i].error);
            return 1;
        }
        munmap(tasks[i].request, page_size);
    }
    pthread_barrier_destroy(&barrier);
    for (uint32_t handle = FIRST_GEM_HANDLE;
         handle <= FIRST_GEM_HANDLE + FAILED_CREATE_THREADS; handle++) {
        unpublished_map = (struct drm_mode_map_dumb){ .handle = handle };
        errno = 0;
        if (ioctl(card, DRM_IOCTL_MODE_MAP_DUMB, &unpublished_map) != -1 ||
            errno != EINVAL) {
            printf("M20_PRIME_FAIL concurrent handle visible handle=%u errno=%d\n",
                   handle, errno);
            return 1;
        }
    }
    printf("M20_PRIME_UNPUBLISHED_HANDLE_OK threads=%d\n", FAILED_CREATE_THREADS);

    /* Consume the first pool span so that dma-buf offset zero must be
     * translated to a nonzero VMO offset for the object under test. */
    struct drm_mode_create_dumb sacrificial = { .width = 1, .height = 1, .bpp = 32 };
    if (ioctl(card, DRM_IOCTL_MODE_CREATE_DUMB, &sacrificial) != 0) {
        printf("M20_PRIME_FAIL sacrificial CREATE_DUMB errno=%d\n", errno); return 1;
    }
    struct drm_mode_map_dumb sacrificial_map = { .handle = sacrificial.handle };
    if (ioctl(card, DRM_IOCTL_MODE_MAP_DUMB, &sacrificial_map) != 0 ||
        sacrificial_map.offset != 0) {
        printf("M20_PRIME_FAIL unpublished allocation retained errno=%d offset=%llu\n",
               errno, (unsigned long long)sacrificial_map.offset);
        return 1;
    }
    struct drm_mode_destroy_dumb destroy = { .handle = sacrificial.handle };
    if (ioctl(card, DRM_IOCTL_MODE_DESTROY_DUMB, &destroy) != 0) {
        printf("M20_PRIME_FAIL sacrificial DESTROY_DUMB errno=%d\n", errno); return 1;
    }

    struct drm_mode_create_dumb cdb = {0};
    cdb.width = 64; cdb.height = 64; cdb.bpp = 32;
    if (ioctl(card, DRM_IOCTL_MODE_CREATE_DUMB, &cdb) != 0) {
        printf("M20_PRIME_FAIL CREATE_DUMB errno=%d\n", errno); return 1;
    }
    printf("M20_PRIME_CREATE_DUMB handle=%u size=%llu\n", cdb.handle, (unsigned long long)cdb.size);

    struct drm_mode_map_dumb map = { .handle = cdb.handle };
    if (ioctl(card, DRM_IOCTL_MODE_MAP_DUMB, &map) != 0) {
        printf("M20_PRIME_FAIL MAP_DUMB errno=%d\n", errno); return 1;
    }
    uint32_t *pixels = mmap(NULL, cdb.size, PROT_READ | PROT_WRITE, MAP_SHARED, card, map.offset);
    if (pixels == MAP_FAILED) {
        printf("M20_PRIME_FAIL mmap errno=%d\n", errno); return 1;
    }
    pixels[0] = 0x51a7c0de;

    struct drm_prime_handle export = {
        .handle = cdb.handle,
        .flags = DRM_CLOEXEC | DRM_RDWR,
        .fd = -1,
    };
    if (ioctl(card, DRM_IOCTL_PRIME_HANDLE_TO_FD, &export) != 0) {
        printf("M20_PRIME_FAIL HANDLE_TO_FD errno=%d\n", errno); return 1;
    }
    if (export.fd < 0) { printf("M20_PRIME_FAIL bad fd\n"); return 1; }
    if (!(fcntl(export.fd, F_GETFD) & FD_CLOEXEC)) {
        printf("M20_PRIME_FAIL missing FD_CLOEXEC\n"); return 1;
    }
    printf("M20_PRIME_HANDLE_TO_FD fd=%d\n", export.fd);

    uint32_t *dma_pixels = mmap(NULL, cdb.size, PROT_READ | PROT_WRITE,
                                MAP_SHARED, export.fd, 0);
    if (dma_pixels == MAP_FAILED || dma_pixels[0] != 0x51a7c0de) {
        printf("M20_PRIME_FAIL dma-buf offset-zero mmap errno=%d pixel=%08x\n",
               errno, dma_pixels == MAP_FAILED ? 0 : dma_pixels[0]);
        return 1;
    }

    /* The exported fd alone must keep the object alive across handle close. */
    destroy.handle = cdb.handle;
    if (ioctl(card, DRM_IOCTL_MODE_DESTROY_DUMB, &destroy) != 0) {
        printf("M20_PRIME_FAIL destroy original errno=%d\n", errno); return 1;
    }
    struct drm_prime_handle import = { .handle = 0, .flags = 0, .fd = export.fd };
    if (ioctl(card, DRM_IOCTL_PRIME_FD_TO_HANDLE, &import) != 0) {
        printf("M20_PRIME_FAIL FD_TO_HANDLE errno=%d\n", errno); return 1;
    }
    if (import.handle == 0) { printf("M20_PRIME_FAIL import handle 0\n"); return 1; }
    printf("M20_PRIME_FD_TO_HANDLE handle=%u\n", import.handle);

    close(export.fd);

    struct drm_mode_map_dumb imported_map = { .handle = import.handle };
    if (ioctl(card, DRM_IOCTL_MODE_MAP_DUMB, &imported_map) != 0 ||
        imported_map.offset != map.offset || pixels[0] != 0x51a7c0de) {
        printf("M20_PRIME_FAIL imported object lifetime errno=%d offset=%llu/%llu pixel=%08x\n",
               errno, (unsigned long long)imported_map.offset,
               (unsigned long long)map.offset, pixels[0]);
        return 1;
    }
    destroy.handle = import.handle;
    if (ioctl(card, DRM_IOCTL_MODE_DESTROY_DUMB, &destroy) != 0) {
        printf("M20_PRIME_FAIL destroy import errno=%d\n", errno); return 1;
    }
    munmap(dma_pixels, cdb.size);
    munmap(pixels, cdb.size);
    close(card);
    printf("M20_PRIME_PASS\n");
    return 0;
}
