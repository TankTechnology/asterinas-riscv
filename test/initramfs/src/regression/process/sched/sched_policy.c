// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE

#include <linux/sched.h>
#include <linux/sched/types.h>
#include <sched.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

#include "../../common/test.h"

static int raw_sched_setscheduler(pid_t pid, int policy,
				  const struct sched_param *param)
{
	return syscall(SYS_sched_setscheduler, pid, policy, param);
}

static int raw_sched_getscheduler(pid_t pid)
{
	return syscall(SYS_sched_getscheduler, pid);
}

static int raw_sched_getparam(pid_t pid, struct sched_param *param)
{
	return syscall(SYS_sched_getparam, pid, param);
}

static int raw_sched_getattr(pid_t pid, struct sched_attr *attr,
			     unsigned int size, unsigned int flags)
{
	return syscall(SYS_sched_getattr, pid, attr, size, flags);
}

FN_TEST(sched_batch)
{
	struct sched_param param = { .sched_priority = 0 };
	struct sched_attr attr = { 0 };
	pid_t child;
	int status;

	TEST_SUCC(raw_sched_setscheduler(0, SCHED_BATCH, &param));
	TEST_RES(raw_sched_getscheduler(0), _ret == SCHED_BATCH);

	attr.size = sizeof(attr);
	TEST_RES(raw_sched_getattr(0, &attr, sizeof(attr), 0),
		 _ret == 0 && attr.sched_policy == SCHED_BATCH &&
			 attr.sched_nice == 0);

	child = TEST_SUCC(fork());
	if (child == 0) {
		int policy = raw_sched_getscheduler(0);
		_exit(policy == SCHED_BATCH ? EXIT_SUCCESS : EXIT_FAILURE);
	}

	TEST_RES(waitpid(child, &status, 0),
		 _ret == child && WIFEXITED(status) &&
			 WEXITSTATUS(status) == EXIT_SUCCESS);
	TEST_SUCC(raw_sched_setscheduler(0, SCHED_OTHER, &param));
}
END_TEST()

FN_TEST(sched_reset_on_fork)
{
	struct sched_param realtime_param = { .sched_priority = 10 };
	struct sched_param child_param = { 0 };
	struct sched_param normal_param = { .sched_priority = 0 };
	struct sched_attr attr = { 0 };
	pid_t child;
	int child_ready[2];
	int child_release[2];
	int status;
	char child_result = EXIT_FAILURE;
	char release = 1;

	TEST_SUCC(pipe(child_ready));
	TEST_SUCC(pipe(child_release));
	TEST_SUCC(raw_sched_setscheduler(0, SCHED_FIFO | SCHED_RESET_ON_FORK,
					 &realtime_param));
	TEST_RES(raw_sched_getscheduler(0),
		 _ret == (SCHED_FIFO | SCHED_RESET_ON_FORK));

	attr.size = sizeof(attr);
	TEST_RES(raw_sched_getattr(0, &attr, sizeof(attr), 0),
		 _ret == 0 && attr.sched_policy == SCHED_FIFO &&
			 (attr.sched_flags & SCHED_FLAG_RESET_ON_FORK));

	child = TEST_SUCC(fork());
	if (child == 0) {
		close(child_ready[0]);
		close(child_release[1]);

		int policy = raw_sched_getscheduler(0);
		int param_result = raw_sched_getparam(0, &child_param);
		child_result = (policy == SCHED_OTHER && param_result == 0 &&
				child_param.sched_priority == 0) ?
				       EXIT_SUCCESS :
				       EXIT_FAILURE;
		if (write(child_ready[1], &child_result, 1) != 1)
			_exit(EXIT_FAILURE);
		if (read(child_release[0], &release, 1) != 1)
			_exit(EXIT_FAILURE);
		_exit(child_result);
	}

	close(child_ready[1]);
	close(child_release[0]);
	TEST_RES(read(child_ready[0], &child_result, 1),
		 _ret == 1 && child_result == EXIT_SUCCESS);
	TEST_RES(raw_sched_getscheduler(child), _ret == SCHED_OTHER);
	TEST_RES(raw_sched_getparam(child, &child_param),
		 _ret == 0 && child_param.sched_priority == 0);
	TEST_RES(write(child_release[1], &release, 1), _ret == 1);
	TEST_RES(waitpid(child, &status, 0),
		 _ret == child && WIFEXITED(status) &&
			 WEXITSTATUS(status) == EXIT_SUCCESS);

	close(child_ready[0]);
	close(child_release[1]);
	TEST_SUCC(raw_sched_setscheduler(0, SCHED_OTHER, &normal_param));
}
END_TEST()
