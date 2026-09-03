// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE

#include "../../common/test.h"

#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#define REALTIME_JUMP_SECONDS 3600
#define COARSE_UPDATE_WAIT_NANOSECONDS 200000000
#define MAX_MONOTONIC_DRIFT_NANOSECONDS 5000000000LL

static long long timespec_difference_ns(const struct timespec *later,
					const struct timespec *earlier)
{
	return (later->tv_sec - earlier->tv_sec) * 1000000000LL +
	       later->tv_nsec - earlier->tv_nsec;
}

static struct timespec timespec_add_ns(struct timespec value,
				       long long nanoseconds)
{
	value.tv_sec += nanoseconds / 1000000000LL;
	value.tv_nsec += nanoseconds % 1000000000LL;
	if (value.tv_nsec >= 1000000000L) {
		value.tv_sec++;
		value.tv_nsec -= 1000000000L;
	}
	return value;
}

FN_TEST(realtime_set_does_not_move_monotonic_coarse)
{
	struct timespec original_realtime;
	struct timespec monotonic_before;
	struct timespec monotonic_after;
	struct timespec jumped_realtime;
	struct timespec restored_realtime;
	struct timespec update_wait = {
		.tv_nsec = COARSE_UPDATE_WAIT_NANOSECONDS,
	};

	TEST_SUCC(
		syscall(SYS_clock_gettime, CLOCK_REALTIME, &original_realtime));
	TEST_SUCC(syscall(SYS_clock_gettime, CLOCK_MONOTONIC_COARSE,
			  &monotonic_before));

	jumped_realtime = original_realtime;
	jumped_realtime.tv_sec += REALTIME_JUMP_SECONDS;
	TEST_SUCC(syscall(SYS_clock_settime, CLOCK_REALTIME, &jumped_realtime));
	TEST_SUCC(clock_nanosleep(CLOCK_MONOTONIC, 0, &update_wait, NULL));
	TEST_RES(syscall(SYS_clock_gettime, CLOCK_MONOTONIC_COARSE,
			 &monotonic_after),
		 timespec_difference_ns(&monotonic_after, &monotonic_before) >=
				 0 &&
			 timespec_difference_ns(&monotonic_after,
						&monotonic_before) <
				 MAX_MONOTONIC_DRIFT_NANOSECONDS);

	restored_realtime = timespec_add_ns(
		original_realtime,
		timespec_difference_ns(&monotonic_after, &monotonic_before));
	TEST_SUCC(
		syscall(SYS_clock_settime, CLOCK_REALTIME, &restored_realtime));
}
END_TEST()
