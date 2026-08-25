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

#define DRM_MODE_CURSOR_BO 0x01U
#define DRM_MODE_CURSOR_MOVE 0x02U

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

struct drm_mode_destroy_dumb {
    uint32_t handle;
};

struct drm_mode_cursor {
    uint32_t flags;
    uint32_t crtc_id;
    int32_t x;
    int32_t y;
    uint32_t width;
    uint32_t height;
    uint32_t handle;
};

struct drm_mode_cursor2 {
    uint32_t flags;
    uint32_t crtc_id;
    int32_t x;
    int32_t y;
    uint32_t width;
    uint32_t height;
    uint32_t handle;
    int32_t hot_x;
    int32_t hot_y;
};

#define DRM_IOCTL_MODE_CURSOR _IOWR('d', 0xA3, struct drm_mode_cursor)
#define DRM_IOCTL_MODE_CREATE_DUMB _IOWR('d', 0xB2, struct drm_mode_create_dumb)
#define DRM_IOCTL_MODE_MAP_DUMB _IOWR('d', 0xB3, struct drm_mode_map_dumb)
#define DRM_IOCTL_MODE_DESTROY_DUMB _IOWR('d', 0xB4, struct drm_mode_destroy_dumb)
#define DRM_IOCTL_MODE_CURSOR2 _IOWR('d', 0xBB, struct drm_mode_cursor2)

#define CURSOR_WIDTH 64U
#define CURSOR_HEIGHT 64U
#define CRTC_ID 1U

typedef int (*cursor_ioctl_fn)(void *context, unsigned long request, void *argument);

enum cursor_stage {
    CURSOR_STAGE_OK = 0,
    CURSOR_STAGE_SET = 1,
    CURSOR_STAGE_MOVE = 2,
    CURSOR_STAGE_HIDE = 3,
};

static void publish_marker(const char *marker)
{
    puts(marker);
    fflush(stdout);
}

static int run_cursor_sequence(cursor_ioctl_fn call, void *context, uint32_t handle,
                               int publish)
{
    struct drm_mode_cursor2 set = {
        .flags = DRM_MODE_CURSOR_BO | DRM_MODE_CURSOR_MOVE,
        .crtc_id = CRTC_ID,
        .x = 32,
        .y = 24,
        .width = CURSOR_WIDTH,
        .height = CURSOR_HEIGHT,
        .handle = handle,
        .hot_x = 3,
        .hot_y = 5,
    };
    if (call(context, DRM_IOCTL_MODE_CURSOR2, &set) != 0)
        return CURSOR_STAGE_SET;
    if (publish)
        publish_marker("DRM_CURSOR_SET PASS");

    struct drm_mode_cursor move = {
        .flags = DRM_MODE_CURSOR_MOVE,
        .crtc_id = CRTC_ID,
        .x = 96,
        .y = 64,
    };
    if (call(context, DRM_IOCTL_MODE_CURSOR, &move) != 0)
        return CURSOR_STAGE_MOVE;
    if (publish)
        publish_marker("DRM_CURSOR_MOVE PASS");

    struct drm_mode_cursor hide = {
        .flags = DRM_MODE_CURSOR_BO,
        .crtc_id = CRTC_ID,
        .handle = 0,
    };
    if (call(context, DRM_IOCTL_MODE_CURSOR, &hide) != 0)
        return CURSOR_STAGE_HIDE;
    if (publish)
        publish_marker("DRM_CURSOR_HIDE PASS");
    return CURSOR_STAGE_OK;
}

#ifndef DRM_CURSOR_GATE_SELF_TEST
static _Noreturn void hold_forever(void)
{
    for (;;) {
        if (pause() < 0 && errno == EINTR)
            continue;
    }
}
#endif

#if defined(DRM_CURSOR_GATE_SELF_TEST) || defined(DRM_CURSOR_GATE_LIFECYCLE_TEST)

struct fake_context {
    int call_index;
    int fail_at;
};

static int fake_ioctl(void *opaque, unsigned long request, void *argument)
{
    struct fake_context *context = opaque;
    context->call_index++;
    if (context->fail_at == context->call_index)
        return -1;

    if (context->call_index == 1) {
        const struct drm_mode_cursor2 *set = argument;
        return request == DRM_IOCTL_MODE_CURSOR2 &&
                       set->flags == (DRM_MODE_CURSOR_BO | DRM_MODE_CURSOR_MOVE) &&
                       set->crtc_id == CRTC_ID && set->x == 32 && set->y == 24 &&
                       set->width == CURSOR_WIDTH && set->height == CURSOR_HEIGHT &&
                       set->handle == 17 && set->hot_x == 3 && set->hot_y == 5
                   ? 0
                   : -1;
    }
    const struct drm_mode_cursor *cursor = argument;
    if (context->call_index == 2)
        return request == DRM_IOCTL_MODE_CURSOR && cursor->flags == DRM_MODE_CURSOR_MOVE &&
                       cursor->x == 96 && cursor->y == 64
                   ? 0
                   : -1;
    return request == DRM_IOCTL_MODE_CURSOR && cursor->flags == DRM_MODE_CURSOR_BO &&
                   cursor->handle == 0
               ? 0
               : -1;
}

#ifdef DRM_CURSOR_GATE_SELF_TEST
int main(int argc, char **argv)
{
    if (argc != 2)
        return 2;
    int fail_at = 0;
    int expected = CURSOR_STAGE_OK;
    if (strcmp(argv[1], "valid") == 0) {
        fail_at = 0;
    } else if (strcmp(argv[1], "set-error") == 0) {
        fail_at = 1;
        expected = CURSOR_STAGE_SET;
    } else if (strcmp(argv[1], "move-error") == 0) {
        fail_at = 2;
        expected = CURSOR_STAGE_MOVE;
    } else if (strcmp(argv[1], "hide-error") == 0) {
        fail_at = 3;
        expected = CURSOR_STAGE_HIDE;
    } else {
        return 2;
    }
    struct fake_context context = {.call_index = 0, .fail_at = fail_at};
    if (run_cursor_sequence(fake_ioctl, &context, 17, 0) != expected)
        return 1;
    printf("DRM_CURSOR_SELF_TEST PASS case=%s\n", argv[1]);
    return 0;
}
#else
int main(void)
{
    struct fake_context context = {.call_index = 0, .fail_at = 0};
    if (run_cursor_sequence(fake_ioctl, &context, 17, 1) != CURSOR_STAGE_OK)
        return 1;
    publish_marker("ASTERINAS_DRM_CURSOR_R1_READY");
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
    printf("DRM_CURSOR_FAIL stage=%s errno=%d\n", stage, errno);
    fflush(stdout);
    hold_forever();
}

int main(void)
{
    int fd = open("/dev/dri/card0", O_RDWR | O_CLOEXEC);
    if (fd < 0)
        fail_and_hold("open-card0");

    struct drm_mode_create_dumb create = {
        .height = CURSOR_HEIGHT,
        .width = CURSOR_WIDTH,
        .bpp = 32,
    };
    if (ioctl(fd, DRM_IOCTL_MODE_CREATE_DUMB, &create) != 0)
        fail_and_hold("create-dumb");
    if (create.pitch < CURSOR_WIDTH * 4 ||
        create.size < (uint64_t)create.pitch * CURSOR_HEIGHT)
        fail_and_hold("invalid-dumb-layout");

    struct drm_mode_map_dumb map = {.handle = create.handle};
    if (ioctl(fd, DRM_IOCTL_MODE_MAP_DUMB, &map) != 0)
        fail_and_hold("map-dumb");
    if (create.size > SIZE_MAX)
        fail_and_hold("dumb-size-overflow");
    uint8_t *pixels = mmap(NULL, (size_t)create.size, PROT_READ | PROT_WRITE,
                           MAP_SHARED, fd, (off_t)map.offset);
    if (pixels == MAP_FAILED)
        fail_and_hold("mmap-dumb");
    memset(pixels, 0, (size_t)create.size);
    for (uint32_t y = 0; y < CURSOR_HEIGHT; ++y) {
        for (uint32_t x = 0; x < CURSOR_WIDTH; ++x) {
            uint8_t *pixel = pixels + (size_t)y * create.pitch + x * 4;
            pixel[0] = (uint8_t)(x * 4);
            pixel[1] = (uint8_t)(y * 4);
            pixel[2] = 0xff;
            pixel[3] = (x == y || x + y == CURSOR_WIDTH - 1) ? 0xff : 0x80;
        }
    }
    if (msync(pixels, (size_t)create.size, MS_SYNC) != 0)
        fail_and_hold("sync-dumb");

    int stage = run_cursor_sequence(real_ioctl, &fd, create.handle, 1);
    if (stage != CURSOR_STAGE_OK) {
        static const char *const names[] = {"ok", "set", "move", "hide"};
        fail_and_hold(names[stage]);
    }
    if (munmap(pixels, (size_t)create.size) != 0)
        fail_and_hold("munmap-dumb");
    struct drm_mode_destroy_dumb destroy = {.handle = create.handle};
    if (ioctl(fd, DRM_IOCTL_MODE_DESTROY_DUMB, &destroy) != 0)
        fail_and_hold("destroy-dumb");
    close(fd);
    publish_marker("ASTERINAS_DRM_CURSOR_R1_READY");
    hold_forever();
}

#endif
