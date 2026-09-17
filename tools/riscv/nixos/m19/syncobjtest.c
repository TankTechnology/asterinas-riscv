// SPDX-License-Identifier: MPL-2.0

// Exercises the public DRM syncobj/timeline ABI without libdrm. This catches
// wire-layout, timeout, sharing, eventfd, and cross-thread wakeup regressions.

#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/eventfd.h>
#include <sys/ioctl.h>
#include <time.h>
#include <unistd.h>

#define DRM_IOWR(nr, type) _IOWR('d', nr, type)

struct drm_get_cap { uint64_t capability, value; };
struct drm_syncobj_create { uint32_t handle, flags; };
struct drm_syncobj_destroy { uint32_t handle, pad; };
struct drm_syncobj_handle {
    uint32_t handle, flags;
    int32_t fd;
    uint32_t pad;
    uint64_t point;
};
struct drm_syncobj_wait {
    uint64_t handles;
    int64_t timeout_nsec;
    uint32_t count_handles, flags, first_signaled, pad;
    uint64_t deadline_nsec;
};
struct drm_syncobj_timeline_wait {
    uint64_t handles, points;
    int64_t timeout_nsec;
    uint32_t count_handles, flags, first_signaled, pad;
    uint64_t deadline_nsec;
};
struct drm_syncobj_array { uint64_t handles; uint32_t count_handles, pad; };
struct drm_syncobj_timeline_array {
    uint64_t handles, points;
    uint32_t count_handles, flags;
};
struct drm_syncobj_transfer {
    uint32_t src_handle, dst_handle;
    uint64_t src_point, dst_point;
    uint32_t flags, pad;
};
struct drm_syncobj_eventfd {
    uint32_t handle, flags;
    uint64_t point;
    int32_t fd;
    uint32_t pad;
};
struct drm_virtgpu_execbuffer {
    uint32_t flags, size;
    uint64_t command, bo_handles;
    uint32_t num_bo_handles;
    int32_t fence_fd;
    uint32_t ring_idx, syncobj_stride, num_in_syncobjs, num_out_syncobjs;
    uint64_t in_syncobjs, out_syncobjs;
};
struct drm_virtgpu_execbuffer_syncobj {
    uint32_t handle, flags;
    uint64_t point;
};

#define DRM_IOCTL_GET_CAP DRM_IOWR(0x0c, struct drm_get_cap)
#define DRM_IOCTL_SYNCOBJ_CREATE DRM_IOWR(0xbf, struct drm_syncobj_create)
#define DRM_IOCTL_SYNCOBJ_DESTROY DRM_IOWR(0xc0, struct drm_syncobj_destroy)
#define DRM_IOCTL_SYNCOBJ_HANDLE_TO_FD DRM_IOWR(0xc1, struct drm_syncobj_handle)
#define DRM_IOCTL_SYNCOBJ_FD_TO_HANDLE DRM_IOWR(0xc2, struct drm_syncobj_handle)
#define DRM_IOCTL_SYNCOBJ_WAIT DRM_IOWR(0xc3, struct drm_syncobj_wait)
#define DRM_IOCTL_SYNCOBJ_RESET DRM_IOWR(0xc4, struct drm_syncobj_array)
#define DRM_IOCTL_SYNCOBJ_SIGNAL DRM_IOWR(0xc5, struct drm_syncobj_array)
#define DRM_IOCTL_SYNCOBJ_TIMELINE_WAIT DRM_IOWR(0xca, struct drm_syncobj_timeline_wait)
#define DRM_IOCTL_SYNCOBJ_QUERY DRM_IOWR(0xcb, struct drm_syncobj_timeline_array)
#define DRM_IOCTL_SYNCOBJ_TRANSFER DRM_IOWR(0xcc, struct drm_syncobj_transfer)
#define DRM_IOCTL_SYNCOBJ_TIMELINE_SIGNAL DRM_IOWR(0xcd, struct drm_syncobj_timeline_array)
#define DRM_IOCTL_SYNCOBJ_EVENTFD DRM_IOWR(0xcf, struct drm_syncobj_eventfd)
#define DRM_IOCTL_VIRTGPU_EXECBUFFER DRM_IOWR(0x42, struct drm_virtgpu_execbuffer)

#define DRM_CAP_SYNCOBJ 0x13
#define DRM_CAP_SYNCOBJ_TIMELINE 0x14
#define SYNCOBJ_CREATE_SIGNALED (1u << 0)
#define WAIT_ALL (1u << 0)
#define WAIT_FOR_SUBMIT (1u << 1)
#define WAIT_AVAILABLE (1u << 2)
#define QUERY_LAST_SUBMITTED (1u << 0)
#define FD_SYNC_FILE (1u << 0)
#define FD_TIMELINE (1u << 1)
#define EXECBUF_SYNCOBJ_RESET (1u << 0)
#define EXECBUF_FENCE_FD_OUT (1u << 1)
#define MAX_EVENT_WATCHERS 4096u

static void fail(const char *stage) {
    printf("M19_SYNCOBJ_FAIL %s errno=%d\n", stage, errno);
    exit(1);
}

static void stage(const char *name) {
    printf("M19_SYNCOBJ_STAGE %s\n", name);
    fflush(stdout);
}

static uint64_t cap(int fd, uint64_t id) {
    struct drm_get_cap request = { .capability = id };
    if (ioctl(fd, DRM_IOCTL_GET_CAP, &request) < 0) fail("get_cap");
    return request.value;
}

static uint32_t create(int fd, uint32_t flags) {
    struct drm_syncobj_create request = { .flags = flags };
    if (ioctl(fd, DRM_IOCTL_SYNCOBJ_CREATE, &request) < 0 || request.handle == 0)
        fail("create");
    return request.handle;
}

static void destroy(int fd, uint32_t handle) {
    struct drm_syncobj_destroy request = { .handle = handle };
    if (ioctl(fd, DRM_IOCTL_SYNCOBJ_DESTROY, &request) < 0) fail("destroy");
}

static void array_ioctl(int fd, unsigned long command, uint32_t handle) {
    struct drm_syncobj_array request = {
        .handles = (uint64_t)(uintptr_t)&handle,
        .count_handles = 1,
    };
    if (ioctl(fd, command, &request) < 0) fail("array_ioctl");
}

static void timeline_signal(int fd, uint32_t handle, uint64_t point) {
    struct drm_syncobj_timeline_array request = {
        .handles = (uint64_t)(uintptr_t)&handle,
        .points = (uint64_t)(uintptr_t)&point,
        .count_handles = 1,
    };
    if (ioctl(fd, DRM_IOCTL_SYNCOBJ_TIMELINE_SIGNAL, &request) < 0)
        fail("timeline_signal");
}

static uint64_t query(int fd, uint32_t handle, uint32_t flags) {
    uint64_t point = UINT64_MAX;
    struct drm_syncobj_timeline_array request = {
        .handles = (uint64_t)(uintptr_t)&handle,
        .points = (uint64_t)(uintptr_t)&point,
        .count_handles = 1,
        .flags = flags,
    };
    if (ioctl(fd, DRM_IOCTL_SYNCOBJ_QUERY, &request) < 0) fail("query");
    return point;
}

static int64_t deadline_after_ms(long milliseconds) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) < 0) fail("clock_gettime");
    return (int64_t)now.tv_sec * 1000000000LL + now.tv_nsec + milliseconds * 1000000LL;
}

static void timeline_wait(int fd, uint32_t handle, uint64_t point, uint32_t flags) {
    struct drm_syncobj_timeline_wait request = {
        .handles = (uint64_t)(uintptr_t)&handle,
        .points = (uint64_t)(uintptr_t)&point,
        .timeout_nsec = deadline_after_ms(2000),
        .count_handles = 1,
        .flags = flags,
    };
    if (ioctl(fd, DRM_IOCTL_SYNCOBJ_TIMELINE_WAIT, &request) < 0 ||
        request.first_signaled != 0)
        fail("timeline_wait");
}

struct wait_thread {
    int fd;
    uint32_t handle;
    uint64_t point;
    int result;
    int error;
};

static void *wait_future(void *opaque) {
    struct wait_thread *thread = opaque;
    struct drm_syncobj_timeline_wait request = {
        .handles = (uint64_t)(uintptr_t)&thread->handle,
        .points = (uint64_t)(uintptr_t)&thread->point,
        .timeout_nsec = deadline_after_ms(2000),
        .count_handles = 1,
        .flags = WAIT_FOR_SUBMIT,
    };
    thread->result = ioctl(thread->fd, DRM_IOCTL_SYNCOBJ_TIMELINE_WAIT, &request);
    thread->error = errno;
    return NULL;
}

int main(void) {
    int fd = open("/dev/dri/renderD128", O_RDWR | O_CLOEXEC);
    if (fd < 0) fail("open");
    if (cap(fd, DRM_CAP_SYNCOBJ) != 1 || cap(fd, DRM_CAP_SYNCOBJ_TIMELINE) != 1)
        fail("caps");
    stage("caps");

    uint32_t first = create(fd, 0);
    struct drm_syncobj_wait binary = {
        .handles = (uint64_t)(uintptr_t)&first,
        .timeout_nsec = 0,
        .count_handles = 1,
    };
    errno = 0;
    if (ioctl(fd, DRM_IOCTL_SYNCOBJ_WAIT, &binary) != -1 || errno != EINVAL)
        fail("empty_binary_wait");
    binary.flags = WAIT_FOR_SUBMIT;
    errno = 0;
    if (ioctl(fd, DRM_IOCTL_SYNCOBJ_WAIT, &binary) != -1 || errno != ETIME)
        fail("empty_submit_poll");
    array_ioctl(fd, DRM_IOCTL_SYNCOBJ_SIGNAL, first);
    binary.flags = 0;
    if (ioctl(fd, DRM_IOCTL_SYNCOBJ_WAIT, &binary) < 0 || binary.first_signaled != 0)
        fail("binary_signal_wait");
    array_ioctl(fd, DRM_IOCTL_SYNCOBJ_RESET, first);
    stage("binary");

    timeline_signal(fd, first, 5);
    if (query(fd, first, 0) != 5 || query(fd, first, QUERY_LAST_SUBMITTED) != 5)
        fail("timeline_query");
    timeline_wait(fd, first, 4, WAIT_ALL);
    uint64_t unavailable = 6;
    struct drm_syncobj_timeline_wait available = {
        .handles = (uint64_t)(uintptr_t)&first,
        .points = (uint64_t)(uintptr_t)&unavailable,
        .timeout_nsec = 0,
        .count_handles = 1,
        .flags = WAIT_AVAILABLE,
    };
    errno = 0;
    if (ioctl(fd, DRM_IOCTL_SYNCOBJ_TIMELINE_WAIT, &available) != -1 || errno != ETIME)
        fail("timeline_available_poll");
    stage("timeline");

    uint32_t second = create(fd, 0);
    struct drm_syncobj_transfer transfer = {
        .src_handle = first,
        .dst_handle = second,
        .src_point = 5,
        .dst_point = 9,
    };
    if (ioctl(fd, DRM_IOCTL_SYNCOBJ_TRANSFER, &transfer) < 0 || query(fd, second, 0) != 9)
        fail("transfer");

    struct drm_syncobj_handle share = { .handle = second };
    if (ioctl(fd, DRM_IOCTL_SYNCOBJ_HANDLE_TO_FD, &share) < 0 || share.fd < 0)
        fail("handle_to_fd");
    struct drm_syncobj_handle imported = { .fd = share.fd };
    if (ioctl(fd, DRM_IOCTL_SYNCOBJ_FD_TO_HANDLE, &imported) < 0 || imported.handle == 0)
        fail("fd_to_handle");
    array_ioctl(fd, DRM_IOCTL_SYNCOBJ_RESET, second);
    array_ioctl(fd, DRM_IOCTL_SYNCOBJ_SIGNAL, imported.handle);
    binary.handles = (uint64_t)(uintptr_t)&second;
    if (ioctl(fd, DRM_IOCTL_SYNCOBJ_WAIT, &binary) < 0) fail("shared_signal");
    close(share.fd);

    struct drm_syncobj_handle sync_file = {
        .handle = first,
        .flags = FD_SYNC_FILE | FD_TIMELINE,
        .point = 5,
    };
    if (ioctl(fd, DRM_IOCTL_SYNCOBJ_HANDLE_TO_FD, &sync_file) < 0)
        fail("export_sync_file");
    uint32_t third = create(fd, 0);
    struct drm_syncobj_handle import_fence = {
        .handle = third,
        .flags = FD_SYNC_FILE | FD_TIMELINE,
        .fd = sync_file.fd,
        .point = 11,
    };
    if (ioctl(fd, DRM_IOCTL_SYNCOBJ_FD_TO_HANDLE, &import_fence) < 0)
        fail("import_sync_file");
    timeline_wait(fd, third, 11, 0);
    close(sync_file.fd);
    stage("transfer_share_sync_file");

    uint32_t fourth = create(fd, 0);
    int event = eventfd(0, EFD_CLOEXEC | EFD_NONBLOCK);
    if (event < 0) fail("eventfd");
    struct drm_syncobj_eventfd event_request = {
        .handle = fourth,
        .point = 7,
        .fd = event,
    };
    if (ioctl(fd, DRM_IOCTL_SYNCOBJ_EVENTFD, &event_request) < 0)
        fail("syncobj_eventfd");
    timeline_signal(fd, fourth, 7);
    struct pollfd event_poll = { .fd = event, .events = POLLIN };
    if (poll(&event_poll, 1, 2000) != 1 || !(event_poll.revents & POLLIN))
        fail("eventfd_poll");
    uint64_t event_value = 0;
    if (read(event, &event_value, sizeof(event_value)) != sizeof(event_value) || event_value != 1)
        fail("eventfd_read");
    close(event);

    int overflow_event = eventfd(0, EFD_CLOEXEC | EFD_NONBLOCK);
    uint64_t saturated = UINT64_MAX - 1;
    if (overflow_event < 0 || write(overflow_event, &saturated, sizeof(saturated)) != sizeof(saturated))
        fail("eventfd_prefill");
    event_request.fd = overflow_event;
    if (ioctl(fd, DRM_IOCTL_SYNCOBJ_EVENTFD, &event_request) < 0)
        fail("syncobj_eventfd_overflow");
    struct pollfd overflow_poll = { .fd = overflow_event, .events = POLLIN };
    if (poll(&overflow_poll, 1, 0) != 1 ||
        (overflow_poll.revents & (POLLIN | POLLERR)) != (POLLIN | POLLERR))
        fail("eventfd_overflow_poll");
    event_value = 0;
    if (read(overflow_event, &event_value, sizeof(event_value)) != sizeof(event_value) ||
        event_value != UINT64_MAX)
        fail("eventfd_overflow_read");
    close(overflow_event);
    stage("eventfd");

    struct wait_thread thread = { .fd = fd, .handle = fourth, .point = 9 };
    pthread_t waiter;
    if (pthread_create(&waiter, NULL, wait_future, &thread) != 0) fail("pthread_create");
    struct timespec pause = { .tv_nsec = 20 * 1000 * 1000 };
    nanosleep(&pause, NULL);
    timeline_signal(fd, fourth, 9);
    if (pthread_join(waiter, NULL) != 0 || thread.result != 0) {
        errno = thread.error;
        fail("wait_for_submit_thread");
    }
    stage("concurrency");

    // A blocking ioctl must retain the syncobj after its original handle is
    // destroyed, and a whole-syncobj fd must be importable on another DRM fd.
    int other_fd = open("/dev/dri/renderD128", O_RDWR | O_CLOEXEC);
    if (other_fd < 0) fail("open_second_drm_fd");
    uint32_t lifetime = create(fd, 0);
    struct drm_syncobj_handle lifetime_share = { .handle = lifetime };
    if (ioctl(fd, DRM_IOCTL_SYNCOBJ_HANDLE_TO_FD, &lifetime_share) < 0)
        fail("lifetime_handle_to_fd");
    destroy(fd, lifetime);
    struct drm_syncobj_handle lifetime_import = { .fd = lifetime_share.fd };
    if (ioctl(other_fd, DRM_IOCTL_SYNCOBJ_FD_TO_HANDLE, &lifetime_import) < 0)
        fail("lifetime_fd_to_handle_after_destroy");
    array_ioctl(other_fd, DRM_IOCTL_SYNCOBJ_SIGNAL, lifetime_import.handle);
    binary.handles = (uint64_t)(uintptr_t)&lifetime_import.handle;
    binary.flags = 0;
    if (ioctl(other_fd, DRM_IOCTL_SYNCOBJ_WAIT, &binary) < 0)
        fail("imported_wait_after_destroy");
    close(lifetime_share.fd);
    destroy(other_fd, lifetime_import.handle);
    close(other_fd);
    stage("lifetime");

    uint32_t fifth = create(fd, 0);
    struct drm_virtgpu_execbuffer_syncobj input = {
        .handle = first,
        .flags = EXECBUF_SYNCOBJ_RESET,
        .point = 5,
    };
    uint32_t sixth = create(fd, 0);
    struct drm_virtgpu_execbuffer_syncobj outputs[] = {
        { .handle = fifth, .point = 13 },
        { .handle = sixth, .point = 17 },
    };
    uint32_t nop = 0;
    struct drm_virtgpu_execbuffer execution = {
        .size = sizeof(nop),
        .command = (uint64_t)(uintptr_t)&nop,
        .fence_fd = -1,
        .syncobj_stride = sizeof(input),
        .num_in_syncobjs = 1,
        .num_out_syncobjs = 2,
        .in_syncobjs = (uint64_t)(uintptr_t)&input,
        .out_syncobjs = (uint64_t)(uintptr_t)outputs,
    };
    outputs[0].flags = 1;
    errno = 0;
    if (ioctl(fd, DRM_IOCTL_VIRTGPU_EXECBUFFER, &execution) != -1 || errno != EINVAL)
        fail("execbuffer_output_flags");
    outputs[0].flags = 0;
    execution.syncobj_stride = 4;
    errno = 0;
    if (ioctl(fd, DRM_IOCTL_VIRTGPU_EXECBUFFER, &execution) != -1 || errno != EINVAL)
        fail("execbuffer_short_stride");
    execution.syncobj_stride = sizeof(input);

    // The out-fence fd is installed before the irreversible submit. A later
    // resource-validation failure must close it and roll back both timeline
    // output reservations.
    int expected_reused_fd = dup(fd);
    if (expected_reused_fd < 0) fail("execbuffer_rollback_probe_before");
    close(expected_reused_fd);
    uint32_t unknown_bo_handle = UINT32_MAX;
    struct drm_virtgpu_execbuffer rejected = execution;
    rejected.flags = EXECBUF_FENCE_FD_OUT;
    rejected.bo_handles = (uint64_t)(uintptr_t)&unknown_bo_handle;
    rejected.num_bo_handles = 1;
    rejected.fence_fd = -1;
    errno = 0;
    if (ioctl(fd, DRM_IOCTL_VIRTGPU_EXECBUFFER, &rejected) != -1 || errno != EINVAL)
        fail("execbuffer_out_fence_rollback");
    int reused_fd = dup(fd);
    if (reused_fd < 0 || reused_fd != expected_reused_fd)
        fail("execbuffer_out_fence_leak");
    close(reused_fd);

    execution.flags = EXECBUF_FENCE_FD_OUT;
    execution.fence_fd = -1;
    stage("execbuffer_submit_begin");
    if (ioctl(fd, DRM_IOCTL_VIRTGPU_EXECBUFFER, &execution) < 0)
        fail("execbuffer_syncobj_submit");
    stage("execbuffer_submit_done");
    timeline_wait(fd, fifth, 13, 0);
    stage("execbuffer_first_wait");
    timeline_wait(fd, sixth, 17, 0);
    stage("execbuffer_second_wait");
    if (query(fd, fifth, 0) != 13 || query(fd, fifth, QUERY_LAST_SUBMITTED) != 13)
        fail("execbuffer_syncobj_query");
    if (query(fd, sixth, 0) != 17 || query(fd, sixth, QUERY_LAST_SUBMITTED) != 17)
        fail("execbuffer_second_syncobj_query");
    struct pollfd output_fence = { .fd = execution.fence_fd, .events = POLLIN };
    if (execution.fence_fd < 0 || poll(&output_fence, 1, 2000) != 1 ||
        !(output_fence.revents & POLLIN))
        fail("execbuffer_output_fence_poll");
    close(execution.fence_fd);
    stage("execbuffer_out_fence");
    stage("execbuffer_multi_output");
    available.points = (uint64_t)(uintptr_t)&input.point;
    available.handles = (uint64_t)(uintptr_t)&first;
    available.flags = 0;
    errno = 0;
    if (ioctl(fd, DRM_IOCTL_SYNCOBJ_TIMELINE_WAIT, &available) != -1 || errno != EINVAL)
        fail("execbuffer_input_reset");

    // The same syncobj may be both a RESET input and a timeline output. RESET
    // consumes the old input fence; it must not erase the new output fence.
    uint32_t alias = create(fd, SYNCOBJ_CREATE_SIGNALED);
    struct drm_virtgpu_execbuffer_syncobj alias_syncobjs[] = {
        { .handle = alias, .flags = EXECBUF_SYNCOBJ_RESET },
        { .handle = alias, .point = 23 },
    };
    execution.flags = 0;
    execution.fence_fd = -1;
    execution.num_in_syncobjs = 1;
    execution.num_out_syncobjs = 1;
    execution.in_syncobjs = (uint64_t)(uintptr_t)&alias_syncobjs[0];
    execution.out_syncobjs = (uint64_t)(uintptr_t)&alias_syncobjs[1];
    if (ioctl(fd, DRM_IOCTL_VIRTGPU_EXECBUFFER, &execution) < 0)
        fail("execbuffer_alias_reset_submit");
    timeline_wait(fd, alias, 23, 0);
    if (query(fd, alias, 0) != 23 || query(fd, alias, QUERY_LAST_SUBMITTED) != 23)
        fail("execbuffer_alias_reset_query");
    stage("execbuffer_alias_reset");

    // Repeated same-object transfers exercise dependency flattening rather
    // than constructing a recursively expanding fence graph.
    uint32_t stress = create(fd, 0);
    outputs[0].handle = stress;
    outputs[0].point = 1;
    execution.num_in_syncobjs = 0;
    execution.num_out_syncobjs = 1;
    execution.in_syncobjs = 0;
    execution.out_syncobjs = (uint64_t)(uintptr_t)outputs;
    if (ioctl(fd, DRM_IOCTL_VIRTGPU_EXECBUFFER, &execution) < 0)
        fail("transfer_stress_submit");
    struct drm_syncobj_transfer self_transfer = {
        .src_handle = stress,
        .dst_handle = stress,
    };
    for (uint64_t point = 1; point <= 256; ++point) {
        self_transfer.src_point = point;
        self_transfer.dst_point = point + 1;
        if (ioctl(fd, DRM_IOCTL_SYNCOBJ_TRANSFER, &self_transfer) < 0)
            fail("self_transfer_stress");
    }
    timeline_wait(fd, stress, 257, 0);
    if (query(fd, stress, 0) != 257 || query(fd, stress, QUERY_LAST_SUBMITTED) != 257)
        fail("self_transfer_stress_query");

    struct drm_syncobj_timeline_array null_points = {
        .handles = (uint64_t)(uintptr_t)&stress,
        .count_handles = 1,
    };
    errno = 0;
    if (ioctl(fd, DRM_IOCTL_SYNCOBJ_TIMELINE_SIGNAL, &null_points) != -1 || errno != EFAULT)
        fail("timeline_signal_null_points");
    stage("stress_boundary");

    // Retained eventfd references are deliberately bounded. The first
    // unavailable point above the limit must fail without
    // disturbing earlier watches.
    uint32_t bounded = create(fd, 0);
    int bounded_event = eventfd(0, EFD_CLOEXEC | EFD_NONBLOCK);
    if (bounded_event < 0) fail("bounded_eventfd");
    struct drm_syncobj_eventfd bounded_request = {
        .handle = bounded,
        .flags = WAIT_AVAILABLE,
        .fd = bounded_event,
    };
    for (uint64_t point = 1; point <= MAX_EVENT_WATCHERS; ++point) {
        bounded_request.point = point;
        if (ioctl(fd, DRM_IOCTL_SYNCOBJ_EVENTFD, &bounded_request) < 0)
            fail("eventfd_watcher_limit_fill");
    }
    bounded_request.point = MAX_EVENT_WATCHERS + 1;
    errno = 0;
    if (ioctl(fd, DRM_IOCTL_SYNCOBJ_EVENTFD, &bounded_request) != -1 || errno != ENOSPC)
        fail("eventfd_watcher_limit");
    destroy(fd, bounded);
    close(bounded_event);
    stage("watcher_bounds");

    uint32_t handles[] = {
        first, second, imported.handle, third, fourth, fifth, sixth, alias, stress
    };
    for (size_t i = 0; i < sizeof(handles) / sizeof(handles[0]); ++i) {
        destroy(fd, handles[i]);
    }
    close(fd);
    printf("M19_SYNCOBJ_PASS binary timeline transfer share sync_file eventfd concurrency execbuffer out_fence rollback multi_output alias_reset lifetime stress bounds\n");
    return 0;
}
