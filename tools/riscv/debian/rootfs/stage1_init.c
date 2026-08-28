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
    ROOT_DEVICE_PATH_SIZE = sizeof("/dev/mmcblk0p3"),
    VIRTIO_CANDIDATE_COUNT = 26,
    ROOT_DEVICE_CANDIDATE_COUNT = VIRTIO_CANDIDATE_COUNT + 3,
    ROOT_DISCOVERY_TIMEOUT_SECONDS = 30,
};

/*
 * An ext2 volume label is a fixed-width byte field, not a C string.  Spell
 * out the only full-width label so newer GCC versions do not diagnose the
 * intentionally absent NUL terminator under -Werror.
 */
static const unsigned char INTERACTIVE_ROOT_LABEL[EXT2_LABEL_LENGTH] = {
    'A', 'S', 'T', 'E', 'R', '_', 'D', 'E',
    'B', 'I', 'A', 'N', 'R', 'O', 'O', 'T',
};
static const unsigned char SYSTEMD_ROOT_LABEL[EXT2_LABEL_LENGTH] =
    "ASTER_DEBIANM2";
static const unsigned char DESKTOP_ROOT_LABEL[EXT2_LABEL_LENGTH] =
    "ASTER_DEBIANM3";
static const unsigned char APPLICATION_DESKTOP_ROOT_LABEL[EXT2_LABEL_LENGTH] =
    "ASTER_DEBIANM4";
static const unsigned char NETWORK_DESKTOP_ROOT_LABEL[EXT2_LABEL_LENGTH] =
    "ASTER_DEBIANM5";
static const unsigned char BROWSER_ROOT_LABEL[EXT2_LABEL_LENGTH] =
    "ASTER_BROWSERM5";

enum RootInitMode {
    ROOT_INIT_INTERACTIVE,
    ROOT_INIT_SYSTEMD,
};

struct ProductionContext {
    enum RootInitMode root_init_mode;
};

static char *const INTERACTIVE_ROOT_INIT_ARGV[] = {
    "/bin/bash", "--noprofile", "--rcfile",
    "/etc/asterinas-rootfs.bashrc", "-i", NULL,
};
static char *const SYSTEMD_ROOT_INIT_ARGV[] = { "/sbin/init", NULL };

static char *const *root_init_arguments(enum RootInitMode mode)
{
    return mode == ROOT_INIT_SYSTEMD ? SYSTEMD_ROOT_INIT_ARGV
                                     : INTERACTIVE_ROOT_INIT_ARGV;
}

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
    HANDOFF_PREPARE_API_DIRS,
};

struct Stage1Ops {
    void *context;
    enum ProbeResult (*probe_device)(void *context, const char *candidate_path,
                                     char path[ROOT_DEVICE_PATH_SIZE]);
    int (*monotonic_now)(void *context, struct timespec *now);
    int (*wait_for_retry)(void *context, const struct timespec *deadline);
    int (*perform_handoff)(void *context, enum HandoffStep step,
                           const char *root_device);
};

struct HandoffAction {
    enum HandoffStep step;
    const char *reason;
};

static int root_candidate_path(size_t index,
                               char path[ROOT_DEVICE_PATH_SIZE])
{
    if (index < VIRTIO_CANDIDATE_COUNT) {
        (void)snprintf(path, ROOT_DEVICE_PATH_SIZE, "/dev/vd%c",
                       (char)('a' + index));
        return 0;
    }

    static const char *const mmc_partitions[] = {
        "/dev/mmcblk0p1",
        "/dev/mmcblk0p2",
        "/dev/mmcblk0p3",
    };
    size_t mmc_index = index - VIRTIO_CANDIDATE_COUNT;
    if (mmc_index >= sizeof(mmc_partitions) / sizeof(mmc_partitions[0])) {
        return -1;
    }
    (void)snprintf(path, ROOT_DEVICE_PATH_SIZE, "%s",
                   mmc_partitions[mmc_index]);
    return 0;
}

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

static int parse_root_init(int argc, char **argv, enum RootInitMode *mode)
{
    *mode = ROOT_INIT_INTERACTIVE;
    int selector_seen = 0;
    for (int index = 1; index < argc; ++index) {
        enum RootInitMode selected_mode;
        if (strcmp(argv[index], "--root-init=interactive") == 0) {
            selected_mode = ROOT_INIT_INTERACTIVE;
        } else if (strcmp(argv[index], "--root-init=systemd") == 0) {
            selected_mode = ROOT_INIT_SYSTEMD;
        } else {
            return -1;
        }
        if (selector_seen) {
            return -1;
        }
        selector_seen = 1;
        *mode = selected_mode;
    }
    return 0;
}

static int ext2_superblock_matches(
    const unsigned char superblock[EXT2_SUPERBLOCK_SIZE],
    const unsigned char expected_label[EXT2_LABEL_LENGTH])
{
    const int has_ext2_magic =
        superblock[EXT2_MAGIC_OFFSET] == 0x53 &&
        superblock[EXT2_MAGIC_OFFSET + 1] == 0xef;
    const int has_root_label =
        memcmp(superblock + EXT2_LABEL_OFFSET, expected_label,
               EXT2_LABEL_LENGTH) == 0;

    return has_ext2_magic && has_root_label;
}

static int ext2_superblock_matches_mode(
    const unsigned char superblock[EXT2_SUPERBLOCK_SIZE],
    enum RootInitMode mode)
{
    if (mode == ROOT_INIT_INTERACTIVE) {
        return ext2_superblock_matches(superblock, INTERACTIVE_ROOT_LABEL);
    }
    return ext2_superblock_matches(superblock, SYSTEMD_ROOT_LABEL) ||
           ext2_superblock_matches(superblock, DESKTOP_ROOT_LABEL) ||
           ext2_superblock_matches(superblock, APPLICATION_DESKTOP_ROOT_LABEL) ||
           ext2_superblock_matches(superblock, NETWORK_DESKTOP_ROOT_LABEL) ||
           ext2_superblock_matches(superblock, BROWSER_ROOT_LABEL);
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

        for (size_t index = 0; index < ROOT_DEVICE_CANDIDATE_COUNT; ++index) {
            char candidate_path[ROOT_DEVICE_PATH_SIZE] = { 0 };
            if (root_candidate_path(index, candidate_path) != 0) {
                return "root-device-candidate";
            }
            char matched_candidate[ROOT_DEVICE_PATH_SIZE] = { 0 };
            enum ProbeResult result = ops->probe_device(
                ops->context, candidate_path, matched_candidate);
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
            memcpy(matched_path, matched_candidate, sizeof(matched_path));
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

static const char *handoff_root(struct Stage1Ops *ops, const char *root_device,
                                enum RootInitMode mode)
{
    static const struct HandoffAction interactive_steps[] = {
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
    static const struct HandoffAction systemd_steps[] = {
        { HANDOFF_MOUNT_ROOT, "root-mount" },
        { HANDOFF_BIND_DEV, "dev-bind" },
        { HANDOFF_PREPARE_API_DIRS, "api-directories" },
        { HANDOFF_MOUNT_RUN, "run-mount" },
        { HANDOFF_MOUNT_TMP, "tmp-mount" },
        { HANDOFF_CHROOT, "chroot" },
        { HANDOFF_CHDIR, "chdir" },
        { HANDOFF_EXEC, "exec" },
    };

    const struct HandoffAction *steps = interactive_steps;
    size_t step_count =
        sizeof(interactive_steps) / sizeof(interactive_steps[0]);
    if (mode == ROOT_INIT_SYSTEMD) {
        steps = systemd_steps;
        step_count = sizeof(systemd_steps) / sizeof(systemd_steps[0]);
    }

    for (size_t index = 0; index < step_count; ++index) {
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
    enum HandoffStep handoff_steps[16];
};

static int is_deadline_boundary_case(const char *case_name)
{
    return strcmp(case_name, "device-before-deadline") == 0 ||
           strcmp(case_name, "device-at-deadline") == 0 ||
           strcmp(case_name, "device-after-deadline") == 0;
}

static void make_valid_superblock(
    unsigned char superblock[EXT2_SUPERBLOCK_SIZE],
    const unsigned char label[EXT2_LABEL_LENGTH])
{
    memset(superblock, 0, EXT2_SUPERBLOCK_SIZE);
    superblock[EXT2_MAGIC_OFFSET] = 0x53;
    superblock[EXT2_MAGIC_OFFSET + 1] = 0xef;
    memcpy(superblock + EXT2_LABEL_OFFSET, label, EXT2_LABEL_LENGTH);
}

static enum ProbeResult mock_probe_device(
    void *opaque, const char *candidate_path,
    char path[ROOT_DEVICE_PATH_SIZE])
{
    struct MockContext *context = opaque;
    unsigned char superblock[EXT2_SUPERBLOCK_SIZE];
    make_valid_superblock(superblock, INTERACTIVE_ROOT_LABEL);

    int is_match = 0;
    if (strcmp(context->case_name, "one-valid-device") == 0) {
        is_match = strcmp(candidate_path, "/dev/vdb") == 0;
    } else if (strcmp(context->case_name, "one-valid-mmc-device") == 0) {
        is_match = strcmp(candidate_path, "/dev/mmcblk0p2") == 0;
    } else if (strcmp(context->case_name, "two-matching-devices") == 0) {
        is_match = strcmp(candidate_path, "/dev/vdb") == 0 ||
                   strcmp(candidate_path, "/dev/vdd") == 0;
    } else if (strcmp(context->case_name, "virtio-and-mmc-ambiguous") ==
               0) {
        is_match = strcmp(candidate_path, "/dev/vdb") == 0 ||
                   strcmp(candidate_path, "/dev/mmcblk0p2") == 0;
    } else if (strcmp(context->case_name, "delayed-valid-device") == 0) {
        is_match = strcmp(candidate_path, "/dev/vdc") == 0 &&
                   context->retry_count >= 2;
    } else if (is_deadline_boundary_case(context->case_name) &&
               strcmp(candidate_path, "/dev/vdc") == 0 &&
               context->retry_count == 1) {
        ++context->boundary_device_probe_count;
        is_match = 1;
        if (strcmp(context->case_name, "device-at-deadline") == 0) {
            context->injected_now.tv_sec = 30;
            context->injected_now.tv_nsec = 0;
        }
    } else if (strcmp(context->case_name, "bad-ext2-magic") == 0 &&
               strcmp(candidate_path, "/dev/vdb") == 0) {
        superblock[EXT2_MAGIC_OFFSET] = 0;
        is_match =
            ext2_superblock_matches(superblock, INTERACTIVE_ROOT_LABEL);
    } else if (strcmp(context->case_name, "wrong-label") == 0 &&
               strcmp(candidate_path, "/dev/vdb") == 0) {
        superblock[EXT2_LABEL_OFFSET] = 'X';
        is_match =
            ext2_superblock_matches(superblock, INTERACTIVE_ROOT_LABEL);
    } else if (strcmp(context->case_name, "non-block-device") == 0 &&
               strcmp(candidate_path, "/dev/vdb") == 0) {
        is_match = 0;
    }

    if (!is_match) {
        return PROBE_NO_MATCH;
    }
    (void)snprintf(path, ROOT_DEVICE_PATH_SIZE, "%s", candidate_path);
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
    if (context->handoff_count <
        sizeof(context->handoff_steps) / sizeof(context->handoff_steps[0])) {
        context->handoff_steps[context->handoff_count] = step;
    }
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
    } else if (strcmp(case_name, "one-valid-mmc-device") == 0) {
        if (reason != NULL || strcmp(root_device, "/dev/mmcblk0p2") != 0 ||
            context.retry_count != 0) {
            return fail_self_test(case_name,
                                  "valid MMC partition was not selected");
        }
    } else if (strcmp(case_name, "two-matching-devices") == 0) {
        if (reason == NULL || strcmp(reason, "root-device-ambiguous") != 0 ||
            context.retry_count != 0) {
            return fail_self_test(case_name, "ambiguity was not immediate");
        }
    } else if (strcmp(case_name, "virtio-and-mmc-ambiguous") == 0) {
        if (reason == NULL || strcmp(reason, "root-device-ambiguous") != 0 ||
            context.retry_count != 0) {
            return fail_self_test(case_name,
                                  "cross-bus ambiguity was not immediate");
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
    const char *reason =
        handoff_root(&ops, "/dev/vdb", ROOT_INIT_INTERACTIVE);

    if (strcmp(reason, expected_reason) != 0 ||
        context.handoff_count != (unsigned int)failing_step + 1) {
        return fail_self_test(case_name, "handoff failure boundary was wrong");
    }
    return 0;
}

static int run_root_init_self_test(const char *case_name)
{
    enum RootInitMode mode = ROOT_INIT_INTERACTIVE;
    char *default_argv[] = { "init", NULL };
    char *interactive_argv[] = { "init", "--root-init=interactive", NULL };
    char *systemd_argv[] = { "init", "--root-init=systemd", NULL };
    char *duplicate_argv[] = {
        "init", "--root-init=interactive", "--root-init=systemd", NULL,
    };
    char *unknown_argv[] = { "init", "--root-init=other", NULL };
    char *control_argv[] = { "init", "--root-init=systemd\n", NULL };

    if (strcmp(case_name, "root-init-default-interactive") == 0) {
        if (parse_root_init(1, default_argv, &mode) != 0 ||
            mode != ROOT_INIT_INTERACTIVE) {
            return fail_self_test(case_name, "default mode was not interactive");
        }
    } else if (strcmp(case_name, "root-init-explicit-interactive") == 0) {
        if (parse_root_init(2, interactive_argv, &mode) != 0 ||
            mode != ROOT_INIT_INTERACTIVE) {
            return fail_self_test(case_name,
                                  "explicit interactive mode was rejected");
        }
    } else if (strcmp(case_name, "root-init-systemd") == 0) {
        if (parse_root_init(2, systemd_argv, &mode) != 0 ||
            mode != ROOT_INIT_SYSTEMD) {
            return fail_self_test(case_name, "systemd mode was rejected");
        }
    } else if (strcmp(case_name, "root-init-duplicate") == 0) {
        if (parse_root_init(3, duplicate_argv, &mode) == 0) {
            return fail_self_test(case_name, "duplicate selector was accepted");
        }
    } else if (strcmp(case_name, "root-init-unknown") == 0) {
        if (parse_root_init(2, unknown_argv, &mode) == 0) {
            return fail_self_test(case_name, "unknown selector was accepted");
        }
    } else if (strcmp(case_name, "root-init-control-character") == 0) {
        if (parse_root_init(2, control_argv, &mode) == 0) {
            return fail_self_test(case_name,
                                  "control character was accepted");
        }
    } else if (strcmp(case_name, "systemd-root-label") == 0) {
        unsigned char superblock[EXT2_SUPERBLOCK_SIZE];
        make_valid_superblock(superblock, SYSTEMD_ROOT_LABEL);
        if (!ext2_superblock_matches(superblock, SYSTEMD_ROOT_LABEL) ||
            ext2_superblock_matches(superblock, INTERACTIVE_ROOT_LABEL)) {
            return fail_self_test(case_name, "M2 root label was not isolated");
        }
    } else if (strcmp(case_name, "systemd-desktop-root-label") == 0) {
        unsigned char superblock[EXT2_SUPERBLOCK_SIZE];
        make_valid_superblock(superblock, DESKTOP_ROOT_LABEL);
        if (!ext2_superblock_matches_mode(superblock, ROOT_INIT_SYSTEMD) ||
            ext2_superblock_matches_mode(superblock,
                                         ROOT_INIT_INTERACTIVE)) {
            return fail_self_test(case_name, "M3 root label was not isolated");
        }
    } else if (strcmp(case_name,
                      "systemd-application-desktop-root-label") == 0) {
        unsigned char superblock[EXT2_SUPERBLOCK_SIZE];
        make_valid_superblock(superblock, APPLICATION_DESKTOP_ROOT_LABEL);
        if (!ext2_superblock_matches_mode(superblock, ROOT_INIT_SYSTEMD) ||
            ext2_superblock_matches_mode(superblock,
                                         ROOT_INIT_INTERACTIVE)) {
            return fail_self_test(case_name, "M4 root label was not isolated");
        }
    } else if (strcmp(case_name,
                      "systemd-network-desktop-root-label") == 0) {
        unsigned char superblock[EXT2_SUPERBLOCK_SIZE];
        make_valid_superblock(superblock, NETWORK_DESKTOP_ROOT_LABEL);
        if (!ext2_superblock_matches_mode(superblock, ROOT_INIT_SYSTEMD) ||
            ext2_superblock_matches_mode(superblock,
                                         ROOT_INIT_INTERACTIVE)) {
            return fail_self_test(case_name, "M5 root label was not isolated");
        }
    } else if (strcmp(case_name, "systemd-browser-root-label") == 0) {
        unsigned char superblock[EXT2_SUPERBLOCK_SIZE];
        make_valid_superblock(superblock, BROWSER_ROOT_LABEL);
        if (!ext2_superblock_matches_mode(superblock, ROOT_INIT_SYSTEMD) ||
            ext2_superblock_matches_mode(superblock,
                                         ROOT_INIT_INTERACTIVE)) {
            return fail_self_test(case_name,
                                  "browser root label was not isolated");
        }
    } else if (strcmp(case_name, "systemd-handoff-sequence") == 0) {
        struct MockContext context = {
            .case_name = case_name,
            .failing_step = (enum HandoffStep)-1,
        };
        struct Stage1Ops ops = {
            .context = &context,
            .perform_handoff = mock_perform_handoff,
        };
        static const enum HandoffStep expected[] = {
            HANDOFF_MOUNT_ROOT, HANDOFF_BIND_DEV,
            HANDOFF_PREPARE_API_DIRS, HANDOFF_MOUNT_RUN,
            HANDOFF_MOUNT_TMP, HANDOFF_CHROOT, HANDOFF_CHDIR, HANDOFF_EXEC,
        };
        const char *reason =
            handoff_root(&ops, "/dev/vdb", ROOT_INIT_SYSTEMD);
        if (strcmp(reason, "exec-returned") != 0 ||
            context.handoff_count != sizeof(expected) / sizeof(expected[0]) ||
            memcmp(context.handoff_steps, expected, sizeof(expected)) != 0) {
            return fail_self_test(case_name,
                                  "systemd handoff sequence was incorrect");
        }
    } else if (strcmp(case_name, "systemd-exec") == 0) {
        char *const *arguments = root_init_arguments(ROOT_INIT_SYSTEMD);
        if (strcmp(arguments[0], "/sbin/init") != 0 ||
            arguments[1] != NULL) {
            return fail_self_test(case_name, "systemd argv was not exact");
        }
    } else {
        return fail_self_test(case_name, "unknown root init case");
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
    } else if (strncmp(case_name, "root-init-", sizeof("root-init-") - 1) ==
                   0 ||
               strncmp(case_name, "systemd-", sizeof("systemd-") - 1) == 0) {
        result = run_root_init_self_test(case_name);
    } else if (strcmp(case_name, "one-valid-device") == 0 ||
               strcmp(case_name, "one-valid-mmc-device") == 0 ||
               strcmp(case_name, "no-match") == 0 ||
               strcmp(case_name, "two-matching-devices") == 0 ||
               strcmp(case_name, "virtio-and-mmc-ambiguous") == 0 ||
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
    void *context, const char *candidate_path,
    char path[ROOT_DEVICE_PATH_SIZE])
{
    const struct ProductionContext *production_context = context;

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
        !ext2_superblock_matches_mode(superblock,
                                      production_context->root_init_mode)) {
        return PROBE_NO_MATCH;
    }

    (void)snprintf(path, ROOT_DEVICE_PATH_SIZE, "%s", candidate_path);
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
    const struct ProductionContext *production_context = context;
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
    case HANDOFF_PREPARE_API_DIRS:
        if (ensure_directory("/newroot/proc") != 0 ||
            ensure_directory("/newroot/sys") != 0 ||
            ensure_directory("/newroot/sys/fs") != 0) {
            return -1;
        }
        return ensure_directory("/newroot/sys/fs/cgroup");
    case HANDOFF_CHROOT:
        return chroot("/newroot");
    case HANDOFF_CHDIR:
        return chdir("/");
    case HANDOFF_EXEC: {
        char *const *arguments =
            root_init_arguments(production_context->root_init_mode);
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
    if (result < 0) {
        return -1;
    }

    int descriptor_flags;
    do {
        descriptor_flags = fcntl(destination_fd, F_GETFD);
    } while (descriptor_flags < 0 && errno == EINTR);
    if (descriptor_flags < 0) {
        return -1;
    }

    do {
        result = fcntl(destination_fd, F_SETFD,
                       descriptor_flags & ~FD_CLOEXEC);
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

int main(int argc, char **argv)
{
    struct ProductionContext context;
    int root_init_result = parse_root_init(argc, argv, &context.root_init_mode);
    if (configure_console() != 0) {
        fail_and_hold("console-open");
    }
    if (root_init_result != 0) {
        fail_and_hold("root-init-argument");
    }

    struct Stage1Ops ops = {
        .context = &context,
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

    reason = handoff_root(&ops, root_device, context.root_init_mode);
    fail_and_hold(reason);
}

#endif
#endif
