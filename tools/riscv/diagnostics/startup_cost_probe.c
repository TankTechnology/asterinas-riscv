// SPDX-License-Identifier: MPL-2.0
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

/* Keep checks and their system calls active even with -DNDEBUG. */
#define REQUIRE(condition)                                                     \
  do {                                                                         \
    if (!(condition)) {                                                        \
      fprintf(stderr, "STARTUP_COST_FAIL line=%d condition=%s errno=%d\n",     \
              __LINE__, #condition, errno);                                    \
      exit(EXIT_FAILURE);                                                      \
    }                                                                          \
  } while (0)

static double wall, cpu, child_cpu;

static double seconds(clockid_t id) {
  struct timespec t;
  REQUIRE(!clock_gettime(id, &t));
  return t.tv_sec + t.tv_nsec / 1e9;
}
static double child_seconds(void) {
  struct rusage usage;
  REQUIRE(!getrusage(RUSAGE_CHILDREN, &usage));
  return usage.ru_utime.tv_sec + usage.ru_stime.tv_sec +
         (usage.ru_utime.tv_usec + usage.ru_stime.tv_usec) / 1e6;
}

static void begin(void) {
  child_cpu = child_seconds();
  wall = seconds(CLOCK_MONOTONIC);
  cpu = seconds(CLOCK_PROCESS_CPUTIME_ID);
}

static void end(const char *phase) {
  double c = seconds(CLOCK_PROCESS_CPUTIME_ID) - cpu;
  double w = seconds(CLOCK_MONOTONIC) - wall;
  printf("STARTUP_COST phase=%s wall=%.6f cpu_self=%.6f cpu_children=%.6f\n",
         phase, w, c, child_seconds() - child_cpu);
  fflush(stdout);
}

int main(void) {
  const size_t size = 32 * 1024 * 1024;
  REQUIRE(sysconf(_SC_PAGESIZE) == 4096);
  volatile unsigned char *p;
  begin();
  p = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1,
           0);
  REQUIRE(p != MAP_FAILED);
  for (size_t i = 0; i < size; i += 4096)
    p[i] = 42;
  end("anon-first-write-32MiB");

  begin();
  for (size_t i = 0; i < size; i += 4096)
    p[i] = 43;
  end("anon-warm-write-32MiB");

  char path[] = "/tmp/startup-cost.XXXXXX";
  int fd = mkstemp(path);
  REQUIRE(fd >= 0);
  REQUIRE(!unlink(path));
  begin();
  for (size_t i = 0; i < size; i += 65536)
    REQUIRE(write(fd, (const void *)(p + i), 65536) == 65536);
  end("file-write-32MiB");

  begin();
  for (size_t i = 0; i < size; i += 65536)
    REQUIRE(pread(fd, (void *)(p + i), 65536, i) == 65536);
  end("file-warm-read-32MiB");

  volatile unsigned char *q;
  uint64_t sum = 0;
  begin();
  q = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_PRIVATE, fd, 0);
  REQUIRE(q != MAP_FAILED);
  for (size_t i = 0; i < size; i += 4096)
    sum += q[i];
  end("file-first-map-read-32MiB");
  REQUIRE(sum == (size / 4096) * 43);

  begin();
  for (size_t i = 0; i < size; i += 4096)
    q[i] = 44;
  end("file-private-cow-32MiB");
  /* Private COW must not change the underlying file. */
  unsigned char original;
  REQUIRE(pread(fd, &original, 1, 0) == 1 && original == 43);

  begin();
  for (int i = 0; i < 3; i++) {
    REQUIRE(!mprotect((void *)q, size, PROT_READ));
    REQUIRE(!mprotect((void *)q, size, PROT_READ | PROT_WRITE));
  }
  end("mprotect-32MiB-x6");

  begin();
  for (int i = 0; i < 3; i++) {
    pid_t child = fork();
    REQUIRE(child >= 0);
    if (!child) {
      for (size_t j = 0; j < size; j += 4096)
        p[j] = 45;
      _exit(0);
    }
    int s;
    REQUIRE(waitpid(child, &s, 0) == child && WIFEXITED(s) && !WEXITSTATUS(s));
    REQUIRE(p[0] == 43);
  }
  end("fork-cow-32MiB-x3");

  REQUIRE(!munmap((void *)q, size));
  REQUIRE(!munmap((void *)p, size));
  REQUIRE(!close(fd));
  puts("STARTUP_COST_OK");
}
