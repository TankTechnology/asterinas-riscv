// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE

#include <inttypes.h>
#include <stdint.h>
#include <sys/auxv.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include "../../common/test.h"

struct task_ticks {
	uint64_t user;
	uint64_t system;
	uint64_t start;
};

static double clock_seconds(clockid_t clock)
{
	struct timespec ts;
	CHECK(clock_gettime(clock, &ts));
	return ts.tv_sec + ts.tv_nsec / 1e9;
}

static struct task_ticks read_ticks(const char *path)
{
	char line[4096];
	FILE *file = CHECK_WITH(fopen(path, "r"), _ret != NULL);
	CHECK_WITH(fgets(line, sizeof(line), file), _ret != NULL);
	CHECK(fclose(file));
	char *end = CHECK_WITH(strrchr(line, ')'), _ret != NULL);
	char *save;
	char *field = strtok_r(end + 1, " ", &save);
	struct task_ticks ticks = { 0 };
	for (int number = 3; number <= 22; number++) {
		CHECK_WITH(field, _ret != NULL);
		if (number == 14)
			ticks.user = strtoull(field, NULL, 10);
		if (number == 15)
			ticks.system = strtoull(field, NULL, 10);
		if (number == 22)
			ticks.start = strtoull(field, NULL, 10);
		field = strtok_r(NULL, " ", &save);
	}
	return ticks;
}

FN_TEST(clock_tick_frequency)
{
	TEST_RES(sysconf(_SC_CLK_TCK), _ret == 100);
	TEST_RES(getauxval(AT_CLKTCK), _ret == 100);
}
END_TEST()

static double idle_seconds(void)
{
	FILE *file = CHECK_WITH(fopen("/proc/uptime", "r"), _ret != NULL);
	double uptime, idle;
	CHECK_WITH(fscanf(file, "%lf %lf", &uptime, &idle), _ret == 2);
	CHECK(fclose(file));
	return idle;
}

FN_TEST(proc_system_ticks_match_uptime)
{
	double before = idle_seconds();
	FILE *file = CHECK_WITH(fopen("/proc/stat", "r"), _ret != NULL);
	char line[512];
	uint64_t global_idle = 0, per_cpu_idle = 0;
	int cpu_count = 0, global_count = 0;
	while (fgets(line, sizeof(line), file)) {
		if (strncmp(line, "cpu", 3))
			continue;
		char label[32];
		uint64_t user, nice, system, idle;
		CHECK_WITH(sscanf(line,
				  "%31s %" SCNu64 " %" SCNu64 " %" SCNu64
				  " %" SCNu64,
				  label, &user, &nice, &system, &idle),
			   _ret == 5);
		if (!strcmp(label, "cpu")) {
			global_idle = idle;
			global_count++;
		} else {
			per_cpu_idle += idle;
			cpu_count++;
		}
	}
	CHECK(fclose(file));
	double after = idle_seconds();
	double hz = sysconf(_SC_CLK_TCK);
	printf("CLOCK_TICKS idle=%.3f per_cpu_idle=%.3f uptime_idle=[%.3f,%.3f]\n",
	       global_idle / hz, per_cpu_idle / hz, before, after);
	TEST_RES(global_count, _ret == 1);
	TEST_RES(cpu_count, _ret > 0);
	TEST_RES(global_idle / hz,
		 _ret >= before - 0.05 && _ret <= after + 0.05);
	/* Each per-CPU value is truncated independently. */
	TEST_RES(per_cpu_idle / hz, _ret >= before - cpu_count / hz - 0.05 &&
					    _ret <= after + 0.05);
}
END_TEST()

FN_TEST(proc_cpu_ticks_match_cpu_clocks)
{
	/* Make a tenfold unit error observable without a desktop workload. */
	double begin = clock_seconds(CLOCK_PROCESS_CPUTIME_ID);
	double deadline = clock_seconds(CLOCK_MONOTONIC) + 5;
	volatile unsigned long work = 1;
	while (clock_seconds(CLOCK_PROCESS_CPUTIME_ID) - begin < 0.2 &&
	       clock_seconds(CLOCK_MONOTONIC) < deadline) {
		for (int i = 0; i < 100000; i++)
			work = work * 1664525 + 1013904223;
	}
	TEST_RES(clock_seconds(CLOCK_PROCESS_CPUTIME_ID) - begin, _ret >= 0.2);

	char thread_path[128];
	CHECK_WITH(snprintf(thread_path, sizeof(thread_path),
			    "/proc/self/task/%ld/stat", syscall(SYS_gettid)),
		   _ret > 0 && _ret < sizeof(thread_path));
	const char *paths[] = { "/proc/self/stat", thread_path };
	clockid_t clocks[] = { CLOCK_PROCESS_CPUTIME_ID,
			       CLOCK_THREAD_CPUTIME_ID };
	for (int i = 0; i < 2; i++) {
		double before = clock_seconds(clocks[i]);
		struct task_ticks ticks = read_ticks(paths[i]);
		double after = clock_seconds(clocks[i]);
		double seconds = (ticks.user + ticks.system) /
				 (double)sysconf(_SC_CLK_TCK);
		printf("CLOCK_TICKS path=%s proc=%.3f clock=[%.3f,%.3f]\n",
		       paths[i], seconds, before, after);
		/* Account for independent truncation of user/system tick fields. */
		TEST_RES(seconds,
			 _ret >= before - 0.05 && _ret <= after + 0.05);
	}
}
END_TEST()

FN_TEST(proc_start_ticks_are_since_boot)
{
	int release[2];
	CHECK(pipe(release));
	double before = clock_seconds(CLOCK_BOOTTIME);
	pid_t child = CHECK(fork());
	if (child == 0) {
		close(release[1]);
		char byte;
		_exit(read(release[0], &byte, 1) == 1 ? 0 : 1);
	}
	CHECK(close(release[0]));
	char path[64];
	CHECK_WITH(snprintf(path, sizeof(path), "/proc/%d/stat", child),
		   _ret > 0 && _ret < sizeof(path));
	struct task_ticks ticks = read_ticks(path);
	double after = clock_seconds(CLOCK_BOOTTIME);
	double start = ticks.start / (double)sysconf(_SC_CLK_TCK);
	printf("CLOCK_TICKS child_start=%.3f creation=[%.3f,%.3f]\n", start,
	       before, after);
	TEST_RES(start, _ret >= before - 0.05 && _ret <= after + 0.05);
	CHECK_WITH(write(release[1], "x", 1), _ret == 1);
	CHECK(close(release[1]));
	int status;
	TEST_RES(waitpid(child, &status, 0), _ret == child &&
						     WIFEXITED(status) &&
						     WEXITSTATUS(status) == 0);
}
END_TEST()
