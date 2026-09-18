// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

struct drm_virtgpu_getparam {
    uint64_t param;
    uint64_t value;
};

#define DRM_IOCTL_VIRTGPU_GETPARAM _IOWR('d', 0x43, struct drm_virtgpu_getparam)

#define VIRTGPU_PARAM_3D_FEATURES 1U
#define VIRTGPU_PARAM_CAPSET_QUERY_FIX 2U
#define VIRTGPU_PARAM_SUPPORTED_CAPSET_IDs 7U

/* The id the host reports for its renderer's capability set. */
#define VIRTGPU_CAPSET_VIRGL 1U

/* An id no driver defines, so the kernel must refuse it rather than answer
 * with a zero it made up. */
#define UNKNOWN_PARAM 0x3fffffffu

typedef int (*param_ioctl_fn)(void *context, unsigned long request, void *argument);

enum param_expectation {
    EXPECT_OK,
    EXPECT_EINVAL,
};

struct check {
    const char *name;
    uint64_t param;
    enum param_expectation expect;
};

static const struct check CHECKS[] = {
    {"3d-features", VIRTGPU_PARAM_3D_FEATURES, EXPECT_OK},
    {"capset-query-fix", VIRTGPU_PARAM_CAPSET_QUERY_FIX, EXPECT_OK},
    {"supported-capsets", VIRTGPU_PARAM_SUPPORTED_CAPSET_IDs, EXPECT_OK},
    {"unknown-param", UNKNOWN_PARAM, EXPECT_EINVAL},
};

static struct drm_virtgpu_getparam request;
static uint64_t answer;

static void reset_argument(void)
{
    answer = 0;
    request.param = 0;
    request.value = (uint64_t)(uintptr_t)&answer;
}

#ifndef DRM_VIRGL_GATE_SELF_TEST
static void publish_marker(const char *marker)
{
    puts(marker);
    fflush(stdout);
}

static _Noreturn void hold_forever(void)
{
    for (;;) {
        if (pause() < 0 && errno == EINTR)
            continue;
    }
}
#endif

/* Reads one parameter, returning its value or failing on anything that is not
 * the outcome the table requires. */
static int read_param(param_ioctl_fn call, void *context, const struct check *check,
                      uint64_t *value)
{
    reset_argument();
    request.param = check->param;
    errno = 0;
    int result = call(context, DRM_IOCTL_VIRTGPU_GETPARAM, &request);
    int call_errno = errno;

    if (check->expect == EXPECT_EINVAL)
        return result == -1 && call_errno == EINVAL ? 0 : -1;
    if (result != 0)
        return -1;
    *value = answer;
    return 0;
}

static int run_checks(param_ioctl_fn call, void *context, uint64_t *features,
                      uint64_t *capsets)
{
    for (size_t index = 0; index < sizeof(CHECKS) / sizeof(CHECKS[0]); ++index) {
        uint64_t value = 0;
        if (read_param(call, context, &CHECKS[index], &value) != 0) {
            printf("DRM_VIRGL_FAIL stage=%s errno=%d\n", CHECKS[index].name, errno);
            fflush(stdout);
            return -1;
        }
        if (strcmp(CHECKS[index].name, "3d-features") == 0)
            *features = value;
        else if (strcmp(CHECKS[index].name, "supported-capsets") == 0)
            *capsets = value;
    }
    return 0;
}

#if defined(DRM_VIRGL_GATE_SELF_TEST) || defined(DRM_VIRGL_GATE_LIFECYCLE_TEST)

/* A fake kernel that answers the way a virgl host does, so the probe's own
 * comparisons are what a fault has to defeat. */
struct fake_context {
    /* Report no 3D, the way a host started without a GL backend does. */
    int no_3d;
    /* Answer an unknown parameter instead of refusing it. */
    int accept_unknown;
    /* Refuse a parameter the driver does define. */
    int refuse_known;
    /* Report a capset id other than the renderer's own. */
    uint32_t capset_id;
};

static int fake_ioctl(void *opaque, unsigned long request_, void *argument)
{
    struct fake_context *context = opaque;
    struct drm_virtgpu_getparam *param = argument;
    uint64_t value;

    /* The probe must ask the question the driver answers: a wrong ioctl number
     * is a wrong question, not an acceptable one. */
    if (request_ != DRM_IOCTL_VIRTGPU_GETPARAM) {
        errno = ENOTTY;
        return -1;
    }

    if (param->param == UNKNOWN_PARAM) {
        if (context->accept_unknown) {
            value = 0;
        } else {
            errno = EINVAL;
            return -1;
        }
    } else {
        if (context->refuse_known) {
            errno = EINVAL;
            return -1;
        }
        switch (param->param) {
        case VIRTGPU_PARAM_3D_FEATURES:
        case VIRTGPU_PARAM_CAPSET_QUERY_FIX:
            value = context->no_3d ? 0 : 1;
            break;
        case VIRTGPU_PARAM_SUPPORTED_CAPSET_IDs:
            value = context->no_3d ? 0 : (1u << context->capset_id);
            break;
        default:
            errno = EINVAL;
            return -1;
        }
    }
    memcpy((void *)(uintptr_t)param->value, &value, sizeof(value));
    return 0;
}

#ifdef DRM_VIRGL_GATE_SELF_TEST
/* The probe's job is to report what it read, not to decide whether 3D ought to
 * be there: the same build has to run against a GL host and a plain one, and
 * the host gate is what compares the answer to the device it launched. So the
 * self-test covers the protocol — a refused query, an unreadable answer, and a
 * value delivered to the wrong place — rather than any particular value. */
int main(int argc, char **argv)
{
    if (argc != 2)
        return 2;

    struct fake_context context = {0, 0, 0, VIRTGPU_CAPSET_VIRGL};
    uint64_t features = 0, capsets = 0;

    if (strcmp(argv[1], "valid") == 0) {
        if (run_checks(fake_ioctl, &context, &features, &capsets) != 0)
            return 1;
        if (features != 1 || capsets != (1u << VIRTGPU_CAPSET_VIRGL))
            return 1;
    } else if (strcmp(argv[1], "no-3d") == 0) {
        /* A host with no GL backend: every query still answers, with zero. */
        context.no_3d = 1;
        if (run_checks(fake_ioctl, &context, &features, &capsets) != 0)
            return 1;
        if (features != 0 || capsets != 0)
            return 1;
    } else if (strcmp(argv[1], "unknown-accepted") == 0) {
        /* The kernel answered a parameter it should have refused; the probe
         * has to notice rather than accept the answer. */
        context.accept_unknown = 1;
        if (run_checks(fake_ioctl, &context, &features, &capsets) == 0)
            return 1;
    } else if (strcmp(argv[1], "known-refused") == 0) {
        /* The kernel refused a parameter it should have answered; the probe
         * has to notice rather than record a zero. */
        context.refuse_known = 1;
        if (run_checks(fake_ioctl, &context, &features, &capsets) == 0)
            return 1;
    } else {
        return 2;
    }

    printf("DRM_VIRGL_SELF_TEST PASS case=%s\n", argv[1]);
    return 0;
}
#else
int main(void)
{
    struct fake_context context = {0, 0, 0, VIRTGPU_CAPSET_VIRGL};
    uint64_t features = 0, capsets = 0;
    if (run_checks(fake_ioctl, &context, &features, &capsets) != 0)
        return 1;
    printf("DRM_VIRGL_PARAM 3d=%llu capsets=0x%llx\n",
           (unsigned long long)features, (unsigned long long)capsets);
    publish_marker("ASTERINAS_DRM_VIRGL_R1_READY");
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
    printf("DRM_VIRGL_FAIL stage=%s errno=%d\n", stage, errno);
    fflush(stdout);
    hold_forever();
}

int main(void)
{
    /* A 3D client reaches the device through the render node, which is also
     * where the permission split has to let the query through. */
    int fd = open("/dev/dri/renderD128", O_RDWR | O_CLOEXEC);
    if (fd < 0)
        fail_and_hold("open-renderD128");

    uint64_t features = 0, capsets = 0;
    if (run_checks(real_ioctl, &fd, &features, &capsets) != 0)
        hold_forever();

    printf("DRM_VIRGL_PARAM 3d=%llu capsets=0x%llx\n",
           (unsigned long long)features, (unsigned long long)capsets);
    fflush(stdout);

    close(fd);
    publish_marker("ASTERINAS_DRM_VIRGL_R1_READY");
    hold_forever();
}

#endif
