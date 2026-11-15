// SPDX-License-Identifier: MPL-2.0
//
// Reproduces the ftest08 readv layout (16 iovecs of 128 bytes over a 2048-byte
// chunk) on tmpfs, which is where the LTP suite actually fails: a read that is
// followed by more file data must return the whole chunk, never a short count.

#define _GNU_SOURCE
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/uio.h>
#include <errno.h>
#include <unistd.h>
#include <sys/wait.h>

#define IOVCNT 16
#define IOVLEN 128
#define CHUNK (IOVCNT * IOVLEN)

static char path[64] = "/ftest_probe.bin";

/* tmpfs is where the LTP suite runs; fall back to the ramfs root when this
 * probe is the init process and nothing mounted /tmp yet. */
static void choose_path(void)
{
    if (mkdir("/tmp", 0777) == 0 || errno == EEXIST) {
        strcpy(path, "/tmp/ftest_probe.bin");
    }
}

static void make_iov(struct iovec *iov, char *buf)
{
    for (int i = 0; i < IOVCNT; i++) {
        iov[i].iov_base = buf + (size_t)i * IOVLEN;
        iov[i].iov_len = IOVLEN;
    }
}


/* Multi-tester replica of ftest08: several processes interleave chunk writes
 * to one file, each reading back its own chunks with 16 iovecs of 128 bytes. */
#define TESTERS 5
#define MAX_CHUNKS 1024

static int multi_tester(int me)
{
    int fd = open(path, O_RDWR);
    if (fd < 0) {
        return 1;
    }
    int bad = 0;
    for (int pass = 0; pass < 2; pass++) {
        for (int i = 0; i < MAX_CHUNKS / TESTERS; i++) {
            off_t off = (off_t)(i * TESTERS + me) * CHUNK;
            char *buf = malloc(CHUNK);
            struct iovec iov[IOVCNT];
            make_iov(iov, buf);
            if (lseek(fd, off, SEEK_SET) < 0) {
                free(buf);
                close(fd);
                return 1;
            }
            memset(buf, 0, CHUNK);
            ssize_t got = readv(fd, iov, IOVCNT);
            if (got != 0 && got != CHUNK) {
                struct stat st;
                fstat(fd, &st);
                printf("FTEST_PROBE mt me=%d off=%ld got=%ld size=%ld\n", me,
                       (long)off, (long)got, (long)st.st_size);
                bad = 1;
            }
            memset(buf, 0x40 + me, CHUNK);
            if (got > 0 && lseek(fd, -got, SEEK_CUR) < 0) {
                free(buf);
                close(fd);
                return 1;
            }
            ssize_t put = writev(fd, iov, IOVCNT);
            if (put != CHUNK) {
                printf("FTEST_PROBE mt_write me=%d off=%ld put=%ld\n", me,
                       (long)off, (long)put);
                bad = 1;
            }
            free(buf);
            if (bad) {
                close(fd);
                return 0;
            }
        }
    }
    close(fd);
    return 0;
}

static void run_multi(void)
{
    int fd = open(path, O_RDWR | O_CREAT | O_TRUNC, 0644);
    if (fd >= 0) {
        close(fd);
    }
    for (int me = 0; me < TESTERS; me++) {
        if (fork() == 0) {
            multi_tester(me);
            _exit(0);
        }
    }
    for (int me = 0; me < TESTERS; me++) {
        wait(NULL);
    }
    printf("FTEST_PROBE multi done\n");
}

int main(void)
{
    choose_path();
    char *buf = malloc(CHUNK);
    if (!buf) {
        return 1;
    }
    struct iovec iov[IOVCNT];
    make_iov(iov, buf);

    int fd = open(path, O_RDWR | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) {
        perror("open");
        return 1;
    }

    /* Four full chunks, written with the same iovec shape the test uses. */
    for (int chunk = 0; chunk < 4; chunk++) {
        memset(buf, 0x5a + chunk, CHUNK);
        off_t off = (off_t)chunk * CHUNK;
        ssize_t written = pwritev(fd, iov, IOVCNT, off);
        printf("FTEST_PROBE pwritev chunk=%d off=%ld ret=%ld\n", chunk,
               (long)off, (long)written);
    }
    struct stat st;
    fstat(fd, &st);
    printf("FTEST_PROBE stat size=%ld\n", (long)st.st_size);

    /* Read each chunk back; every read below EOF must return CHUNK bytes. */
    for (int chunk = 0; chunk < 5; chunk++) {
        memset(buf, 0, CHUNK);
        off_t off = (off_t)chunk * CHUNK;
        ssize_t got = preadv(fd, iov, IOVCNT, off);
        printf("FTEST_PROBE preadv chunk=%d off=%ld ret=%ld first=%02x\n",
               chunk, (long)off, (long)got, (unsigned char)buf[0]);
    }

    /* Sparse read: a hole in the middle of the file still reads as CHUNK. */
    if (ftruncate(fd, (off_t)8 * CHUNK) == 0) {
        memset(buf, 0, CHUNK);
        ssize_t got = preadv(fd, iov, IOVCNT, (off_t)6 * CHUNK);
        printf("FTEST_PROBE sparse off=%ld ret=%ld first=%02x\n",
               (long)((off_t)6 * CHUNK), (long)got, (unsigned char)buf[0]);
    }

    /* And the tail of a file that ends before the iovec array is exhausted. */
    if (ftruncate(fd, (off_t)2 * CHUNK + 1920) == 0) {
        memset(buf, 0, CHUNK);
        ssize_t got = preadv(fd, iov, IOVCNT, (off_t)2 * CHUNK);
        printf("FTEST_PROBE partial off=%ld ret=%ld\n",
               (long)((off_t)2 * CHUNK), (long)got);
    }


    /* The test's actual sequence: seek, readv, then seek back by the number of
     * bytes read (SEEK_CUR with a negative offset) and writev. */
    if (ftruncate(fd, 0) == 0) {
        for (int chunk = 0; chunk < 4; chunk++) {
            off_t target = (off_t)chunk * CHUNK;
            if (lseek(fd, target, SEEK_SET) < 0) {
                printf("FTEST_PROBE seek_set fail chunk=%d\n", chunk);
                break;
            }
            memset(buf, 0, CHUNK);
            ssize_t got = readv(fd, iov, IOVCNT);
            memset(buf, 0x70 + chunk, CHUNK);
            if (got > 0 && lseek(fd, -got, SEEK_CUR) < 0) {
                printf("FTEST_PROBE seek_cur fail chunk=%d got=%ld\n", chunk,
                       (long)got);
                break;
            }
            ssize_t put = writev(fd, iov, IOVCNT);
            fstat(fd, &st);
            printf("FTEST_PROBE seq chunk=%d read=%ld pos_seek=%ld write=%ld size=%ld\n",
                   chunk, (long)got, (long)target, (long)put, (long)st.st_size);
        }
    }

    close(fd);
    run_multi();
    unlink(path);
    printf("FTEST_PROBE done\n");
    return 0;
}
