// SPDX-License-Identifier: MPL-2.0
#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

/* Reproduce page-table contention without a browser or a root filesystem. */
#define REQUIRE(expr)                                                          \
  do {                                                                         \
    if (!(expr)) {                                                             \
      fprintf(stderr, "TLB_PROBE_FAIL line=%d expr=%s errno=%d\n", __LINE__,   \
              #expr, errno);                                                   \
      exit(EXIT_FAILURE);                                                      \
    }                                                                          \
  } while (0)

enum { THREADS = 4, PAGE_SIZE = 4096, PAGES = 4096, MPROTECT_ROUNDS = 12 };

static volatile uint8_t *mapping;
static pthread_barrier_t barrier;
static volatile uint64_t read_checksums[THREADS];
static atomic_bool readers_stop;

static double now_seconds(void) {
  struct timespec t;
  REQUIRE(clock_gettime(CLOCK_MONOTONIC, &t) == 0);
  return t.tv_sec + t.tv_nsec / 1e9;
}

static void *write_cow_pages(void *arg) {
  uintptr_t tid = (uintptr_t)arg;
  int barrier_result = pthread_barrier_wait(&barrier);
  REQUIRE(barrier_result == 0 ||
          barrier_result == PTHREAD_BARRIER_SERIAL_THREAD);
  for (size_t page = tid; page < PAGES; page += THREADS)
    mapping[page * PAGE_SIZE] = (uint8_t)(page + 1);
  return NULL;
}

static void *read_during_mprotect(void *arg) {
  uintptr_t tid = (uintptr_t)arg;
  uint64_t sum = 0;
  int barrier_result = pthread_barrier_wait(&barrier);
  REQUIRE(barrier_result == 0 ||
          barrier_result == PTHREAD_BARRIER_SERIAL_THREAD);
  while (!atomic_load_explicit(&readers_stop, memory_order_acquire))
    for (size_t page = tid; page < PAGES; page += THREADS)
      sum += mapping[page * PAGE_SIZE];
  read_checksums[tid] = sum;
  return NULL;
}

static void run_threads(void *(*fn)(void *), const char *phase) {
  pthread_t threads[THREADS];
  REQUIRE(pthread_barrier_init(&barrier, NULL, THREADS + 1) == 0);
  for (uintptr_t tid = 0; tid < THREADS; ++tid)
    REQUIRE(pthread_create(&threads[tid], NULL, fn, (void *)tid) == 0);
  int barrier_result = pthread_barrier_wait(&barrier);
  REQUIRE(barrier_result == 0 ||
          barrier_result == PTHREAD_BARRIER_SERIAL_THREAD);
  for (size_t tid = 0; tid < THREADS; ++tid)
    REQUIRE(pthread_join(threads[tid], NULL) == 0);
  REQUIRE(pthread_barrier_destroy(&barrier) == 0);
  printf("TLB_PROBE phase=%s done\n", phase);
  fflush(stdout);
}

int main(void) {
  REQUIRE(sysconf(_SC_PAGESIZE) == PAGE_SIZE);
  mapping = mmap(NULL, PAGES * PAGE_SIZE, PROT_READ | PROT_WRITE,
                 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
  REQUIRE(mapping != MAP_FAILED);
  for (size_t page = 0; page < PAGES; ++page)
    mapping[page * PAGE_SIZE] = 1;

  int child_wait_pipe[2];
  REQUIRE(pipe(child_wait_pipe) == 0);
  pid_t child = fork();
  REQUIRE(child >= 0);
  if (child == 0) {
    REQUIRE(close(child_wait_pipe[1]) == 0);
    char token;
    REQUIRE(read(child_wait_pipe[0], &token, 1) == 1 && token == 'x');
    REQUIRE(mapping[0] == 1);
    _exit(EXIT_SUCCESS);
  }
  REQUIRE(close(child_wait_pipe[0]) == 0);

  double started = now_seconds();
  run_threads(write_cow_pages, "concurrent-cow");
  printf("TLB_PROBE phase=concurrent-cow wall=%.6f\n", now_seconds() - started);
  fflush(stdout);
  REQUIRE(write(child_wait_pipe[1], "x", 1) == 1);
  REQUIRE(close(child_wait_pipe[1]) == 0);
  int status;
  REQUIRE(waitpid(child, &status, 0) == child && WIFEXITED(status) &&
          WEXITSTATUS(status) == 0);

  /* Readers keep the address space active on other CPUs during shootdowns. */
  pthread_t readers[THREADS];
  REQUIRE(pthread_barrier_init(&barrier, NULL, THREADS + 1) == 0);
  for (uintptr_t tid = 0; tid < THREADS; ++tid)
    REQUIRE(pthread_create(&readers[tid], NULL, read_during_mprotect,
                           (void *)tid) == 0);
  int barrier_result = pthread_barrier_wait(&barrier);
  REQUIRE(barrier_result == 0 ||
          barrier_result == PTHREAD_BARRIER_SERIAL_THREAD);
  started = now_seconds();
  double syscall_wall = 0;
  for (size_t round = 0; round < MPROTECT_ROUNDS; ++round) {
    double call_started = now_seconds();
    REQUIRE(mprotect((void *)mapping, PAGES * PAGE_SIZE, PROT_READ) == 0);
    double read_only_wall = now_seconds() - call_started;
    call_started = now_seconds();
    REQUIRE(mprotect((void *)mapping, PAGES * PAGE_SIZE,
                     PROT_READ | PROT_WRITE) == 0);
    double read_write_wall = now_seconds() - call_started;
    syscall_wall += read_only_wall + read_write_wall;
    printf(
        "TLB_PROBE phase=mprotect round=%zu read_only=%.6f read_write=%.6f\n",
        round + 1, read_only_wall, read_write_wall);
    fflush(stdout);
  }
  printf("TLB_PROBE phase=mprotect syscalls=%d wall=%.6f\n",
         2 * MPROTECT_ROUNDS, syscall_wall);
  fflush(stdout);
  double join_started = now_seconds();
  atomic_store_explicit(&readers_stop, true, memory_order_release);
  for (size_t tid = 0; tid < THREADS; ++tid)
    REQUIRE(pthread_join(readers[tid], NULL) == 0 && read_checksums[tid] != 0);
  printf("TLB_PROBE phase=mprotect reader_join wall=%.6f\n",
         now_seconds() - join_started);
  REQUIRE(pthread_barrier_destroy(&barrier) == 0);
  printf("TLB_PROBE phase=mprotect wall=%.6f\n", now_seconds() - started);
  REQUIRE(munmap((void *)mapping, PAGES * PAGE_SIZE) == 0);
  puts("TLB_PROBE_OK");
  return EXIT_SUCCESS;
}
