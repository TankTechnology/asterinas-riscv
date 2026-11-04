// SPDX-License-Identifier: MPL-2.0
// Where a pipe's lifetime is spent: creation (the 64 KiB ring buffer's
// allocation), closing the reader, and closing the writer (the last close
// releases the ring buffer). Complements syscall_bench_board.c, which reports
// the whole create+close as one number.

#include <stdio.h>
#include <time.h>
#include <unistd.h>

static long long ns(struct timespec *a, struct timespec *b)
{
	return (b->tv_sec - a->tv_sec) * 1000000000LL + (b->tv_nsec - a->tv_nsec);
}

int main(void)
{
	enum { N = 200 };
	static int fds[200][2];
	struct timespec a, b;
	int i, n = 0;

	clock_gettime(CLOCK_MONOTONIC, &a);
	for (i = 0; i < N; i++) {
		if (pipe(fds[i]) < 0)
			break;
		n++;
	}
	clock_gettime(CLOCK_MONOTONIC, &b);
	printf("PBENCH create %.1f ns/op n=%d\n", (double)ns(&a, &b) / n, n);

	clock_gettime(CLOCK_MONOTONIC, &a);
	for (i = 0; i < n; i++)
		close(fds[i][0]);
	clock_gettime(CLOCK_MONOTONIC, &b);
	printf("PBENCH close1 %.1f ns/op\n", (double)ns(&a, &b) / n);

	clock_gettime(CLOCK_MONOTONIC, &a);
	for (i = 0; i < n; i++)
		close(fds[i][1]);
	clock_gettime(CLOCK_MONOTONIC, &b);
	printf("PBENCH close2 %.1f ns/op\n", (double)ns(&a, &b) / n);
	return 0;
}
