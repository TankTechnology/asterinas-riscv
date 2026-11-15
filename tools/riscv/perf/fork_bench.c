// SPDX-License-Identifier: MPL-2.0
// Where `fork` spends its time.
//
// `fork+wait` on Megrez costs milliseconds; this splits the cost into task
// setup (measured by creating a thread, which shares the address space) and
// address-space cloning (measured by forking after touching increasing amounts
// of memory), plus `vfork` as the no-copy control.

#define _GNU_SOURCE
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static long long ns(struct timespec *a, struct timespec *b)
{
	return (b->tv_sec - a->tv_sec) * 1000000000LL + (b->tv_nsec - a->tv_nsec);
}

static void *thread_body(void *arg)
{
	return arg;
}

static void bench_thread_create(void)
{
	struct timespec a, b;
	enum { N = 2000 };
	int i;

	clock_gettime(CLOCK_MONOTONIC, &a);
	for (i = 0; i < N; i++) {
		pthread_t t;
		pthread_create(&t, NULL, thread_body, NULL);
		pthread_join(t, NULL);
	}
	clock_gettime(CLOCK_MONOTONIC, &b);
	printf("FBENCH thread create+join %.1f ns/op\n", (double)ns(&a, &b) / N);
}

static void bench_fork_small(void)
{
	struct timespec a, b;
	enum { N = 500 };
	int i;

	clock_gettime(CLOCK_MONOTONIC, &a);
	for (i = 0; i < N; i++) {
		pid_t pid = fork();
		if (pid == 0)
			_exit(0);
		if (pid > 0)
			waitpid(pid, NULL, 0);
	}
	clock_gettime(CLOCK_MONOTONIC, &b);
	printf("FBENCH fork+wait (no touched memory) %.1f ns/op\n", (double)ns(&a, &b) / N);
}

static void bench_fork_touched(size_t mb)
{
	size_t bytes = mb << 20;
	struct timespec a, b;
	enum { N = 300 };
	int i;
	char *p;

	p = mmap(NULL, bytes, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (p == MAP_FAILED) {
		perror("mmap");
		return;
	}
	for (size_t off = 0; off < bytes; off += 4096)
		p[off] = 1;

	clock_gettime(CLOCK_MONOTONIC, &a);
	for (i = 0; i < N; i++) {
		pid_t pid = fork();
		if (pid == 0)
			_exit(0);
		if (pid > 0)
			waitpid(pid, NULL, 0);
	}
	clock_gettime(CLOCK_MONOTONIC, &b);
	printf("FBENCH fork+wait (%zu MB touched) %.1f ns/op\n", mb, (double)ns(&a, &b) / N);
	munmap(p, bytes);
}

static void bench_vfork(void)
{
	struct timespec a, b;
	enum { N = 500 };
	int i;

	clock_gettime(CLOCK_MONOTONIC, &a);
	for (i = 0; i < N; i++) {
		pid_t pid = vfork();
		if (pid == 0)
			_exit(0);
		if (pid > 0)
			waitpid(pid, NULL, 0);
	}
	clock_gettime(CLOCK_MONOTONIC, &b);
	printf("FBENCH vfork+wait %.1f ns/op\n", (double)ns(&a, &b) / N);
}

int main(void)
{
	setvbuf(stdout, NULL, _IONBF, 0);
	printf("FBENCH_BEGIN\n");
	bench_thread_create();
	bench_fork_small();
	bench_fork_touched(16);
	bench_fork_touched(64);
	bench_vfork();
	printf("FBENCH_END\n");
	return 0;
}
