// SPDX-License-Identifier: MPL-2.0
//
// Minimal reproduction of the file-IO sequence fcitx5-pinyin uses for its
// history file. Every step prints its result so the first failing one can
// be identified in the guest transcript.

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#define PATH "/root/.local/share/fcitx5/pinyin/history"

static void report(const char *step, long ret) {
    printf("IOTEST %-16s ret=%ld errno=%d%s%s%s\n", step, ret, ret < 0 ? errno : 0,
           ret < 0 ? " (" : "", ret < 0 ? strerrorname_np(errno) : "",
           ret < 0 ? ")" : "");
}

int main(void) {
    mkdir("/root/.local", 0755);
    mkdir("/root/.local/share", 0755);
    mkdir("/root/.local/share/fcitx5", 0755);
    mkdir("/root/.local/share/fcitx5/pinyin", 0755);

    int fd = open(PATH, O_RDWR | O_CREAT, 0600);
    report("open_rw_create", fd);
    if (fd < 0) {
        return 1;
    }

    struct stat st;
    report("fstat", fstat(fd, &st));
    char buf[64];
    report("read", read(fd, buf, sizeof(buf)));
    report("ftruncate", ftruncate(fd, 4096));

    char *map = mmap(NULL, 4096, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    report("mmap", map == MAP_FAILED ? -1 : 0);
    if (map != MAP_FAILED) {
        map[0] = 'x';
        report("msync", msync(map, 4096, MS_SYNC));
        report("munmap", munmap(map, 4096));
    }
    report("fdatasync", fdatasync(fd));
    report("fsync", fsync(fd));
    report("close", close(fd));

    fd = open(PATH, O_RDONLY);
    report("open_rd", fd);
    if (fd >= 0) {
        report("read_back", read(fd, buf, 8));
        close(fd);
    }

    // The C++ iostreams used by fcitx5 sit on stdio-like streams.
    FILE *stream = fopen(PATH, "r+");
    report("fopen_r+", stream == NULL ? -1 : 0);
    if (stream != NULL) {
        report("fseek", fseek(stream, 0, SEEK_END));
        report("fwrite", fwrite("y", 1, 1, stream));
        report("fflush", fflush(stream));
        fclose(stream);
    }

    printf("IOTEST_DONE\n");
    return 0;
}
