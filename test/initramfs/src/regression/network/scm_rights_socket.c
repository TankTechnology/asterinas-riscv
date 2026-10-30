// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE

#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>

#include "../common/test.h"

/*
 * Linux permits passing a UNIX socket through SCM_RIGHTS without
 * CAP_SYS_ADMIN.  Firefox relies on this to hand its IPC sockets to content
 * processes that run without privileges.
 */
FN_TEST(scm_rights_unix_socket)
{
	int transport[2];
	int ipc[2];
	int passed_fd = -1;
	int valid;
	int child_ok;
	pid_t pid;
	int status;
	char byte = 'x';
	char reply = 'y';
	char cbuf[CMSG_SPACE(sizeof(int))];
	struct iovec iov = {
		.iov_base = &byte,
		.iov_len = 1,
	};
	struct msghdr msg = {
		.msg_iov = &iov,
		.msg_iovlen = 1,
		.msg_control = cbuf,
		.msg_controllen = sizeof(cbuf),
	};
	struct cmsghdr *cmsg;

	TEST_SUCC(socketpair(AF_UNIX, SOCK_STREAM, 0, transport));
	TEST_SUCC(socketpair(AF_UNIX, SOCK_STREAM, 0, ipc));

	pid = TEST_SUCC(fork());
	if (pid == 0) {
		if (getuid() == 0 && setuid(65534) != 0)
			_exit(EXIT_FAILURE);

		memset(cbuf, 0, sizeof(cbuf));
		cmsg = CMSG_FIRSTHDR(&msg);
		cmsg->cmsg_level = SOL_SOCKET;
		cmsg->cmsg_type = SCM_RIGHTS;
		cmsg->cmsg_len = CMSG_LEN(sizeof(int));
		memcpy(CMSG_DATA(cmsg), &ipc[0], sizeof(int));
		if (sendmsg(transport[0], &msg, 0) != 1)
			_exit(EXIT_FAILURE);
		_exit(EXIT_SUCCESS);
	}
	TEST_SUCC(waitpid(pid, &status, 0));
	child_ok = WIFEXITED(status) && WEXITSTATUS(status) == 0;
	TEST_RES(status, child_ok);

	if (child_ok) {
		memset(cbuf, 0, sizeof(cbuf));
		msg.msg_controllen = sizeof(cbuf);
		msg.msg_flags = 0;
		TEST_RES(recvmsg(transport[1], &msg, 0),
			 _ret == 1 && (msg.msg_flags & MSG_CTRUNC) == 0);

		cmsg = CMSG_FIRSTHDR(&msg);
		valid = cmsg != NULL && cmsg->cmsg_level == SOL_SOCKET &&
			cmsg->cmsg_type == SCM_RIGHTS &&
			cmsg->cmsg_len == CMSG_LEN(sizeof(int));
		TEST_RES(cmsg, valid);

		if (valid) {
			passed_fd = *(int *)CMSG_DATA(cmsg);
			TEST_RES(passed_fd, passed_fd >= 0);

			TEST_RES(write(passed_fd, &reply, 1), _ret == 1);
			TEST_RES(read(ipc[1], &reply, 1),
				 _ret == 1 && reply == 'y');
		}
	}

	if (passed_fd >= 0)
		TEST_SUCC(close(passed_fd));
	TEST_SUCC(close(ipc[0]));
	TEST_SUCC(close(ipc[1]));
	TEST_SUCC(close(transport[0]));
	TEST_SUCC(close(transport[1]));
}
END_TEST()
