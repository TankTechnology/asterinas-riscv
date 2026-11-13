// Distinguish "pages not retained" from "buffered read re-issues I/O":
// mmap and touch every page twice, then read() twice.
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
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
    const char *path = "/tmp/mmapprobe.bin";
    int fd = open(path, O_CREAT | O_TRUNC | O_RDWR, 0600);
    if (fd < 0) { perror("open"); return 1; }
    memset(buf, 0x5a, sizeof buf);
    for (unsigned off = 0; off < BYTES; off += sizeof buf)
        if (write(fd, buf, sizeof buf) != sizeof buf) { perror("write"); return 1; }
    fsync(fd);

    char *p = mmap(NULL, BYTES, PROT_READ, MAP_PRIVATE, fd, 0);
    if (p == MAP_FAILED) { perror("mmap"); return 1; }
    for (int pass = 1; pass <= 2; pass++) {
        volatile unsigned long sum = 0;
        double t = now_s();
        for (unsigned off = 0; off < BYTES; off += 4096) sum += (unsigned char)p[off];
        printf("mmap_pass%d=%.0f MiB/s (sum=%lu)\n", pass,
               (BYTES / (1u << 20)) / (now_s() - t), sum & 0xf);
    }
    munmap(p, BYTES);

    for (int pass = 1; pass <= 2; pass++) {
        lseek(fd, 0, SEEK_SET);
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
