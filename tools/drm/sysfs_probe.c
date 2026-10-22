// SPDX-License-Identifier: MPL-2.0

// Acceptance probe for the DRM sysfs view.
//
// libdrm answers two questions from `/sys/dev/char/<major>:<minor>/` before it
// hands a DRM file descriptor to Mesa: which bus the device sits on
// (`device/subsystem` plus `device/uevent`) and which nodes belong to it
// (`device/drm/`). Without that view `drmGetDevice2()` fails, GBM cannot
// create a device and DRI3 never comes up, while every ioctl still works.
//
// The probe mounts sysfs (as PID 1 in a one-file initramfs), opens the card
// node, and checks the tree that libdrm 2.4 reads:
//
//   * `drmGetMinorType`        `/sys/dev/char/M:m/device/drm` lists nodes
//   * `drmParseSubsystemType`  `/sys/dev/char/M:m/device/subsystem` resolves
//   * `drmParseOFBusInfo`      `device/uevent` carries `MODALIAS`
//   * `drmGetDeviceNameFromFd2` `uevent` maps the node to `/dev/dri/<name>`
//
// Prints `SYSFSPROBE_PASS` on success.

#define _GNU_SOURCE

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <unistd.h>

#define DRM_MAJOR_FALLBACK 226
#define PATH_MAX_LEN 512

static int failures;

static void fail(const char *what, int err) {
    printf("SYSFSPROBE_FAIL %s errno=%d (%s)\n", what, err, strerror(err));
    failures++;
}

static int read_file(const char *path, char *buffer, size_t size) {
    int fd = open(path, O_RDONLY);
    if (fd < 0) {
        return -errno;
    }
    ssize_t count = read(fd, buffer, size - 1);
    close(fd);
    if (count < 0) {
        return -errno;
    }
    buffer[count] = '\0';
    return 0;
}

static int dir_has_entry(const char *path, const char *name) {
    DIR *dir = opendir(path);
    if (!dir) {
        return 0;
    }
    int found = 0;
    struct dirent *entry;
    while ((entry = readdir(dir))) {
        if (strcmp(entry->d_name, name) == 0) {
            found = 1;
            break;
        }
    }
    closedir(dir);
    return found;
}

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);

    if (getpid() == 1) {
        mkdir("/dev", 0755);
        if (mount("devtmpfs", "/dev", "devtmpfs", 0, NULL) != 0 && errno != EBUSY) {
            printf("SYSFSPROBE_FAIL mount-devtmpfs errno=%d\n", errno);
            return 1;
        }
        mkdir("/sys", 0755);
        if (mount("sysfs", "/sys", "sysfs", 0, NULL) != 0 && errno != EBUSY) {
            printf("SYSFSPROBE_FAIL mount-sysfs errno=%d\n", errno);
            return 1;
        }
    }

    // The node names carry the minor, but the major comes from the kernel.
    const char *card = "/dev/dri/card0";
    struct stat sb;
    if (stat(card, &sb) != 0) {
        fail("stat-card", errno);
        return 1;
    }
    unsigned major_number = major(sb.st_rdev);
    unsigned minor_number = minor(sb.st_rdev);
    printf("SYSFSPROBE_NODE card0=%u:%u\n", major_number, minor_number);
    if (major_number != DRM_MAJOR_FALLBACK) {
        printf("note: DRM major is %u, not %u\n", major_number, DRM_MAJOR_FALLBACK);
    }

    char path[PATH_MAX_LEN];

    struct stat render_sb;
    printf("SYSFSPROBE_RENDER_NODE %s\n",
           stat("/dev/dri/renderD128", &render_sb) == 0 ? "present" : "absent");

    // drmGetMinorType(): the device directory lists the node name.
    snprintf(path, sizeof(path), "/sys/dev/char/%u:%u/device/drm",
             major_number, minor_number);
    if (!dir_has_entry(path, "card0")) {
        fail("device-drm-card0", ENOENT);
    }
    // A render node exists only when the driver advertises one; virtio-gpu
    // without virgl exposes the card node alone, exactly as Linux does.
    if (stat("/dev/dri/renderD128", &render_sb) == 0 &&
        !dir_has_entry(path, "renderD128")) {
        fail("device-drm-renderD128", ENOENT);
    }

    // drmParseSubsystemType(): the bus is a symlink whose last component is
    // the bus name.
    snprintf(path, sizeof(path), "/sys/dev/char/%u:%u/device/subsystem",
             major_number, minor_number);
    char link[PATH_MAX_LEN];
    ssize_t link_length = readlink(path, link, sizeof(link) - 1);
    if (link_length < 0) {
        fail("device-subsystem", errno);
    } else {
        link[link_length] = '\0';
        printf("SYSFSPROBE_SUBSYSTEM %s\n", link);
        const char *base = strrchr(link, '/');
        base = base ? base + 1 : link;
        if (strcmp(base, "platform") != 0 && strcmp(base, "virtio") != 0 &&
            strcmp(base, "pci") != 0) {
            fail("subsystem-name", EINVAL);
        }
    }

    // drmParseOFBusInfo(): MODALIAS identifies the platform device.
    snprintf(path, sizeof(path), "/sys/dev/char/%u:%u/device/uevent",
             major_number, minor_number);
    char uevent[1024];
    if (read_file(path, uevent, sizeof(uevent)) != 0) {
        fail("device-uevent", errno);
    } else {
        if (!strstr(uevent, "DRIVER=")) {
            fail("uevent-driver", ENOENT);
        }
        if (!strstr(uevent, "MODALIAS=")) {
            fail("uevent-modalias", ENOENT);
        }
        printf("SYSFSPROBE_UEVENT %s", uevent);
    }

    // drmGetDeviceNameFromFd2(): DEVNAME names the node under /dev.
    snprintf(path, sizeof(path), "/sys/dev/char/%u:%u/uevent",
             major_number, minor_number);
    if (read_file(path, uevent, sizeof(uevent)) != 0) {
        fail("char-uevent", errno);
    } else if (!strstr(uevent, "DEVNAME=dri/card0")) {
        fail("char-uevent-devname", ENOENT);
    } else {
        printf("SYSFSPROBE_CHAR_UEVENT %s", uevent);
    }

    // /sys/class/drm is what a desktop tool reads to enumerate cards.
    if (!dir_has_entry("/sys/class/drm", "card0")) {
        fail("class-drm-card0", ENOENT);
    }

    if (failures != 0) {
        printf("SYSFSPROBE_FAILURES %d\n", failures);
        return 1;
    }

    printf("SYSFSPROBE_PASS\n");
    return 0;
}
