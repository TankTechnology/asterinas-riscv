// SPDX-License-Identifier: MPL-2.0
// Read-only inspection of anonymous pages: correct behavior is all zeros.
// Any non-zero word means a shared/read-only mapping resolved to a frame
// that someone else has written, i.e. the page was recycled underneath.
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

int main(void) {
    size_t len = 16UL * 1024 * 1024;
    unsigned char *p = mmap(NULL, len, PROT_READ | PROT_WRITE,
                            MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (p == MAP_FAILED) {
        printf("ZERO_PROBE mmap failed\n");
        return 1;
    }
    long nonzero = 0;
    for (size_t off = 0; off < len; off += 4096) {
        unsigned long v;
        memcpy(&v, p + off, sizeof(v));
        if (v != 0) {
            nonzero++;
            if (nonzero <= 4)
                printf("ZERO_PROBE page@%#zx = %#lx\n", off, v);
        }
    }
    printf("ZERO_PROBE nonzero_pages=%ld total=%zu\n", nonzero, len / 4096);
    fflush(stdout);
    for (;;)
        pause();
}
