// LD_PRELOAD ioctl logger for DRM fds (M19 diagnostics).
// Logs every ioctl on /dev/dri/* fds with the decoded command name.
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <unistd.h>

static int (*real_ioctl)(int, unsigned long, ...) = NULL;
static int (*real_open)(const char *, int, ...) = NULL;
static int (*real_open64)(const char *, int, ...) = NULL;
static void *(*real_dlopen)(const char *, int) = NULL;
static int is_drm[64];

static void init(void) {
    if (!real_ioctl) real_ioctl = dlsym(RTLD_NEXT, "ioctl");
    if (!real_open) real_open = dlsym(RTLD_NEXT, "open");
    if (!real_open64) real_open64 = dlsym(RTLD_NEXT, "open64");
    if (!real_dlopen) real_dlopen = dlsym(RTLD_NEXT, "dlopen");
}

/* DRM device nodes are char devices with major 226. Detecting by fstat catches
 * dup()'d fds (GBM/EGL dup the card0 fd internally), which open-tracking misses. */
static int is_drm_fd(int fd) {
    struct stat st;
    if (fstat(fd, &st) != 0) return 0;
    if (!S_ISCHR(st.st_mode)) return 0;
    return major(st.st_rdev) == 226;
}

/* Mesa loads DRI drivers via dlopen; log those paths. */
void *dlopen(const char *path, int flags) {
    init();
    if (path && (strstr(path, "_dri.so") || strstr(path, "gallium") ||
                 strstr(path, "gbm") || strstr(path, "EGL") || strstr(path, "GLES"))) {
        fprintf(stderr, "DLOPEN %s\n", path);
    }
    return real_dlopen(path, flags);
}

static const char *drm_cmd_name(unsigned long cmd) {
    switch (cmd) {
    case 0xc0406400: return "VERSION";
    case 0xc010640c: return "GET_CAP";
    case 0x4010640d: return "SET_CLIENT_CAP";
    case 0x641e: return "SET_MASTER";
    case 0x641f: return "DROP_MASTER";
    case 0xc0106441: return "VIRTGPU_MAP";
    case 0xc0406442: return "VIRTGPU_EXECBUFFER";
    case 0xc0106443: return "VIRTGPU_GETPARAM";
    case 0xc0386444: return "VIRTGPU_RESOURCE_CREATE";
    case 0xc0106445: return "VIRTGPU_RESOURCE_INFO";
    case 0xc02c6446: return "VIRTGPU_TRANSFER_FROM_HOST";
    case 0xc02c6447: return "VIRTGPU_TRANSFER_TO_HOST";
    case 0xc0086448: return "VIRTGPU_WAIT";
    case 0xc0186449: return "VIRTGPU_GET_CAPS";
    case 0xc010644b: return "VIRTGPU_CONTEXT_INIT";
    case 0xc04064a0: return "MODE_GETRESOURCES";
    case 0xc03864a1: return "MODE_GETCRTC";
    case 0xc06864a2: return "MODE_SETCRTC";
    case 0xc05064a7: return "MODE_GETCONNECTOR";
    case 0xc01c64a6: return "MODE_GETENCODER";
    case 0xc01864ae: return "MODE_ADDFB";
    case 0xc06864b8: return "MODE_ADDFB2";
    case 0xc01064b0: return "MODE_PAGE_FLIP";
    case 0xc02064b2: return "MODE_CREATE_DUMB";
    case 0xc01064b3: return "MODE_MAP_DUMB";
    case 0xc00864b4: return "MODE_DESTROY_DUMB";
    case 0xc01064b5: return "MODE_GETPLANERESOURCES";
    case 0xc02064b6: return "MODE_GETPLANE";
    case 0xc02064b9: return "MODE_OBJ_GETPROPERTIES";
    case 0xc04064aa: return "MODE_GETPROPERTY";
    case 0xc01064ac: return "MODE_GETPROPBLOB";
    case 0xc01064bd: return "MODE_CREATEPROPBLOB";
    case 0xc05064bc: return "MODE_ATOMIC";
    case 0xc0086409: return "GEM_CLOSE";
    case 0xc008640a: return "GEM_FLINK";
    case 0xc00c642d: return "PRIME_HANDLE_TO_FD";
    case 0xc00c642e: return "PRIME_FD_TO_HANDLE";
    default: return NULL;
    }
}

int ioctl(int fd, unsigned long request, ...) {
    va_list ap;
    va_start(ap, request);
    void *arg = va_arg(ap, void *);
    va_end(ap);
    init();
    int ret = real_ioctl(fd, request, arg);
    if (is_drm_fd(fd)) {
        const char *name = drm_cmd_name(request);
        if (name)
            fprintf(stderr, "IOCTL[%d] %s -> %d\n", fd, name, ret);
        else
            fprintf(stderr, "IOCTL[%d] 0x%lx -> %d\n", fd, request, ret);
    }
    return ret;
}

static int wrap_open(const char *path, int flags, mode_t mode, int has_mode) {
    init();
    int fd = has_mode ? real_open(path, flags, mode) : real_open(path, flags);
    if (path && (strstr(path, "/dev/dri/") || strstr(path, "_dri.so") ||
                 strstr(path, "gallium") || strstr(path, "/gbm/"))) {
        fprintf(stderr, "OPEN %s -> %d\n", path, fd);
    }
    if (fd >= 0 && fd < 64 && strstr(path, "/dev/dri/"))
        is_drm[fd] = 1;
    return fd;
}

int open(const char *path, int flags, ...) {
    mode_t mode = 0;
    int has_mode = (flags & (O_CREAT | O_TMPFILE)) != 0;
    if (has_mode) {
        va_list ap;
        va_start(ap, flags);
        mode = va_arg(ap, mode_t);
        va_end(ap);
    }
    return wrap_open(path, flags, mode, has_mode);
}

int open64(const char *path, int flags, ...) {
    mode_t mode = 0;
    int has_mode = (flags & (O_CREAT | O_TMPFILE)) != 0;
    if (has_mode) {
        va_list ap;
        va_start(ap, flags);
        mode = va_arg(ap, mode_t);
        va_end(ap);
    }
    return wrap_open(path, flags, mode, has_mode);
}

/* glibc opens files via openat; log interesting paths (dri drivers, devices). */
static int (*real_openat)(int, const char *, int, ...) = NULL;

int openat(int dirfd, const char *path, int flags, ...) {
    mode_t mode = 0;
    int has_mode = (flags & (O_CREAT | O_TMPFILE)) != 0;
    if (has_mode) {
        va_list ap;
        va_start(ap, flags);
        mode = va_arg(ap, mode_t);
        va_end(ap);
    }
    init();
    if (!real_openat) real_openat = dlsym(RTLD_NEXT, "openat");
    int fd = has_mode ? real_openat(dirfd, path, flags, mode)
                      : real_openat(dirfd, path, flags);
    if (path && (strstr(path, "/dev/dri/") || strstr(path, "_dri.so") ||
                 strstr(path, "gallium") || strstr(path, "/gbm/"))) {
        fprintf(stderr, "OPEN %s -> %d\n", path, fd);
    }
    if (fd >= 0 && fd < 64 && strstr(path, "/dev/dri/"))
        is_drm[fd] = 1;
    return fd;
}

int close(int fd) {
    if (fd >= 0 && fd < 64) is_drm[fd] = 0;
    init();
    int (*real_close)(int) = dlsym(RTLD_NEXT, "close");
    return real_close(fd);
}

/* Log blocking poll/ppoll so we can see what the render path waits on. */
static int (*real_poll)(struct pollfd *, nfds_t, int) = NULL;
static int (*real_ppoll)(struct pollfd *, nfds_t, const struct timespec *,
                         const sigset_t *) = NULL;

static void log_poll(const char *which, struct pollfd *fds, nfds_t nfds, int timeout) {
    char buf[512];
    int off = snprintf(buf, sizeof(buf), "%s nfds=%d timeout=%d",
                       which, (int)nfds, timeout);
    for (nfds_t i = 0; i < nfds && off < (int)sizeof(buf) - 48; i++) {
        off += snprintf(buf + off, sizeof(buf) - off, " [fd=%d ev=0x%x]",
                        fds[i].fd, fds[i].events);
    }
    fprintf(stderr, "%s\n", buf);
}

int poll(struct pollfd *fds, nfds_t nfds, int timeout) {
    init();
    if (!real_poll) real_poll = dlsym(RTLD_NEXT, "poll");
    log_poll("POLL", fds, nfds, timeout);
    return real_poll(fds, nfds, timeout);
}

int ppoll(struct pollfd *fds, nfds_t nfds, const struct timespec *timeout_ts,
          const sigset_t *sigmask) {
    init();
    if (!real_ppoll) real_ppoll = dlsym(RTLD_NEXT, "ppoll");
    log_poll("PPOLL", fds, nfds, timeout_ts ? -2 : -1);
    return real_ppoll(fds, nfds, timeout_ts, sigmask);
}
