// SPDX-License-Identifier: MPL-2.0
// Does a futex wake actually wake a thread that is already sleeping?
//
// The ping-pong benchmark hangs; this reduces it to one waiter and one wake,
// with print statements on both sides, so a lost wakeup is unambiguous.

#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#ifndef FUTEX_WAIT
#define FUTEX_WAIT 0
#endif
#ifndef FUTEX_WAKE
#define FUTEX_WAKE 1
#endif

static int word;
static atomic_int woken;
static atomic_long wake_ns;

static long long now_ns(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (long long)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

static void *waiter(void *arg)
{
	(void)arg;
	printf("WAITER: about to wait (word=%d)\n", word);
	long rc = syscall(SYS_futex, &word, FUTEX_WAIT, 0, NULL, NULL, 0);
	long long observed = now_ns();
	printf("WAITER: futex_wait returned %ld errno=%d\n", rc, rc < 0 ? errno : 0);
	atomic_store(&wake_ns, observed);
	atomic_store(&woken, 1);
	return NULL;
}

int main(void)
{
	pthread_t t;
	struct timespec ts = { .tv_sec = 0, .tv_nsec = 200 * 1000 * 1000 };

	setvbuf(stdout, NULL, _IONBF, 0);
	printf("FW_BEGIN\n");

	pthread_create(&t, NULL, waiter, NULL);
	usleep(500 * 1000); /* let the waiter really sleep */

	printf("MAIN: waking (word -> 1)\n");
	word = 1;
	long long woken_at = now_ns();
	long n = syscall(SYS_futex, &word, FUTEX_WAKE, 1, NULL, NULL, 0);
	printf("MAIN: futex_wake returned %ld\n", n);

	/* Wait up to two seconds for the waiter to report in. */
	for (int i = 0; i < 20 && !atomic_load(&woken); i++) {
		nanosleep(&ts, NULL);
	}

	if (atomic_load(&woken))
		printf("FW_RESULT woken wake_to_run_ns=%lld\n",
		       atomic_load(&wake_ns) - woken_at);
	else
		printf("FW_RESULT LOST_WAKEUP\n");

	fflush(stdout);
	_exit(atomic_load(&woken) ? 0 : 1);
}
