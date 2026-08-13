// SPDX-License-Identifier: MPL-2.0
//
// M4 minimal repro for the clone(CLONE_THREAD|CLONE_SETTLS) + user-fault ->
// SIGSEGV kernel gaps described in m3/M3-report.md "Blocking gap".
//
// Test 1 (TLS): raw clone(CLONE_THREAD|CLONE_SETTLS) with a synthetic TCB. The
//   child reads `tp` and writes `tp[1]` (musl's DTV slot). If the kernel sets
//   the child's `tp` register to the requested TLS pointer, the write lands in
//   the mapped TCB and the test reports the real tp. If `tp` is 0 the child
//   faults at 0x8 (the M3 symptom).
//
// Test 2 (SEGV): fork(); the child writes to unmapped 0x8; the parent expects
//   it to be killed by SIGSEGV (not hang/loop).

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <sys/wait.h>
#include <sys/mman.h>

#define SYS_clone        220
#define SYS_exit         93
#define SYS_sched_yield  124

#define CLONE_VM      0x00000100
#define CLONE_SIGHAND 0x00000800
#define CLONE_THREAD  0x00010000
#define CLONE_SETTLS  0x00080000

static volatile unsigned long g_child_tp = 0;
static volatile int g_child_done = 0;   // 0=not done, 1=fail, 2=wrote+readback ok

static long raw_clone(unsigned long flags, void *stack, int *ptid, int *ctid,
                      void *tls) {
    register long a0 asm("a0") = (long)flags;
    register long a1 asm("a1") = (long)stack;
    register long a2 asm("a2") = (long)ptid;
    register long a3 asm("a3") = (long)ctid;
    register long a4 asm("a4") = (long)tls;
    register long a7 asm("a7") = SYS_clone;
    asm volatile("ecall"
                 : "+r"(a0)
                 : "r"(a1), "r"(a2), "r"(a3), "r"(a4), "r"(a7)
                 : "memory");
    return a0;
}

static void raw_exit(long code) {
    register long a0 asm("a0") = code;
    register long a7 asm("a7") = SYS_exit;
    asm volatile("ecall" : : "r"(a0), "r"(a7) : "memory");
    __builtin_unreachable();
}

static void raw_sched_yield(void) {
    register long a0 asm("a0") = 0;
    register long a7 asm("a7") = SYS_sched_yield;
    asm volatile("ecall" : : "r"(a0), "r"(a7) : "memory");
}

static unsigned long read_tp(void) {
    unsigned long tp;
    asm volatile("mv %0, tp" : "=r"(tp));
    return tp;
}

// Runs on the freshly cloned child stack. Must not touch glibc (its tp is a
// synthetic TCB, not glibc's), so it only uses raw syscalls and shared globals.
static void child_entry(void) {
    unsigned long tp = read_tp();
    g_child_tp = tp;

    volatile unsigned long *dtv = (unsigned long *)(tp + 8);
    *dtv = 0xdeadbeefcafebabeUL;
    unsigned long rd = *dtv;
    g_child_done = (rd == 0xdeadbeefcafebabeUL) ? 2 : 1;

    raw_exit(0);
}

static int test_tls(void) {
    const size_t tcb_sz = 4096;
    const size_t stack_sz = 64 * 1024;

    unsigned char *tcb = mmap(NULL, tcb_sz, PROT_READ | PROT_WRITE,
                              MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    unsigned char *stack = mmap(NULL, stack_sz, PROT_READ | PROT_WRITE,
                                MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (tcb == MAP_FAILED || stack == MAP_FAILED) {
        printf("M4 TLS: mmap failed\n");
        return 1;
    }
    memset(tcb, 0, tcb_sz);

    // On riscv64 musl/glibc, the stack arg to clone is the top of the stack.
    void *stack_top = stack + stack_sz;

    unsigned long flags = CLONE_VM | CLONE_THREAD | CLONE_SIGHAND | CLONE_SETTLS;
    g_child_tp = 0;
    g_child_done = 0;

    long r = raw_clone(flags, stack_top, NULL, NULL, tcb);
    if (r < 0) {
        printf("M4 TLS: raw_clone failed errno=%ld\n", -r);
        return 1;
    }
    if (r == 0) {
        // child
        child_entry();
    }

    // parent: bounded wait for the child.
    int done = 0;
    for (int i = 0; i < 200000; i++) {
        if (g_child_done != 0) { done = 1; break; }
        raw_sched_yield();
    }

    if (!done) {
        printf("__M4_TLS_FAIL__ child never completed (tp=%#lx)\n", g_child_tp);
        return 1;
    }
    if (g_child_done == 2 && g_child_tp == (unsigned long)tcb) {
        printf("__M4_TLS_OK__ child tp=%#lx, tp[1] write+readback ok\n",
               g_child_tp);
        return 0;
    }
    printf("__M4_TLS_FAIL__ child tp=%#lx (expected %#lx), done=%d\n",
           g_child_tp, (unsigned long)tcb, g_child_done);
    return 1;
}

static int test_segv(void) {
    pid_t pid = fork();
    if (pid < 0) {
        printf("M4 SEGV: fork failed\n");
        return 1;
    }
    if (pid == 0) {
        // child process: intentionally dereference unmapped address 0x8.
        *(volatile int *)0x8 = 1;
        _exit(99); /* should be unreachable */
    }

    int st = 0;
    waitpid(pid, &st, 0);
    if (WIFSIGNALED(st) && WTERMSIG(st) == SIGSEGV) {
        printf("__M4_SEGV_OK__ child killed by SIGSEGV\n");
        return 0;
    }
    printf("__M4_SEGV_FAIL__ status=%#x (not SIGSEGV)\n", st);
    return 1;
}

int main(void) {
    printf(">>> M4 init: clone CLONE_SETTLS + SIGSEGV repro <<<\n");

    int tls_rc = test_tls();
    int segv_rc = test_segv();

    printf("__M4_DONE__ tls=%s segv=%s\n",
           tls_rc == 0 ? "ok" : "fail",
           segv_rc == 0 ? "ok" : "fail");
    return (tls_rc == 0 && segv_rc == 0) ? 0 : 1;
}
