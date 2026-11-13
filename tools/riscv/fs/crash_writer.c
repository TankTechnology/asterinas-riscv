// SPDX-License-Identifier: MPL-2.0
//
// Appends a deterministic record stream to a mounted filesystem and fsyncs in
// batches, printing CRASH_SYNC <seq> after every durable batch. The host kills
// QEMU at an arbitrary point and verifies that every fsynced record survived
// intact and that nothing earlier was corrupted.
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <unistd.h>

#define RECORD_BYTES 64
#define RECORDS_PER_SYNC 8
#define MAGIC 0x4153545245434431ULL /* "ASTRECD1" */

struct record {
    uint64_t magic;
    uint64_t seq;
    uint64_t payload[6];
};

static uint64_t payload_word(uint64_t seq, int index) {
    return seq * 0x9e3779b97f4a7c15ULL + (uint64_t)index * 0xc2b2ae3d27d4eb4fULL;
}

static int write_all(int fd, const void *buf, size_t len) {
    const char *p = buf;
    while (len > 0) {
        ssize_t written = write(fd, p, len);
        if (written < 0) {
            if (errno == EINTR)
                continue;
            return -1;
        }
        p += written;
        len -= (size_t)written;
    }
    return 0;
}

// Churn mode: create, write, truncate and unlink without ever calling fsync.
//
// The point is to leave freed and newly allocated blocks in the window between
// the inode table and the allocation bitmap, the state in which a volume
// without a journal can end up with an inode pointing at a block the bitmap
// calls free. The host kills QEMU at an arbitrary point and checks that the
// image has no such dangling reference, whatever else the crash left behind.
static int churn(void) {
    static char buf[65536];
    memset(buf, 0x5a, sizeof buf);

    printf("CHURN_READY\n");
    fflush(stdout);

    // Keep a small ring of live files so that every sync has both fresh
    // allocations (a live inode referencing blocks the previous bitmap called
    // free) and fresh frees. Deleting each file in the same round it is
    // created would leave nothing for the sync to publish.
    enum { RING = 8 };
    for (unsigned iteration = 1;; iteration++) {
        char path[64];
        snprintf(path, sizeof path, "/mnt/churn-%u", (iteration - 1) % RING);
        unlink(path);
        int fd = open(path, O_CREAT | O_RDWR | O_TRUNC, 0644);
        if (fd < 0) {
            printf("CHURN_FAIL open errno=%d\n", errno);
            return 1;
        }
        if (write_all(fd, buf, sizeof buf) != 0)
            return 1;
        close(fd);

        if (iteration > RING) {
            snprintf(path, sizeof path, "/mnt/churn-%u", (iteration - 1 - RING) % RING);
            unlink(path);
        }

        // Announce the round, then start a filesystem-wide sync. The gate kills
        // QEMU while that sync is in flight, which is where the metadata write
        // order decides whether the crash can leave a dangling reference.
        printf("CHURN %u\n", iteration);
        fflush(stdout);
        int mount_point = open("/mnt", O_RDONLY | O_DIRECTORY);
        if (mount_point >= 0) {
            syncfs(mount_point);
            close(mount_point);
        }
    }
}

int main(int argc, char **argv) {
    const char *types[] = {"ext4", "ext2"};
    int mounted = 0;
    int churn_mode = argc > 1 && strcmp(argv[1], "churn") == 0;
    for (size_t i = 0; i < sizeof(types) / sizeof(types[0]); i++) {
        if (mount("/dev/vda", "/mnt", types[i], 0, NULL) == 0) {
            mounted = 1;
            printf("CRASH_WRITER mounted=%s\n", types[i]);
            break;
        }
        printf("CRASH_WRITER mount %s failed errno=%d\n", types[i], errno);
    }
    if (!mounted) {
        printf("CRASH_WRITER mount failed\n");
        return 1;
    }

    if (churn_mode)
        return churn();

    int fd = open("/mnt/data", O_CREAT | O_RDWR | O_TRUNC | O_APPEND, 0644);
    if (fd < 0) {
        printf("CRASH_WRITER open failed errno=%d\n", errno);
        return 1;
    }
    (void)fsync(fd);
    int dirfd = open("/mnt", O_RDONLY | O_DIRECTORY);
    if (dirfd >= 0) {
        (void)fsync(dirfd);
        close(dirfd);
    }
    printf("CRASH_READY\n");
    fflush(stdout);

    uint64_t seq = 0;
    for (;;) {
        struct record record;
        record.magic = MAGIC;
        record.seq = ++seq;
        for (int i = 0; i < 6; i++)
            record.payload[i] = payload_word(seq, i);
        if (write_all(fd, &record, sizeof(record)) != 0) {
            printf("CRASH_WRITER write failed errno=%d seq=%llu\n", errno,
                   (unsigned long long)seq);
            return 1;
        }
        if (seq % RECORDS_PER_SYNC == 0) {
            if (fsync(fd) != 0) {
                printf("CRASH_WRITER fsync failed errno=%d seq=%llu\n", errno,
                       (unsigned long long)seq);
                return 1;
            }
            printf("CRASH_SYNC %llu\n", (unsigned long long)seq);
            fflush(stdout);
        }
    }
}
