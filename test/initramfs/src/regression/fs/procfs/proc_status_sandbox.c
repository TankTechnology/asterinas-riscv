// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE

#include <linux/filter.h>
#include <linux/seccomp.h>
#include <pthread.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <unistd.h>

#include "../../common/test.h"

struct worker_context {
	pthread_barrier_t ready;
	pthread_barrier_t filter_installed;
	pid_t tid;
	int seccomp_mode;
};

static int read_decimal_field(const char *path, const char *field)
{
	char *line = NULL;
	size_t capacity = 0;
	size_t field_len = strlen(field);
	int matches = 0;
	long value = -1;
	FILE *file = CHECK_WITH(fopen(path, "r"), _ret != NULL);

	while (getline(&line, &capacity, file) >= 0) {
		if (strncmp(line, field, field_len) != 0)
			continue;
		char *end;
		value = strtol(line + field_len, &end, 10);
		CHECK_WITH(end != line + field_len && (*end == '\n' || *end == '\0'),
			   _ret);
		matches++;
	}
	CHECK_WITH(feof(file), _ret != 0);
	CHECK(fclose(file));
	free(line);
	CHECK_WITH(matches, _ret == 1);
	return (int)value;
}

static int wait_barrier(pthread_barrier_t *barrier)
{
	int result = pthread_barrier_wait(barrier);
	CHECK_WITH(result,
		   _ret == 0 || _ret == PTHREAD_BARRIER_SERIAL_THREAD);
	return 0;
}

static void install_allow_filter(void)
{
	struct sock_filter instructions[] = {
		BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
	};
	struct sock_fprog program = {
		.len = sizeof(instructions) / sizeof(instructions[0]),
		.filter = instructions,
	};

	CHECK(syscall(SYS_seccomp, SECCOMP_SET_MODE_FILTER, 0, &program));
}

static void *worker_fn(void *arg)
{
	struct worker_context *context = arg;
	char path[128];

	context->tid = CHECK(syscall(SYS_gettid));
	CHECK(wait_barrier(&context->ready));
	CHECK(wait_barrier(&context->filter_installed));
	CHECK_WITH(snprintf(path, sizeof(path), "/proc/self/task/%d/status",
			    context->tid),
		   _ret > 0 && (size_t)_ret < sizeof(path));
	context->seccomp_mode = read_decimal_field(path, "Seccomp:\t");
	return NULL;
}

FN_TEST(proc_status_reports_no_new_privs_and_per_thread_seccomp)
{
	struct worker_context context = {
		.tid = -1,
		.seccomp_mode = -1,
	};
	pthread_t worker;

	TEST_RES(read_decimal_field("/proc/self/status", "NoNewPrivs:\t"),
		 _ret == 0);
	TEST_RES(read_decimal_field("/proc/self/status", "Seccomp:\t"),
		 _ret == 0);

	TEST_SUCC(pthread_barrier_init(&context.ready, NULL, 2));
	TEST_SUCC(pthread_barrier_init(&context.filter_installed, NULL, 2));
	TEST_SUCC(pthread_create(&worker, NULL, worker_fn, &context));
	TEST_SUCC(wait_barrier(&context.ready));

	TEST_SUCC(prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0));
	TEST_RES(read_decimal_field("/proc/self/status", "NoNewPrivs:\t"),
		 _ret == 1);
	TEST_RES(read_decimal_field("/proc/self/status", "Seccomp:\t"),
		 _ret == 0);

	install_allow_filter();
	TEST_RES(read_decimal_field("/proc/self/status", "NoNewPrivs:\t"),
		 _ret == 1);
	TEST_RES(read_decimal_field("/proc/self/status", "Seccomp:\t"),
		 _ret == SECCOMP_MODE_FILTER);

	TEST_SUCC(wait_barrier(&context.filter_installed));
	TEST_SUCC(pthread_join(worker, NULL));
	TEST_RES(context.seccomp_mode, _ret == SECCOMP_MODE_DISABLED);
	TEST_SUCC(pthread_barrier_destroy(&context.ready));
	TEST_SUCC(pthread_barrier_destroy(&context.filter_installed));
}
END_TEST()
