// SPDX-License-Identifier: MPL-2.0

// DRM-M22: repeated GEM/PRIME/virgl/fence lifetime verification.

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <unistd.h>

#define DRM_IOCTL_BASE 'd'
#define DRM_IOW(nr, type) _IOW(DRM_IOCTL_BASE, nr, type)
#define DRM_IOWR(nr, type) _IOWR(DRM_IOCTL_BASE, nr, type)

#define DRM_IOCTL_GEM_CLOSE DRM_IOW(0x09, struct drm_gem_close)
#define DRM_IOCTL_PRIME_HANDLE_TO_FD DRM_IOWR(0x2d, struct drm_prime_handle)
#define DRM_IOCTL_PRIME_FD_TO_HANDLE DRM_IOWR(0x2e, struct drm_prime_handle)
#define DRM_IOCTL_MODE_CREATE_DUMB DRM_IOWR(0xb2, struct drm_mode_create_dumb)
#define DRM_IOCTL_MODE_MAP_DUMB DRM_IOWR(0xb3, struct drm_mode_map_dumb)
#define DRM_IOCTL_VIRTGPU_EXECBUFFER DRM_IOWR(0x42, struct drm_virtgpu_execbuffer)
#define DRM_IOCTL_VIRTGPU_RESOURCE_CREATE DRM_IOWR(0x44, struct drm_virtgpu_resource_create)
#define DRM_IOCTL_VIRTGPU_WAIT DRM_IOWR(0x48, struct drm_virtgpu_3d_wait)

#define DRM_CLOEXEC O_CLOEXEC
#define DRM_RDWR O_RDWR
#define VIRTGPU_EXECBUF_FENCE_FD_OUT 0x02
#define PIPE_TEXTURE_2D 2
#define PIPE_FORMAT_B8G8R8X8_UNORM 1
#define PIPE_BIND_RENDER_TARGET 2
#define STRESS_ROUNDS 32
#define REUSE_CYCLES 4200

struct drm_gem_close {
    uint32_t handle;
    uint32_t pad;
};

struct drm_prime_handle {
    uint32_t handle;
    uint32_t flags;
    int32_t fd;
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

struct drm_virtgpu_resource_create {
    uint32_t target;
    uint32_t format;
    uint32_t bind;
    uint32_t width;
    uint32_t height;
    uint32_t depth;
    uint32_t array_size;
    uint32_t last_level;
    uint32_t nr_samples;
    uint32_t flags;
    uint32_t bo_handle;
    uint32_t res_handle;
    uint32_t size;
    uint32_t stride;
};

struct drm_virtgpu_execbuffer {
    uint32_t flags;
    uint32_t size;
    uint64_t command;
    uint64_t bo_handles;
    uint32_t num_bo_handles;
    int32_t fence_fd;
    uint32_t ring_idx;
    uint32_t syncobj_stride;
    uint32_t num_in_syncobjs;
    uint32_t num_out_syncobjs;
    uint64_t in_syncobjs;
    uint64_t out_syncobjs;
};

struct drm_virtgpu_3d_wait {
    uint32_t handle;
    uint32_t flags;
};

enum resource_counter {
    DUMB_POOL_USED_BYTES,
    DUMB_POOL_HIGH_WATER_BYTES,
    DUMB_POOL_CAPACITY_BYTES,
    GEM_OBJECTS,
    GEM_REFERENCES,
    FLINK_NAMES,
    HOST_RESOURCES,
    HOST_RESOURCES_CLEANUP_ONLY,
    RESOURCE_CLEANUP_PENDING,
    CONTEXTS,
    CONTEXT_ATTACHMENTS,
    CONTEXT_CLEANUP_PENDING,
    FENCES_TRACKED,
    FENCE_ASSOCIATIONS,
    BACKEND_BACKING_OWNERS,
    BACKEND_CLEANUP_PENDING,
    SCANOUT_RESOURCES,
    CURSOR_RESOURCES,
    RESOURCE_COUNTER_COUNT,
};

static const char *const counter_names[RESOURCE_COUNTER_COUNT] = {
    [DUMB_POOL_USED_BYTES] = "drm-device-dumb-pool-used-bytes",
    [DUMB_POOL_HIGH_WATER_BYTES] = "drm-device-dumb-pool-high-water-bytes",
    [DUMB_POOL_CAPACITY_BYTES] = "drm-device-dumb-pool-capacity-bytes",
    [GEM_OBJECTS] = "drm-device-gem-objects",
    [GEM_REFERENCES] = "drm-device-gem-references",
    [FLINK_NAMES] = "drm-device-flink-names",
    [HOST_RESOURCES] = "drm-device-host-resources",
    [HOST_RESOURCES_CLEANUP_ONLY] = "drm-device-host-resources-cleanup-only",
    [RESOURCE_CLEANUP_PENDING] = "drm-device-resource-cleanup-pending",
    [CONTEXTS] = "drm-device-contexts",
    [CONTEXT_ATTACHMENTS] = "drm-device-context-attachments",
    [CONTEXT_CLEANUP_PENDING] = "drm-device-context-cleanup-pending",
    [FENCES_TRACKED] = "drm-device-fences-tracked",
    [FENCE_ASSOCIATIONS] = "drm-device-fence-associations",
    [BACKEND_BACKING_OWNERS] = "drm-device-backend-backing-owners",
    [BACKEND_CLEANUP_PENDING] = "drm-device-backend-cleanup-pending",
    [SCANOUT_RESOURCES] = "drm-device-scanout-resources",
    [CURSOR_RESOURCES] = "drm-device-cursor-resources",
};

struct resource_snapshot {
    uint64_t value[RESOURCE_COUNTER_COUNT];
};

static int failures;

#define CHECK(condition, ...) do { \
    if (condition) { \
        printf("M22_PASS " __VA_ARGS__); \
        printf("\n"); \
    } else { \
        printf("M22_FAIL " __VA_ARGS__); \
        printf(" errno=%d\n", errno); \
        failures++; \
    } \
} while (0)

static int read_snapshot(int drm_fd, struct resource_snapshot *snapshot)
{
    char path[64];
    char buffer[4096];
    snprintf(path, sizeof(path), "/proc/self/fdinfo/%d", drm_fd);
    int fd = open(path, O_RDONLY);
    if (fd < 0)
        return -1;
    ssize_t length = read(fd, buffer, sizeof(buffer) - 1);
    close(fd);
    if (length < 0)
        return -1;
    buffer[length] = '\0';

    for (int counter = 0; counter < RESOURCE_COUNTER_COUNT; counter++)
        snapshot->value[counter] = UINT64_MAX;

    char *save = NULL;
    for (char *line = strtok_r(buffer, "\n", &save); line;
         line = strtok_r(NULL, "\n", &save)) {
        for (int counter = 0; counter < RESOURCE_COUNTER_COUNT; counter++) {
            size_t name_length = strlen(counter_names[counter]);
            if (strncmp(line, counter_names[counter], name_length) != 0 ||
                line[name_length] != ':')
                continue;
            char *end = NULL;
            unsigned long long value = strtoull(line + name_length + 1, &end, 10);
            if (end == line + name_length + 1)
                return -1;
            snapshot->value[counter] = value;
        }
    }

    for (int counter = 0; counter < RESOURCE_COUNTER_COUNT; counter++) {
        if (snapshot->value[counter] == UINT64_MAX) {
            fprintf(stderr, "missing fdinfo counter %s\n", counter_names[counter]);
            return -1;
        }
    }
    return 0;
}

static int reclaimable_counters_equal(const struct resource_snapshot *left,
                                      const struct resource_snapshot *right)
{
    for (int counter = 0; counter < RESOURCE_COUNTER_COUNT; counter++) {
        // The high-water mark is monotonic; every live-resource counter,
        // including currently allocated pool bytes, must return to baseline.
        if (counter == DUMB_POOL_HIGH_WATER_BYTES)
            continue;
        if (left->value[counter] != right->value[counter]) {
            printf("M22_COUNTER_DIFF %s baseline=%llu actual=%llu\n",
                   counter_names[counter],
                   (unsigned long long)left->value[counter],
                   (unsigned long long)right->value[counter]);
            return 0;
        }
    }
    return 1;
}

static void print_snapshot(const char *label, const struct resource_snapshot *snapshot)
{
    printf("M22_SNAPSHOT %s gem=%llu refs=%llu host=%llu ctx=%llu attach=%llu "
           "fences=%llu assoc=%llu backing=%llu pending=%llu/%llu/%llu "
           "pool=%llu/%llu high-water=%llu\n",
           label,
           (unsigned long long)snapshot->value[GEM_OBJECTS],
           (unsigned long long)snapshot->value[GEM_REFERENCES],
           (unsigned long long)snapshot->value[HOST_RESOURCES],
           (unsigned long long)snapshot->value[CONTEXTS],
           (unsigned long long)snapshot->value[CONTEXT_ATTACHMENTS],
           (unsigned long long)snapshot->value[FENCES_TRACKED],
           (unsigned long long)snapshot->value[FENCE_ASSOCIATIONS],
           (unsigned long long)snapshot->value[BACKEND_BACKING_OWNERS],
           (unsigned long long)snapshot->value[RESOURCE_CLEANUP_PENDING],
           (unsigned long long)snapshot->value[CONTEXT_CLEANUP_PENDING],
           (unsigned long long)snapshot->value[BACKEND_CLEANUP_PENDING],
           (unsigned long long)snapshot->value[DUMB_POOL_USED_BYTES],
           (unsigned long long)snapshot->value[DUMB_POOL_CAPACITY_BYTES],
           (unsigned long long)snapshot->value[DUMB_POOL_HIGH_WATER_BYTES]);
}

static int create_dumb(int drm_fd, struct drm_mode_create_dumb *dumb,
                       struct drm_mode_map_dumb *map)
{
    *dumb = (struct drm_mode_create_dumb) {
        .width = 64,
        .height = 64,
        .bpp = 32,
    };
    if (ioctl(drm_fd, DRM_IOCTL_MODE_CREATE_DUMB, dumb) < 0)
        return -1;
    *map = (struct drm_mode_map_dumb) { .handle = dumb->handle };
    if (ioctl(drm_fd, DRM_IOCTL_MODE_MAP_DUMB, map) < 0)
        return -1;
    return 0;
}

static int close_gem(int drm_fd, uint32_t handle)
{
    struct drm_gem_close close = { .handle = handle };
    return ioctl(drm_fd, DRM_IOCTL_GEM_CLOSE, &close);
}

static int run_mapping_lifetime_test(int control_fd,
                                     const struct resource_snapshot *baseline,
                                     const char *device,
                                     int use_prime)
{
    int result = -1;
    int worker_fd = open(device, O_RDWR);
    int prime_fd = -1;
    void *mapping = MAP_FAILED;
    struct drm_mode_create_dumb first = { 0 }, second = { 0 }, reused = { 0 };
    struct drm_mode_map_dumb first_map = { 0 }, second_map = { 0 }, reused_map = { 0 };
    const char *kind = use_prime ? "PRIME" : "DRM";

    if (worker_fd < 0 || create_dumb(worker_fd, &first, &first_map) < 0)
        goto out;
    if (use_prime) {
        struct drm_prime_handle exported = {
            .handle = first.handle,
            .flags = DRM_CLOEXEC | DRM_RDWR,
            .fd = -1,
        };
        if (ioctl(worker_fd, DRM_IOCTL_PRIME_HANDLE_TO_FD, &exported) < 0)
            goto out;
        prime_fd = exported.fd;
        mapping = mmap(NULL, first.size, PROT_READ | PROT_WRITE, MAP_SHARED, prime_fd, 0);
    } else {
        mapping = mmap(NULL, first.size, PROT_READ | PROT_WRITE, MAP_SHARED,
                       worker_fd, first_map.offset);
    }
    if (mapping == MAP_FAILED)
        goto out;
    memset(mapping, 0x5a, first.size);
    if (close_gem(worker_fd, first.handle) < 0)
        goto out;
    first.handle = 0;
    if (prime_fd >= 0) {
        close(prime_fd);
        prime_fd = -1;
    }

    if (create_dumb(worker_fd, &second, &second_map) < 0)
        goto out;
    if (second_map.offset == first_map.offset || ((unsigned char *)mapping)[0] != 0x5a) {
        errno = EFAULT;
        goto out;
    }
    if (close_gem(worker_fd, second.handle) < 0)
        goto out;
    second.handle = 0;

    munmap(mapping, first.size);
    mapping = MAP_FAILED;
    if (create_dumb(worker_fd, &reused, &reused_map) < 0)
        goto out;
    if (reused_map.offset != first_map.offset) {
        errno = EFAULT;
        goto out;
    }
    if (close_gem(worker_fd, reused.handle) < 0)
        goto out;
    reused.handle = 0;

    struct resource_snapshot released;
    if (read_snapshot(control_fd, &released) < 0)
        goto out;
    if (!reclaimable_counters_equal(baseline, &released))
        goto out;
    printf("M22_MAPPING_LIFETIME %s protected-and-reused offset=%llu\n", kind,
           (unsigned long long)first_map.offset);
    result = 0;

out:
    if (mapping != MAP_FAILED)
        munmap(mapping, first.size);
    if (first.handle)
        close_gem(worker_fd, first.handle);
    if (second.handle)
        close_gem(worker_fd, second.handle);
    if (reused.handle)
        close_gem(worker_fd, reused.handle);
    if (prime_fd >= 0)
        close(prime_fd);
    if (worker_fd >= 0)
        close(worker_fd);
    return result;
}

static int run_pool_reuse_cycles(void)
{
    int worker_fd = open("/dev/dri/renderD128", O_RDWR);
    uint64_t expected_offset = UINT64_MAX;
    if (worker_fd < 0)
        return -1;
    for (int cycle = 0; cycle < REUSE_CYCLES; cycle++) {
        struct drm_mode_create_dumb dumb;
        struct drm_mode_map_dumb map;
        if (create_dumb(worker_fd, &dumb, &map) < 0) {
            close(worker_fd);
            return -1;
        }
        if (expected_offset == UINT64_MAX)
            expected_offset = map.offset;
        if (map.offset != expected_offset || close_gem(worker_fd, dumb.handle) < 0) {
            close(worker_fd);
            errno = EFAULT;
            return -1;
        }
    }
    close(worker_fd);
    printf("M22_POOL_REUSE cycles=%d offset=%llu\n", REUSE_CYCLES,
           (unsigned long long)expected_offset);
    return 0;
}

static int run_round(int control_fd, const struct resource_snapshot *baseline, int round,
                     uint64_t *mapped_offset)
{
    int result = -1;
    int worker_fd = -1;
    int prime_fd = -1;
    int fence_fd = -1;
    void *mapping = MAP_FAILED;
    size_t mapping_size = 0;
    const char *stage = "open worker";

    worker_fd = open("/dev/dri/renderD128", O_RDWR);
    if (worker_fd < 0)
        goto out;

    struct drm_mode_create_dumb dumb = {
        .width = 64,
        .height = 64,
        .bpp = 32,
    };
    stage = "create dumb buffer";
    if (ioctl(worker_fd, DRM_IOCTL_MODE_CREATE_DUMB, &dumb) < 0)
        goto out;

    struct drm_mode_map_dumb map = { .handle = dumb.handle };
    stage = "map dumb buffer";
    if (ioctl(worker_fd, DRM_IOCTL_MODE_MAP_DUMB, &map) < 0)
        goto out;
    *mapped_offset = map.offset;
    mapping_size = dumb.size;
    mapping = mmap(NULL, mapping_size, PROT_READ | PROT_WRITE, MAP_SHARED,
                   worker_fd, map.offset);
    stage = "mmap dumb buffer";
    if (mapping == MAP_FAILED)
        goto out;
    memset(mapping, round, mapping_size);

    struct drm_virtgpu_resource_create resource = {
        .target = PIPE_TEXTURE_2D,
        .format = PIPE_FORMAT_B8G8R8X8_UNORM,
        .bind = PIPE_BIND_RENDER_TARGET,
        .width = 64,
        .height = 64,
        .depth = 1,
        .array_size = 1,
        .last_level = 1,
        .bo_handle = dumb.handle,
    };
    stage = "create host resource";
    if (ioctl(worker_fd, DRM_IOCTL_VIRTGPU_RESOURCE_CREATE, &resource) < 0)
        goto out;

    struct drm_prime_handle exported = {
        .handle = dumb.handle,
        .flags = DRM_CLOEXEC | DRM_RDWR,
        .fd = -1,
    };
    stage = "export PRIME fd";
    if (ioctl(worker_fd, DRM_IOCTL_PRIME_HANDLE_TO_FD, &exported) < 0)
        goto out;
    prime_fd = exported.fd;

    struct drm_prime_handle imported = {
        .flags = DRM_CLOEXEC | DRM_RDWR,
        .fd = prime_fd,
    };
    stage = "import PRIME handle";
    if (ioctl(worker_fd, DRM_IOCTL_PRIME_FD_TO_HANDLE, &imported) < 0)
        goto out;

    uint32_t nop = 0;
    uint32_t bo_handle = imported.handle;
    struct drm_virtgpu_execbuffer exec = {
        .flags = VIRTGPU_EXECBUF_FENCE_FD_OUT,
        .size = sizeof(nop),
        .command = (uint64_t)&nop,
        .bo_handles = (uint64_t)&bo_handle,
        .num_bo_handles = 1,
        .fence_fd = -1,
    };
    stage = "submit fenced command";
    if (ioctl(worker_fd, DRM_IOCTL_VIRTGPU_EXECBUFFER, &exec) < 0)
        goto out;
    fence_fd = exec.fence_fd;
    struct pollfd poll_fd = { .fd = fence_fd, .events = POLLIN };
    stage = "wait for fence";
    if (poll(&poll_fd, 1, 5000) <= 0 || !(poll_fd.revents & POLLIN) ||
        (poll_fd.revents & (POLLERR | POLLHUP | POLLNVAL)))
        goto out;
    close(fence_fd);
    fence_fd = -1;

    struct drm_virtgpu_3d_wait wait = { .handle = bo_handle };
    stage = "verify successful resource wait";
    if (ioctl(worker_fd, DRM_IOCTL_VIRTGPU_WAIT, &wait) < 0)
        goto out;

    struct drm_gem_close close_import = { .handle = imported.handle };
    stage = "close imported GEM handle";
    if (ioctl(worker_fd, DRM_IOCTL_GEM_CLOSE, &close_import) < 0)
        goto out;

    struct resource_snapshot peak;
    stage = "read peak snapshot";
    if (read_snapshot(control_fd, &peak) < 0)
        goto out;
    stage = "validate peak snapshot";
    if (peak.value[GEM_OBJECTS] != baseline->value[GEM_OBJECTS] + 1 ||
        peak.value[HOST_RESOURCES] != baseline->value[HOST_RESOURCES] + 1 ||
        peak.value[CONTEXTS] != baseline->value[CONTEXTS] + 1 ||
        peak.value[CONTEXT_ATTACHMENTS] != baseline->value[CONTEXT_ATTACHMENTS] + 1 ||
        peak.value[BACKEND_BACKING_OWNERS] !=
            baseline->value[BACKEND_BACKING_OWNERS] + 1)
        goto out;

    munmap(mapping, mapping_size);
    mapping = MAP_FAILED;
    close(worker_fd);
    worker_fd = -1;

    struct resource_snapshot prime_held;
    stage = "read PRIME-held snapshot";
    if (read_snapshot(control_fd, &prime_held) < 0)
        goto out;
    stage = "validate PRIME-held snapshot";
    if (prime_held.value[GEM_OBJECTS] != baseline->value[GEM_OBJECTS] + 1 ||
        prime_held.value[HOST_RESOURCES] != baseline->value[HOST_RESOURCES] + 1 ||
        prime_held.value[CONTEXTS] != baseline->value[CONTEXTS] ||
        prime_held.value[CONTEXT_ATTACHMENTS] != baseline->value[CONTEXT_ATTACHMENTS] ||
        prime_held.value[FENCES_TRACKED] != baseline->value[FENCES_TRACKED])
        goto out;

    close(prime_fd);
    prime_fd = -1;

    struct resource_snapshot released;
    stage = "read released snapshot";
    if (read_snapshot(control_fd, &released) < 0)
        goto out;
    stage = "validate released snapshot";
    if (!reclaimable_counters_equal(baseline, &released))
        goto out;

    printf("M22_ROUND %d baseline-restored\n", round);
    result = 0;

out:
    int saved_errno = errno;
    if (result < 0)
        printf("M22_ROUND_FAIL round=%d stage=%s errno=%d\n", round, stage, saved_errno);
    if (mapping != MAP_FAILED)
        munmap(mapping, mapping_size);
    if (fence_fd >= 0)
        close(fence_fd);
    if (worker_fd >= 0)
        close(worker_fd);
    if (prime_fd >= 0)
        close(prime_fd);
    errno = saved_errno;
    return result;
}

int main(void)
{
    setvbuf(stdout, NULL, _IOLBF, 0);
    printf("M22_CONFIG rounds=%d\n", STRESS_ROUNDS);
    mkdir("/dev", 0755);
    mkdir("/proc", 0755);
    // Minimal Asterinas initramfs boots may already have a usable `/dev` even
    // though a second devtmpfs mount reports `ENODEV`.
    mount("devtmpfs", "/dev", "devtmpfs", 0, NULL);
    if (mount("proc", "/proc", "proc", 0, NULL) < 0 && errno != EBUSY) {
        perror("mount proc");
        return 1;
    }

    int control_fd = open("/dev/dri/renderD128", O_RDWR);
    CHECK(control_fd >= 0, "open control DRM fd");
    if (control_fd < 0)
        return 1;

    struct resource_snapshot baseline;
    int snapshot_result = read_snapshot(control_fd, &baseline);
    CHECK(snapshot_result == 0, "read complete DRM fdinfo resource snapshot");
    if (snapshot_result < 0) {
        close(control_fd);
        return 1;
    }
    print_snapshot("baseline", &baseline);
    CHECK(baseline.value[RESOURCE_CLEANUP_PENDING] == 0 &&
          baseline.value[CONTEXT_CLEANUP_PENDING] == 0 &&
          baseline.value[BACKEND_CLEANUP_PENDING] == 0,
          "baseline has no deferred cleanup");

    CHECK(run_mapping_lifetime_test(control_fd, &baseline, "/dev/dri/card0", 0) == 0,
          "DRM mmap pins its GEM span until munmap");
    CHECK(run_mapping_lifetime_test(control_fd, &baseline, "/dev/dri/renderD128", 1) == 0,
          "PRIME mmap pins its GEM span after dma-buf close");
    CHECK(run_pool_reuse_cycles() == 0,
          "pool survives %d cycles exceeding old cumulative capacity", REUSE_CYCLES);

    uint64_t expected_map_offset = UINT64_MAX;
    for (int round = 0; round < STRESS_ROUNDS; round++) {
        uint64_t mapped_offset = UINT64_MAX;
        errno = 0;
        if (run_round(control_fd, &baseline, round, &mapped_offset) < 0) {
            CHECK(0, "round %d restores all resource counters", round);
            break;
        }
        if (expected_map_offset == UINT64_MAX)
            expected_map_offset = mapped_offset;
        CHECK(mapped_offset == expected_map_offset,
              "round %d reused dumb-pool offset %llu", round,
              (unsigned long long)expected_map_offset);
    }

    struct resource_snapshot final;
    int final_result = read_snapshot(control_fd, &final);
    CHECK(final_result == 0, "read final DRM resource snapshot");
    if (final_result == 0) {
        print_snapshot("final", &final);
        CHECK(reclaimable_counters_equal(&baseline, &final),
              "all reclaimable resource counters returned to baseline after %d rounds",
              STRESS_ROUNDS);
        CHECK(final.value[DUMB_POOL_USED_BYTES] == baseline.value[DUMB_POOL_USED_BYTES],
              "dumb pool live usage returned to baseline");
        CHECK(final.value[DUMB_POOL_HIGH_WATER_BYTES] >=
                  baseline.value[DUMB_POOL_HIGH_WATER_BYTES] &&
              final.value[DUMB_POOL_HIGH_WATER_BYTES] <=
                  final.value[DUMB_POOL_CAPACITY_BYTES],
              "dumb pool high-water mark remains within capacity");
    }
    close(control_fd);

    if (failures == 0) {
        printf("M22_RESOURCE_STRESS_PASS\n");
        return 0;
    }
    printf("M22_RESOURCE_STRESS_FAILED count=%d\n", failures);
    return 1;
}
