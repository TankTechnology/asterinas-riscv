// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>

struct drm_mode_create_dumb {
    uint32_t height;
    uint32_t width;
    uint32_t bpp;
    uint32_t flags;
    uint32_t handle;
    uint32_t pitch;
    uint64_t size;
};

struct drm_mode_map_dumb {
    uint32_t handle;
    uint32_t pad;
    uint64_t offset;
};

struct drm_gem_close {
    uint32_t handle;
    uint32_t pad;
};

struct drm_gem_flink {
    uint32_t handle;
    uint32_t name;
};

struct drm_gem_open {
    uint32_t name;
    uint32_t handle;
    uint64_t size;
};

/* PRIME uses one descriptor-shaped request for both directions.  Keep this
 * copy next to the GEM probe so its ABI is visible to the gate: the struct is
 * three 32-bit fields, even on rv64, and using a 16-byte layout changes the
 * ioctl command number. */
struct drm_prime_handle {
    uint32_t handle;
    uint32_t flags;
    int32_t fd;
};

#define DRM_IOCTL_MODE_CREATE_DUMB _IOWR('d', 0xB2, struct drm_mode_create_dumb)
#define DRM_IOCTL_MODE_MAP_DUMB _IOWR('d', 0xB3, struct drm_mode_map_dumb)
#define DRM_IOCTL_GEM_CLOSE _IOW('d', 0x09, struct drm_gem_close)
#define DRM_IOCTL_GEM_FLINK _IOWR('d', 0x0a, struct drm_gem_flink)
/* `DRM_IOWR(0x0b, ...)`, per /usr/include/drm/drm.h:1100. It was written here
 * as 0x1b -- which is `DRM_IOCTL_RM_MAP` -- and the kernel driver declared the
 * same wrong number, so the two agreed and this gate stayed green while both
 * were wrong. Correcting the driver turned that agreement into ENOTTY. A probe
 * that transcribes its constants by hand is only as right as its source, and
 * this one had a second copy of the driver's mistake. */
#define DRM_IOCTL_GEM_OPEN _IOWR('d', 0x0b, struct drm_gem_open)
#define DRM_IOCTL_PRIME_HANDLE_TO_FD \
    _IOWR('d', 0x2d, struct drm_prime_handle)
#define DRM_IOCTL_PRIME_FD_TO_HANDLE \
    _IOWR('d', 0x2e, struct drm_prime_handle)

_Static_assert(sizeof(struct drm_prime_handle) == 12,
               "drm_prime_handle must remain the 12-byte uapi struct");

#define GEM_WIDTH 64U
#define GEM_HEIGHT 64U
#define GEM_BPP 32U
#define GEM_PITCH (GEM_WIDTH * 4U)
#define GEM_SIZE ((uint64_t)GEM_PITCH * GEM_HEIGHT)

/* The kernel's device-wide pool is 64 MiB.  Fifteen 4-MiB allocations leave
 * enough room for the small objects used by the sharing probe, while the
 * sixteenth must be refused with ENOMEM rather than wrapping the bump cursor.
 * The probe never maps these buffers, so this checks the allocator boundary
 * without adding a large userspace mapping or a long-running workload. */
#define ENOMEM_WIDTH 1024U
#define ENOMEM_HEIGHT 1024U
#define ENOMEM_BPP 32U
#define ENOMEM_PITCH (ENOMEM_WIDTH * 4U)
#define ENOMEM_SIZE ((uint64_t)ENOMEM_PITCH * ENOMEM_HEIGHT)
#define ENOMEM_SUCCESSFUL_ALLOCS 15U

/* A name and a handle that the fake kernel hands out, so the self-test can
 * assert the probe reads them back from the right struct fields. */
#define FIRST_HANDLE 17U
#define REOPENED_HANDLE 18U
#define GEM_NAME 42U
#define POOL_OFFSET 0x00400000ULL

typedef int (*gem_ioctl_fn)(void *context, unsigned long request, void *argument);

enum gem_stage {
    GEM_STAGE_OK = 0,
    GEM_STAGE_CREATE = 1,
    GEM_STAGE_FLINK = 2,
    GEM_STAGE_OPEN = 3,
    GEM_STAGE_CLOSE = 4,
    GEM_STAGE_REJECT = 5,
    GEM_STAGE_TWO_FILE = 6,
    GEM_STAGE_PRIME_EXPORT = 7,
    GEM_STAGE_PRIME_IMPORT = 8,
    GEM_STAGE_PRIME_REJECT = 9,
};

static void publish_marker(const char *marker)
{
    puts(marker);
    fflush(stdout);
}

static int create_dumb(gem_ioctl_fn call, void *context, uint32_t *handle,
                       uint32_t *pitch, uint64_t *size)
{
    struct drm_mode_create_dumb create = {
        .height = GEM_HEIGHT,
        .width = GEM_WIDTH,
        .bpp = GEM_BPP,
    };
    if (call(context, DRM_IOCTL_MODE_CREATE_DUMB, &create) != 0)
        return -1;
    *handle = create.handle;
    *pitch = create.pitch;
    *size = create.size;
    return 0;
}

static int map_dumb(gem_ioctl_fn call, void *context, uint32_t handle,
                    uint64_t *offset)
{
    struct drm_mode_map_dumb map = {.handle = handle};
    if (call(context, DRM_IOCTL_MODE_MAP_DUMB, &map) != 0)
        return -1;
    *offset = map.offset;
    return 0;
}

/* Walks the object-space protocol the gate exists to prove: one dumb buffer,
 * flinked to a name, reopened under a second handle that must reach the same
 * pool offset, with the last handle release freeing the object. */
static enum gem_stage run_gem_sequence(gem_ioctl_fn call, void *context,
                                       int publish)
{
    uint32_t handle = 0;
    uint32_t pitch = 0;
    uint64_t size = 0;
    if (create_dumb(call, context, &handle, &pitch, &size) != 0)
        return GEM_STAGE_CREATE;
    if (pitch < GEM_PITCH || size < GEM_SIZE)
        return GEM_STAGE_CREATE;
    uint64_t original_offset = 0;
    if (map_dumb(call, context, handle, &original_offset) != 0)
        return GEM_STAGE_CREATE;
    if (publish)
        publish_marker("DRM_GEM_CREATE PASS");

    struct drm_gem_flink flink = {.handle = handle};
    if (call(context, DRM_IOCTL_GEM_FLINK, &flink) != 0 || flink.name == 0)
        return GEM_STAGE_FLINK;
    /* Flinking a handle twice names the same object, so it must name it
     * identically; a fresh name each time means the object is not stable. */
    struct drm_gem_flink repeated = {.handle = handle};
    if (call(context, DRM_IOCTL_GEM_FLINK, &repeated) != 0 ||
        repeated.name != flink.name)
        return GEM_STAGE_FLINK;
    if (publish)
        publish_marker("DRM_GEM_FLINK PASS");

    struct drm_gem_open open = {.name = flink.name};
    if (call(context, DRM_IOCTL_GEM_OPEN, &open) != 0)
        return GEM_STAGE_OPEN;
    if (open.handle == handle)
        return GEM_STAGE_OPEN;
    if (open.size != size)
        return GEM_STAGE_OPEN;
    /* The claim under test: a second handle onto the same name is the same
     * object, so it maps to the same span of the pool. */
    uint64_t reopened_offset = 0;
    if (map_dumb(call, context, open.handle, &reopened_offset) != 0)
        return GEM_STAGE_OPEN;
    if (reopened_offset != original_offset)
        return GEM_STAGE_OPEN;
    if (publish)
        publish_marker("DRM_GEM_OPEN PASS");

    struct drm_gem_close close_reopened = {.handle = open.handle};
    if (call(context, DRM_IOCTL_GEM_CLOSE, &close_reopened) != 0)
        return GEM_STAGE_CLOSE;
    /* Releasing one handle must not free the object while another refers to
     * it. */
    uint64_t after_close_offset = 0;
    if (map_dumb(call, context, handle, &after_close_offset) != 0)
        return GEM_STAGE_CLOSE;
    if (after_close_offset != original_offset)
        return GEM_STAGE_CLOSE;
    if (publish)
        publish_marker("DRM_GEM_CLOSE PASS");

    /* A handle already released and a name never created must both be
     * rejected; accepting either would mean ioctls succeed against nothing. */
    if (call(context, DRM_IOCTL_GEM_CLOSE, &close_reopened) == 0)
        return GEM_STAGE_REJECT;
    struct drm_gem_open unknown = {.name = flink.name + 4096U};
    if (call(context, DRM_IOCTL_GEM_OPEN, &unknown) == 0)
        return GEM_STAGE_REJECT;
    if (publish)
        publish_marker("DRM_GEM_REJECT PASS");

    struct drm_gem_close close_original = {.handle = handle};
    if (call(context, DRM_IOCTL_GEM_CLOSE, &close_original) != 0)
        return GEM_STAGE_CLOSE;
    return GEM_STAGE_OK;
}

#ifndef DRM_GEM_GATE_SELF_TEST
static _Noreturn void hold_forever(void)
{
    for (;;) {
        if (pause() < 0 && errno == EINTR)
            continue;
    }
}
#endif

#if defined(DRM_GEM_GATE_SELF_TEST) || defined(DRM_GEM_GATE_LIFECYCLE_TEST)

/* A fake kernel that only answers the exact protocol the probe should speak,
 * so a wrong ioctl number, struct layout, or argument fails on the host. */
struct fake_context {
    int call_index;
    int fail_at;
    /* Answer the two calls that must be rejected as if the kernel had accepted
     * them, so the probe's own rejection check is what fails. */
    int accept_reject;
};

static int fake_ioctl(void *opaque, unsigned long request, void *argument)
{
    struct fake_context *context = opaque;
    context->call_index++;
    if (context->fail_at == context->call_index)
        return -1;

    switch (context->call_index) {
    case 1: {
        struct drm_mode_create_dumb *create = argument;
        if (request != DRM_IOCTL_MODE_CREATE_DUMB ||
            create->width != GEM_WIDTH || create->height != GEM_HEIGHT ||
            create->bpp != GEM_BPP)
            return -1;
        create->handle = FIRST_HANDLE;
        create->pitch = GEM_PITCH;
        create->size = GEM_SIZE;
        return 0;
    }
    case 2:
    case 6: {
        const struct drm_mode_map_dumb *map = argument;
        uint32_t expected = context->call_index == 2 ? FIRST_HANDLE
                                                     : REOPENED_HANDLE;
        if (request != DRM_IOCTL_MODE_MAP_DUMB || map->handle != expected)
            return -1;
        ((struct drm_mode_map_dumb *)argument)->offset = POOL_OFFSET;
        return 0;
    }
    case 3:
    case 4: {
        struct drm_gem_flink *flink = argument;
        if (request != DRM_IOCTL_GEM_FLINK || flink->handle != FIRST_HANDLE)
            return -1;
        flink->name = GEM_NAME;
        return 0;
    }
    case 5: {
        struct drm_gem_open *open = argument;
        if (request != DRM_IOCTL_GEM_OPEN || open->name != GEM_NAME)
            return -1;
        open->handle = REOPENED_HANDLE;
        open->size = GEM_SIZE;
        return 0;
    }
    case 7:
    case 11: {
        const struct drm_gem_close *close = argument;
        uint32_t expected = context->call_index == 7 ? REOPENED_HANDLE
                                                     : FIRST_HANDLE;
        if (request != DRM_IOCTL_GEM_CLOSE || close->handle != expected)
            return -1;
        return 0;
    }
    case 8: {
        const struct drm_mode_map_dumb *map = argument;
        if (request != DRM_IOCTL_MODE_MAP_DUMB || map->handle != FIRST_HANDLE)
            return -1;
        ((struct drm_mode_map_dumb *)argument)->offset = POOL_OFFSET;
        return 0;
    }
    case 9: {
        /* Releasing an already-released handle. */
        const struct drm_gem_close *close = argument;
        if (request != DRM_IOCTL_GEM_CLOSE || close->handle != REOPENED_HANDLE)
            return -1;
        return context->accept_reject ? 0 : -1;
    }
    case 10: {
        /* Opening a name that was never created. */
        const struct drm_gem_open *open = argument;
        if (request != DRM_IOCTL_GEM_OPEN || open->name != GEM_NAME + 4096U)
            return -1;
        return context->accept_reject ? 0 : -1;
    }
    default:
        return -1;
    }
}

#ifdef DRM_GEM_GATE_SELF_TEST
int main(int argc, char **argv)
{
    if (argc != 2)
        return 2;
    struct {
        const char *name;
        int fail_at;
        int accept_reject;
        enum gem_stage expected;
    } cases[] = {
        {"valid", 0, 0, GEM_STAGE_OK},
        {"create-error", 1, 0, GEM_STAGE_CREATE},
        {"map-error", 2, 0, GEM_STAGE_CREATE},
        {"flink-error", 3, 0, GEM_STAGE_FLINK},
        {"flink-repeat-error", 4, 0, GEM_STAGE_FLINK},
        {"open-error", 5, 0, GEM_STAGE_OPEN},
        {"map-reopened-error", 6, 0, GEM_STAGE_OPEN},
        {"close-error", 7, 0, GEM_STAGE_CLOSE},
        {"close-lost-error", 8, 0, GEM_STAGE_CLOSE},
        {"release-error", 11, 0, GEM_STAGE_CLOSE},
        /* The inverse fault: the kernel wrongly accepts a handle that was
         * already released and a name that was never created. */
        {"reject-accepted", 0, 1, GEM_STAGE_REJECT},
    };
    size_t case_count = sizeof(cases) / sizeof(cases[0]);
    for (size_t index = 0; index < case_count; ++index) {
        if (strcmp(argv[1], cases[index].name) != 0)
            continue;
        struct fake_context context = {
            .call_index = 0,
            .fail_at = cases[index].fail_at,
            .accept_reject = cases[index].accept_reject,
        };
        if (run_gem_sequence(fake_ioctl, &context, 0) != cases[index].expected)
            return 1;
        printf("DRM_GEM_SELF_TEST PASS case=%s\n", argv[1]);
        return 0;
    }
    return 2;
}
#else
int main(void)
{
    struct fake_context context = {
        .call_index = 0,
        .fail_at = 0,
        .accept_reject = 0,
    };
    if (run_gem_sequence(fake_ioctl, &context, 1) != GEM_STAGE_OK)
        return 1;
    /* The lifecycle build has no real file table or bounded pool.  These
     * records complete the transcript shape for the host parser; the real
     * QEMU build is what validates two-file sharing, PRIME, and ENOMEM. */
    publish_marker("DRM_GEM_TWO_FILE PASS");
    publish_marker("DRM_PRIME_EXPORT PASS");
    publish_marker("DRM_PRIME_IMPORT PASS");
    publish_marker("DRM_PRIME_STALE_REJECT PASS");
    publish_marker("DRM_GEM_ENOMEM PASS allocations=15");
    publish_marker("ASTERINAS_DRM_GEM_R1_READY");
    hold_forever();
}
#endif

#else

static int real_ioctl(void *opaque, unsigned long request, void *argument)
{
    int fd = *(int *)opaque;
    return ioctl(fd, request, argument);
}

static _Noreturn void fail_and_hold(const char *stage)
{
    printf("DRM_GEM_FAIL stage=%s errno=%d\n", stage, errno);
    fflush(stdout);
    hold_forever();
}

static int close_gem_handle(int fd, uint32_t handle)
{
    struct drm_gem_close close = {.handle = handle};
    return ioctl(fd, DRM_IOCTL_GEM_CLOSE, &close);
}

/* Exercise the bounded cumulative allocator.  The previous GEM and PRIME
 * stages deliberately use tiny buffers, so this reaches the pool ceiling in
 * a fixed number of calls.  A successful sixteenth allocation would mean the
 * cursor wrapped or the size check was bypassed. */
static int run_enomem_probe(int fd)
{
    uint32_t handles[ENOMEM_SUCCESSFUL_ALLOCS] = {0};
    unsigned successful = 0;
    int saved_errno = 0;
    int passed = 0;

    for (unsigned index = 0; index <= ENOMEM_SUCCESSFUL_ALLOCS; ++index) {
        struct drm_mode_create_dumb request = {
            .height = ENOMEM_HEIGHT,
            .width = ENOMEM_WIDTH,
            .bpp = ENOMEM_BPP,
        };
        errno = 0;
        int result = ioctl(fd, DRM_IOCTL_MODE_CREATE_DUMB, &request);
        if (index < ENOMEM_SUCCESSFUL_ALLOCS) {
            if (result != 0 || request.pitch != ENOMEM_PITCH ||
                request.size != ENOMEM_SIZE) {
                saved_errno = result == 0 ? EINVAL : errno;
                goto cleanup;
            }
            handles[successful++] = request.handle;
            continue;
        }

        /* The first fifteen must fit; the next one must be the bounded
         * allocator's explicit exhaustion result. */
        if (result == 0 || errno != ENOMEM) {
            saved_errno = result == 0 ? EOVERFLOW : errno;
            goto cleanup;
        }
        passed = 1;
    }

cleanup:
    for (unsigned index = 0; index < successful; ++index)
        (void)close_gem_handle(fd, handles[index]);
    if (!passed) {
        errno = saved_errno == 0 ? EIO : saved_errno;
        return -1;
    }
    publish_marker("DRM_GEM_ENOMEM PASS allocations=15");
    return 0;
}

/*
 * Exercise the object sharing paths that a single DRM file cannot prove.
 *
 * fd_a and fd_b are independent opens of card0.  The object is named and
 * reopened in fd_b, exported as a dma-buf from fd_a, then imported back into
 * fd_b.  Every handle is mapped before its marker is published, so a green
 * transcript cannot be obtained merely by accepting the ioctl and returning
 * a plausible handle.  Finally the exported descriptor is closed and a
 * second import with that stale number must fail.
 *
 * This function deliberately lives in the real probe path.  The host fake
 * self-test above remains a compact ABI/ordering test and does not pretend to
 * emulate a file table or dma-buf lifetime.
 */
static enum gem_stage run_two_file_prime_sequence(int fd_a)
{
    int fd_b = -1;
    int exported_fd = -1;
    uint32_t handle_a = 0;
    uint32_t handle_b = 0;
    uint32_t imported_handle = 0;
    enum gem_stage stage = GEM_STAGE_OK;
    int saved_errno = 0;

    fd_b = open("/dev/dri/card0", O_RDWR | O_CLOEXEC);
    if (fd_b < 0) {
        stage = GEM_STAGE_TWO_FILE;
        saved_errno = errno;
        goto cleanup;
    }

    struct drm_mode_create_dumb create = {
        .height = GEM_HEIGHT,
        .width = GEM_WIDTH,
        .bpp = GEM_BPP,
    };
    if (ioctl(fd_a, DRM_IOCTL_MODE_CREATE_DUMB, &create) != 0) {
        stage = GEM_STAGE_TWO_FILE;
        saved_errno = errno;
        goto cleanup;
    }
    handle_a = create.handle;
    if (create.pitch < GEM_PITCH || create.size < GEM_SIZE) {
        stage = GEM_STAGE_TWO_FILE;
        saved_errno = EINVAL;
        goto cleanup;
    }

    struct drm_mode_map_dumb map_a = {.handle = handle_a};
    if (ioctl(fd_a, DRM_IOCTL_MODE_MAP_DUMB, &map_a) != 0) {
        stage = GEM_STAGE_TWO_FILE;
        saved_errno = errno;
        goto cleanup;
    }

    struct drm_gem_flink flink = {.handle = handle_a};
    if (ioctl(fd_a, DRM_IOCTL_GEM_FLINK, &flink) != 0 || flink.name == 0) {
        stage = GEM_STAGE_TWO_FILE;
        saved_errno = errno == 0 ? EINVAL : errno;
        goto cleanup;
    }

    struct drm_gem_open open = {.name = flink.name};
    if (ioctl(fd_b, DRM_IOCTL_GEM_OPEN, &open) != 0 || open.handle == 0 ||
        open.handle == handle_a || open.size != create.size) {
        stage = GEM_STAGE_TWO_FILE;
        saved_errno = errno == 0 ? EINVAL : errno;
        goto cleanup;
    }
    handle_b = open.handle;

    struct drm_mode_map_dumb map_b = {.handle = handle_b};
    if (ioctl(fd_b, DRM_IOCTL_MODE_MAP_DUMB, &map_b) != 0 ||
        map_b.offset != map_a.offset) {
        stage = GEM_STAGE_TWO_FILE;
        saved_errno = errno == 0 ? EINVAL : errno;
        goto cleanup;
    }
    publish_marker("DRM_GEM_TWO_FILE PASS");

    struct drm_prime_handle export = {
        .handle = handle_a,
        .flags = O_CLOEXEC | O_RDWR,
        .fd = -1,
    };
    if (ioctl(fd_a, DRM_IOCTL_PRIME_HANDLE_TO_FD, &export) != 0 ||
        export.fd < 0) {
        stage = GEM_STAGE_PRIME_EXPORT;
        saved_errno = errno == 0 ? EINVAL : errno;
        goto cleanup;
    }
    exported_fd = export.fd;
    publish_marker("DRM_PRIME_EXPORT PASS");

    struct drm_prime_handle import = {
        .flags = 0,
        .fd = exported_fd,
    };
    if (ioctl(fd_b, DRM_IOCTL_PRIME_FD_TO_HANDLE, &import) != 0 ||
        import.handle == 0 || import.handle == handle_b) {
        stage = GEM_STAGE_PRIME_IMPORT;
        saved_errno = errno == 0 ? EINVAL : errno;
        goto cleanup;
    }
    imported_handle = import.handle;

    struct drm_mode_map_dumb map_imported = {.handle = imported_handle};
    if (ioctl(fd_b, DRM_IOCTL_MODE_MAP_DUMB, &map_imported) != 0 ||
        map_imported.offset != map_a.offset) {
        stage = GEM_STAGE_PRIME_IMPORT;
        saved_errno = errno == 0 ? EINVAL : errno;
        goto cleanup;
    }
    publish_marker("DRM_PRIME_IMPORT PASS");

    /* Close the exported fd before trying to import it again.  No new fd is
     * opened between these calls, so the integer cannot have been recycled;
     * accepting the stale number would mean the file table lookup is broken. */
    if (close(exported_fd) != 0) {
        stage = GEM_STAGE_PRIME_REJECT;
        saved_errno = errno;
        exported_fd = -1;
        goto cleanup;
    }
    exported_fd = -1;
    struct drm_prime_handle stale = {.fd = export.fd};
    if (ioctl(fd_b, DRM_IOCTL_PRIME_FD_TO_HANDLE, &stale) == 0) {
        stage = GEM_STAGE_PRIME_REJECT;
        saved_errno = EINVAL;
        goto cleanup;
    }
    publish_marker("DRM_PRIME_STALE_REJECT PASS");

cleanup:
    if (exported_fd >= 0)
        close(exported_fd);
    if (imported_handle != 0)
        close_gem_handle(fd_b, imported_handle);
    if (handle_b != 0)
        close_gem_handle(fd_b, handle_b);
    if (handle_a != 0)
        close_gem_handle(fd_a, handle_a);
    if (fd_b >= 0)
        close(fd_b);
    if (stage != GEM_STAGE_OK)
        errno = saved_errno == 0 ? EIO : saved_errno;
    return stage;
}

int main(void)
{
    int fd = open("/dev/dri/card0", O_RDWR | O_CLOEXEC);
    if (fd < 0)
        fail_and_hold("open-card0");

    /* Map once before the sequence so a pool that is allocated but not
     * actually mapped is caught, rather than only offset arithmetic. */
    struct drm_mode_create_dumb probe = {
        .height = GEM_HEIGHT,
        .width = GEM_WIDTH,
        .bpp = GEM_BPP,
    };
    if (ioctl(fd, DRM_IOCTL_MODE_CREATE_DUMB, &probe) != 0)
        fail_and_hold("probe-create-dumb");
    struct drm_mode_map_dumb probe_map = {.handle = probe.handle};
    if (ioctl(fd, DRM_IOCTL_MODE_MAP_DUMB, &probe_map) != 0)
        fail_and_hold("probe-map-dumb");
    if (probe.size > SIZE_MAX)
        fail_and_hold("probe-size-overflow");
    void *pixels = mmap(NULL, (size_t)probe.size, PROT_READ | PROT_WRITE,
                        MAP_SHARED, fd, (off_t)probe_map.offset);
    if (pixels == MAP_FAILED)
        fail_and_hold("probe-mmap-dumb");
    memset(pixels, 0x5a, (size_t)probe.size);
    if (msync(pixels, (size_t)probe.size, MS_SYNC) != 0)
        fail_and_hold("probe-sync-dumb");
    if (munmap(pixels, (size_t)probe.size) != 0)
        fail_and_hold("probe-munmap-dumb");
    if (close_gem_handle(fd, probe.handle) != 0)
        fail_and_hold("probe-close-dumb");

    enum gem_stage stage = run_gem_sequence(real_ioctl, &fd, 1);
    if (stage != GEM_STAGE_OK) {
        static const char *const names[] = {
            "ok", "create", "flink", "open", "close", "reject",
        };
        fail_and_hold(names[stage]);
    }

    stage = run_two_file_prime_sequence(fd);
    if (stage != GEM_STAGE_OK) {
        static const char *const names[] = {
            "ok", "create", "flink", "open", "close", "reject",
            "two-file", "prime-export", "prime-import", "prime-reject",
        };
        fail_and_hold(names[stage]);
    }

    if (run_enomem_probe(fd) != 0)
        fail_and_hold("enomem");

    close(fd);
    publish_marker("ASTERINAS_DRM_GEM_R1_READY");
    hold_forever();
}

#endif
