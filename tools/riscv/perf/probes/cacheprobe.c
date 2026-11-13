// Does the page cache keep what was just written? Write a 64 MiB file, then
// read it back three times; a warm cache makes reads 2 and 3 far faster.
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/time.h>
#include <unistd.h>

#define BYTES (64u << 20)
static char buf[1u << 20];

static double now_s(void) {
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return tv.tv_sec + tv.tv_usec / 1e6;
}

int main(void) {
    const char *path = "/tmp/cacheprobe.bin";
    int fd = open(path, O_CREAT | O_TRUNC | O_RDWR, 0600);
    if (fd < 0) { perror("open"); return 1; }
    memset(buf, 0x5a, sizeof buf);
    for (unsigned off = 0; off < BYTES; off += sizeof buf)
        if (write(fd, buf, sizeof buf) != sizeof buf) { perror("write"); return 1; }
    fsync(fd);

    for (int pass = 1; pass <= 3; pass++) {
        if (lseek(fd, 0, SEEK_SET) != 0) { perror("lseek"); return 1; }
        unsigned long total = 0;
        double t = now_s();
        for (;;) {
            ssize_t n = read(fd, buf, sizeof buf);
            if (n <= 0) break;
            total += n;
        }
        printf("read_pass%d=%.0f MiB/s\n", pass, total / (1u << 20) / (now_s() - t));
    }
    close(fd);
    unlink(path);
    return 0;
}
