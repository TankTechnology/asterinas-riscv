// SPDX-License-Identifier: MPL-2.0
// Split the process/thread latency into creation, reaping, and wakeup.
//
// The first fork_bench run showed `vfork` as slow as `fork` and thread
// creation slower still, so the page tables are not the dominant cost.
// These four cases separate the pieces:
//
//   fork, no wait     creation only (children are reaped afterwards)
//   reap              waitpid on children that have already exited
//   (wakeup latency is measured by futex_wake_check.c)
//   sched_yield       the cost of giving up the CPU

#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/syscall.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#ifndef FUTEX_WAIT
#define FUTEX_WAIT 0
#endif
#ifndef FUTEX_WAKE
#define FUTEX_WAKE 1
#endif

static long long ns(struct timespec *a, struct timespec *b)
{
	return (b->tv_sec - a->tv_sec) * 1000000000LL + (b->tv_nsec - a->tv_nsec);
}

static void bench_fork_no_wait(int n)
{
	static pid_t pids[1024];
	struct timespec a, b;
	int i;

	if (n > 1024)
		n = 1024;

	clock_gettime(CLOCK_MONOTONIC, &a);
	for (i = 0; i < n; i++) {
		pids[i] = fork();
		if (pids[i] == 0)
			_exit(0);
		if (pids[i] < 0) {
			printf("CBENCH fork failed at %d: %d\n", i, errno);
			n = i;
			break;
		}
	}
	clock_gettime(CLOCK_MONOTONIC, &b);
	printf("CBENCH fork (no wait) %.1f ns/op n=%d\n", n ? (double)ns(&a, &b) / n : 0.0, n);

	clock_gettime(CLOCK_MONOTONIC, &a);
	for (i = 0; i < n; i++)
		waitpid(pids[i], NULL, 0);
	clock_gettime(CLOCK_MONOTONIC, &b);
	printf("CBENCH reap (already exited) %.1f ns/op\n", n ? (double)ns(&a, &b) / n : 0.0);
}

static void bench_sched_yield(void)
{
	enum { N = 200000 };
	struct timespec a, b;
	int i;

	clock_gettime(CLOCK_MONOTONIC, &a);
	for (i = 0; i < N; i++)
		sched_yield();
	clock_gettime(CLOCK_MONOTONIC, &b);
	printf("CBENCH sched_yield %.1f ns/op\n", (double)ns(&a, &b) / N);
}

int main(int argc, char **argv)
{
	int n = argc > 1 ? atoi(argv[1]) : 100;

	setvbuf(stdout, NULL, _IONBF, 0);
	printf("CBENCH_BEGIN n=%d\n", n);
	bench_fork_no_wait(n);
	bench_sched_yield();
	printf("CBENCH_END\n");
	return 0;
}
