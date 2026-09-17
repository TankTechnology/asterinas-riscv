// DRM-M7 `/init` — persistent-storage smoke test.
//
// Mounts the second virtio-blk disk (/dev/vdb, ext2) at /home and proves data
// survives a reboot:
//
//   boot 1 (no sentinel)  -> mounts /dev/vdb, writes /home/PERSISTED, sync, reports
//                            `__M7_PERSIST__=WROTE`.
//   boot 2 (same disk)    -> mounts /dev/vdb, sees /home/PERSISTED, reports
//                            `__M7_PERSIST__=SURVIVED`.
//
// The ext2 disk is populated once (boot 1) and must be byte-identical on boot 2.
// Static glibc, run as PID 1.
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <unistd.h>

static void say(const char *s) { (void)write(1, s, strlen(s)); }

static int write_file(const char *path, const char *content) {
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0)
        return -1;
    ssize_t n = write(fd, content, strlen(content));
    (void)close(fd);
    return n == (ssize_t)strlen(content) ? 0 : -1;
}

static int read_file(const char *path, char *buf, size_t cap) {
    int fd = open(path, O_RDONLY);
    if (fd < 0)
        return -1;
    ssize_t n = read(fd, buf, cap - 1);
    (void)close(fd);
    if (n < 0)
        return -1;
    buf[n] = '\0';
    return 0;
}

int main(void) {
    int fd = open("/dev/console", O_RDWR);
    if (fd < 0)
        fd = open("/dev/ttyS0", O_RDWR);
    if (fd >= 0) {
        (void)dup2(fd, 0);
        (void)dup2(fd, 1);
        (void)dup2(fd, 2);
        if (fd > 2)
            (void)close(fd);
    }

    say(">>> M7 init: persistent storage on /dev/vdb (ext2) <<<\n");

    if (stat("/dev/vdb", &(struct stat){0}) != 0) {
        say("__M7_PERSIST__=NODEV (missing /dev/vdb)\n");
        return 1;
    }

    (void)mkdir("/home", 0755);
    if (mount("/dev/vdb", "/home", "ext2", 0, NULL) != 0) {
        printf("__M7_PERSIST__=MOUNT_FAIL errno=%d (%s)\n", errno, strerror(errno));
        return 1;
    }
    say("__M7_MOUNT__=OK (ext2 /dev/vdb on /home)\n");

    char buf[128];
    if (read_file("/home/PERSISTED", buf, sizeof buf) == 0) {
        printf("__M7_PERSIST__=SURVIVED content=%s\n", buf);
    } else {
        /* First boot: populate the disk. */
        const char *marker = "m7-persisted";
        if (write_file("/home/PERSISTED", marker) != 0) {
            say("__M7_PERSIST__=WRITE_FAIL\n");
            return 1;
        }
        (void)sync();
        say("__M7_PERSIST__=WROTE\n");
    }

    say(">>> M7 init done <<<\n");
    return 0;
}
