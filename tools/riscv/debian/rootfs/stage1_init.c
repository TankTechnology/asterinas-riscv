// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#if !defined(DEBIAN_STAGE1_SELF_TEST)
static void fail_and_hold(const char *reason)
{
    (void)printf("DEBIAN_ROOTFS_FAIL reason=%s\n", reason);
    (void)fflush(stdout);

    for (;;) {
        while (pause() < 0 && errno == EINTR) {
        }
    }
}
#endif

#if defined(DEBIAN_STAGE1_LIFECYCLE_TEST)
int main(void)
{
    fail_and_hold("test-lifecycle");
}
#else

enum {
    EXT2_SUPERBLOCK_OFFSET = 1024,
    EXT2_SUPERBLOCK_SIZE = 1024,
    EXT2_MAGIC_OFFSET = 56,
    EXT2_LABEL_OFFSET = 120,
    EXT2_LABEL_LENGTH = 16,
    ROOT_DEVICE_PATH_SIZE = sizeof("/dev/vda"),
    ROOT_DISCOVERY_TIMEOUT_SECONDS = 30,
};

#define ROOT_LABEL "ASTER_DEBIANROOT"

enum ProbeResult {
    PROBE_NO_MATCH,
    PROBE_MATCH,
    PROBE_FATAL,
};

enum HandoffStep {
    HANDOFF_MOUNT_ROOT,
    HANDOFF_BIND_DEV,
    HANDOFF_MOUNT_PROC,
    HANDOFF_MOUNT_SYSFS,
    HANDOFF_MOUNT_RUN,
    HANDOFF_MOUNT_TMP,
    HANDOFF_CHROOT,
    HANDOFF_CHDIR,
    HANDOFF_EXEC,
};

struct Stage1Ops {
    void *context;
    enum ProbeResult (*probe_device)(void *context, char suffix,
                                     char path[ROOT_DEVICE_PATH_SIZE]);
    int (*monotonic_now)(void *context, struct timespec *now);
    int (*wait_for_retry)(void *context, const struct timespec *deadline);
    int (*perform_handoff)(void *context, enum HandoffStep step,
                           const char *root_device);
};

static int compare_timespec(const struct timespec *left,
                            const struct timespec *right)
{
    if (left->tv_sec != right->tv_sec) {
        return left->tv_sec < right->tv_sec ? -1 : 1;
    }
    if (left->tv_nsec != right->tv_nsec) {
        return left->tv_nsec < right->tv_nsec ? -1 : 1;
    }
    return 0;
}

static int ext2_superblock_matches(
    const unsigned char superblock[EXT2_SUPERBLOCK_SIZE])
{
    const int has_ext2_magic =
        superblock[EXT2_MAGIC_OFFSET] == 0x53 &&
        superblock[EXT2_MAGIC_OFFSET + 1] == 0xef;
    const int has_root_label =
        memcmp(superblock + EXT2_LABEL_OFFSET, ROOT_LABEL,
               EXT2_LABEL_LENGTH) == 0;

    return has_ext2_magic && has_root_label;
}

static const char *discover_root(struct Stage1Ops *ops,
                                 char root_device[ROOT_DEVICE_PATH_SIZE])
{
    struct timespec deadline;
    if (ops->monotonic_now(ops->context, &deadline) != 0) {
        return "root-discovery-clock";
    }
    deadline.tv_sec += ROOT_DISCOVERY_TIMEOUT_SECONDS;

    int is_initial_scan = 1;
    for (;;) {
        if (!is_initial_scan) {
            struct timespec now;
            if (ops->monotonic_now(ops->context, &now) != 0) {
                return "root-discovery-clock";
            }
            if (compare_timespec(&now, &deadline) >= 0) {
                return "root-discovery-timeout";
            }
        }

        unsigned int match_count = 0;
        char matched_path[ROOT_DEVICE_PATH_SIZE] = { 0 };

        for (char suffix = 'a'; suffix <= 'z'; ++suffix) {
            char candidate_path[ROOT_DEVICE_PATH_SIZE] = { 0 };
            enum ProbeResult result =
                ops->probe_device(ops->context, suffix, candidate_path);
            if (result == PROBE_FATAL) {
                return "root-device-probe";
            }
            if (result != PROBE_MATCH) {
                continue;
            }

            ++match_count;
            if (match_count > 1) {
                return "root-device-ambiguous";
            }
            memcpy(matched_path, candidate_path, sizeof(matched_path));
        }

        if (match_count == 1) {
            struct timespec now;
            if (ops->monotonic_now(ops->context, &now) != 0) {
                return "root-discovery-clock";
            }
            if (compare_timespec(&now, &deadline) >= 0) {
                return "root-discovery-timeout";
            }
            memcpy(root_device, matched_path, ROOT_DEVICE_PATH_SIZE);
            return NULL;
        }

        struct timespec now;
        if (ops->monotonic_now(ops->context, &now) != 0) {
            return "root-discovery-clock";
        }
        if (compare_timespec(&now, &deadline) >= 0) {
            return "root-discovery-timeout";
        }
        if (ops->wait_for_retry(ops->context, &deadline) != 0) {
            return "root-discovery-wait";
        }
        is_initial_scan = 0;
    }
}

static const char *handoff_root(struct Stage1Ops *ops, const char *root_device)
{
    static const struct {
        enum HandoffStep step;
        const char *reason;
    } steps[] = {
        { HANDOFF_MOUNT_ROOT, "root-mount" },
        { HANDOFF_BIND_DEV, "dev-bind" },
        { HANDOFF_MOUNT_PROC, "proc-mount" },
        { HANDOFF_MOUNT_SYSFS, "sysfs-mount" },
        { HANDOFF_MOUNT_RUN, "run-mount" },
        { HANDOFF_MOUNT_TMP, "tmp-mount" },
        { HANDOFF_CHROOT, "chroot" },
        { HANDOFF_CHDIR, "chdir" },
        { HANDOFF_EXEC, "exec" },
    };

    for (size_t index = 0; index < sizeof(steps) / sizeof(steps[0]); ++index) {
        if (ops->perform_handoff(ops->context, steps[index].step,
                                 root_device) != 0) {
            return steps[index].reason;
        }
    }

    return "exec-returned";
}

#if defined(DEBIAN_STAGE1_SELF_TEST)

struct MockContext {
    const char *case_name;
    unsigned int retry_count;
    enum HandoffStep failing_step;
    unsigned int handoff_count;
    struct timespec injected_now;
    unsigned int boundary_device_probe_count;
};

static int is_deadline_boundary_case(const char *case_name)
{
    return strcmp(case_name, "device-before-deadline") == 0 ||
           strcmp(case_name, "device-at-deadline") == 0 ||
           strcmp(case_name, "device-after-deadline") == 0;
}

static void make_valid_superblock(
    unsigned char superblock[EXT2_SUPERBLOCK_SIZE])
{
    memset(superblock, 0, EXT2_SUPERBLOCK_SIZE);
    superblock[EXT2_MAGIC_OFFSET] = 0x53;
    superblock[EXT2_MAGIC_OFFSET + 1] = 0xef;
    memcpy(superblock + EXT2_LABEL_OFFSET, ROOT_LABEL, EXT2_LABEL_LENGTH);
}

static enum ProbeResult mock_probe_device(
    void *opaque, char suffix, char path[ROOT_DEVICE_PATH_SIZE])
{
    struct MockContext *context = opaque;
    unsigned char superblock[EXT2_SUPERBLOCK_SIZE];
    make_valid_superblock(superblock);

    int is_match = 0;
    if (strcmp(context->case_name, "one-valid-device") == 0) {
        is_match = suffix == 'b';
    } else if (strcmp(context->case_name, "two-matching-devices") == 0) {
        is_match = suffix == 'b' || suffix == 'd';
    } else if (strcmp(context->case_name, "delayed-valid-device") == 0) {
        is_match = suffix == 'c' && context->retry_count >= 2;
    } else if (is_deadline_boundary_case(context->case_name) &&
               suffix == 'c' && context->retry_count == 1) {
        ++context->boundary_device_probe_count;
        is_match = 1;
        if (strcmp(context->case_name, "device-at-deadline") == 0) {
            context->injected_now.tv_sec = 30;
            context->injected_now.tv_nsec = 0;
        }
    } else if (strcmp(context->case_name, "bad-ext2-magic") == 0 &&
               suffix == 'b') {
        superblock[EXT2_MAGIC_OFFSET] = 0;
        is_match = ext2_superblock_matches(superblock);
    } else if (strcmp(context->case_name, "wrong-label") == 0 &&
               suffix == 'b') {
        superblock[EXT2_LABEL_OFFSET] = 'X';
        is_match = ext2_superblock_matches(superblock);
    } else if (strcmp(context->case_name, "non-block-device") == 0 &&
               suffix == 'b') {
        is_match = 0;
    }

    if (!is_match) {
        return PROBE_NO_MATCH;
    }
    (void)snprintf(path, ROOT_DEVICE_PATH_SIZE, "/dev/vd%c", suffix);
    return PROBE_MATCH;
}

static int mock_monotonic_now(void *opaque, struct timespec *now)
{
    struct MockContext *context = opaque;
    if (is_deadline_boundary_case(context->case_name)) {
        *now = context->injected_now;
        return 0;
    }
    now->tv_sec = (time_t)context->retry_count;
    now->tv_nsec = 0;
    return 0;
}

static int mock_wait_for_retry(void *opaque, const struct timespec *deadline)
{
    struct MockContext *context = opaque;
    (void)deadline;
    ++context->retry_count;
    if (strcmp(context->case_name, "device-before-deadline") == 0 ||
        strcmp(context->case_name, "device-at-deadline") == 0) {
        context->injected_now.tv_sec = 29;
        context->injected_now.tv_nsec = 999999999;
    } else if (strcmp(context->case_name, "device-after-deadline") == 0) {
        context->injected_now.tv_sec = 31;
        context->injected_now.tv_nsec = 0;
    }
    return 0;
}

static int mock_perform_handoff(void *opaque, enum HandoffStep step,
                                const char *root_device)
{
    struct MockContext *context = opaque;
    (void)root_device;
    ++context->handoff_count;
    return step == context->failing_step ? -1 : 0;
}

static int fail_self_test(const char *case_name, const char *message)
{
    (void)fprintf(stderr, "self-test case %s: %s\n", case_name, message);
    return 1;
}

static int run_discovery_self_test(const char *case_name)
{
    struct MockContext context = {
        .case_name = case_name,
        .retry_count = 0,
        .failing_step = HANDOFF_EXEC,
        .handoff_count = 0,
        .injected_now = { .tv_sec = 0, .tv_nsec = 0 },
        .boundary_device_probe_count = 0,
    };
    struct Stage1Ops ops = {
        .context = &context,
        .probe_device = mock_probe_device,
        .monotonic_now = mock_monotonic_now,
        .wait_for_retry = mock_wait_for_retry,
        .perform_handoff = mock_perform_handoff,
    };
    char root_device[ROOT_DEVICE_PATH_SIZE] = { 0 };
    const char *reason = discover_root(&ops, root_device);

    if (strcmp(case_name, "one-valid-device") == 0) {
        if (reason != NULL || strcmp(root_device, "/dev/vdb") != 0 ||
            context.retry_count != 0) {
            return fail_self_test(case_name, "valid device was not selected");
        }
    } else if (strcmp(case_name, "two-matching-devices") == 0) {
        if (reason == NULL || strcmp(reason, "root-device-ambiguous") != 0 ||
            context.retry_count != 0) {
            return fail_self_test(case_name, "ambiguity was not immediate");
        }
    } else if (strcmp(case_name, "delayed-valid-device") == 0) {
        if (reason != NULL || strcmp(root_device, "/dev/vdc") != 0 ||
            context.retry_count != 2) {
            return fail_self_test(case_name, "delayed device was not retried");
        }
    } else if (strcmp(case_name, "device-before-deadline") == 0) {
        if (reason != NULL || strcmp(root_device, "/dev/vdc") != 0 ||
            context.retry_count != 1 ||
            context.boundary_device_probe_count != 1 ||
            context.injected_now.tv_sec != 29 ||
            context.injected_now.tv_nsec != 999999999) {
            return fail_self_test(case_name,
                                  "pre-deadline device was not accepted");
        }
    } else if (strcmp(case_name, "device-at-deadline") == 0) {
        if (reason == NULL || strcmp(reason, "root-discovery-timeout") != 0 ||
            context.retry_count != 1 ||
            context.boundary_device_probe_count != 1 ||
            context.injected_now.tv_sec != 30 ||
            context.injected_now.tv_nsec != 0) {
            return fail_self_test(case_name,
                                  "deadline device was not rejected");
        }
    } else if (strcmp(case_name, "device-after-deadline") == 0) {
        if (reason == NULL || strcmp(reason, "root-discovery-timeout") != 0 ||
            context.retry_count != 1 ||
            context.boundary_device_probe_count != 0 ||
            context.injected_now.tv_sec != 31 ||
            context.injected_now.tv_nsec != 0) {
            return fail_self_test(case_name,
                                  "post-deadline scan was not rejected");
        }
    } else {
        if (reason == NULL || strcmp(reason, "root-discovery-timeout") != 0 ||
            context.retry_count != ROOT_DISCOVERY_TIMEOUT_SECONDS) {
            return fail_self_test(case_name, "deadline was not enforced");
        }
    }

    return 0;
}

static int handoff_case(const char *case_name, enum HandoffStep *step,
                        const char **reason)
{
    static const struct {
        const char *case_name;
        enum HandoffStep step;
        const char *reason;
    } cases[] = {
        { "root-mount-failure", HANDOFF_MOUNT_ROOT, "root-mount" },
        { "dev-bind-failure", HANDOFF_BIND_DEV, "dev-bind" },
        { "proc-mount-failure", HANDOFF_MOUNT_PROC, "proc-mount" },
        { "sysfs-mount-failure", HANDOFF_MOUNT_SYSFS, "sysfs-mount" },
        { "run-mount-failure", HANDOFF_MOUNT_RUN, "run-mount" },
        { "tmp-mount-failure", HANDOFF_MOUNT_TMP, "tmp-mount" },
        { "chroot-failure", HANDOFF_CHROOT, "chroot" },
        { "chdir-failure", HANDOFF_CHDIR, "chdir" },
        { "exec-failure", HANDOFF_EXEC, "exec" },
    };

    for (size_t index = 0; index < sizeof(cases) / sizeof(cases[0]); ++index) {
        if (strcmp(case_name, cases[index].case_name) == 0) {
            *step = cases[index].step;
            *reason = cases[index].reason;
            return 1;
        }
    }
    return 0;
}

static int run_handoff_self_test(const char *case_name,
                                 enum HandoffStep failing_step,
                                 const char *expected_reason)
{
    struct MockContext context = {
        .case_name = case_name,
        .retry_count = 0,
        .failing_step = failing_step,
        .handoff_count = 0,
        .injected_now = { .tv_sec = 0, .tv_nsec = 0 },
        .boundary_device_probe_count = 0,
    };
    struct Stage1Ops ops = {
        .context = &context,
        .probe_device = mock_probe_device,
        .monotonic_now = mock_monotonic_now,
        .wait_for_retry = mock_wait_for_retry,
        .perform_handoff = mock_perform_handoff,
    };
    const char *reason = handoff_root(&ops, "/dev/vdb");

    if (strcmp(reason, expected_reason) != 0 ||
        context.handoff_count != (unsigned int)failing_step + 1) {
        return fail_self_test(case_name, "handoff failure boundary was wrong");
    }
    return 0;
}

int main(int argc, char **argv)
{
    if (argc != 2) {
        (void)fprintf(stderr, "usage: %s CASE\n", argv[0]);
        return 2;
    }

    const char *case_name = argv[1];
    int result;
    enum HandoffStep failing_step;
    const char *expected_reason;
    if (handoff_case(case_name, &failing_step, &expected_reason)) {
        result = run_handoff_self_test(case_name, failing_step, expected_reason);
    } else if (strcmp(case_name, "one-valid-device") == 0 ||
               strcmp(case_name, "no-match") == 0 ||
               strcmp(case_name, "two-matching-devices") == 0 ||
               strcmp(case_name, "bad-ext2-magic") == 0 ||
               strcmp(case_name, "wrong-label") == 0 ||
               strcmp(case_name, "non-block-device") == 0 ||
               strcmp(case_name, "delayed-valid-device") == 0 ||
               strcmp(case_name, "device-before-deadline") == 0 ||
               strcmp(case_name, "device-at-deadline") == 0 ||
               strcmp(case_name, "device-after-deadline") == 0 ||
               strcmp(case_name, "discovery-deadline") == 0) {
        result = run_discovery_self_test(case_name);
    } else {
        return fail_self_test(case_name, "unknown case");
    }

    if (result != 0) {
        return result;
    }
    (void)printf("DEBIAN_STAGE1_SELF_TEST PASS case=%s\n", case_name);
    return 0;
}

#else

static ssize_t pread_complete(int fd, void *buffer, size_t size, off_t offset)
{
    size_t completed = 0;
    while (completed < size) {
        ssize_t result =
            pread(fd, (unsigned char *)buffer + completed, size - completed,
                  offset + (off_t)completed);
        if (result > 0) {
            completed += (size_t)result;
            continue;
        }
        if (result < 0 && errno == EINTR) {
            continue;
        }
        return result < 0 ? -1 : (ssize_t)completed;
    }
    return (ssize_t)completed;
}

static enum ProbeResult production_probe_device(
    void *context, char suffix, char path[ROOT_DEVICE_PATH_SIZE])
{
    (void)context;
    char candidate_path[ROOT_DEVICE_PATH_SIZE];
    (void)snprintf(candidate_path, sizeof(candidate_path), "/dev/vd%c", suffix);

    int fd;
    do {
        fd = open(candidate_path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    } while (fd < 0 && errno == EINTR);
    if (fd < 0) {
        return PROBE_NO_MATCH;
    }

    struct stat metadata;
    int stat_result;
    do {
        stat_result = fstat(fd, &metadata);
    } while (stat_result < 0 && errno == EINTR);
    if (stat_result != 0 || !S_ISBLK(metadata.st_mode)) {
        (void)close(fd);
        return PROBE_NO_MATCH;
    }

    unsigned char superblock[EXT2_SUPERBLOCK_SIZE];
    ssize_t bytes_read =
        pread_complete(fd, superblock, sizeof(superblock), EXT2_SUPERBLOCK_OFFSET);
    (void)close(fd);
    if (bytes_read != (ssize_t)sizeof(superblock) ||
        !ext2_superblock_matches(superblock)) {
        return PROBE_NO_MATCH;
    }

    memcpy(path, candidate_path, sizeof(candidate_path));
    return PROBE_MATCH;
}

static int production_monotonic_now(void *context, struct timespec *now)
{
    (void)context;
    return clock_gettime(CLOCK_MONOTONIC, now);
}

static int production_wait_for_retry(void *context,
                                     const struct timespec *deadline)
{
    (void)context;
    static const long retry_nanoseconds = 100 * 1000 * 1000;
    struct timespec wake_time;
    if (clock_gettime(CLOCK_MONOTONIC, &wake_time) != 0) {
        return -1;
    }
    wake_time.tv_nsec += retry_nanoseconds;
    if (wake_time.tv_nsec >= 1000 * 1000 * 1000) {
        ++wake_time.tv_sec;
        wake_time.tv_nsec -= 1000 * 1000 * 1000;
    }
    if (compare_timespec(&wake_time, deadline) > 0) {
        wake_time = *deadline;
    }

    int result;
    do {
        result = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &wake_time,
                                 NULL);
    } while (result == EINTR);
    return result == 0 ? 0 : -1;
}

static int ensure_directory(const char *path)
{
    if (mkdir(path, 0755) == 0) {
        return 0;
    }
    if (errno != EEXIST) {
        return -1;
    }

    struct stat metadata;
    return stat(path, &metadata) == 0 && S_ISDIR(metadata.st_mode) ? 0 : -1;
}

static int production_perform_handoff(void *context, enum HandoffStep step,
                                      const char *root_device)
{
    (void)context;
    switch (step) {
    case HANDOFF_MOUNT_ROOT:
        return mount(root_device, "/newroot", "ext2", 0, NULL);
    case HANDOFF_BIND_DEV:
        if (ensure_directory("/newroot/dev") != 0) {
            return -1;
        }
        return mount("/dev", "/newroot/dev", NULL, MS_BIND, NULL);
    case HANDOFF_MOUNT_PROC:
        if (ensure_directory("/newroot/proc") != 0) {
            return -1;
        }
        return mount("proc", "/newroot/proc", "proc", 0, NULL);
    case HANDOFF_MOUNT_SYSFS:
        if (ensure_directory("/newroot/sys") != 0) {
            return -1;
        }
        return mount("sysfs", "/newroot/sys", "sysfs", 0, NULL);
    case HANDOFF_MOUNT_RUN:
        if (ensure_directory("/newroot/run") != 0) {
            return -1;
        }
        return mount("tmpfs", "/newroot/run", "tmpfs", 0, NULL);
    case HANDOFF_MOUNT_TMP:
        if (ensure_directory("/newroot/tmp") != 0) {
            return -1;
        }
        return mount("tmpfs", "/newroot/tmp", "tmpfs", 0, NULL);
    case HANDOFF_CHROOT:
        return chroot("/newroot");
    case HANDOFF_CHDIR:
        return chdir("/");
    case HANDOFF_EXEC: {
        char *const arguments[] = {
            "/bin/bash", "--noprofile", "--rcfile",
            "/etc/asterinas-rootfs.bashrc", "-i", NULL,
        };
        return execv(arguments[0], arguments);
    }
    }
    return -1;
}

static int duplicate_console_fd(int console_fd, int destination_fd)
{
    int result;
    do {
        result = dup2(console_fd, destination_fd);
    } while (result < 0 && errno == EINTR);
    return result;
}

static int configure_console(void)
{
    int console_fd;
    do {
        console_fd = open("/dev/console", O_RDWR | O_CLOEXEC | O_NOCTTY);
    } while (console_fd < 0 && errno == EINTR);
    if (console_fd < 0) {
        return -1;
    }

    for (int destination_fd = STDIN_FILENO; destination_fd <= STDERR_FILENO;
         ++destination_fd) {
        if (duplicate_console_fd(console_fd, destination_fd) < 0) {
            if (console_fd > STDERR_FILENO) {
                (void)close(console_fd);
            }
            return -1;
        }
    }
    if (console_fd > STDERR_FILENO) {
        (void)close(console_fd);
    }
    return 0;
}

int main(void)
{
    if (configure_console() != 0) {
        fail_and_hold("console-open");
    }

    struct Stage1Ops ops = {
        .context = NULL,
        .probe_device = production_probe_device,
        .monotonic_now = production_monotonic_now,
        .wait_for_retry = production_wait_for_retry,
        .perform_handoff = production_perform_handoff,
    };
    char root_device[ROOT_DEVICE_PATH_SIZE] = { 0 };
    const char *reason = discover_root(&ops, root_device);
    if (reason != NULL) {
        fail_and_hold(reason);
    }
    if (ensure_directory("/newroot") != 0) {
        fail_and_hold("newroot-directory");
    }

    reason = handoff_root(&ops, root_device);
    fail_and_hold(reason);
}

#endif
#endif
