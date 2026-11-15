// SPDX-License-Identifier: MPL-2.0
//
// brk/sbrk semantics probe. The LTP tests that cover this (shmt09,
// mmapstress02) go through musl's sbrk(), which is a stub that always fails
// for a nonzero increment, so drive the brk syscall directly and check the
// semantics the kernel must provide:
//
//  * growth returns the requested break, including an unaligned request;
//  * shrinking back to an earlier break succeeds;
//  * a break below the heap start is rejected by returning the old break;
//  * a mapping gap above the break is honored: the break can stop below it,
//    but cannot be moved across it;
//  * a SysV shared-memory attachment just above the break (shmt09's case)
//    blocks the break the same way.
#define _GNU_SOURCE
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ipc.h>
#include <sys/mman.h>
#include <sys/shm.h>
#include <sys/syscall.h>
#include <unistd.h>

static int failures;

static long brk_sys(long addr) {
    errno = 0;
    return syscall(SYS_brk, (unsigned long)addr);
}

static void check(const char *what, long want, long got) {
    int ok = want == got;
    printf("BRK_PROBE %-32s want=%#lx got=%#lx %s\n", what, (unsigned long)want,
           (unsigned long)got, ok ? "ok" : "FAIL");
    fflush(stdout);
    if (!ok)
        failures++;
}

int main(void) {
    long page = sysconf(_SC_PAGESIZE);
    long start = brk_sys(0);
    printf("BRK_PROBE heap_start=%#lx page=%ld\n", (unsigned long)start, page);
    fflush(stdout);

    long grown = start + 4 * page;
    check("grow", grown, brk_sys(grown));
    check("grow-unaligned", grown + 320, brk_sys(grown + 320));
    check("shrink-to-grown", grown, brk_sys(grown));
    check("shrink-to-start", start, brk_sys(start));
    check("below-start-rejected", start, brk_sys(start - 1));
    check("below-start-unaligned", start, brk_sys(start - 2 * page));

    /* A gap above the break: reserve [start+8p, start+12p) with mmap, then the
     * break may move up to start+8p but not into or past the mapping. */
    long gap = start + 8 * page;
    void *reserved = mmap((void *)gap, 4 * page, PROT_READ | PROT_WRITE,
                          MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED_NOREPLACE, -1, 0);
    int gap_ok = reserved == (void *)gap;
    printf("BRK_PROBE mmap-gap                %s\n", gap_ok ? "ok" : "FAIL");
    fflush(stdout);
    if (!gap_ok)
        failures++;

    check("grow-until-gap", gap, brk_sys(gap));
    check("grow-into-gap-rejected", gap, brk_sys(gap + page));
    check("shrink-from-gap", start + page, brk_sys(start + page));
    munmap(reserved, 4 * page);

    /* shmt09's case: attach a shared segment at the aligned address just past
     * the break, then the break must not move across it. */
    int shmid = shmget(IPC_PRIVATE, 10 * 16, IPC_CREAT | 0666);
    if (shmid >= 0) {
        long attach = (brk_sys(0) + page - 1) & ~(page - 1);
        void *addr = shmat(shmid, (void *)attach, SHM_RND);
        int attach_ok = addr != (void *)-1;
        printf("BRK_PROBE shmat-above-break       %s (%p)\n",
               attach_ok ? "ok" : "FAIL", addr);
        fflush(stdout);
        if (!attach_ok)
            failures++;
        else {
            long current = brk_sys(0);
            check("grow-across-shm-rejected", current, brk_sys(current + page));
            shmdt(addr);
            check("grow-after-detach", current + page, brk_sys(current + page));
        }
        shmctl(shmid, IPC_RMID, NULL);
    } else {
        printf("BRK_PROBE shmget                  FAIL errno=%d\n", errno);
        failures++;
    }

    printf("BRK_PROBE failures=%d\n", failures);
    fflush(stdout);
    for (;;)
        pause();
}
