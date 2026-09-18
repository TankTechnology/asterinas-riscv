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
#include <time.h>
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

struct drm_virtgpu_map {
    uint64_t offset;
    uint32_t handle;
    uint32_t pad;
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

struct drm_virtgpu_resource_info {
    uint32_t bo_handle;
    uint32_t res_handle;
    uint32_t size;
    uint32_t blob_mem;
};

#define DRM_IOCTL_VIRTGPU_GETPARAM _IOWR('d', 0x43, struct drm_virtgpu_getparam)
#define DRM_IOCTL_VIRTGPU_GET_CAPS _IOWR('d', 0x49, struct drm_virtgpu_get_caps)
#define DRM_IOCTL_VIRTGPU_CONTEXT_INIT _IOWR('d', 0x4b, struct drm_virtgpu_context_init)
#define DRM_IOCTL_VIRTGPU_MAP _IOWR('d', 0x41, struct drm_virtgpu_map)
#define DRM_IOCTL_VIRTGPU_RESOURCE_CREATE _IOWR('d', 0x44, struct drm_virtgpu_resource_create)
#define DRM_IOCTL_VIRTGPU_RESOURCE_INFO _IOWR('d', 0x45, struct drm_virtgpu_resource_info)

/* Mesa's Gallium resource contract, which is what the `target` field carries. */
#define PIPE_BUFFER 0U
/* PIPE_FORMAT_NONE: a buffer's contents are described by the commands that
 * read it, not by the resource. */
#define PIPE_FORMAT_NONE 0U

/* A small but not trivially small buffer: large enough that a backing that was
 * never really allocated cannot pass by accident. */
#define RESOURCE_SIZE 4096U

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

static struct drm_virtgpu_map map_request;
static struct drm_virtgpu_resource_create resource_request;
static struct drm_virtgpu_resource_info info_request;

/* What a resource creation handed back, for the caller to check further. */
struct resource_handles {
    uint32_t bo_handle;
    uint32_t res_handle;
    uint32_t size;
    uint64_t offset;
};

static void reset_resource_requests(void)
{
    memset(&resource_request, 0, sizeof(resource_request));
    resource_request.target = PIPE_BUFFER;
    resource_request.format = PIPE_FORMAT_NONE;
    resource_request.size = RESOURCE_SIZE;
    resource_request.width = RESOURCE_SIZE;
    resource_request.height = 1;
    resource_request.depth = 1;
    resource_request.array_size = 1;
    resource_request.last_level = 1;
    resource_request.nr_samples = 1;

    memset(&map_request, 0, sizeof(map_request));
    memset(&info_request, 0, sizeof(info_request));
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

/* Creates a 3D resource and obtains the guest offset of the memory backing it.
 *
 * The point of the check is that the buffer a client writes into is real: the
 * creation has to hand back both a handle it can be named by and a resource id
 * the renderer knows, and the handle has to resolve to a mappable offset. */
static int run_resource(param_ioctl_fn call, void *context, int three_d, int publish,
                        struct resource_handles *out)
{
    reset_resource_requests();
    errno = 0;
    int result = call(context, DRM_IOCTL_VIRTGPU_RESOURCE_CREATE, &resource_request);

    if (!three_d) {
        if (result == 0 || errno != EINVAL)
            return report_failure("resource-without-3d");
        if (publish)
            publish_marker("DRM_VIRGL_RESOURCE PASS bo=0 res=0 size=0");
        return 0;
    }

    if (result != 0)
        return report_failure("resource-create");
    if (resource_request.bo_handle == 0 || resource_request.res_handle == 0)
        return report_failure("resource-handles-empty");
    if (resource_request.size != RESOURCE_SIZE)
        return report_failure("resource-size-wrong");

    /* The handle has to name memory the client can reach, or there is nothing
     * to put commands in. */
    memset(&map_request, 0, sizeof(map_request));
    map_request.handle = resource_request.bo_handle;
    errno = 0;
    if (call(context, DRM_IOCTL_VIRTGPU_MAP, &map_request) != 0)
        return report_failure("resource-map");

    /* And the renderer's name for it has to lead back to the same handle,
     * which is only true if both were recorded together. */
    memset(&info_request, 0, sizeof(info_request));
    info_request.res_handle = resource_request.res_handle;
    errno = 0;
    if (call(context, DRM_IOCTL_VIRTGPU_RESOURCE_INFO, &info_request) != 0)
        return report_failure("resource-info");
    if (info_request.bo_handle != resource_request.bo_handle)
        return report_failure("resource-info-handle");
    if (info_request.size != RESOURCE_SIZE)
        return report_failure("resource-info-size");

    out->bo_handle = resource_request.bo_handle;
    out->res_handle = resource_request.res_handle;
    out->size = resource_request.size;
    out->offset = map_request.offset;

    if (publish) {
        printf("DRM_VIRGL_RESOURCE PASS bo=%u res=%u size=%u\n", out->bo_handle,
               out->res_handle, out->size);
        fflush(stdout);
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
    /* Resources created so far, which is how the fake names them. */
    int resources_created;
    /* Answer a resource-info query by echoing it rather than looking it up. */
    int echo_resource_info;
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

    if (request_ == DRM_IOCTL_VIRTGPU_RESOURCE_CREATE) {
        struct drm_virtgpu_resource_create *create = argument;
        if (context->no_3d) {
            errno = EINVAL;
            return -1;
        }
        /* The renderer is told the resource's shape, so a request that does not
         * carry one is not a resource this fake will invent. */
        if (create->target != PIPE_BUFFER || create->size != RESOURCE_SIZE ||
            create->array_size != 1) {
            errno = EINVAL;
            return -1;
        }
        context->resources_created++;
        create->bo_handle = 0x40 + context->resources_created;
        create->res_handle = 0x100 + context->resources_created;
        return 0;
    }

    if (request_ == DRM_IOCTL_VIRTGPU_MAP) {
        struct drm_virtgpu_map *map = argument;
        if (map->handle < 0x40) {
            errno = EINVAL;
            return -1;
        }
        map->offset = 0x100000;
        return 0;
    }

    if (request_ == DRM_IOCTL_VIRTGPU_RESOURCE_INFO) {
        struct drm_virtgpu_resource_info *info = argument;
        /* Answer only for a resource this fake handed out, so a kernel that
         * echoes the query instead of looking the resource up is caught. */
        if (info->res_handle < 0x100) {
            errno = EINVAL;
            return -1;
        }
        info->bo_handle = context->echo_resource_info
                              ? info->bo_handle
                              : 0x40 + (info->res_handle - 0x100);
        info->size = RESOURCE_SIZE;
        info->blob_mem = 0;
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
    } else if (strcmp(argv[1], "resource") == 0) {
        struct resource_handles handles;
        if (run_checks(fake_ioctl, &context, &features, &capsets) != 0)
            return 1;
        if (run_caps_and_context(fake_ioctl, &context, 1, 0) != 0)
            return 1;
        if (run_resource(fake_ioctl, &context, 1, 0, &handles) != 0)
            return 1;
        if (handles.bo_handle == 0 || handles.res_handle == 0 ||
            handles.size != RESOURCE_SIZE || handles.offset == 0)
            return 1;
    } else if (strcmp(argv[1], "resource-info-echoed") == 0) {
        /* The kernel answered the resource lookup by echoing the query rather
         * than naming the handle, which would make the pair meaningless. */
        context.echo_resource_info = 1;
        struct resource_handles handles;
        if (run_checks(fake_ioctl, &context, &features, &capsets) != 0)
            return 1;
        if (run_caps_and_context(fake_ioctl, &context, 1, 0) != 0)
            return 1;
        if (run_resource(fake_ioctl, &context, 1, 0, &handles) == 0)
            return 1;
    } else if (strcmp(argv[1], "resource-without-3d") == 0) {
        /* A host without 3D must refuse resource creation too. */
        context.no_3d = 1;
        struct resource_handles handles;
        if (run_resource(fake_ioctl, &context, 0, 0, &handles) != 0)
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
    struct resource_handles handles;
    if (run_resource(fake_ioctl, &context, (int)features, 1, &handles) != 0)
        return 1;
    /* The fake has no file description, so it cannot make the mapping check the
     * real build makes. These two markers stand in for it so that this build
     * emits the whole sequence the host gate parses — the parser is what this
     * build exists to exercise, and the real run is what makes the checks
     * mean anything. */
    if (features != 0)
        publish_marker("DRM_VIRGL_BACKING PASS");
    publish_marker("DRM_VIRGL_TIMING caps_context_us=0 resource_us=0 backing_us=0");
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

static uint64_t now_us(void)
{
    struct timespec timestamp;
    if (clock_gettime(CLOCK_MONOTONIC, &timestamp) != 0)
        return 0;
    return (uint64_t)timestamp.tv_sec * 1000000ULL +
           (uint64_t)timestamp.tv_nsec / 1000ULL;
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

    uint64_t start = now_us();
    if (run_caps_and_context(real_ioctl, &fd, (int)features, 1) != 0)
        hold_forever();
    uint64_t after_caps = now_us();

    struct resource_handles handles = {0, 0, 0, 0};
    if (run_resource(real_ioctl, &fd, (int)features, 1, &handles) != 0)
        hold_forever();
    uint64_t after_resource = now_us();

    /* The backing has to be memory the client can actually write into, so map
     * it through the offset the ioctl reported and read back what was written.
     * Only the real path can do this: it needs the file description. */
    uint64_t after_backing = after_resource;
    if (features != 0) {
        if (handles.size > (uint32_t)SIZE_MAX)
            fail_and_hold("resource-size-overflow");
        unsigned char *backing = mmap(NULL, handles.size, PROT_READ | PROT_WRITE,
                                      MAP_SHARED, fd, (off_t)handles.offset);
        if (backing == MAP_FAILED)
            fail_and_hold("resource-mmap");
        for (uint32_t index = 0; index < handles.size; ++index)
            backing[index] = (unsigned char)(index * 7 + 1);
        if (msync(backing, handles.size, MS_SYNC) != 0)
            fail_and_hold("resource-msync");
        for (uint32_t index = 0; index < handles.size; ++index) {
            if (backing[index] != (unsigned char)(index * 7 + 1))
                fail_and_hold("resource-readback");
        }
        if (munmap(backing, handles.size) != 0)
            fail_and_hold("resource-munmap");
        after_backing = now_us();
        publish_marker("DRM_VIRGL_BACKING PASS");
    }

    /* Reported in microseconds for the work each phase did, as a record of
     * what this path costs rather than as a threshold anything is held to:
     * the guest is emulated, so the number describes the emulator as much as
     * the driver. */
    printf("DRM_VIRGL_TIMING caps_context_us=%llu resource_us=%llu backing_us=%llu\n",
           (unsigned long long)(after_caps - start),
           (unsigned long long)(after_resource - after_caps),
           (unsigned long long)(after_backing - after_resource));
    fflush(stdout);

    /* Closing the file tears the context down; the host would otherwise hold
     * it for the device's lifetime. */
    close(fd);
    publish_marker("ASTERINAS_DRM_VIRGL_R1_READY");
    hold_forever();
}

#endif
