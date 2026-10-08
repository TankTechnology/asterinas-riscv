// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <linux/kcmp.h>
#include <sched.h>
#include <signal.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

#include "../common/test.h"

#define CHILD_STACK_SIZE (64 * 1024)

static char child_stack[CHILD_STACK_SIZE] __attribute__((aligned(16)));

struct clone_sync {
	int ready_fd;
	int release_fd;
};

static long kcmp_call(pid_t pid1, pid_t pid2, int type, unsigned long idx1,
		      unsigned long idx2)
{
	return syscall(SYS_kcmp, pid1, pid2, type, idx1, idx2);
}

static int clone_files_child(void *arg)
{
	struct clone_sync *sync = arg;
	char byte = 'R';

	if (write(sync->ready_fd, &byte, 1) != 1)
		return 1;
	if (read(sync->release_fd, &byte, 1) != 1)
		return 1;
	return 0;
}

static int repeat_self_comparison(pid_t self)
{
	for (int i = 0; i < 1000; i++) {
		if (kcmp_call(self, self, KCMP_FILES, 0, 0) != 0)
			return -1;
	}
	return 0;
}

FN_TEST(file_description_identity)
{
	int fd = TEST_SUCC(open("/dev/null", O_RDONLY));
	int duplicate = TEST_SUCC(dup(fd));
	int distinct = TEST_SUCC(open("/dev/null", O_RDONLY));
	pid_t self = getpid();

	TEST_RES(kcmp_call(self, self, KCMP_FILE, fd, fd), _ret == 0);
	TEST_RES(kcmp_call(self, self, KCMP_FILE, fd, duplicate), _ret == 0);
	/* 3 means unequal with no address-order information available. */
	TEST_RES(kcmp_call(self, self, KCMP_FILE, fd, distinct), _ret == 3);

	TEST_SUCC(close(distinct));
	TEST_SUCC(close(duplicate));
	TEST_SUCC(close(fd));
}
END_TEST()

FN_TEST(fork_shares_file_description_not_table)
{
	int ready[2];
	int release[2];
	TEST_SUCC(pipe(ready));
	TEST_SUCC(pipe(release));
	int fd = TEST_SUCC(open("/dev/null", O_RDONLY));
	pid_t self = getpid();
	pid_t child = TEST_SUCC(fork());

	if (child == 0) {
		char byte = 'R';
		if (write(ready[1], &byte, 1) != 1 ||
		    read(release[0], &byte, 1) != 1)
			_exit(1);
		_exit(0);
	}

	char byte;
	TEST_RES(read(ready[0], &byte, 1), _ret == 1);
	TEST_RES(kcmp_call(self, child, KCMP_FILE, fd, fd), _ret == 0);
	TEST_RES(kcmp_call(self, child, KCMP_FILES, 0, 0), _ret == 3);
	TEST_RES(write(release[1], &byte, 1), _ret == 1);

	int status = 0;
	TEST_RES(waitpid(child, &status, 0), _ret == child &&
						     WIFEXITED(status) &&
						     WEXITSTATUS(status) == 0);

	TEST_SUCC(close(fd));
	TEST_SUCC(close(ready[0]));
	TEST_SUCC(close(ready[1]));
	TEST_SUCC(close(release[0]));
	TEST_SUCC(close(release[1]));
}
END_TEST()

FN_TEST(clone_files_shares_table)
{
	int ready[2];
	int release[2];
	TEST_SUCC(pipe(ready));
	TEST_SUCC(pipe(release));
	struct clone_sync sync = {
		.ready_fd = ready[1],
		.release_fd = release[0],
	};
	pid_t self = getpid();
	pid_t child = TEST_SUCC(clone(clone_files_child,
				      child_stack + sizeof(child_stack),
				      CLONE_FILES | SIGCHLD, &sync));

	char byte;
	TEST_RES(read(ready[0], &byte, 1), _ret == 1);
	TEST_RES(kcmp_call(self, child, KCMP_FILES, 0, 0), _ret == 0);
	TEST_RES(write(release[1], &byte, 1), _ret == 1);

	int status = 0;
	TEST_RES(waitpid(child, &status, 0), _ret == child &&
						     WIFEXITED(status) &&
						     WEXITSTATUS(status) == 0);

	TEST_SUCC(close(ready[0]));
	TEST_SUCC(close(ready[1]));
	TEST_SUCC(close(release[0]));
	TEST_SUCC(close(release[1]));
}
END_TEST()

FN_TEST(error_contract)
{
	pid_t self = getpid();
	int fd = TEST_SUCC(open("/dev/null", O_RDONLY));

	TEST_ERRNO(kcmp_call(0, self, KCMP_FILES, 0, 0), ESRCH);
	TEST_ERRNO(kcmp_call(self, self, KCMP_FILE, (unsigned long)-1, fd),
		   EBADF);
	TEST_ERRNO(kcmp_call(self, self, KCMP_VM, 0, 0), EOPNOTSUPP);
	TEST_ERRNO(kcmp_call(self, self, 8, 0, 0), EINVAL);

	TEST_SUCC(close(fd));
}
END_TEST()

FN_TEST(same_process_comparison_does_not_deadlock)
{
	TEST_RES(repeat_self_comparison(getpid()), _ret == 0);
}
END_TEST()
