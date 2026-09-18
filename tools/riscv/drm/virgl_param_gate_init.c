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

struct drm_virtgpu_get_caps {
    uint32_t cap_set_id;
    uint32_t cap_set_ver;
    uint64_t addr;
    uint32_t size;
    uint32_t pad;
};

struct drm_virtgpu_context_set_param {
    uint64_t param;
    uint64_t value;
};

struct drm_virtgpu_context_init {
    uint32_t num_params;
    uint32_t pad;
    uint64_t ctx_set_params;
};

#define DRM_IOCTL_VIRTGPU_GETPARAM _IOWR('d', 0x43, struct drm_virtgpu_getparam)
#define DRM_IOCTL_VIRTGPU_GET_CAPS _IOWR('d', 0x49, struct drm_virtgpu_get_caps)
#define DRM_IOCTL_VIRTGPU_CONTEXT_INIT _IOWR('d', 0x4b, struct drm_virtgpu_context_init)

#define VIRTGPU_PARAM_3D_FEATURES 1U
#define VIRTGPU_PARAM_CAPSET_QUERY_FIX 2U
#define VIRTGPU_PARAM_SUPPORTED_CAPSET_IDs 7U

#define VIRTGPU_CONTEXT_PARAM_CAPSET_ID 1U
#define VIRTGPU_CONTEXT_PARAM_NUM_RINGS 2U
#define VIRTGPU_CONTEXT_PARAM_DEBUG_NAME 4U

/* Larger than any renderer's capability blob, so the kernel is the one that
 * decides how much of it is real. */
#define CAPS_BUFFER_SIZE 8192U
/* How much of the blob must carry data for it to be the host's rather than
 * zeros the kernel invented. */
#define CAPS_MIN_DELIVERED 256U

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

static unsigned char caps_buffer[CAPS_BUFFER_SIZE];
static struct drm_virtgpu_get_caps caps_request;
static struct drm_virtgpu_context_init context_request;
static struct drm_virtgpu_context_set_param context_params[2];

static void reset_argument(void)
{
    answer = 0;
    request.param = 0;
    request.value = (uint64_t)(uintptr_t)&answer;

    memset(caps_buffer, 0xa5, sizeof(caps_buffer));
    memset(&caps_request, 0, sizeof(caps_request));
    caps_request.cap_set_id = VIRTGPU_CAPSET_VIRGL;
    caps_request.addr = (uint64_t)(uintptr_t)caps_buffer;
    caps_request.size = sizeof(caps_buffer);

    memset(context_params, 0, sizeof(context_params));
    context_params[0].param = VIRTGPU_CONTEXT_PARAM_CAPSET_ID;
    context_params[0].value = VIRTGPU_CAPSET_VIRGL;
    context_params[1].param = VIRTGPU_CONTEXT_PARAM_DEBUG_NAME;
    context_params[1].value = (uint64_t)(uintptr_t)"asterinas-gate";

    memset(&context_request, 0, sizeof(context_request));
    context_request.num_params = 2;
    context_request.ctx_set_params = (uint64_t)(uintptr_t)context_params;
}

/* How many bytes of the capability buffer the host wrote.
 *
 * The probe fills the buffer itself first, so a byte still holding that
 * pattern was never delivered. Counting the whole buffer rather than a prefix
 * matters: a prefix stops at the first byte the blob happens to share with the
 * pattern and so understates a blob of any size. */
static unsigned delivered_bytes(void)
{
    unsigned count = 0;
    for (unsigned index = 0; index < sizeof(caps_buffer); ++index) {
        if (caps_buffer[index] != 0xa5)
            count++;
    }
    return count;
}

static void publish_marker(const char *marker)
{
    puts(marker);
    fflush(stdout);
}

#ifndef DRM_VIRGL_GATE_SELF_TEST
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

/* A failed check, reported with the name the host gate classifies. */
static int report_failure(const char *stage)
{
    printf("DRM_VIRGL_FAIL stage=%s errno=%d\n", stage, errno);
    fflush(stdout);
    return -1;
}

/* Exercises the capability blob and the context, which only exist when the
 * host has 3D. Both directions matter: on a 3D host these must work, and on a
 * host without 3D they must be refused rather than half-work, so the probe
 * asserts whichever the report it just read calls for. */
static int run_caps_and_context(param_ioctl_fn call, void *context, int three_d,
                                int publish)
{
    reset_argument();
    errno = 0;
    int result = call(context, DRM_IOCTL_VIRTGPU_GET_CAPS, &caps_request);

    if (!three_d) {
        if (result == 0 || errno != EINVAL) {
            errno = result == 0 ? 0 : errno;
            return report_failure("caps-without-3d");
        }
        reset_argument();
        errno = 0;
        if (call(context, DRM_IOCTL_VIRTGPU_CONTEXT_INIT, &context_request) == 0 ||
            errno != EINVAL)
            return report_failure("context-without-3d");
        if (publish) {
            publish_marker("DRM_VIRGL_CAPS PASS caps_bytes=0");
            publish_marker("DRM_VIRGL_CONTEXT PASS");
        }
        return 0;
    }

    if (result != 0)
        return report_failure("get-caps");

    /* The kernel must have copied the host's blob, not left the probe's own
     * fill pattern in place. */
    unsigned delivered = delivered_bytes();
    if (delivered < CAPS_MIN_DELIVERED)
        return report_failure("caps-empty");

    /* Creating the context is what proves the capability set the probe read is
     * one the host will actually build a renderer from. */
    reset_argument();
    errno = 0;
    if (call(context, DRM_IOCTL_VIRTGPU_CONTEXT_INIT, &context_request) != 0)
        return report_failure("context-init");

    /* One context per file is the contract, so a second must be refused. */
    reset_argument();
    errno = 0;
    if (call(context, DRM_IOCTL_VIRTGPU_CONTEXT_INIT, &context_request) == 0 ||
        errno != EEXIST)
        return report_failure("context-init-twice");

    if (publish) {
        /* `delivered` is how far past the probe's own fill pattern the host's
         * data reaches, which is the closest this can get to the blob's length:
         * the ioctl does not report how much it copied. */
        printf("DRM_VIRGL_CAPS PASS caps_bytes=%u\n", delivered);
        fflush(stdout);
        publish_marker("DRM_VIRGL_CONTEXT PASS");
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
    /* Copy nothing into the capability buffer, leaving the caller's fill. */
    int caps_empty;
    /* Let a second context be created on the same file. */
    int allow_second_context;
    /* Contexts created so far, so the fake can refuse the second. */
    int contexts_created;
};

static int fake_ioctl(void *opaque, unsigned long request_, void *argument)
{
    struct fake_context *context = opaque;
    uint64_t value;

    if (request_ == DRM_IOCTL_VIRTGPU_GET_CAPS) {
        struct drm_virtgpu_get_caps *caps = argument;
        if (context->no_3d) {
            errno = EINVAL;
            return -1;
        }
        if (caps->cap_set_id != VIRTGPU_CAPSET_VIRGL) {
            errno = EINVAL;
            return -1;
        }
        if (!context->caps_empty)
            memset((void *)(uintptr_t)caps->addr, 0x5a, caps->size);
        return 0;
    }

    if (request_ == DRM_IOCTL_VIRTGPU_CONTEXT_INIT) {
        const struct drm_virtgpu_context_init *init = argument;
        if (context->no_3d) {
            errno = EINVAL;
            return -1;
        }
        /* The context must be created against the renderer's own capability
         * set, so the fake reads the parameters back rather than accepting any
         * array the probe happens to pass. */
        if (init->num_params != 2 || init->ctx_set_params == 0) {
            errno = EINVAL;
            return -1;
        }
        const struct drm_virtgpu_context_set_param *params =
            (const struct drm_virtgpu_context_set_param *)(uintptr_t)init->ctx_set_params;
        if (params[0].param != VIRTGPU_CONTEXT_PARAM_CAPSET_ID ||
            params[0].value != VIRTGPU_CAPSET_VIRGL ||
            params[1].param != VIRTGPU_CONTEXT_PARAM_DEBUG_NAME ||
            params[1].value == 0) {
            errno = EINVAL;
            return -1;
        }
        if (context->contexts_created > 0 && !context->allow_second_context) {
            errno = EEXIST;
            return -1;
        }
        context->contexts_created++;
        return 0;
    }

    struct drm_virtgpu_getparam *param = argument;

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

    struct fake_context context = {.capset_id = VIRTGPU_CAPSET_VIRGL};
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
    } else if (strcmp(argv[1], "caps-and-context") == 0) {
        if (run_checks(fake_ioctl, &context, &features, &capsets) != 0)
            return 1;
        if (run_caps_and_context(fake_ioctl, &context, 1, 0) != 0)
            return 1;
    } else if (strcmp(argv[1], "caps-empty") == 0) {
        /* The kernel returned success without copying the host's blob; the
         * probe must not accept the caller's fill pattern as evidence. */
        context.caps_empty = 1;
        if (run_checks(fake_ioctl, &context, &features, &capsets) != 0)
            return 1;
        if (run_caps_and_context(fake_ioctl, &context, 1, 0) == 0)
            return 1;
    } else if (strcmp(argv[1], "context-twice-allowed") == 0) {
        /* The kernel let a second context be created on one file. */
        context.allow_second_context = 1;
        if (run_checks(fake_ioctl, &context, &features, &capsets) != 0)
            return 1;
        if (run_caps_and_context(fake_ioctl, &context, 1, 0) == 0)
            return 1;
    } else if (strcmp(argv[1], "no-3d-refuses-caps") == 0) {
        /* A host without 3D must refuse the capability and context requests
         * rather than answer them. */
        context.no_3d = 1;
        if (run_checks(fake_ioctl, &context, &features, &capsets) != 0)
            return 1;
        if (run_caps_and_context(fake_ioctl, &context, 0, 0) != 0)
            return 1;
    } else if (strcmp(argv[1], "no-3d-caps-succeed") == 0) {
        /* The inverse: a kernel that answers capability requests on a host
         * with no 3D, which would hand a client a blob that means nothing. */
        if (run_checks(fake_ioctl, &context, &features, &capsets) != 0)
            return 1;
        struct fake_context answering = {.caps_empty = 0};
        if (run_caps_and_context(fake_ioctl, &answering, 0, 0) == 0)
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
    struct fake_context context = {.capset_id = VIRTGPU_CAPSET_VIRGL};
    uint64_t features = 0, capsets = 0;
    if (run_checks(fake_ioctl, &context, &features, &capsets) != 0)
        return 1;
    printf("DRM_VIRGL_PARAM 3d=%llu capsets=0x%llx\n",
           (unsigned long long)features, (unsigned long long)capsets);
    fflush(stdout);
    if (run_caps_and_context(fake_ioctl, &context, (int)features, 1) != 0)
        return 1;
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

    if (run_caps_and_context(real_ioctl, &fd, (int)features, 1) != 0)
        hold_forever();

    /* Closing the file tears the context down; the host would otherwise hold
     * it for the device's lifetime. */
    close(fd);
    publish_marker("ASTERINAS_DRM_VIRGL_R1_READY");
    hold_forever();
}

#endif
