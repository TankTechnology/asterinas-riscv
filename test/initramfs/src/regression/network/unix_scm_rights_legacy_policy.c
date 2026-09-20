// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE

#include <errno.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

enum passed_kind {
	PASSED_STREAM,
	PASSED_DATAGRAM,
	PASSED_PIPE,
};

static void fail(const char *format, ...)
{
	va_list args;

	fprintf(stderr, "B1_SLICE4_SCM_LEGACY_POLICY_FAIL: ");
	va_start(args, format);
	vfprintf(stderr, format, args);
	va_end(args);
	fputc('\n', stderr);
	exit(EXIT_FAILURE);
}

static int send_fd(int socket, int passed_fd)
{
	char byte = 'x';
	char control[CMSG_SPACE(sizeof(passed_fd))] = {};
	struct iovec iov = { .iov_base = &byte, .iov_len = sizeof(byte) };
	struct msghdr message = {
		.msg_iov = &iov,
		.msg_iovlen = 1,
		.msg_control = control,
		.msg_controllen = sizeof(control),
	};
	struct cmsghdr *header = CMSG_FIRSTHDR(&message);

	header->cmsg_level = SOL_SOCKET;
	header->cmsg_type = SCM_RIGHTS;
	header->cmsg_len = CMSG_LEN(sizeof(passed_fd));
	memcpy(CMSG_DATA(header), &passed_fd, sizeof(passed_fd));

	return sendmsg(socket, &message, 0);
}

static int receive_fd(int socket)
{
	char byte;
	char control[CMSG_SPACE(sizeof(int))] = {};
	struct iovec iov = { .iov_base = &byte, .iov_len = sizeof(byte) };
	struct msghdr message = {
		.msg_iov = &iov,
		.msg_iovlen = 1,
		.msg_control = control,
		.msg_controllen = sizeof(control),
	};
	struct cmsghdr *header;
	int received_fd;

	if (recvmsg(socket, &message, 0) != 1)
		fail("recvmsg failed: %s", strerror(errno));
	header = CMSG_FIRSTHDR(&message);
	if (!header || header->cmsg_level != SOL_SOCKET ||
	    header->cmsg_type != SCM_RIGHTS ||
	    header->cmsg_len < CMSG_LEN(sizeof(received_fd)))
		fail("recvmsg returned malformed SCM_RIGHTS data");
	memcpy(&received_fd, CMSG_DATA(header), sizeof(received_fd));
	return received_fd;
}

static void verify_received_fd(enum passed_kind kind, int received_fd,
			       int peer_fd)
{
	char byte = 'v';
	char received = 0;

	if (kind == PASSED_PIPE) {
		if (write(peer_fd, &byte, 1) != 1 ||
		    read(received_fd, &received, 1) != 1 || received != byte)
			fail("received pipe FD is not usable");
		return;
	}

	int type = 0;
	socklen_t type_len = sizeof(type);
	int expected = kind == PASSED_STREAM ? SOCK_STREAM : SOCK_DGRAM;
	if (getsockopt(received_fd, SOL_SOCKET, SO_TYPE, &type, &type_len) !=
		    0 ||
	    type != expected)
		fail("received socket has type %d, expected %d", type,
		     expected);
	if (send(peer_fd, &byte, 1, 0) != 1 ||
	    recv(received_fd, &received, 1, 0) != 1 || received != byte)
		fail("received socket FD is not usable");
}

static void run_case(int carrier_type, enum passed_kind kind, bool expect_eperm)
{
	int carrier[2];
	int passed[2];
	int result;

	if (socketpair(AF_UNIX, carrier_type, 0, carrier) != 0)
		fail("carrier socketpair failed: %s", strerror(errno));
	if (kind == PASSED_PIPE) {
		if (pipe(passed) != 0)
			fail("pipe failed: %s", strerror(errno));
	} else if (socketpair(AF_UNIX,
			      kind == PASSED_STREAM ? SOCK_STREAM : SOCK_DGRAM,
			      0, passed) != 0) {
		fail("passed socketpair failed: %s", strerror(errno));
	}

	errno = 0;
	result = send_fd(carrier[0], passed[0]);
	if (expect_eperm) {
		if (result != -1 || errno != EPERM)
			fail("stream FD send returned %d/%s, expected EPERM",
			     result, strerror(errno));
	} else {
		if (result != 1)
			fail("SCM_RIGHTS send returned %d/%s", result,
			     strerror(errno));
		int received_fd = receive_fd(carrier[1]);
		verify_received_fd(kind, received_fd, passed[1]);
		close(received_fd);
	}

	close(passed[0]);
	close(passed[1]);
	close(carrier[0]);
	close(carrier[1]);
}

static void run_policy_matrix(bool expect_stream_eperm)
{
	const int carrier_types[] = { SOCK_STREAM, SOCK_DGRAM };

	for (size_t i = 0; i < sizeof(carrier_types) / sizeof(carrier_types[0]);
	     i++) {
		run_case(carrier_types[i], PASSED_STREAM, expect_stream_eperm);
		run_case(carrier_types[i], PASSED_DATAGRAM, false);
		run_case(carrier_types[i], PASSED_PIPE, false);
	}
}

int main(void)
{
#ifdef __asterinas__
	if (geteuid() != 0)
		fail("Asterinas guest init must start as root");
	// The pre-Slice-4 privileged behavior must remain unchanged for both protocols.
	run_case(SOCK_STREAM, PASSED_STREAM, false);
	run_case(SOCK_DGRAM, PASSED_STREAM, false);

	pid_t child = fork();
	if (child < 0)
		fail("fork failed: %s", strerror(errno));
	if (child == 0) {
		if (setgid(65534) != 0 || setuid(65534) != 0)
			fail("dropping privileges failed: %s", strerror(errno));
		run_policy_matrix(true);
		_exit(EXIT_SUCCESS);
	}
	int status;
	if (waitpid(child, &status, 0) != child || !WIFEXITED(status) ||
	    WEXITSTATUS(status) != 0)
		fail("unprivileged child failed with status %#x", status);

	fprintf(stderr, "B1_SLICE4_SCM_LEGACY_POLICY_PASS\n");
	fflush(stderr);
	return EXIT_SUCCESS;
#else
	// Linux does not have Asterinas's temporary capability gate. Running as the ordinary host UID
	// is the oracle: all three passed-file classes must remain accepted and usable.
	run_policy_matrix(false);
	fprintf(stderr, "B1_SLICE4_SCM_LINUX_ORACLE_PASS\n");
	return EXIT_SUCCESS;
#endif
}
