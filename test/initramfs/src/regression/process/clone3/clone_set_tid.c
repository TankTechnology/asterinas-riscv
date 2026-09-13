// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE

#include <linux/sched.h>
#include <signal.h>
#include <stdint.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

#include "../../common/test.h"

#define ptr_to_u64(ptr) ((__u64)((uintptr_t)(ptr)))

static pid_t sys_clone3(struct clone_args *args)
{
	return syscall(SYS_clone3, args, sizeof(*args));
}

FN_TEST(set_tid_rejects_invalid_arrays)
{
	pid_t requested_pid = 1;
	struct clone_args args = {
		.exit_signal = SIGCHLD,
	};

	args.set_tid = ptr_to_u64(&requested_pid);
	TEST_ERRNO(sys_clone3(&args), EINVAL);

	args.set_tid = 0;
	args.set_tid_size = 1;
	TEST_ERRNO(sys_clone3(&args), EINVAL);

	args.set_tid = ptr_to_u64(&requested_pid);
	args.set_tid_size = 33;
	TEST_ERRNO(sys_clone3(&args), EINVAL);

	args.set_tid_size = 1;
	requested_pid = 0;
	TEST_ERRNO(sys_clone3(&args), EINVAL);

	requested_pid = -1;
	TEST_ERRNO(sys_clone3(&args), EINVAL);
}
END_TEST()

FN_TEST(set_tid_reserves_and_reuses_global_pid)
{
	static const pid_t requested_pid = 500000;
	int release_pipe[2];
	int status;
	pid_t child;
	struct clone_args args = {
		.exit_signal = SIGCHLD,
		.set_tid = ptr_to_u64(&requested_pid),
		.set_tid_size = 1,
	};

	TEST_SUCC(pipe(release_pipe));
	child = sys_clone3(&args);
	if (child == 0) {
		char byte;

		if (getpid() != requested_pid)
			_exit(10);
		if (read(release_pipe[0], &byte, sizeof(byte)) != sizeof(byte))
			_exit(11);
		_exit(0);
	}
	TEST_RES(child, _ret == requested_pid);

	child = TEST_ERRNO(sys_clone3(&args), EEXIST);
	if (child == 0)
		_exit(12);

	TEST_RES(write(release_pipe[1], "x", 1), _ret == 1);
	TEST_RES(waitpid(requested_pid, &status, 0),
		 _ret == requested_pid && WIFEXITED(status) &&
			 WEXITSTATUS(status) == 0);
	TEST_SUCC(close(release_pipe[0]));
	TEST_SUCC(close(release_pipe[1]));

	child = sys_clone3(&args);
	if (child == 0)
		_exit(getpid() == requested_pid ? 0 : 13);
	TEST_RES(child, _ret == requested_pid);
	TEST_RES(waitpid(requested_pid, &status, 0),
		 _ret == requested_pid && WIFEXITED(status) &&
			 WEXITSTATUS(status) == 0);
}
END_TEST()

FN_TEST(set_tid_maps_pid_namespace_levels)
{
	static const pid_t requested_pids[] = { 1, 500001 };
	int status;
	pid_t child;
	struct clone_args args = {
		.flags = CLONE_NEWPID,
		.exit_signal = SIGCHLD,
		.set_tid = ptr_to_u64(requested_pids),
		.set_tid_size = 2,
	};

	child = sys_clone3(&args);
	if (child == 0)
		_exit(getpid() == 1 ? 0 : 20);
	TEST_RES(child, _ret == requested_pids[1]);
	TEST_RES(waitpid(requested_pids[1], &status, 0),
		 _ret == requested_pids[1] && WIFEXITED(status) &&
			 WEXITSTATUS(status) == 0);
}
END_TEST()

FN_TEST(set_tid_checks_the_target_user_namespace)
{
	int status;
	pid_t child = TEST_SUCC(fork());

	if (child == 0) {
		static const pid_t requested_pid = 500002;
		struct clone_args args = {
			.exit_signal = SIGCHLD,
			.set_tid = ptr_to_u64(&requested_pid),
			.set_tid_size = 1,
		};

		if (unshare(CLONE_NEWUSER) < 0)
			_exit(30);
		errno = 0;
		if (sys_clone3(&args) != -1 || errno != EPERM)
			_exit(31);
		_exit(0);
	}

	TEST_RES(waitpid(child, &status, 0), _ret == child &&
						     WIFEXITED(status) &&
						     WEXITSTATUS(status) == 0);
}
END_TEST()
