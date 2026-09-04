// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#include "../../common/test.h"

#define CPUCLOCK_SCHED 2
#define CPUCLOCK_PERTHREAD_MASK 4

static volatile sig_atomic_t timer_fired;

static void handle_timer(int signo)
{
	timer_fired = signo;
}

static clockid_t process_cpuclock(pid_t pid)
{
	return (~(clockid_t)pid << 3) | CPUCLOCK_SCHED;
}

static clockid_t thread_cpuclock(pid_t tid)
{
	return (~(clockid_t)tid << 3) | CPUCLOCK_SCHED |
	       CPUCLOCK_PERTHREAD_MASK;
}

static clockid_t fd_clockid(int fd)
{
	return (~(clockid_t)fd << 3) | 3;
}

static int raw_timer_create(clockid_t clockid, const struct sigevent *event,
			    int *timerid)
{
	return syscall(SYS_timer_create, clockid, event, timerid);
}

static int raw_timer_delete(int timerid)
{
	return syscall(SYS_timer_delete, timerid);
}

FN_TEST(dynamic_sched_clocks)
{
	struct timespec resolution;
	struct timespec value;
	clockid_t process_clock = process_cpuclock(getpid());
	clockid_t thread_clock = thread_cpuclock(syscall(SYS_gettid));

	TEST_SUCC(syscall(SYS_clock_gettime, process_clock, &value));
	TEST_SUCC(syscall(SYS_clock_gettime, thread_clock, &value));
	TEST_SUCC(syscall(SYS_clock_getres, process_clock, &resolution));
	TEST_RES(resolution.tv_sec > 0 || resolution.tv_nsec > 0, _ret);
}
END_TEST()

FN_TEST(dynamic_sched_timers)
{
	struct sigevent event = { .sigev_notify = SIGEV_NONE };
	clockid_t clocks[] = {
		process_cpuclock(getpid()),
		thread_cpuclock(syscall(SYS_gettid)),
	};

	for (size_t i = 0; i < sizeof(clocks) / sizeof(clocks[0]); i++) {
		int timerid = -1;

		if (TEST_SUCC(raw_timer_create(clocks[i], &event, &timerid)) ==
		    0)
			TEST_SUCC(raw_timer_delete(timerid));
	}
}
END_TEST()

FN_TEST(dynamic_sched_timer_expires)
{
	struct sigaction action = { .sa_handler = handle_timer };
	struct sigevent event = {
		.sigev_notify = SIGEV_SIGNAL,
		.sigev_signo = SIGUSR1,
	};
	struct itimerspec setting = {
		.it_value = { .tv_nsec = 20 * 1000 * 1000 },
	};
	struct timespec start;
	struct timespec now;
	int timerid = -1;

	timer_fired = 0;
	CHECK(sigemptyset(&action.sa_mask));
	TEST_SUCC(sigaction(SIGUSR1, &action, NULL));
	if (TEST_SUCC(raw_timer_create(process_cpuclock(getpid()), &event,
				       &timerid)) != 0)
		goto out;
	TEST_SUCC(syscall(SYS_timer_settime, timerid, 0, &setting, NULL));
	CHECK(clock_gettime(CLOCK_MONOTONIC, &start));
	do {
		CHECK(clock_gettime(CLOCK_MONOTONIC, &now));
	} while (!timer_fired && now.tv_sec - start.tv_sec < 5);
	TEST_RES(timer_fired, _ret == SIGUSR1);

out:
	if (timerid >= 0)
		TEST_SUCC(raw_timer_delete(timerid));
}
END_TEST()

FN_TEST(non_clock_fd_is_rejected)
{
	struct sigevent event = { .sigev_notify = SIGEV_NONE };
	struct timespec value;
	int fd = CHECK(open("/dev/null", O_RDONLY));
	int timerid = -1;
	clockid_t clockid = fd_clockid(fd);

	TEST_ERRNO(syscall(SYS_clock_gettime, clockid, &value), EINVAL);
	TEST_ERRNO(raw_timer_create(clockid, &event, &timerid), EOPNOTSUPP);
	CHECK(close(fd));
}
END_TEST()

FN_TEST(raw_sigev_thread_is_accepted)
{
	struct sigevent event = {
		.sigev_notify = SIGEV_THREAD,
		.sigev_signo = SIGRTMIN,
	};
	int timerid = -1;

	if (TEST_SUCC(raw_timer_create(CLOCK_MONOTONIC, &event, &timerid)) == 0)
		TEST_SUCC(raw_timer_delete(timerid));
}
END_TEST()
