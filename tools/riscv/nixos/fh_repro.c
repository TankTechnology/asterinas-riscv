// SPDX-License-Identifier: MPL-2.0
//
// Round-trip probe for the cgroup file-handle syscalls
// (`name_to_handle_at` 264 / `open_by_handle_at` 265) on Asterinas RISC-V.
//
// Run as `/init` of a minimal initramfs: it mounts cgroup2, creates a child
// cgroup, obtains a file handle for it with `name_to_handle_at`, re-opens it
// with `open_by_handle_at`, and checks that the reopened fd refers to the same
// inode and can read the cgroup's `cgroup.events` attribute. Prints
// `FH_REPRO_ALL_OK` on success.
#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

// glibc's `struct file_handle` has a flexible `f_handle[]`; use a fixed backing
// buffer to avoid heap allocation in the initramfs.
static unsigned char fh_storage[sizeof(struct file_handle) + 64] __attribute__((aligned(8)));

static void open_console(void) {
    int fd = open("/dev/console", O_RDWR | O_CLOEXEC);
    if (fd < 0)
        return;
    dup2(fd, 0);
    dup2(fd, 1);
    dup2(fd, 2);
    if (fd > 2)
        close(fd);
}

int main(void) {
    open_console();

    // Mount cgroup2 at /sys/fs/cgroup.
    mkdir("/sys", 0755);
    mkdir("/sys/fs", 0755);
    mkdir("/sys/fs/cgroup", 0755);
    if (mount("cgroup2", "/sys/fs/cgroup", "cgroup2", 0, NULL) < 0) {
        printf("MOUNT_FAIL: %s\n", strerror(errno));
        return 1;
    }
    printf("MOUNT_OK\n");

    // Create a child cgroup to hand a handle for.
    if (mkdir("/sys/fs/cgroup/fhtest", 0755) < 0) {
        printf("MKDIR_FAIL: %s\n", strerror(errno));
        return 1;
    }

    struct stat before;
    if (stat("/sys/fs/cgroup/fhtest", &before) < 0) {
        printf("STAT_FAIL: %s\n", strerror(errno));
        return 1;
    }
    printf("INO_BEFORE=%llu\n", (unsigned long long)before.st_ino);

    // name_to_handle_at(AT_FDCWD, path, &fh, &mnt_id, 0).
    struct file_handle *fh = (struct file_handle *)fh_storage;
    memset(fh_storage, 0, sizeof(fh_storage));
    fh->handle_bytes = 64;
    int mnt_id = -1;
    if (name_to_handle_at(AT_FDCWD, "/sys/fs/cgroup/fhtest", fh, &mnt_id, 0) < 0) {
        printf("NAME_TO_HANDLE_FAIL: %s\n", strerror(errno));
        return 1;
    }
    printf("NAME_TO_HANDLE_OK bytes=%u type=%d mnt_id=%d\n",
           fh->handle_bytes, fh->handle_type, mnt_id);

    // open_by_handle_at(mount_fd, &fh, flags): mount_fd is any fd on the mount.
    int mount_fd = open("/sys/fs/cgroup", O_PATH | O_DIRECTORY | O_CLOEXEC);
    if (mount_fd < 0) {
        printf("MOUNTFD_FAIL: %s\n", strerror(errno));
        return 1;
    }
    int fd = open_by_handle_at(mount_fd, fh, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (fd < 0) {
        printf("OPEN_BY_HANDLE_FAIL: %s\n", strerror(errno));
        return 1;
    }
    printf("OPEN_BY_HANDLE_OK fd=%d\n", fd);

    struct stat after;
    if (fstat(fd, &after) < 0) {
        printf("FSTAT_FAIL: %s\n", strerror(errno));
        return 1;
    }
    printf("INO_AFTER=%llu\n", (unsigned long long)after.st_ino);

    if (before.st_ino != after.st_ino) {
        printf("ROUNDTRIP_MISMATCH\n");
        return 1;
    }
    printf("ROUNDTRIP_INO_MATCH\n");

    // Prove the reopened fd is functional: read `cgroup.events` through it.
    int ev = openat(fd, "cgroup.events", O_RDONLY | O_CLOEXEC);
    if (ev < 0) {
        printf("OPENAT_EVENTS_FAIL: %s\n", strerror(errno));
        return 1;
    }
    char buf[128];
    ssize_t n = read(ev, buf, sizeof(buf) - 1);
    if (n < 0) {
        printf("READ_EVENTS_FAIL: %s\n", strerror(errno));
        return 1;
    }
    buf[n] = '\0';
    printf("EVENTS_READ=%s\n", buf);
    close(ev);

    printf("FH_REPRO_ALL_OK\n");
    return 0;
}
