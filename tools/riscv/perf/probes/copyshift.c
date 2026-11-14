// SPDX-License-Identifier: MPL-2.0
//
// Measures the kernel's user-copy path from every byte offset.
//
// The fallible copy behind `read()`/`write()` is word-wise only when both
// pointers share an alignment, so the throughput of one 16 MiB buffered read
// depends on where inside the destination buffer it lands -- which is not
// something a single aligned measurement can show. This probe reads the same
// file eight times, once per offset, verifies every byte, and prints the rate.
//
// Expected on the Megrez board, 2026-11-14:
//   offset 0     ~202 MiB/s   (jointly aligned, word-wise loop)
//   offset 1..7  ~116 MiB/s   (byte-wise fallback)
// A misaligned load or store traps on that core, so anything that trades the
// byte loop for misaligned accesses shows up here as a collapse to ~7 MiB/s.
//
// Build and run as init in QEMU:
//   riscv64-linux-gnu-gcc -O2 -static -o copyshift copyshift.c
//   printf '.\ninit\n' | cpio -o -H newc > probe.cpio   (with copyshift as ./init)
//   qemu-system-riscv64 -M virt -m 2G -smp 1 -nographic \
//       -kernel <kernel image> -initrd probe.cpio \
//       -append "console=ttyS0 loglevel=info init=/init"
// `init=/init` is required: without it the kernel looks for the default init
// path in the initramfs, does not find it, and panics on a negative dentry.
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <unistd.h>

#define BYTES (16u << 20)

static unsigned char pattern(size_t i) { return (unsigned char)(i * 31u + (i >> 8)); }

static double now_s(void)
{
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return tv.tv_sec + tv.tv_usec / 1e6;
}

int main(void)
{
    mkdir("/tmp", 0777);
    const size_t chunk = 1u << 20;
    unsigned char *wbuf = malloc(chunk + 8), *rbuf = malloc(chunk + 16);
    if (!wbuf || !rbuf) {
        printf("COPYFAIL malloc\n");
        return 1;
    }
    for (size_t i = 0; i < chunk; i++)
        wbuf[i] = pattern(i);

    int fd = open("/tmp/copyshift.bin", O_CREAT | O_TRUNC | O_RDWR, 0600);
    if (fd < 0) {
        printf("COPYFAIL open\n");
        return 1;
    }
    for (unsigned long off = 0; off < BYTES; off += chunk)
        if (write(fd, wbuf, chunk) != (ssize_t)chunk) {
            printf("COPYFAIL write\n");
            return 1;
        }
    fsync(fd);

    int bad_total = 0;
    for (int shift = 0; shift < 8; shift++) {
        unsigned char *dst = rbuf + shift;
        lseek(fd, 0, SEEK_SET);
        unsigned long got = 0, bad = 0;
        double t = now_s();
        for (;;) {
            ssize_t n = read(fd, dst, chunk);
            if (n <= 0)
                break;
            /* Verify each chunk as it arrives: the buffer holds one chunk,
               not the whole file. */
            for (ssize_t k = 0; k < n; k++)
                if (dst[k] != pattern(((unsigned long)got + (unsigned long)k) % chunk))
                    bad++;
            got += (unsigned long)n;
        }
        double el = now_s() - t;
        printf("read_shift%d=%4.0f MiB/s bad=%lu\n", shift, got / (1u << 20) / el, bad);
        fflush(stdout);
        bad_total += (int)bad;
        if (got != BYTES)
            bad_total++;
    }
    close(fd);
    unlink("/tmp/copyshift.bin");
    printf("COPYVERIFY %s\n", bad_total ? "FAILED" : "OK");
    fflush(stdout);
    return bad_total ? 1 : 0;
}
