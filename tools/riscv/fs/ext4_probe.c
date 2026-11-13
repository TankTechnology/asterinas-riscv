// SPDX-License-Identifier: MPL-2.0
//
// ext4 compatibility probe. Mounts the first virtio-blk disk as ext4, then
// exercises the operations that the ext4 feature surface touches: file
// creation with data, many directory entries (dir_nlink), a larger file
// (huge_file accounting), unlink, sync and unmount. Every failure is printed
// with its errno so the host gate can tell a mount rejection from an I/O bug.
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/statfs.h>
#include <unistd.h>

#define FILES 200
#define BIG_BYTES (8 * 1024 * 1024)

static int failures;

static void fail(const char *what) {
    printf("EXT4_PROBE_FAIL %s errno=%d (%s)\n", what, errno, strerror(errno));
    fflush(stdout);
    failures++;
}

int main(int argc, char **argv) {
    const char *device = argc > 1 ? argv[1] : "/dev/vda";

    mkdir("/mnt", 0755);
    if (mount(device, "/mnt", "ext4", 0, NULL) != 0) {
        fail("mount");
        printf("EXT4_PROBE_DONE failures=%d\n", failures);
        fflush(stdout);
        for (;;)
            pause();
    }
    printf("EXT4_PROBE mounted\n");
    fflush(stdout);

    /* Data integrity on a small file. */
    int fd = open("/mnt/probe.dat", O_RDWR | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) {
        fail("create probe.dat");
    } else {
        char buf[4096];
        memset(buf, 0x5a, sizeof(buf));
        for (int i = 0; i < 64; i++) {
            if (write(fd, buf, sizeof(buf)) != (ssize_t)sizeof(buf)) {
                fail("write probe.dat");
                break;
            }
        }
        if (lseek(fd, 0, SEEK_SET) != 0)
            fail("lseek probe.dat");
        memset(buf, 0, sizeof(buf));
        if (read(fd, buf, sizeof(buf)) != (ssize_t)sizeof(buf))
            fail("read probe.dat");
        for (size_t i = 0; i < sizeof(buf); i++) {
            if ((unsigned char)buf[i] != 0x5a) {
                fail("verify probe.dat");
                break;
            }
        }
        close(fd);
    }

    /* Many directory entries in one directory. */
    for (int i = 0; i < FILES; i++) {
        char path[64];
        snprintf(path, sizeof(path), "/mnt/f%03d", i);
        int f = open(path, O_RDWR | O_CREAT | O_TRUNC, 0644);
        if (f < 0) {
            fail("create many files");
            break;
        }
        if (write(f, "x", 1) != 1)
            fail("write many files");
        close(f);
    }

    /* A file large enough to need many extents. */
    fd = open("/mnt/big.bin", O_RDWR | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) {
        fail("create big.bin");
    } else {
        char chunk[65536];
        memset(chunk, 0xa5, sizeof(chunk));
        for (int i = 0; i < BIG_BYTES / (int)sizeof(chunk); i++) {
            if (write(fd, chunk, sizeof(chunk)) != (ssize_t)sizeof(chunk)) {
                fail("write big.bin");
                break;
            }
        }
        close(fd);
        struct stat st;
        if (stat("/mnt/big.bin", &st) != 0 || st.st_size != BIG_BYTES)
            fail("stat big.bin");
    }

    /* Remove half of the files again. */
    for (int i = 0; i < FILES; i += 2) {
        char path[64];
        snprintf(path, sizeof(path), "/mnt/f%03d", i);
        if (unlink(path) != 0)
            fail("unlink many files");
    }

    struct statfs sfs;
    if (statfs("/mnt", &sfs) != 0)
        fail("statfs");
    else
        printf("EXT4_PROBE statfs blocks=%llu bfree=%llu\n",
               (unsigned long long)sfs.f_blocks, (unsigned long long)sfs.f_bfree);

    sync();
    if (umount("/mnt") != 0) {
        fail("umount");
    } else {
        printf("EXT4_PROBE unmounted\n");
    }

    printf("EXT4_PROBE_DONE failures=%d\n", failures);
    fflush(stdout);
    for (;;)
        pause();
}
