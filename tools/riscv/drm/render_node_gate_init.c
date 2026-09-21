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

struct drm_version {
    int version_major;
    int version_minor;
    int version_patchlevel;
    size_t name_len;
    char *name;
    size_t date_len;
    char *date;
    size_t desc_len;
    char *desc;
};

struct drm_get_cap {
    uint64_t capability;
    uint64_t value;
};

struct drm_set_client_cap {
    uint64_t capability;
    uint64_t value;
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

/* Twelve bytes, with no padding: `drm_prime_handle` has three fields and the
 * struct's size is part of the ioctl's command number. */
struct drm_prime_handle {
    uint32_t handle;
    uint32_t flags;
    int32_t fd;
};

struct drm_mode_modeinfo {
    uint32_t clock;
    uint16_t hdisplay, hsync_start, hsync_end, htotal, hskew;
    uint16_t vdisplay, vsync_start, vsync_end, vtotal, vscan;
    uint32_t vrefresh;
    uint32_t flags;
    uint32_t type;
    char name[32];
};

struct drm_mode_crtc {
    uint64_t set_connectors_ptr;
    uint32_t count_connectors;
    uint32_t crtc_id;
    uint32_t fb_id;
    uint32_t x, y;
    uint32_t gamma_size;
    uint32_t mode_valid;
    struct drm_mode_modeinfo mode;
};

struct drm_mode_card_res {
    uint64_t fb_id_ptr;
    uint64_t crtc_id_ptr;
    uint64_t connector_id_ptr;
    uint64_t encoder_id_ptr;
    uint32_t count_fbs;
    uint32_t count_crtcs;
    uint32_t count_connectors;
    uint32_t count_encoders;
    uint32_t min_width, max_width, min_height, max_height;
};

struct drm_mode_cursor {
    uint32_t flags;
    uint32_t crtc_id;
    int32_t x, y;
    uint32_t width, height;
    uint32_t handle;
};

struct drm_mode_crtc_page_flip {
    uint32_t crtc_id;
    uint32_t fb_id;
    uint32_t flags;
    uint32_t reserved;
    uint64_t user_data;
};

struct drm_mode_create_dumb {
    uint32_t height, width, bpp, flags;
    uint32_t handle, pitch;
    uint64_t size;
};

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

struct drm_virtgpu_context_init {
    uint32_t num_params;
    uint32_t pad;
    uint64_t ctx_set_params;
};

#define DRM_IOCTL_VERSION _IOWR('d', 0x00, struct drm_version)
#define DRM_IOCTL_GEM_CLOSE _IOW('d', 0x09, struct drm_gem_close)
#define DRM_IOCTL_GEM_FLINK _IOWR('d', 0x0a, struct drm_gem_flink)
#define DRM_IOCTL_GET_CAP _IOWR('d', 0x0c, struct drm_get_cap)
#define DRM_IOCTL_SET_CLIENT_CAP _IOW('d', 0x0d, struct drm_set_client_cap)
/* `DRM_IOWR(0x0b, ...)`, per /usr/include/drm/drm.h:1100 -- see the longer note
 * in gem_gate_init.c. */
#define DRM_IOCTL_GEM_OPEN _IOWR('d', 0x0b, struct drm_gem_open)
#define DRM_IOCTL_SET_MASTER _IO('d', 0x1e)
#define DRM_IOCTL_DROP_MASTER _IO('d', 0x1f)
#define DRM_IOCTL_MODE_GETRESOURCES _IOWR('d', 0xa0, struct drm_mode_card_res)
#define DRM_IOCTL_MODE_GETCRTC _IOWR('d', 0xa1, struct drm_mode_crtc)
#define DRM_IOCTL_MODE_SETCRTC _IOWR('d', 0xa2, struct drm_mode_crtc)
#define DRM_IOCTL_MODE_CURSOR _IOWR('d', 0xa3, struct drm_mode_cursor)
#define DRM_IOCTL_MODE_PAGE_FLIP _IOWR('d', 0xb0, struct drm_mode_crtc_page_flip)
#define DRM_IOCTL_MODE_CREATE_DUMB _IOWR('d', 0xb2, struct drm_mode_create_dumb)
/* `DRM_COMMAND_BASE` (0x40) plus the `DRM_VIRTGPU_*` command number. */
#define DRM_IOCTL_PRIME_HANDLE_TO_FD _IOWR('d', 0x2d, struct drm_prime_handle)

#define DRM_IOCTL_VIRTGPU_GETPARAM _IOWR('d', 0x43, struct drm_virtgpu_getparam)
#define DRM_IOCTL_VIRTGPU_GET_CAPS _IOWR('d', 0x49, struct drm_virtgpu_get_caps)
#define DRM_IOCTL_VIRTGPU_CONTEXT_INIT _IOWR('d', 0x4b, struct drm_virtgpu_context_init)

#define VIRTGPU_PARAM_3D_FEATURES 1U

#define DRM_CAP_DUMB_BUFFER 1U
#define DRM_CLIENT_CAP_UNIVERSAL_PLANES 2U
#define DRM_MODE_CURSOR_BO 0x01U
#define DRM_MODE_PAGE_FLIP_EVENT 0x01U
#define CRTC_ID 1U

/* Values the kernel cannot know about, so a permitted call reaches its handler
 * and fails on the merits rather than on argument validation. */
#define UNKNOWN_HANDLE 0x3fffffffu
#define UNKNOWN_NAME 0x3fffffffu

typedef int (*node_ioctl_fn)(void *context, unsigned long request, void *argument);

enum node_mode {
    /* `renderD128`, which keeps only the render-allowed ioctls. */
    NODE_RENDER,
    /* `card0`, which keeps everything. */
    NODE_CARD,
};

enum expectation {
    /* The call must succeed. */
    EXPECT_OK,
    /* Permitted, then failing on the merits — which is how a permitted call is
     * told apart from one the node refused. */
    EXPECT_EINVAL,
    /* Refused by the node's permission policy, unless this table is run
     * against the card node, where it must not be. */
    EXPECT_EACCES,
};

struct check {
    const char *name;
    unsigned long request;
    enum expectation expect;
};

/* The ioctls a render node withholds: the entries of Linux's `drm_ioctls[]`
 * that lack `DRM_RENDER_ALLOW`. The arguments are ones the card node accepts,
 * so a refusal can only come from the permission check. */
static const struct check REFUSED[] = {
    {"set-client-cap", DRM_IOCTL_SET_CLIENT_CAP, EXPECT_EACCES},
    {"set-master", DRM_IOCTL_SET_MASTER, EXPECT_EACCES},
    {"drop-master", DRM_IOCTL_DROP_MASTER, EXPECT_EACCES},
    {"gem-flink", DRM_IOCTL_GEM_FLINK, EXPECT_EACCES},
    {"gem-open", DRM_IOCTL_GEM_OPEN, EXPECT_EACCES},
    {"mode-getresources", DRM_IOCTL_MODE_GETRESOURCES, EXPECT_EACCES},
    {"mode-getcrtc", DRM_IOCTL_MODE_GETCRTC, EXPECT_EACCES},
    {"mode-setcrtc", DRM_IOCTL_MODE_SETCRTC, EXPECT_EACCES},
    {"mode-cursor", DRM_IOCTL_MODE_CURSOR, EXPECT_EACCES},
    {"mode-page-flip", DRM_IOCTL_MODE_PAGE_FLIP, EXPECT_EACCES},
    {"mode-create-dumb", DRM_IOCTL_MODE_CREATE_DUMB, EXPECT_EACCES},
};

/* The ioctls a render node keeps: the whole of `DRM_RENDER_ALLOW`. `GEM_CLOSE`
 * is listed with `EXPECT_EINVAL` because the only thing a render node can be
 * asked to close is a handle it never had, so reaching `EINVAL` is itself the
 * evidence that the call was permitted. */
static const struct check ALLOWED[] = {
    {"version", DRM_IOCTL_VERSION, EXPECT_OK},
    {"get-cap", DRM_IOCTL_GET_CAP, EXPECT_OK},
    {"gem-close", DRM_IOCTL_GEM_CLOSE, EXPECT_EINVAL},
    /* Same shape as `gem-close`: the only handle a render node can be asked to
     * export is one it never had, so reaching `EINVAL` is the evidence that the
     * call was permitted. This is also the ioctl `DRM_CAP_PRIME` promises. */
    {"prime-handle-to-fd", DRM_IOCTL_PRIME_HANDLE_TO_FD, EXPECT_EINVAL},
    {"virtgpu-getparam", DRM_IOCTL_VIRTGPU_GETPARAM, EXPECT_OK},
    {"virtgpu-get-caps", DRM_IOCTL_VIRTGPU_GET_CAPS, EXPECT_OK},
    {"virtgpu-context-init", DRM_IOCTL_VIRTGPU_CONTEXT_INIT, EXPECT_OK},
};

static struct drm_version version;
static struct drm_get_cap cap;
static struct drm_set_client_cap client_cap;
static struct drm_gem_close close_request;
static struct drm_gem_flink flink;
static struct drm_gem_open open_request;
static struct drm_prime_handle prime_request;
static struct drm_mode_card_res resources;
static struct drm_mode_crtc crtc;
static struct drm_mode_cursor cursor;
static struct drm_mode_crtc_page_flip flip;
static struct drm_mode_create_dumb dumb;
static struct drm_virtgpu_getparam getparam;
static uint64_t getparam_value;
static struct drm_virtgpu_get_caps caps_request;
static struct drm_virtgpu_context_init context_request;

static char name_buffer[64];
static char date_buffer[64];
static char desc_buffer[64];

/* Every ioctl writes back, so each phase starts from the same arguments. */
static void reset_arguments(void)
{
    memset(&version, 0, sizeof(version));
    version.name = name_buffer;
    version.name_len = sizeof(name_buffer);
    version.date = date_buffer;
    version.date_len = sizeof(date_buffer);
    version.desc = desc_buffer;
    version.desc_len = sizeof(desc_buffer);

    memset(&cap, 0, sizeof(cap));
    cap.capability = DRM_CAP_DUMB_BUFFER;

    memset(&client_cap, 0, sizeof(client_cap));
    client_cap.capability = DRM_CLIENT_CAP_UNIVERSAL_PLANES;
    client_cap.value = 1;

    memset(&close_request, 0, sizeof(close_request));
    close_request.handle = UNKNOWN_HANDLE;

    memset(&flink, 0, sizeof(flink));
    flink.handle = UNKNOWN_HANDLE;

    memset(&open_request, 0, sizeof(open_request));
    open_request.name = UNKNOWN_NAME;

    memset(&prime_request, 0, sizeof(prime_request));
    prime_request.handle = UNKNOWN_HANDLE;

    memset(&resources, 0, sizeof(resources));

    memset(&crtc, 0, sizeof(crtc));
    crtc.crtc_id = CRTC_ID;

    memset(&cursor, 0, sizeof(cursor));
    cursor.flags = DRM_MODE_CURSOR_BO;
    cursor.crtc_id = CRTC_ID;

    memset(&flip, 0, sizeof(flip));
    flip.crtc_id = CRTC_ID;
    flip.flags = DRM_MODE_PAGE_FLIP_EVENT;

    memset(&dumb, 0, sizeof(dumb));
    dumb.width = 64;
    dumb.height = 64;
    dumb.bpp = 32;

    /* `value` is a userspace pointer the kernel writes one u64 to. */
    getparam_value = 0;
    memset(&getparam, 0, sizeof(getparam));
    getparam.param = VIRTGPU_PARAM_3D_FEATURES;
    getparam.value = (uint64_t)(uintptr_t)&getparam_value;

    /* Zeroed requests: this gate asks only whether the node lets the call
     * through, and the permission check runs before the arguments are read. */
    memset(&caps_request, 0, sizeof(caps_request));
    memset(&context_request, 0, sizeof(context_request));
}

static void *argument_for(unsigned long request)
{
    switch (request) {
    case DRM_IOCTL_VERSION:
        return &version;
    case DRM_IOCTL_GET_CAP:
        return &cap;
    case DRM_IOCTL_SET_CLIENT_CAP:
        return &client_cap;
    case DRM_IOCTL_GEM_CLOSE:
        return &close_request;
    case DRM_IOCTL_GEM_FLINK:
        return &flink;
    case DRM_IOCTL_GEM_OPEN:
        return &open_request;
    case DRM_IOCTL_PRIME_HANDLE_TO_FD:
        return &prime_request;
    case DRM_IOCTL_MODE_GETRESOURCES:
        return &resources;
    case DRM_IOCTL_MODE_GETCRTC:
    case DRM_IOCTL_MODE_SETCRTC:
        return &crtc;
    case DRM_IOCTL_MODE_CURSOR:
        return &cursor;
    case DRM_IOCTL_MODE_PAGE_FLIP:
        return &flip;
    case DRM_IOCTL_MODE_CREATE_DUMB:
        return &dumb;
    case DRM_IOCTL_VIRTGPU_GETPARAM:
        return &getparam;
    case DRM_IOCTL_VIRTGPU_GET_CAPS:
        return &caps_request;
    case DRM_IOCTL_VIRTGPU_CONTEXT_INIT:
        return &context_request;
    default:
        return NULL;
    }
}

#ifndef DRM_RENDER_GATE_SELF_TEST
static void publish_marker(const char *marker)
{
    puts(marker);
    fflush(stdout);
}

#endif

/* Runs one table against a node, returning the name of the first check that did
 * not produce what the node requires, or NULL when all of them did.
 *
 * On the card node a "refused" entry inverts: the point of running the same
 * table there is that the calls must survive the permission check, so a kernel
 * that refused them on every node could not satisfy both runs. */
static const char *run_checks(node_ioctl_fn call, void *context,
                              const struct check *checks, size_t count,
                              enum node_mode mode)
{
    for (size_t index = 0; index < count; ++index) {
        errno = 0;
        int result = call(context, checks[index].request,
                          argument_for(checks[index].request));
        int call_errno = errno;
        enum expectation expect = checks[index].expect;
        if (mode == NODE_CARD && expect == EXPECT_EACCES)
            expect = EXPECT_OK;

        int satisfied = 0;
        switch (expect) {
        /* On the card node a formerly-refused call may fail for its own
         * reasons; what matters is that the permission gate was not what
         * stopped it. */
        case EXPECT_OK:
            satisfied = !(result == -1 && call_errno == EACCES);
            break;
        case EXPECT_EINVAL:
            satisfied = result == -1 && call_errno == EINVAL;
            break;
        case EXPECT_EACCES:
            satisfied = result == -1 && call_errno == EACCES;
            break;
        }
        if (!satisfied)
            return checks[index].name;
    }
    return NULL;
}

#ifndef DRM_RENDER_GATE_SELF_TEST
static _Noreturn void hold_forever(void)
{
    for (;;) {
        if (pause() < 0 && errno == EINTR)
            continue;
    }
}
#endif

#if defined(DRM_RENDER_GATE_SELF_TEST) || defined(DRM_RENDER_GATE_LIFECYCLE_TEST)

/* A fake kernel that enforces the same policy the gate checks, so a fault
 * injection has to defeat the probe's own comparison rather than its
 * assumptions about a particular node. */
struct fake_context {
    enum node_mode mode;
    /* Fault: permit a call the render node must refuse. */
    int allow_refused;
    /* Fault: refuse a call the render node must keep. */
    int refuse_allowed;
    /* Fault: refuse on the card node a call it must permit. This is what the
     * card control exists to catch, so the fake has to be able to produce it. */
    int refuse_on_card;
};

static int fake_ioctl(void *opaque, unsigned long request, void *argument)
{
    struct fake_context *context = opaque;
    int render_allowed = request == DRM_IOCTL_VERSION ||
                         request == DRM_IOCTL_GET_CAP ||
                         request == DRM_IOCTL_GEM_CLOSE ||
                         request == DRM_IOCTL_PRIME_HANDLE_TO_FD ||
                         request == DRM_IOCTL_VIRTGPU_GETPARAM ||
                         request == DRM_IOCTL_VIRTGPU_GET_CAPS ||
                         request == DRM_IOCTL_VIRTGPU_CONTEXT_INIT;

    if (render_allowed) {
        if (context->refuse_allowed) {
            errno = EACCES;
            return -1;
        }
        if (request == DRM_IOCTL_VERSION) {
            struct drm_version *version = argument;
            /* The spelling is the kernel's, not a label: Mesa's loader matches
             * the DRM version name against "virtio_gpu" with strcmp. */
            snprintf(version->name, version->name_len, "virtio_gpu");
            return 0;
        }
        if (request == DRM_IOCTL_GET_CAP) {
            struct drm_get_cap *cap = argument;
            cap->value = 1;
            return 0;
        }
        /* Permitted, then failing on the merits: no such handle. */
        errno = EINVAL;
        return -1;
    }

    if (context->mode == NODE_CARD) {
        if (context->refuse_on_card) {
            errno = EACCES;
            return -1;
        }
        return 0;
    }
    if (context->allow_refused)
        return 0;
    errno = EACCES;
    return -1;
}

#ifdef DRM_RENDER_GATE_SELF_TEST
static int self_test(const char *case_name)
{
    struct fake_context context = {.mode = NODE_RENDER};
    if (strcmp(case_name, "render") == 0) {
        reset_arguments();
        if (run_checks(fake_ioctl, &context, ALLOWED,
                       sizeof(ALLOWED) / sizeof(ALLOWED[0]), NODE_RENDER) != NULL)
            return 1;
        reset_arguments();
        if (run_checks(fake_ioctl, &context, REFUSED,
                       sizeof(REFUSED) / sizeof(REFUSED[0]), NODE_RENDER) != NULL)
            return 1;
    } else if (strcmp(case_name, "card") == 0) {
        context.mode = NODE_CARD;
        reset_arguments();
        if (run_checks(fake_ioctl, &context, REFUSED,
                       sizeof(REFUSED) / sizeof(REFUSED[0]), NODE_CARD) != NULL)
            return 1;
    } else if (strcmp(case_name, "allow-refused") == 0) {
        /* A node that wrongly permits what it must refuse has to be caught. */
        context.allow_refused = 1;
        reset_arguments();
        if (run_checks(fake_ioctl, &context, REFUSED,
                       sizeof(REFUSED) / sizeof(REFUSED[0]), NODE_RENDER) == NULL)
            return 1;
    } else if (strcmp(case_name, "refuse-allowed") == 0) {
        /* A node that wrongly refuses what it must keep has to be caught. */
        context.refuse_allowed = 1;
        reset_arguments();
        if (run_checks(fake_ioctl, &context, ALLOWED,
                       sizeof(ALLOWED) / sizeof(ALLOWED[0]), NODE_RENDER) == NULL)
            return 1;
    } else if (strcmp(case_name, "card-refuses") == 0) {
        /* The control must notice a node that refuses what it must permit,
         * otherwise it would pass against any kernel at all. */
        context.mode = NODE_CARD;
        context.refuse_on_card = 1;
        reset_arguments();
        if (run_checks(fake_ioctl, &context, REFUSED,
                       sizeof(REFUSED) / sizeof(REFUSED[0]), NODE_CARD) == NULL)
            return 1;
    } else {
        return 2;
    }

    printf("DRM_RENDER_SELF_TEST PASS case=%s\n", case_name);
    return 0;
}

int main(int argc, char **argv)
{
    if (argc != 2)
        return 2;
    return self_test(argv[1]);
}
#else
/* Turns a failed check into the marker the host gate classifies. The real
 * build reports through `fail_and_hold` instead, which has the errno. */
static int report(const char *stage, const char *failed)
{
    if (failed != NULL) {
        printf("DRM_RENDER_FAIL stage=%s detail=%s\n", stage, failed);
        fflush(stdout);
        return 1;
    }
    return 0;
}

int main(void)
{
    struct fake_context context = {.mode = NODE_RENDER};

    reset_arguments();
    if (report("allowed", run_checks(fake_ioctl, &context, ALLOWED,
                                     sizeof(ALLOWED) / sizeof(ALLOWED[0]),
                                     NODE_RENDER)))
        return 1;
    publish_marker("DRM_RENDER_ALLOWED PASS");

    reset_arguments();
    if (report("refused", run_checks(fake_ioctl, &context, REFUSED,
                                     sizeof(REFUSED) / sizeof(REFUSED[0]),
                                     NODE_RENDER)))
        return 1;
    publish_marker("DRM_RENDER_REFUSED PASS");

    context.mode = NODE_CARD;
    reset_arguments();
    if (report("card", run_checks(fake_ioctl, &context, REFUSED,
                                  sizeof(REFUSED) / sizeof(REFUSED[0]),
                                  NODE_CARD)))
        return 1;
    publish_marker("DRM_RENDER_CARD PASS");

    publish_marker("ASTERINAS_DRM_RENDER_R1_READY");
    hold_forever();
}
#endif

#else

static int real_ioctl(void *opaque, unsigned long request, void *argument)
{
    int fd = *(int *)opaque;
    return ioctl(fd, request, argument);
}

static _Noreturn void fail_and_hold(const char *stage, const char *detail)
{
    printf("DRM_RENDER_FAIL stage=%s detail=%s errno=%d\n", stage, detail, errno);
    fflush(stdout);
    hold_forever();
}

static _Noreturn void fail_open(const char *stage)
{
    printf("DRM_RENDER_FAIL stage=%s errno=%d\n", stage, errno);
    fflush(stdout);
    hold_forever();
}

int main(void)
{
    int render = open("/dev/dri/renderD128", O_RDWR | O_CLOEXEC);
    if (render < 0)
        fail_open("open-renderD128");

    reset_arguments();
    const char *failed = run_checks(real_ioctl, &render, ALLOWED,
                                    sizeof(ALLOWED) / sizeof(ALLOWED[0]),
                                    NODE_RENDER);
    if (failed != NULL)
        fail_and_hold("allowed", failed);
    publish_marker("DRM_RENDER_ALLOWED PASS");

    reset_arguments();
    failed = run_checks(real_ioctl, &render, REFUSED,
                        sizeof(REFUSED) / sizeof(REFUSED[0]), NODE_RENDER);
    if (failed != NULL)
        fail_and_hold("refused", failed);
    publish_marker("DRM_RENDER_REFUSED PASS");

    close(render);

    /* The control: the same calls must survive on the card node. A kernel that
     * refused them on every node would otherwise look correct. */
    int card = open("/dev/dri/card0", O_RDWR | O_CLOEXEC);
    if (card < 0)
        fail_open("open-card0");
    reset_arguments();
    failed = run_checks(real_ioctl, &card, REFUSED,
                        sizeof(REFUSED) / sizeof(REFUSED[0]), NODE_CARD);
    if (failed != NULL)
        fail_and_hold("card", failed);
    publish_marker("DRM_RENDER_CARD PASS");

    close(card);
    publish_marker("ASTERINAS_DRM_RENDER_R1_READY");
    hold_forever();
}

#endif
