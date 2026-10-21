// SPDX-License-Identifier: MPL-2.0

// Acceptance probe for the DRM sync-object surface.
//
// Covers create (plain and signaled), query, non-blocking and blocking waits,
// signal/reset, timeline signal/wait/query, WAIT_ALL semantics, a cross-process
// wait/signal handshake, and the error cases (destroy twice, wait on a
// destroyed handle, wait without a timeout on an unsignaled object).
//
// As PID 1 it mounts devtmpfs first, so it can be used as a one-file initramfs
// `/init`.

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define DRM_IOCTL_BASE 'd'

struct drm_get_cap {
    uint64_t capability;
    uint64_t value;
};

struct drm_syncobj_create {
    uint32_t handle;
    uint32_t flags;
};

struct drm_syncobj_destroy {
    uint32_t handle;
    uint32_t pad;
};

struct drm_syncobj_array {
    uint64_t handles;
    uint32_t count_handles;
    uint32_t pad;
};

struct drm_syncobj_timeline_array {
    uint64_t handles;
    uint64_t points;
    uint32_t count_handles;
    uint32_t flags;
};

struct drm_syncobj_wait {
    uint64_t handles;
    int64_t timeout_nsec;
    uint32_t count_handles;
    uint32_t flags;
    uint32_t first_signaled;
    uint32_t pad;
    uint64_t deadline_nsec;
};

struct drm_syncobj_timeline_wait {
    uint64_t handles;
    uint64_t points;
    int64_t timeout_nsec;
    uint32_t count_handles;
    uint32_t flags;
    uint32_t first_signaled;
    uint32_t pad;
    uint64_t deadline_nsec;
};

#define DRM_IOCTL_GET_CAP _IOWR(DRM_IOCTL_BASE, 0x0c, struct drm_get_cap)
#define DRM_IOCTL_SYNCOBJ_CREATE \
    _IOWR(DRM_IOCTL_BASE, 0xbf, struct drm_syncobj_create)
#define DRM_IOCTL_SYNCOBJ_DESTROY \
    _IOWR(DRM_IOCTL_BASE, 0xc0, struct drm_syncobj_destroy)
#define DRM_IOCTL_SYNCOBJ_WAIT _IOWR(DRM_IOCTL_BASE, 0xc3, struct drm_syncobj_wait)
#define DRM_IOCTL_SYNCOBJ_RESET _IOWR(DRM_IOCTL_BASE, 0xc4, struct drm_syncobj_array)
#define DRM_IOCTL_SYNCOBJ_SIGNAL \
    _IOWR(DRM_IOCTL_BASE, 0xc5, struct drm_syncobj_array)
#define DRM_IOCTL_SYNCOBJ_TIMELINE_WAIT \
    _IOWR(DRM_IOCTL_BASE, 0xca, struct drm_syncobj_timeline_wait)
#define DRM_IOCTL_SYNCOBJ_QUERY \
    _IOWR(DRM_IOCTL_BASE, 0xcb, struct drm_syncobj_timeline_array)
#define DRM_IOCTL_SYNCOBJ_TIMELINE_SIGNAL \
    _IOWR(DRM_IOCTL_BASE, 0xcd, struct drm_syncobj_timeline_array)

#define DRM_CAP_SYNCOBJ 0x13
#define DRM_CAP_SYNCOBJ_TIMELINE 0x14
#define SYNC_CREATE_SIGNALED (1u << 0)
#define WAIT_ALL (1u << 0)

static int card = -1;

static void fail(const char *step) {
    printf("SYNCPROBE_FAIL %s errno=%d\n", step, errno);
    exit(1);
}

static void create_syncobj(uint32_t flags, uint32_t *handle) {
    struct drm_syncobj_create create = {.flags = flags};
    if (ioctl(card, DRM_IOCTL_SYNCOBJ_CREATE, &create) != 0) {
        fail("create");
    }
    *handle = create.handle;
}

static int wait_handles(const uint32_t *handles, uint32_t count, uint32_t flags,
                        int64_t timeout_nsec, uint32_t *first_signaled) {
    struct drm_syncobj_wait wait = {
        .handles = (uint64_t)(uintptr_t)handles,
        .timeout_nsec = timeout_nsec,
        .count_handles = count,
        .flags = flags,
    };
    int result = ioctl(card, DRM_IOCTL_SYNCOBJ_WAIT, &wait);
    if (first_signaled != NULL) {
        *first_signaled = wait.first_signaled;
    }
    return result;
}

static uint64_t query_point(uint32_t handle) {
    uint64_t point = 0xdeadbeef;
    struct drm_syncobj_timeline_array query = {
        .handles = (uint64_t)(uintptr_t)&handle,
        .points = (uint64_t)(uintptr_t)&point,
        .count_handles = 1,
    };
    if (ioctl(card, DRM_IOCTL_SYNCOBJ_QUERY, &query) != 0) {
        fail("query");
    }
    return point;
}

static void signal_handles(const uint32_t *handles, uint32_t count) {
    struct drm_syncobj_array array = {
        .handles = (uint64_t)(uintptr_t)handles,
        .count_handles = count,
    };
    if (ioctl(card, DRM_IOCTL_SYNCOBJ_SIGNAL, &array) != 0) {
        fail("signal");
    }
}

static void reset_handles(const uint32_t *handles, uint32_t count) {
    struct drm_syncobj_array array = {
        .handles = (uint64_t)(uintptr_t)handles,
        .count_handles = count,
    };
    if (ioctl(card, DRM_IOCTL_SYNCOBJ_RESET, &array) != 0) {
        fail("reset");
    }
}

static void timeline_signal(uint32_t handle, uint64_t point) {
    struct drm_syncobj_timeline_array array = {
        .handles = (uint64_t)(uintptr_t)&handle,
        .points = (uint64_t)(uintptr_t)&point,
        .count_handles = 1,
    };
    if (ioctl(card, DRM_IOCTL_SYNCOBJ_TIMELINE_SIGNAL, &array) != 0) {
        fail("timeline-signal");
    }
}

static int timeline_wait(uint32_t handle, uint64_t point, int64_t timeout_nsec) {
    struct drm_syncobj_timeline_wait wait = {
        .handles = (uint64_t)(uintptr_t)&handle,
        .points = (uint64_t)(uintptr_t)&point,
        .timeout_nsec = timeout_nsec,
        .count_handles = 1,
    };
    return ioctl(card, DRM_IOCTL_SYNCOBJ_TIMELINE_WAIT, &wait);
}

static void destroy_syncobj(uint32_t handle) {
    struct drm_syncobj_destroy destroy = {.handle = handle};
    if (ioctl(card, DRM_IOCTL_SYNCOBJ_DESTROY, &destroy) != 0) {
        fail("destroy");
    }
}

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);

    if (getpid() == 1) {
        mkdir("/dev", 0755);
        if (mount("devtmpfs", "/dev", "devtmpfs", 0, NULL) != 0 && errno != EBUSY) {
            fail("mount-devtmpfs");
        }
    }

    card = open("/dev/dri/card0", O_RDWR | O_CLOEXEC);
    if (card < 0) {
        fail("open-card");
    }

    struct drm_get_cap cap = {.capability = DRM_CAP_SYNCOBJ};
    if (ioctl(card, DRM_IOCTL_GET_CAP, &cap) != 0 || cap.value != 1) {
        fail("cap-syncobj");
    }
    cap.capability = DRM_CAP_SYNCOBJ_TIMELINE;
    if (ioctl(card, DRM_IOCTL_GET_CAP, &cap) != 0 || cap.value != 1) {
        fail("cap-syncobj-timeline");
    }
    printf("SYNCPROBE_CAP syncobj=1 timeline=1\n");

    uint32_t plain, signaled;
    create_syncobj(0, &plain);
    create_syncobj(SYNC_CREATE_SIGNALED, &signaled);
    printf("SYNCPROBE_CREATE plain=%u signaled=%u\n", plain, signaled);

    if (query_point(plain) != 0 || query_point(signaled) != 1) {
        fail("query-initial");
    }
    printf("SYNCPROBE_QUERY initial=0/1\n");

    // A zero timeout is a non-blocking probe.
    errno = 0;
    if (wait_handles(&plain, 1, 0, 0, NULL) == 0 || errno != ETIME) {
        fail("wait-unsignaled-nonblocking");
    }
    if (wait_handles(&signaled, 1, 0, 0, NULL) != 0) {
        fail("wait-signaled");
    }
    printf("SYNCPROBE_WAIT nonblocking=ok\n");

    signal_handles(&plain, 1);
    if (query_point(plain) != 1) {
        fail("query-after-signal");
    }
    if (wait_handles(&plain, 1, 0, 0, NULL) != 0) {
        fail("wait-after-signal");
    }
    printf("SYNCPROBE_SIGNAL ok\n");

    reset_handles(&plain, 1);
    errno = 0;
    if (wait_handles(&plain, 1, 0, 0, NULL) == 0 || errno != ETIME) {
        fail("wait-after-reset");
    }
    printf("SYNCPROBE_RESET ok\n");

    // WAIT_ALL over one signaled and one unsignaled object must not succeed.
    uint32_t mixed[2] = {signaled, plain};
    errno = 0;
    if (wait_handles(mixed, 2, WAIT_ALL, 0, NULL) == 0 || errno != ETIME) {
        fail("wait-all-mixed");
    }
    uint32_t first = 0xffffffff;
    if (wait_handles(mixed, 2, 0, 0, &first) != 0 || first != 0) {
        fail("wait-any-first-signaled");
    }
    printf("SYNCPROBE_WAIT_ALL first_signaled=%u\n", first);

    // Timeline: point 5 is signaled, point 6 is not, point 0 is always ready.
    timeline_signal(plain, 5);
    if (query_point(plain) != 5) {
        fail("query-timeline");
    }
    if (timeline_wait(plain, 5, 0) != 0 || timeline_wait(plain, 0, 0) != 0) {
        fail("timeline-wait-satisfied");
    }
    errno = 0;
    if (timeline_wait(plain, 6, 0) == 0 || errno != ETIME) {
        fail("timeline-wait-future");
    }
    printf("SYNCPROBE_TIMELINE point=5\n");

    // Cross-process handshake: the child waits, the parent signals.
    reset_handles(&plain, 1);
    pid_t child = fork();
    if (child < 0) {
        fail("fork");
    }
    if (child == 0) {
        // 500 ms budget; the parent signals well inside it.
        int result = wait_handles(&plain, 1, 0, -500000000LL, NULL);
        if (result != 0) {
            printf("SYNCPROBE_CHILD_FAIL wait errno=%d\n", errno);
            _exit(2);
        }
        printf("SYNCPROBE_CHILD_PASS\n");
        _exit(0);
    }
    struct timespec pause = {.tv_sec = 0, .tv_nsec = 100 * 1000 * 1000};
    nanosleep(&pause, NULL);
    signal_handles(&plain, 1);
    int status = 0;
    waitpid(child, &status, 0);
    if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
        printf("SYNCPROBE_FAIL child status=%d\n", status);
        return 1;
    }
    printf("SYNCPROBE_CROSS_PROCESS ok\n");

    destroy_syncobj(plain);
    destroy_syncobj(signaled);

    errno = 0;
    if (wait_handles(&plain, 1, 0, 0, NULL) == 0 || errno != ENOENT) {
        fail("wait-destroyed");
    }
    errno = 0;
    struct drm_syncobj_destroy again = {.handle = plain};
    if (ioctl(card, DRM_IOCTL_SYNCOBJ_DESTROY, &again) == 0 || errno != ENOENT) {
        fail("destroy-twice");
    }
    printf("SYNCPROBE_ERRORS ok\n");

    printf("SYNCPROBE_PASS\n");

    if (getpid() == 1) {
        sleep(3600);
    }
    return 0;
}
