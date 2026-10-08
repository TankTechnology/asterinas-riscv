// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <stddef.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/eventfd.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

#define MAX_RIGHTS 8

static void fail(const char *format, ...)
{
	va_list args;

	fprintf(stderr, "B1_SLICE5_SCM_ACYCLIC_FAIL: ");
	va_start(args, format);
	vfprintf(stderr, format, args);
	va_end(args);
	fputc('\n', stderr);
	exit(EXIT_FAILURE);
}

static ssize_t send_rights(int carrier, const int *fds, size_t nr_fds,
			   const void *payload, size_t payload_len, int flags)
{
	char control[CMSG_SPACE(MAX_RIGHTS * sizeof(int))] = {};
	struct iovec iov = { .iov_base = (void *)payload,
			     .iov_len = payload_len };
	struct msghdr message = {
		.msg_iov = &iov,
		.msg_iovlen = 1,
		.msg_control = control,
		.msg_controllen = CMSG_SPACE(nr_fds * sizeof(int)),
	};
	struct cmsghdr *header;

	if (nr_fds == 0 || nr_fds > MAX_RIGHTS)
		fail("invalid SCM_RIGHTS count %zu", nr_fds);
	header = CMSG_FIRSTHDR(&message);
	header->cmsg_level = SOL_SOCKET;
	header->cmsg_type = SCM_RIGHTS;
	header->cmsg_len = CMSG_LEN(nr_fds * sizeof(int));
	memcpy(CMSG_DATA(header), fds, nr_fds * sizeof(int));
	return sendmsg(carrier, &message, flags);
}

static ssize_t receive_rights(int carrier, int flags, int *fds, size_t expected,
			      char *payload)
{
	char control[CMSG_SPACE(MAX_RIGHTS * sizeof(int))] = {};
	struct iovec iov = { .iov_base = payload, .iov_len = 1 };
	struct msghdr message = {
		.msg_iov = &iov,
		.msg_iovlen = 1,
		.msg_control = control,
		.msg_controllen = sizeof(control),
	};
	struct cmsghdr *header;
	ssize_t result = recvmsg(carrier, &message, flags);

	if (result < 0)
		fail("recvmsg failed: %s", strerror(errno));
	header = CMSG_FIRSTHDR(&message);
	if (!header || header->cmsg_level != SOL_SOCKET ||
	    header->cmsg_type != SCM_RIGHTS ||
	    header->cmsg_len != CMSG_LEN(expected * sizeof(int)))
		fail("recvmsg returned malformed SCM_RIGHTS data");
	memcpy(fds, CMSG_DATA(header), expected * sizeof(int));
	return result;
}

static void expect_send(int carrier, const int *fds, size_t nr_fds,
			bool expect_eperm)
{
	const char byte = 'x';
	errno = 0;
	ssize_t result = send_rights(carrier, fds, nr_fds, &byte, 1, 0);

	if (expect_eperm) {
		if (result != -1 || errno != EPERM)
			fail("sendmsg returned %zd/%s, expected EPERM", result,
			     strerror(errno));
	} else if (result != 1) {
		fail("sendmsg returned %zd/%s, expected one byte", result,
		     strerror(errno));
	}
}

static void close_fds(int *fds, size_t nr_fds)
{
	for (size_t i = 0; i < nr_fds; i++)
		if (close(fds[i]) != 0)
			fail("close received FD failed: %s", strerror(errno));
}

static void verify_socket_fd(int fd, int expected_type, int peer)
{
	int type = 0;
	socklen_t type_len = sizeof(type);
	char byte = 'v';
	char received = 0;

	if (getsockopt(fd, SOL_SOCKET, SO_TYPE, &type, &type_len) != 0 ||
	    type != expected_type)
		fail("received socket type %d, expected %d", type,
		     expected_type);
	if (send(peer, &byte, 1, 0) != 1 || recv(fd, &received, 1, 0) != 1 ||
	    received != byte)
		fail("received socket is not usable peer-to-received");
	byte = 'w';
	if (send(fd, &byte, 1, 0) != 1 || recv(peer, &received, 1, 0) != 1 ||
	    received != byte)
		fail("received socket is not usable received-to-peer");
}

static void test_unrelated_socket(int carrier_type, int passed_type)
{
	int carrier[2];
	int passed[2];
	int received_fd;
	char payload;

	if (socketpair(AF_UNIX, carrier_type, 0, carrier) != 0 ||
	    socketpair(AF_UNIX, passed_type, 0, passed) != 0)
		fail("unrelated socketpair failed: %s", strerror(errno));
	expect_send(carrier[0], passed, 1, false);
	if (close(passed[0]) != 0)
		fail("closing original passed FD failed: %s", strerror(errno));
	if (receive_rights(carrier[1], 0, &received_fd, 1, &payload) != 1 ||
	    payload != 'x')
		fail("unrelated SCM payload mismatch");
	verify_socket_fd(received_fd, passed_type, passed[1]);
	close(received_fd);
	close(passed[1]);
	close(carrier[0]);
	close(carrier[1]);
}

static void test_pipe_and_regular_leaf(void)
{
	int carrier[2];
	int pipe_fds[2];
	int received[2];
	char path[] = "/tmp/b1-slice5-XXXXXX";
	char payload;
	char byte = 'p';
	int regular;

	if (socketpair(AF_UNIX, SOCK_STREAM, 0, carrier) != 0 ||
	    pipe(pipe_fds) != 0)
		fail("leaf setup failed: %s", strerror(errno));
	regular = mkstemp(path);
	if (regular < 0 || unlink(path) != 0)
		fail("regular-file setup failed: %s", strerror(errno));
	int sent[2] = { pipe_fds[0], regular };
	expect_send(carrier[0], sent, 2, false);
	close(pipe_fds[0]);
	close(regular);
	if (receive_rights(carrier[1], 0, received, 2, &payload) != 1)
		fail("leaf receive length mismatch");
	if (write(pipe_fds[1], &byte, 1) != 1 ||
	    read(received[0], &payload, 1) != 1 || payload != byte)
		fail("received pipe leaf is unusable");
	if (write(received[1], &byte, 1) != 1 ||
	    lseek(received[1], 0, SEEK_SET) != 0 ||
	    read(received[1], &payload, 1) != 1 || payload != byte)
		fail("received regular-file leaf is unusable");
	close_fds(received, 2);
	close(pipe_fds[1]);
	close(carrier[0]);
	close(carrier[1]);
}

static void test_pidfd_leaf(void)
{
	int carrier[2];
	int received;
	char payload;
	int pidfd;

	if (socketpair(AF_UNIX, SOCK_STREAM, 0, carrier) != 0)
		fail("pidfd carrier socketpair failed: %s", strerror(errno));
	pidfd = syscall(SYS_pidfd_open, getpid(), 0);
	if (pidfd < 0)
		fail("pidfd_open failed: %s", strerror(errno));
	expect_send(carrier[0], &pidfd, 1, false);
	if (receive_rights(carrier[1], 0, &received, 1, &payload) != 1 ||
	    payload != 'x')
		fail("pidfd SCM payload mismatch");
	if (fcntl(received, F_GETFD) < 0)
		fail("received pidfd is unusable: %s", strerror(errno));
	close(received);
	close(pidfd);
	close_fds(carrier, 2);
}

static void test_slice5_closed_classes(void)
{
	int carrier[2];
	int datagram[2];
#ifndef __asterinas__
	int received;
	char payload;
#endif
	int event;

	if (socketpair(AF_UNIX, SOCK_STREAM, 0, carrier) != 0 ||
	    socketpair(AF_UNIX, SOCK_DGRAM, 0, datagram) != 0)
		fail("closed-class socketpair failed: %s", strerror(errno));
#ifdef __asterinas__
	expect_send(carrier[0], datagram, 1, true);
#else
	expect_send(carrier[0], datagram, 1, false);
	receive_rights(carrier[1], 0, &received, 1, &payload);
	close(received);
#endif
	event = eventfd(0, 0);
	if (event < 0)
		fail("eventfd setup failed: %s", strerror(errno));
#ifdef __asterinas__
	expect_send(carrier[0], &event, 1, true);
#else
	expect_send(carrier[0], &event, 1, false);
	receive_rights(carrier[1], 0, &received, 1, &payload);
	close(received);
#endif
	close(event);
	close(datagram[0]);
	close(datagram[1]);
	close(carrier[0]);
	close(carrier[1]);
}

static void test_self_cycle(int carrier_type)
{
	int carrier[2];
#ifndef __asterinas__
	int received;
	char payload;
#endif

	if (socketpair(AF_UNIX, carrier_type, 0, carrier) != 0)
		fail("self-cycle socketpair failed: %s", strerror(errno));
#ifdef __asterinas__
	expect_send(carrier[0], &carrier[0], 1, true);
	expect_send(carrier[0], &carrier[1], 1, true);
#else
	expect_send(carrier[0], &carrier[0], 1, false);
	receive_rights(carrier[1], 0, &received, 1, &payload);
	close(received);
	expect_send(carrier[0], &carrier[1], 1, false);
	receive_rights(carrier[1], 0, &received, 1, &payload);
	close(received);
#endif
	close(carrier[0]);
	close(carrier[1]);
}

static void test_two_storage_cycle(int first_type, int second_type)
{
	int first[2];
	int second[2];
	int received;
	char payload;

	if (socketpair(AF_UNIX, first_type, 0, first) != 0 ||
	    socketpair(AF_UNIX, second_type, 0, second) != 0)
		fail("two-storage setup failed: %s", strerror(errno));
	expect_send(first[0], &second[0], 1, false);
#ifdef __asterinas__
	expect_send(second[0], &first[0], 1, true);
#else
	expect_send(second[0], &first[0], 1, false);
#endif
	receive_rights(first[1], 0, &received, 1, &payload);
	close(received);
#ifndef __asterinas__
	receive_rights(second[1], 0, &received, 1, &payload);
	close(received);
#endif
	close(first[0]);
	close(first[1]);
	close(second[0]);
	close(second[1]);
}

static void test_long_cycle(void)
{
	int first[2], second[2], third[2];
	int received;
	char payload;

	if (socketpair(AF_UNIX, SOCK_STREAM, 0, first) != 0 ||
	    socketpair(AF_UNIX, SOCK_SEQPACKET, 0, second) != 0 ||
	    socketpair(AF_UNIX, SOCK_STREAM, 0, third) != 0)
		fail("long-cycle setup failed: %s", strerror(errno));
	expect_send(first[0], &second[0], 1, false);
	expect_send(second[0], &third[0], 1, false);
#ifdef __asterinas__
	expect_send(third[0], &first[0], 1, true);
#else
	expect_send(third[0], &first[0], 1, false);
#endif
	receive_rights(first[1], 0, &received, 1, &payload);
	close(received);
	receive_rights(second[1], 0, &received, 1, &payload);
	close(received);
#ifndef __asterinas__
	receive_rights(third[1], 0, &received, 1, &payload);
	close(received);
#endif
	close_fds(first, 2);
	close_fds(second, 2);
	close_fds(third, 2);
}

static void test_duplicate_peek_then_consume(void)
{
	int carrier[2], passed[2];
	int received[2];
	char payload;

	if (socketpair(AF_UNIX, SOCK_STREAM, 0, carrier) != 0 ||
	    socketpair(AF_UNIX, SOCK_STREAM, 0, passed) != 0)
		fail("duplicate setup failed: %s", strerror(errno));
	int duplicates[2] = { passed[0], passed[0] };
	expect_send(carrier[0], duplicates, 2, false);
	if (receive_rights(carrier[1], MSG_PEEK, received, 2, &payload) != 1)
		fail("peek SCM length mismatch");
	close_fds(received, 2);
	if (receive_rights(carrier[1], 0, received, 2, &payload) != 1)
		fail("consume SCM length mismatch");
	verify_socket_fd(received[0], SOCK_STREAM, passed[1]);
	close_fds(received, 2);
	close_fds(passed, 2);
	close_fds(carrier, 2);
}

static void test_efault_then_retry(void)
{
	int carrier[2], passed[2];
	int received;
	char payload;

	if (socketpair(AF_UNIX, SOCK_STREAM, 0, carrier) != 0 ||
	    socketpair(AF_UNIX, SOCK_STREAM, 0, passed) != 0)
		fail("EFAULT setup failed: %s", strerror(errno));
	errno = 0;
	if (send_rights(carrier[0], passed, 1, (void *)(uintptr_t)-1, 1, 0) !=
		    -1 ||
	    errno != EFAULT)
		fail("invalid iov did not return EFAULT: %s", strerror(errno));
	expect_send(carrier[0], passed, 1, false);
	receive_rights(carrier[1], 0, &received, 1, &payload);
	close(received);
	close_fds(passed, 2);
	close_fds(carrier, 2);
}

static void test_full_buffer_then_retry(void)
{
	int carrier[2], passed[2];
	char buffer[4096] = {};
	ssize_t result;
	size_t queued = 0;
	int received;
	char payload;

	if (socketpair(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK, 0, carrier) != 0 ||
	    socketpair(AF_UNIX, SOCK_STREAM, 0, passed) != 0)
		fail("full-buffer setup failed: %s", strerror(errno));
	for (;;) {
		result = send(carrier[0], buffer, sizeof(buffer), 0);
		if (result > 0) {
			queued += result;
			continue;
		}
		if (result == -1 && errno == EAGAIN)
			break;
		fail("filling stream buffer failed: %s", strerror(errno));
	}
	errno = 0;
	if (send_rights(carrier[0], passed, 1, "r", 1, 0) != -1 ||
	    errno != EAGAIN)
		fail("full-buffer SCM send did not return EAGAIN");
	while (queued > 0) {
		result = recv(carrier[1], buffer,
			      queued < sizeof(buffer) ? queued : sizeof(buffer),
			      0);
		if (result <= 0)
			fail("draining stream buffer failed: %s",
			     strerror(errno));
		queued -= result;
	}
	expect_send(carrier[0], passed, 1, false);
	receive_rights(carrier[1], 0, &received, 1, &payload);
	close(received);
	close_fds(passed, 2);
	close_fds(carrier, 2);
}

static void test_full_seqpacket_then_retry(void)
{
	int carrier[2], passed[2];
	char packet[1024] = {};
	int queued_packets = 0;
	int received;
	char payload;

	if (socketpair(AF_UNIX, SOCK_SEQPACKET | SOCK_NONBLOCK, 0, carrier) !=
		    0 ||
	    socketpair(AF_UNIX, SOCK_STREAM, 0, passed) != 0)
		fail("full-seqpacket setup failed: %s", strerror(errno));
	while (send(carrier[0], packet, sizeof(packet), 0) == sizeof(packet))
		queued_packets++;
	if (errno != EAGAIN || queued_packets == 0)
		fail("filling seqpacket buffer did not reach EAGAIN");
	errno = 0;
	if (send_rights(carrier[0], passed, 1, "s", 1, 0) != -1 ||
	    errno != EAGAIN)
		fail("full seqpacket SCM send did not return EAGAIN");
	for (int i = 0; i < queued_packets; i++)
		if (recv(carrier[1], packet, sizeof(packet), 0) !=
		    sizeof(packet))
			fail("draining seqpacket buffer failed: %s",
			     strerror(errno));
	expect_send(carrier[0], passed, 1, false);
	receive_rights(carrier[1], 0, &received, 1, &payload);
	close(received);
	close_fds(passed, 2);
	close_fds(carrier, 2);
}

static void test_blocking_retry(void)
{
	int carrier[2], passed[2], ready[2];
	char buffer[4096] = {};
	size_t queued = 0;
	ssize_t result;
	int received;
	char payload;
	pid_t sender;

	if (socketpair(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK, 0, carrier) != 0 ||
	    socketpair(AF_UNIX, SOCK_STREAM, 0, passed) != 0 ||
	    pipe(ready) != 0)
		fail("blocking-retry setup failed: %s", strerror(errno));
	for (;;) {
		result = send(carrier[0], buffer, sizeof(buffer), 0);
		if (result > 0) {
			queued += result;
			continue;
		}
		if (result == -1 && errno == EAGAIN)
			break;
		fail("blocking-retry fill failed: %s", strerror(errno));
	}
	int flags = fcntl(carrier[0], F_GETFL);
	if (flags < 0 || fcntl(carrier[0], F_SETFL, flags & ~O_NONBLOCK) != 0)
		fail("clearing O_NONBLOCK failed: %s", strerror(errno));

	sender = fork();
	if (sender < 0)
		fail("blocking-retry fork failed: %s", strerror(errno));
	if (sender == 0) {
		close(ready[0]);
		if (write(ready[1], "r", 1) != 1)
			_exit(2);
		close(ready[1]);
		expect_send(carrier[0], passed, 1, false);
		_exit(0);
	}
	close(ready[1]);
	if (read(ready[0], &payload, 1) != 1)
		fail("blocking-retry synchronization failed");
	close(ready[0]);
	// Give the sender a scheduling window to enter sendmsg while the buffer is
	// still full. It can only complete after the receiver drains below.
	usleep(100000);
	while (queued > 0) {
		result = recv(carrier[1], buffer,
			      queued < sizeof(buffer) ? queued : sizeof(buffer),
			      0);
		if (result <= 0)
			fail("blocking-retry drain failed: %s",
			     strerror(errno));
		queued -= result;
	}
	int status;
	if (waitpid(sender, &status, 0) != sender || !WIFEXITED(status) ||
	    WEXITSTATUS(status) != 0)
		fail("blocking sender failed with status %#x", status);
	receive_rights(carrier[1], 0, &received, 1, &payload);
	verify_socket_fd(received, SOCK_STREAM, passed[1]);
	close(received);
	close_fds(passed, 2);
	close_fds(carrier, 2);
}

static void test_receiver_close_drains_edges(void)
{
	int carrier[2], passed[2];
	int received;
	char payload;

	if (socketpair(AF_UNIX, SOCK_STREAM, 0, carrier) != 0 ||
	    socketpair(AF_UNIX, SOCK_STREAM, 0, passed) != 0)
		fail("receiver-close setup failed: %s", strerror(errno));
	expect_send(carrier[0], passed, 1, false);
	close(carrier[1]);
	// If close failed to detach carrier-storage -> passed[0], this reverse send
	// would still close a graph cycle through carrier[0] -> carrier-storage.
	expect_send(passed[0], carrier, 1, false);
	receive_rights(passed[1], 0, &received, 1, &payload);
	close(received);
	close_fds(passed, 2);
	close(carrier[0]);
}

static void test_listener_close_drains_preaccept_edges(void)
{
	int listener, client, passed[2], received;
	struct sockaddr_un address = { .sun_family = AF_UNIX };
	char payload;

	snprintf(address.sun_path, sizeof(address.sun_path),
		 "/tmp/b1-slice5-listener-%ld", (long)getpid());
	unlink(address.sun_path);
	listener = socket(AF_UNIX, SOCK_STREAM, 0);
	client = socket(AF_UNIX, SOCK_STREAM, 0);
	if (listener < 0 || client < 0 ||
	    bind(listener, (struct sockaddr *)&address, sizeof(address)) != 0 ||
	    listen(listener, 1) != 0 ||
	    connect(client, (struct sockaddr *)&address, sizeof(address)) !=
		    0 ||
	    socketpair(AF_UNIX, SOCK_STREAM, 0, passed) != 0)
		fail("preaccept setup failed: %s", strerror(errno));
	expect_send(client, passed, 1, false);
	close(listener);
	unlink(address.sun_path);
	// The queued server endpoint was never accepted. Listener close must detach
	// its queued edge before dropping the passed file, so the reverse ownership
	// direction is now legal.
	expect_send(passed[0], &client, 1, false);
	receive_rights(passed[1], 0, &received, 1, &payload);
	close(received);
	close_fds(passed, 2);
	close(client);
}

static void
test_preaccept_listener_self_cycle_and_address_reuse(int socket_type)
{
	struct sockaddr_un address = { .sun_family = AF_UNIX };
	int listener, client, replacement;
	socklen_t address_len;
#ifndef __asterinas__
	int accepted, received;
	char payload;
#endif

	int name_len = snprintf(address.sun_path + 1,
				sizeof(address.sun_path) - 1,
				"b1-slice5-listener-self-%ld-%d",
				(long)getpid(), socket_type);
	if (name_len < 0 || (size_t)name_len >= sizeof(address.sun_path) - 1)
		fail("listener-self abstract address is too long");
	address_len = offsetof(struct sockaddr_un, sun_path) + 1 + name_len;

	listener = socket(AF_UNIX, socket_type, 0);
	client = socket(AF_UNIX, socket_type, 0);
	if (listener < 0 || client < 0 ||
	    bind(listener, (struct sockaddr *)&address, address_len) != 0 ||
	    listen(listener, 1) != 0 ||
	    connect(client, (struct sockaddr *)&address, address_len) != 0) {
		fail("listener-self setup failed: %s", strerror(errno));
	}

	/*
	 * Before accept, the listener owns the pending server storage through its
	 * backlog. Queuing the listener in that storage would close a real ownership
	 * cycle and must be rejected by Asterinas B1 even for an otherwise acyclic
	 * stream send path. Linux accepts the cycle and is used only as the syscall
	 * shape oracle.
	 */
#ifdef __asterinas__
	expect_send(client, &listener, 1, true);
#else
	expect_send(client, &listener, 1, false);
	accepted = accept(listener, NULL, NULL);
	if (accepted < 0)
		fail("listener-self accept failed: %s", strerror(errno));
	receive_rights(accepted, 0, &received, 1, &payload);
	close(received);
	close(accepted);
#endif

	close(listener);
	close(client);

	// Listener drop must remove both the backlog table entry and its graph edge,
	// so the exact same abstract address is immediately reusable.
	replacement = socket(AF_UNIX, socket_type, 0);
	if (replacement < 0 ||
	    bind(replacement, (struct sockaddr *)&address, address_len) != 0 ||
	    listen(replacement, 1) != 0)
		fail("listener-self address was not reusable: %s",
		     strerror(errno));
	close(replacement);
}

static void test_fork_and_dup_identity(void)
{
	int carrier[2], passed[2];
	int duplicate, received;
	char payload;
	pid_t sender;

	if (socketpair(AF_UNIX, SOCK_STREAM, 0, carrier) != 0 ||
	    socketpair(AF_UNIX, SOCK_STREAM, 0, passed) != 0)
		fail("fork/dup setup failed: %s", strerror(errno));
	duplicate = dup(passed[0]);
	if (duplicate < 0)
		fail("dup failed: %s", strerror(errno));
	sender = fork();
	if (sender < 0)
		fail("fork/dup fork failed: %s", strerror(errno));
	if (sender == 0) {
		expect_send(carrier[0], &duplicate, 1, false);
		_exit(0);
	}
	receive_rights(carrier[1], 0, &received, 1, &payload);
	int status;
	if (waitpid(sender, &status, 0) != sender || !WIFEXITED(status) ||
	    WEXITSTATUS(status) != 0)
		fail("fork/dup sender failed with status %#x", status);
	verify_socket_fd(received, SOCK_STREAM, passed[1]);
	close(received);
	close(duplicate);
	close_fds(passed, 2);
	close_fds(carrier, 2);
}

static void test_empty_seqpacket(void)
{
	int carrier[2], passed[2];
	int received;
	char payload = 'q';

	if (socketpair(AF_UNIX, SOCK_SEQPACKET, 0, carrier) != 0 ||
	    socketpair(AF_UNIX, SOCK_STREAM, 0, passed) != 0)
		fail("empty-seqpacket setup failed: %s", strerror(errno));
	if (send_rights(carrier[0], passed, 1, &payload, 0, 0) != 0)
		fail("empty seqpacket SCM send did not return zero: %s",
		     strerror(errno));
	if (receive_rights(carrier[1], 0, &received, 1, &payload) != 0)
		fail("empty seqpacket SCM receive did not return zero");
	close(received);
	close_fds(passed, 2);
	close_fds(carrier, 2);
}

static void run_all_tests(void)
{
	test_unrelated_socket(SOCK_STREAM, SOCK_STREAM);
	test_unrelated_socket(SOCK_STREAM, SOCK_SEQPACKET);
	test_unrelated_socket(SOCK_SEQPACKET, SOCK_STREAM);
	test_pipe_and_regular_leaf();
	test_pidfd_leaf();
	test_slice5_closed_classes();
	test_self_cycle(SOCK_STREAM);
	test_self_cycle(SOCK_SEQPACKET);
	test_two_storage_cycle(SOCK_STREAM, SOCK_STREAM);
	test_two_storage_cycle(SOCK_STREAM, SOCK_SEQPACKET);
	test_long_cycle();
	test_duplicate_peek_then_consume();
	test_efault_then_retry();
	test_full_buffer_then_retry();
	test_full_seqpacket_then_retry();
	test_blocking_retry();
	test_receiver_close_drains_edges();
	test_listener_close_drains_preaccept_edges();
	test_preaccept_listener_self_cycle_and_address_reuse(SOCK_STREAM);
	test_preaccept_listener_self_cycle_and_address_reuse(SOCK_SEQPACKET);
	test_fork_and_dup_identity();
	test_empty_seqpacket();
}

int main(void)
{
#ifdef __asterinas__
	pid_t child = fork();
	if (child < 0)
		fail("privilege-test fork failed: %s", strerror(errno));
	if (child == 0) {
		if (setgid(65534) != 0 || setuid(65534) != 0)
			_exit(125);
		run_all_tests();
		_exit(0);
	}
	int status;
	if (waitpid(child, &status, 0) != child || !WIFEXITED(status) ||
	    WEXITSTATUS(status) != 0)
		fail("unprivileged SCM regression failed with status %#x",
		     status);
#else
	run_all_tests();
#endif
	fprintf(stderr, "B1_SLICE5_SCM_ACYCLIC_PASS\n");
	fflush(stderr);
	return EXIT_SUCCESS;
}
