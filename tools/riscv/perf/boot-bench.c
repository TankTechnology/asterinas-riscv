// SPDX-License-Identifier: MPL-2.0
//
// In-guest microbenchmark for the DRM desktop comparison.
//
// Each line measures one operation in its own loop, so a number can be
// attributed to a mechanism rather than inferred from whole-boot timings.
//
// Two of the loops deliberately never enter the kernel:
//
//   alu        A dependent integer chain, so the work cannot be folded away.
//              Both kernels run under the same QEMU/TCG with the same userspace
//              binaries, so this should match on both.  It is here as a control:
//              if it does not match, the harness is comparing different
//              emulation and no kernel-side conclusion is safe.
//   vdso       clock_gettime(CLOCK_MONOTONIC).  glibc serves this from the vDSO
//              when the kernel provides one, and falls back to a real syscall
//              when it does not.  A large gap here between the two kernels is
//              not a slow syscall, it is a missing vDSO, and it changes how
//              every timestamp in the boot is paid for.
//
// The remaining loops are the kernel operations the desktop boot is made of.
// `fstat` against an already-open descriptor and `stat` against a path are the
// same operation differing only in whether a path has to be walked, which is
// what separates path-resolution cost from syscall-entry cost.
#define _GNU_SOURCE
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static double now(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

int main(int argc, char **argv) {
  int n = argc > 1 ? atoi(argv[1]) : 200;
  char *const av[] = {"/bin/true", NULL};
  char *const ev[] = {NULL};
  double t0, t1;

  // Userspace control: no syscall, no trap, no kernel.  Sized to take a
  // comparable slice of time to the syscall loops so the two are legible side
  // by side.
  {
    volatile unsigned long sink = 1;
    unsigned long acc = 1;
    t0 = now();
    for (long i = 0; i < (long)n * 20000; ++i) acc = acc * 6364136223846793005UL + 1;
    t1 = now();
    sink = acc;
    if (sink == 0) return 1;
    printf("BENCH alu n=%ld total=%.3fs per=%.4fns\n", (long)n * 20000, t1 - t0,
           (t1 - t0) * 1e9 / ((double)n * 20000));
  }

  // vDSO control: a kernel call that may be served entirely in userspace.
  {
    struct timespec ts;
    volatile long sink = 0;
    t0 = now();
    for (int i = 0; i < n * 2000; ++i) {
      clock_gettime(CLOCK_MONOTONIC, &ts);
      sink += ts.tv_nsec;
    }
    t1 = now();
    if (sink == 0) return 1;
    printf("BENCH clock_gettime n=%d total=%.3fs per=%.3fus\n", n * 2000, t1 - t0,
           (t1 - t0) * 1e6 / (n * 2000));
  }

  t0 = now();
  for (int i = 0; i < n; ++i) {
    pid_t p = fork();
    if (p == 0) { execve("/bin/true", av, ev); _exit(127); }
    int st; waitpid(p, &st, 0);
  }
  t1 = now();
  printf("BENCH fork+exec+wait n=%d total=%.3fs per=%.3fms\n", n, t1 - t0,
         (t1 - t0) * 1000.0 / n);

  t0 = now();
  volatile pid_t sink = 0;
  for (int i = 0; i < n * 200; ++i) sink = getpid();
  t1 = now();
  if (sink == (pid_t)-1) return 1;
  printf("BENCH getpid n=%d total=%.3fs per=%.3fus\n", n * 200, t1 - t0,
         (t1 - t0) * 1e6 / (n * 200));

  t0 = now();
  for (int i = 0; i < n; ++i) { int fd = open("/etc/hostname", O_RDONLY); if (fd >= 0) close(fd); }
  t1 = now();
  printf("BENCH open+close n=%d total=%.3fs per=%.3fms\n", n, t1 - t0,
         (t1 - t0) * 1000.0 / n);

  t0 = now();
  for (int i = 0; i < n * 10; ++i) { char b[64]; (void)!readlink("/proc/self/exe", b, sizeof b); }
  t1 = now();
  printf("BENCH readlink n=%d total=%.3fs per=%.3fus\n", n * 10, t1 - t0,
         (t1 - t0) * 1e6 / (n * 10));
  t0 = now();
  for (int i = 0; i < n; ++i) {
    struct timespec req = {0, 100000};   /* 100us */
    nanosleep(&req, NULL);
  }
  t1 = now();
  printf("BENCH nanosleep100us n=%d total=%.3fs per=%.3fms\n", n, t1 - t0,
         (t1 - t0) * 1000.0 / n);
  /* path resolution vs an already-open fd: the same operation, differing only
     in whether a path has to be walked to find the inode. */
  {
    struct stat st;
    int fd = open("/etc/hostname", O_RDONLY);
    t0 = now();
    for (int i = 0; i < n * 10; ++i) (void)fstat(fd, &st);
    t1 = now();
    printf("BENCH fstat fd n=%d total=%.3fs per=%.3fus\n", n * 10, t1 - t0,
           (t1 - t0) * 1e6 / (n * 10));
    if (fd >= 0) close(fd);
    t0 = now();
    for (int i = 0; i < n * 10; ++i) (void)stat("/etc/hostname", &st);
    t1 = now();
    printf("BENCH stat path n=%d total=%.3fs per=%.3fus\n", n * 10, t1 - t0,
           (t1 - t0) * 1e6 / (n * 10));
  }
  return 0;
}
