// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <sched.h>
#include <stdint.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>

#include "../../common/test.h"

#define TEST_FILE "/tmp/fcntl_lock_regression"
#define CLONE_STACK_SIZE 4096

static char clone_stack[CLONE_STACK_SIZE];

static int open_test_file(void)
{
	return open(TEST_FILE, O_CREAT | O_RDWR | O_TRUNC, 0666);
}

static int try_write_lock(int fd, off_t start, off_t len)
{
	struct flock lock = {
		.l_type = F_WRLCK,
		.l_whence = SEEK_SET,
		.l_start = start,
		.l_len = len,
	};

	return fcntl(fd, F_SETLK, &lock);
}

static int unlock_range(int fd, off_t start, off_t len)
{
	struct flock lock = {
		.l_type = F_UNLCK,
		.l_whence = SEEK_SET,
		.l_start = start,
		.l_len = len,
	};

	return fcntl(fd, F_SETLK, &lock);
}

static int child_try_write_lock(off_t start, off_t len)
{
	pid_t child = CHECK(fork());
	if (child == 0) {
		int fd = CHECK(open(TEST_FILE, O_RDWR));
		int ret = try_write_lock(fd, start, len);

		if (ret == 0) {
			_exit(0);
		}

		_exit(errno);
	}

	int status = 0;
	CHECK(waitpid(child, &status, 0));
	if (!WIFEXITED(status)) {
		errno = ECHILD;
		return -1;
	}

	return WEXITSTATUS(status);
}

static int child_try_fd_write_lock(int fd, off_t start, off_t len)
{
	pid_t child = CHECK(fork());
	if (child == 0) {
		int ret = try_write_lock(fd, start, len);

		if (ret == 0)
			_exit(0);

		_exit(errno);
	}

	int status = 0;
	CHECK(waitpid(child, &status, 0));
	if (!WIFEXITED(status)) {
		errno = ECHILD;
		return -1;
	}

	return WEXITSTATUS(status);
}

static int clone_child_exit(void *arg)
{
	(void)arg;
	return 0;
}

static int clone_child_close(void *arg)
{
	CHECK(close((int)(intptr_t)arg));
	return 0;
}

FN_SETUP(create)
{
	int fd = CHECK(open_test_file());
	CHECK(close(fd));
}
END_SETUP()

FN_TEST(unlock_middle_range)
{
	int fd = TEST_SUCC(open(TEST_FILE, O_RDWR));

	TEST_SUCC(try_write_lock(fd, 0, 100));
	TEST_SUCC(unlock_range(fd, 20, 60));
	TEST_RES(child_try_write_lock(20, 60), _ret == 0);

	TEST_SUCC(close(fd));
}
END_TEST()

FN_TEST(close_dup_fd_releases_locks)
{
	int fd = TEST_SUCC(open(TEST_FILE, O_RDWR));
	int duplicated_fd = TEST_SUCC(dup(fd));

	TEST_SUCC(try_write_lock(fd, 0, 100));
	TEST_SUCC(close(duplicated_fd));
	TEST_RES(child_try_write_lock(0, 100), _ret == 0);

	TEST_SUCC(close(fd));
}
END_TEST()

FN_TEST(socketpair_setlkw_and_close_releases_lock)
{
	int sockets[2];
	TEST_SUCC(socketpair(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0, sockets));
	int duplicated_fd = TEST_SUCC(dup(sockets[0]));
	struct flock lock = {
		.l_type = F_WRLCK,
		.l_whence = SEEK_SET,
		.l_start = 0,
		.l_len = 0,
	};
	char byte;
	struct iovec iov = {
		.iov_base = &byte,
		.iov_len = sizeof(byte),
	};
	struct msghdr message = {
		.msg_iov = &iov,
		.msg_iovlen = 1,
	};

	/* systemd uses this lock to serialize shareable network namespace setup. */
	TEST_SUCC(fcntl(sockets[0], F_SETLKW, &lock));
	TEST_ERRNO(recvmsg(sockets[0], &message, MSG_PEEK | MSG_DONTWAIT),
		   EAGAIN);
	TEST_RES(child_try_fd_write_lock(sockets[0], 0, 0), _ret == EAGAIN);
	/* The peer socket has its own sockfs inode and must not share this lock. */
	TEST_RES(child_try_fd_write_lock(sockets[1], 0, 0), _ret == 0);

	/* Closing any duplicate releases this process's POSIX locks on the socket. */
	TEST_SUCC(close(duplicated_fd));
	TEST_RES(child_try_fd_write_lock(sockets[0], 0, 0), _ret == 0);

	TEST_SUCC(close(sockets[0]));
	TEST_SUCC(close(sockets[1]));
}
END_TEST()

FN_TEST(ofd_lock_is_owned_by_open_file_description)
{
	int fd = TEST_SUCC(open(TEST_FILE, O_RDWR));
	int duplicate = TEST_SUCC(dup(fd));
	int independent = TEST_SUCC(open(TEST_FILE, O_RDWR));
	struct flock lock = {
		.l_type = F_WRLCK,
		.l_whence = SEEK_SET,
		.l_start = 0,
		.l_len = 0,
		.l_pid = 0,
	};

	struct flock invalid_pid = lock;
	invalid_pid.l_pid = 1;
	TEST_ERRNO(fcntl(fd, F_OFD_SETLK, &invalid_pid), EINVAL);
	TEST_SUCC(fcntl(fd, F_OFD_SETLKW, &lock));
	struct flock query = lock;
	TEST_SUCC(fcntl(duplicate, F_OFD_GETLK, &query));
	TEST_RES(query.l_type, _ret == F_UNLCK);
	query = lock;
	TEST_SUCC(fcntl(independent, F_GETLK, &query));
	TEST_RES(query.l_pid, _ret == -1);
	TEST_ERRNO(fcntl(independent, F_OFD_SETLK, &lock), EAGAIN);
	TEST_ERRNO(try_write_lock(fd, 0, 0), EAGAIN);
	TEST_SUCC(close(fd));
	TEST_ERRNO(fcntl(independent, F_OFD_SETLK, &lock), EAGAIN);
	TEST_SUCC(close(duplicate));
	TEST_SUCC(fcntl(independent, F_OFD_SETLK, &lock));
	TEST_SUCC(close(independent));
}
END_TEST()

FN_TEST(ofd_lock_survives_fork_until_last_description_closes)
{
	int fd = TEST_SUCC(open(TEST_FILE, O_RDWR));
	int channel[2];
	TEST_SUCC(pipe(channel));
	struct flock lock = {
		.l_type = F_WRLCK,
		.l_whence = SEEK_SET,
		.l_pid = 0,
	};
	TEST_SUCC(fcntl(fd, F_OFD_SETLK, &lock));

	pid_t child = TEST_SUCC(fork());
	if (child == 0) {
		CHECK(close(channel[1]));
		char release;
		CHECK_WITH(read(channel[0], &release, 1), _ret == 1);
		CHECK(close(fd));
		_exit(0);
	}
	TEST_SUCC(close(channel[0]));
	TEST_SUCC(close(fd));
	int independent = TEST_SUCC(open(TEST_FILE, O_RDWR));
	TEST_ERRNO(fcntl(independent, F_OFD_SETLK, &lock), EAGAIN);
	TEST_RES(write(channel[1], "x", 1), _ret == 1);
	int status = 0;
	TEST_RES(waitpid(child, &status, 0), _ret == child &&
					     WIFEXITED(status) &&
					     WEXITSTATUS(status) == 0);
	TEST_SUCC(fcntl(independent, F_OFD_SETLK, &lock));
	TEST_SUCC(close(independent));
	TEST_SUCC(close(channel[1]));
}
END_TEST()

FN_TEST(process_exit_releases_locks)
{
	int fd = TEST_SUCC(open(TEST_FILE, O_RDWR));

	pid_t child = TEST_SUCC(fork());
	if (child == 0) {
		CHECK(try_write_lock(fd, 0, 100));
		_exit(0);
	}

	/* Exiting the sole owner closes its file table and releases its locks. */
	int status = 0;
	TEST_RES(waitpid(child, &status, 0), _ret == child &&
						     WIFEXITED(status) &&
						     WEXITSTATUS(status) == 0);
	TEST_SUCC(try_write_lock(fd, 0, 100));

	TEST_SUCC(close(fd));
}
END_TEST()

FN_TEST(forked_process_close_keeps_parent_locks)
{
	int fd = TEST_SUCC(open(TEST_FILE, O_RDWR));

	TEST_SUCC(try_write_lock(fd, 0, 100));
	pid_t child = TEST_SUCC(fork());
	if (child == 0) {
		/* fork() gives the child a distinct file table and lock owner. */
		CHECK(close(fd));
		_exit(0);
	}

	int status = 0;
	TEST_RES(waitpid(child, &status, 0), _ret == child &&
						     WIFEXITED(status) &&
						     WEXITSTATUS(status) == 0);
	TEST_RES(child_try_write_lock(0, 100), _ret == EAGAIN);

	TEST_SUCC(close(fd));
}
END_TEST()

FN_TEST(shared_file_table_exit_keeps_locks)
{
	int fd = TEST_SUCC(open(TEST_FILE, O_RDWR));

	TEST_SUCC(try_write_lock(fd, 0, 100));
	/* CLONE_FILES makes the child share the parent's file table. */
	pid_t child = TEST_SUCC(clone(clone_child_exit,
				      clone_stack + sizeof(clone_stack),
				      CLONE_FILES | SIGCHLD, NULL));

	/* One sharer exiting must not release locks owned by the shared table. */
	int status = 0;
	TEST_RES(waitpid(child, &status, 0), _ret == child &&
						     WIFEXITED(status) &&
						     WEXITSTATUS(status) == 0);
	TEST_RES(child_try_write_lock(0, 100), _ret == EAGAIN);

	TEST_SUCC(close(fd));
}
END_TEST()

FN_TEST(shared_file_table_close_releases_locks)
{
	int fd = TEST_SUCC(open(TEST_FILE, O_RDWR));
	int duplicated_fd = TEST_SUCC(dup(fd));

	TEST_SUCC(try_write_lock(fd, 0, 100));
	/* The child closes the duplicate in the file table shared with its parent. */
	pid_t child = TEST_SUCC(
		clone(clone_child_close, clone_stack + sizeof(clone_stack),
		      CLONE_FILES | SIGCHLD, (void *)(intptr_t)duplicated_fd));

	/* Closing any fd for the file releases this table owner's POSIX locks. */
	int status = 0;
	TEST_RES(waitpid(child, &status, 0), _ret == child &&
						     WIFEXITED(status) &&
						     WEXITSTATUS(status) == 0);
	TEST_RES(child_try_write_lock(0, 100), _ret == 0);

	TEST_SUCC(close(fd));
}
END_TEST()

FN_SETUP(cleanup)
{
	CHECK(unlink(TEST_FILE));
}
END_SETUP()
