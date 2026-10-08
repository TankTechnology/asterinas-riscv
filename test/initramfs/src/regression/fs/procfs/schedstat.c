// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#include "../../common/test.h"

struct schedstat {
	uint64_t runtime_ns;
	uint64_t run_delay_ns;
	uint64_t dispatches;
};

struct worker_context {
	atomic_bool ready;
	atomic_bool stop;
};

static int read_schedstat(const char *path, struct schedstat *value)
{
	char actual[256];
	char expected[256];
	char newline = '\0';
	ssize_t length = 0;
	int fd;
	int parsed;
	int used = -1;

	fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0) {
		fprintf(stderr, "open %s: %s\n", path, strerror(errno));
		return -1;
	}

	while ((size_t)length < sizeof(actual) - 1) {
		ssize_t count = read(fd, actual + length,
				     sizeof(actual) - 1 - (size_t)length);
		if (count < 0) {
			int saved_errno = errno;
			close(fd);
			errno = saved_errno;
			return -1;
		}
		if (count == 0)
			break;
		length += count;
	}
	close(fd);
	actual[length] = '\0';

	parsed = sscanf(actual, "%" SCNu64 " %" SCNu64 " %" SCNu64 "%c%n",
			&value->runtime_ns, &value->run_delay_ns,
			&value->dispatches, &newline, &used);
	if (parsed != 4 || newline != '\n' || used != length) {
		errno = EPROTO;
		return -1;
	}

	int expected_length = snprintf(expected, sizeof(expected),
				       "%" PRIu64 " %" PRIu64 " %" PRIu64 "\n",
				       value->runtime_ns, value->run_delay_ns,
				       value->dispatches);
	if (expected_length < 0 || expected_length != length ||
	    memcmp(actual, expected, (size_t)length) != 0) {
		errno = EPROTO;
		return -1;
	}

	return 0;
}

static bool nondecreasing(const struct schedstat *before,
			  const struct schedstat *after)
{
	return after->runtime_ns >= before->runtime_ns &&
	       after->run_delay_ns >= before->run_delay_ns &&
	       after->dispatches >= before->dispatches;
}

static uint64_t monotonic_ns(void)
{
	struct timespec now;

	if (clock_gettime(CLOCK_MONOTONIC, &now) < 0)
		return 0;
	return (uint64_t)now.tv_sec * 1000000000ULL + (uint64_t)now.tv_nsec;
}

static void *busy_worker(void *arg)
{
	struct worker_context *context = arg;
	uint64_t value = 1;

	atomic_store_explicit(&context->ready, true, memory_order_release);
	while (!atomic_load_explicit(&context->stop, memory_order_relaxed)) {
		value = value * 6364136223846793005ULL + 1;
		__asm__ volatile("" : "+r"(value) : : "memory");
	}
	return (void *)(uintptr_t)(value & 1);
}

static int first_available_cpu(const cpu_set_t *set)
{
	for (int cpu = 0; cpu < CPU_SETSIZE; cpu++) {
		if (CPU_ISSET(cpu, set))
			return cpu;
	}
	return -1;
}

FN_TEST(linux_compatible_schedstat)
{
	const uint64_t competition_ns = 250000000ULL;
	const uint64_t sleeping_ns = 100000000ULL;
	const uint64_t sleeping_wait_limit_ns = 75000000ULL;
	struct schedstat leader;
	struct schedstat task;
	struct schedstat before_competition;
	struct schedstat after_competition;
	struct schedstat before_sleep;
	struct schedstat after_sleep;
	struct worker_context worker = {
		.ready = ATOMIC_VAR_INIT(false),
		.stop = ATOMIC_VAR_INIT(false),
	};
	cpu_set_t original_set;
	cpu_set_t one_cpu_set;
	char task_path[128];
	pthread_t worker_thread;
	struct timespec sleep_time = {
		.tv_sec = 0,
		.tv_nsec = (long)sleeping_ns,
	};
	pid_t tid = (pid_t)syscall(SYS_gettid);
	int cpu;
	int rc;
	bool affinity_changed = false;
	bool worker_started = false;

	rc = read_schedstat("/proc/self/schedstat", &leader);
	TEST_RES(rc, _ret == 0);
	if (rc < 0)
		goto out;

	TEST_RES(snprintf(task_path, sizeof(task_path),
			  "/proc/self/task/%d/schedstat", tid),
		 _ret > 0 && (size_t)_ret < sizeof(task_path));
	rc = read_schedstat(task_path, &task);
	TEST_RES(rc, _ret == 0 && nondecreasing(&leader, &task));
	if (rc < 0)
		goto out;

	TEST(open("/proc/self/schedstat", O_WRONLY | O_CLOEXEC), EACCES,
	     _ret < 0);

	rc = sched_getaffinity(0, sizeof(original_set), &original_set);
	TEST_RES(rc, _ret == 0);
	if (rc < 0)
		goto out;
	cpu = first_available_cpu(&original_set);
	TEST_RES(cpu, _ret >= 0);
	if (cpu < 0)
		goto out;
	CPU_ZERO(&one_cpu_set);
	CPU_SET(cpu, &one_cpu_set);
	rc = sched_setaffinity(0, sizeof(one_cpu_set), &one_cpu_set);
	TEST_RES(rc, _ret == 0);
	if (rc < 0)
		goto out;
	affinity_changed = true;
	rc = pthread_create(&worker_thread, NULL, busy_worker, &worker);
	TEST_RES(rc, _ret == 0);
	if (rc != 0)
		goto restore_affinity;
	worker_started = true;
	while (!atomic_load_explicit(&worker.ready, memory_order_acquire))
		sched_yield();

	rc = read_schedstat(task_path, &before_competition);
	TEST_RES(rc, _ret == 0);
	if (rc < 0)
		goto stop_worker;

	uint64_t start = monotonic_ns();
	int yield_error = 0;
	TEST_RES(start, _ret > 0);
	if (start == 0)
		goto stop_worker;
	uint64_t deadline = start + competition_ns;
	while (monotonic_ns() < deadline) {
		if (sched_yield() < 0) {
			yield_error = errno;
			break;
		}
	}
	TEST_RES(yield_error, _ret == 0);

	rc = read_schedstat(task_path, &after_competition);
	TEST_RES(rc,
		 _ret == 0 && nondecreasing(&before_competition,
					    _ret == 0 ? &after_competition :
							&before_competition));
	if (rc == 0) {
		TEST_RES(after_competition.runtime_ns,
			 _ret > before_competition.runtime_ns);
		TEST_RES(after_competition.run_delay_ns,
			 _ret > before_competition.run_delay_ns);
		TEST_RES(after_competition.dispatches,
			 _ret > before_competition.dispatches);
	}

stop_worker:
	if (worker_started) {
		atomic_store_explicit(&worker.stop, true, memory_order_relaxed);
		TEST_SUCC(pthread_join(worker_thread, NULL));
		worker_started = false;
	}
restore_affinity:
	if (affinity_changed) {
		TEST_SUCC(sched_setaffinity(0, sizeof(original_set),
					    &original_set));
		affinity_changed = false;
	}

	rc = read_schedstat(task_path, &before_sleep);
	TEST_RES(rc, _ret == 0);
	if (rc < 0)
		goto out;
	TEST_SUCC(nanosleep(&sleep_time, NULL));
	rc = read_schedstat(task_path, &after_sleep);
	TEST_RES(rc, _ret == 0 && nondecreasing(&before_sleep, &after_sleep));
	if (rc == 0) {
		TEST_RES(after_sleep.run_delay_ns - before_sleep.run_delay_ns,
			 _ret < sleeping_wait_limit_ns);
	}

out:
	if (worker_started) {
		atomic_store_explicit(&worker.stop, true, memory_order_relaxed);
		TEST_SUCC(pthread_join(worker_thread, NULL));
	}
	if (affinity_changed)
		TEST_SUCC(sched_setaffinity(0, sizeof(original_set),
					    &original_set));
	if (__tests_failed == 0)
		puts("SCHEDSTAT_TEST PASS");
}
END_TEST()
