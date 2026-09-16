// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define ARRAY_SIZE(array) (sizeof(array) / sizeof((array)[0]))
#define ITERATIONS 8
#define IO_TIMEOUT_MS 30000
#define SEND_AFTER_ARM_NS (20 * 1000 * 1000)

static const size_t request_lengths[] = { 92, 40, 134, 1176 };
static const size_t response_lengths[] = { 739, 51, 25, 0 };

static void fail(const char *what)
{
	perror(what);
	exit(EXIT_FAILURE);
}

static int64_t monotonic_ns(void)
{
	struct timespec now;
	if (clock_gettime(CLOCK_MONOTONIC, &now) < 0)
		fail("clock_gettime");
	return (int64_t)now.tv_sec * 1000000000 + now.tv_nsec;
}

static void fill_pattern(uint8_t *buffer, size_t length, size_t iteration,
			 size_t round, uint8_t direction)
{
	for (size_t i = 0; i < length; i++)
		buffer[i] = (uint8_t)(i * 17 + iteration * 31 + round * 47 +
				      direction);
}

static int write_all(int fd, const void *buffer, size_t length)
{
	const uint8_t *bytes = buffer;
	size_t written = 0;

	while (written < length) {
		ssize_t count = write(fd, bytes + written, length - written);
		if (count < 0 && errno == EINTR)
			continue;
		if (count <= 0)
			return -1;
		written += (size_t)count;
	}
	return 0;
}

static int poll_readable(int fd)
{
	struct pollfd poll_fd = { .fd = fd, .events = POLLIN };
	int result;

	do {
		result = poll(&poll_fd, 1, IO_TIMEOUT_MS);
	} while (result < 0 && errno == EINTR);
	return result == 1 && (poll_fd.revents & POLLIN) != 0;
}

static int recv_exact_bounded(int fd, void *buffer, size_t length)
{
	uint8_t *bytes = buffer;
	size_t received = 0;

	while (received < length) {
		if (!poll_readable(fd))
			return -1;
		ssize_t count =
			recv(fd, bytes + received, length - received, 0);
		if (count < 0 && errno == EINTR)
			continue;
		if (count <= 0)
			return -1;
		received += (size_t)count;
	}
	return 0;
}

static void server_report(int status_fd, char status, const char *message)
{
	if (message != NULL)
		fprintf(stderr, "TCP_PPOLL server_failure=%s errno=%d\n",
			message, errno);
	(void)write_all(status_fd, &status, 1);
}

static void run_server(int socket_fd, int armed_fd, int control_fd,
		       int inert_fd, int status_fd, const char *case_name)
{
	uint8_t buffer[1176];
	uint8_t expected[1176];
	struct timeval timeout = { .tv_sec = IO_TIMEOUT_MS / 1000 };

	if (setsockopt(socket_fd, SOL_SOCKET, SO_RCVTIMEO, &timeout,
		       sizeof(timeout)) < 0) {
		server_report(status_fd, 'F', "setsockopt(SO_RCVTIMEO)");
		_exit(EXIT_FAILURE);
	}

	/*
	 * Keep the accepted socket blocking for this first probe. MSG_DONTWAIT
	 * itself must make the operation non-blocking, independently of O_NONBLOCK.
	 */
	uint8_t peek;
	int64_t probe_started_ns = monotonic_ns();
	ssize_t peeked =
		recv(socket_fd, &peek, sizeof(peek), MSG_PEEK | MSG_DONTWAIT);
	int64_t probe_elapsed_us = (monotonic_ns() - probe_started_ns) / 1000;
	printf("TCP_PPOLL case=%s stage=msg-dontwait-probe result=%zd "
	       "errno=%d elapsed_us=%lld\n",
	       case_name, peeked, errno, (long long)probe_elapsed_us);
	if (peeked >= 0 || errno != EAGAIN) {
		errno = peeked >= 0 ? EBUSY : errno;
		server_report(status_fd, 'F',
			      "MSG_DONTWAIT blocked or misreported");
		_exit(EXIT_FAILURE);
	}

	int status_flags = fcntl(socket_fd, F_GETFL);
	if (status_flags < 0 ||
	    fcntl(socket_fd, F_SETFL, status_flags | O_NONBLOCK) < 0) {
		server_report(status_fd, 'F', "set O_NONBLOCK");
		_exit(EXIT_FAILURE);
	}

	for (size_t iteration = 0; iteration < ITERATIONS; iteration++) {
		for (size_t round = 0; round < ARRAY_SIZE(request_lengths);
		     round++) {
			struct pollfd poll_fds[] = {
				{ .fd = socket_fd, .events = POLLIN },
				{ .fd = control_fd, .events = POLLIN },
				{ .fd = inert_fd, .events = POLLIN },
			};
			char armed = 'A';
			peeked = recv(socket_fd, &peek, sizeof(peek), MSG_PEEK);
			if (peeked >= 0 || errno != EAGAIN) {
				errno = peeked >= 0 ? EBUSY : errno;
				server_report(
					status_fd, 'F',
					"socket not drained before ppoll");
				_exit(EXIT_FAILURE);
			}
			if (write_all(armed_fd, &armed, 1) < 0) {
				server_report(status_fd, 'F',
					      "write armed token");
				_exit(EXIT_FAILURE);
			}

			int64_t started_ns = monotonic_ns();
			int result;
			do {
				result = ppoll(poll_fds, ARRAY_SIZE(poll_fds),
					       NULL, NULL);
			} while (result < 0 && errno == EINTR);
			int64_t elapsed_us =
				(monotonic_ns() - started_ns) / 1000;
			printf("TCP_PPOLL case=%s iteration=%zu round=%zu "
			       "request=%zu result=%d socket_revents=0x%x "
			       "control_revents=0x%x inert_revents=0x%x "
			       "elapsed_us=%lld\n",
			       case_name, iteration, round,
			       request_lengths[round], result,
			       poll_fds[0].revents, poll_fds[1].revents,
			       poll_fds[2].revents, (long long)elapsed_us);

			if (result != 1 || poll_fds[0].revents != POLLIN ||
			    poll_fds[1].revents != 0 ||
			    poll_fds[2].revents != 0) {
				errno = ETIMEDOUT;
				server_report(
					status_fd, 'F',
					"ppoll did not report only TCP readability");
				_exit(EXIT_FAILURE);
			}

			if (recv_exact_bounded(socket_fd, buffer,
					       request_lengths[round]) < 0) {
				server_report(status_fd, 'F', "recv request");
				_exit(EXIT_FAILURE);
			}
			fill_pattern(expected, request_lengths[round],
				     iteration, round, 0x11);
			if (memcmp(buffer, expected, request_lengths[round]) !=
			    0) {
				errno = EBADMSG;
				server_report(status_fd, 'F',
					      "request mismatch");
				_exit(EXIT_FAILURE);
			}

			if (response_lengths[round] != 0) {
				fill_pattern(buffer, response_lengths[round],
					     iteration, round, 0x77);
				if (write_all(socket_fd, buffer,
					      response_lengths[round]) < 0) {
					server_report(status_fd, 'F',
						      "send response");
					_exit(EXIT_FAILURE);
				}
			}
		}
	}

	server_report(status_fd, 'P', NULL);
	_exit(EXIT_SUCCESS);
}

static int run_case(const char *case_name, int use_tcp_nodelay)
{
	struct sockaddr_in address = {
		.sin_family = AF_INET,
		.sin_addr.s_addr = htonl(INADDR_LOOPBACK),
		.sin_port = htons(2828 + use_tcp_nodelay),
	};
	socklen_t address_length = sizeof(address);
	int armed_pipe[2];
	int control_pipe[2];
	int inert_pipe[2];
	int status_pipe[2];
	uint8_t buffer[1176];
	uint8_t expected[1176];

	int listen_fd = socket(AF_INET, SOCK_STREAM, 0);
	if (listen_fd < 0)
		fail("socket(listener)");
	if (bind(listen_fd, (struct sockaddr *)&address, sizeof(address)) < 0)
		fail("bind");
	if (getsockname(listen_fd, (struct sockaddr *)&address,
			&address_length) < 0)
		fail("getsockname");
	if (listen(listen_fd, 1) < 0)
		fail("listen");

	int client_fd = socket(AF_INET, SOCK_STREAM, 0);
	if (client_fd < 0)
		fail("socket(client)");
	if (use_tcp_nodelay) {
		int enabled = 1;
		if (setsockopt(client_fd, IPPROTO_TCP, TCP_NODELAY, &enabled,
			       sizeof(enabled)) < 0)
			fail("setsockopt(TCP_NODELAY)");
	}
	if (connect(client_fd, (struct sockaddr *)&address, sizeof(address)) <
	    0)
		fail("connect");
	int server_fd = accept(listen_fd, NULL, NULL);
	if (server_fd < 0)
		fail("accept");

	if (pipe(armed_pipe) < 0 || pipe(control_pipe) < 0 ||
	    pipe(inert_pipe) < 0 || pipe(status_pipe) < 0)
		fail("pipe");
	pid_t child = fork();
	if (child < 0)
		fail("fork");
	if (child == 0) {
		close(listen_fd);
		close(client_fd);
		close(armed_pipe[0]);
		close(control_pipe[1]);
		close(inert_pipe[1]);
		close(status_pipe[0]);
		run_server(server_fd, armed_pipe[1], control_pipe[0],
			   inert_pipe[0], status_pipe[1], case_name);
	}

	close(listen_fd);
	close(server_fd);
	close(armed_pipe[1]);
	close(control_pipe[0]);
	close(inert_pipe[0]);
	close(status_pipe[1]);

	for (size_t iteration = 0; iteration < ITERATIONS; iteration++) {
		for (size_t round = 0; round < ARRAY_SIZE(request_lengths);
		     round++) {
			char armed;
			if (!poll_readable(armed_pipe[0]) ||
			    read(armed_pipe[0], &armed, 1) != 1 ||
			    armed != 'A') {
				fprintf(stderr,
					"TCP_PPOLL client_failure=wait_for_arm "
					"case=%s iteration=%zu round=%zu\n",
					case_name, iteration, round);
				goto failure;
			}
			struct timespec delay = { .tv_nsec =
							  SEND_AFTER_ARM_NS };
			while (nanosleep(&delay, &delay) < 0 &&
			       errno == EINTR) {
			}

			fill_pattern(buffer, request_lengths[round], iteration,
				     round, 0x11);
			if (write_all(client_fd, buffer,
				      request_lengths[round]) < 0) {
				fprintf(stderr,
					"TCP_PPOLL client_failure=send case=%s "
					"iteration=%zu round=%zu errno=%d\n",
					case_name, iteration, round, errno);
				goto failure;
			}

			if (response_lengths[round] != 0) {
				if (recv_exact_bounded(
					    client_fd, buffer,
					    response_lengths[round]) < 0) {
					fprintf(stderr,
						"TCP_PPOLL client_failure=response "
						"case=%s iteration=%zu round=%zu "
						"errno=%d\n",
						case_name, iteration, round,
						errno);
					goto failure;
				}
				fill_pattern(expected, response_lengths[round],
					     iteration, round, 0x77);
				if (memcmp(buffer, expected,
					   response_lengths[round]) != 0) {
					fprintf(stderr,
						"TCP_PPOLL client_failure=response_mismatch "
						"case=%s iteration=%zu round=%zu\n",
						case_name, iteration, round);
					goto failure;
				}
			}
		}
	}

	if (poll_readable(status_pipe[0])) {
		char status;
		if (read(status_pipe[0], &status, 1) == 1 && status == 'P') {
			int child_status;
			if (waitpid(child, &child_status, 0) == child &&
			    WIFEXITED(child_status) &&
			    WEXITSTATUS(child_status) == EXIT_SUCCESS) {
				printf("TCP_PPOLL case=%s result=PASS\n",
				       case_name);
				close(client_fd);
				close(armed_pipe[0]);
				close(control_pipe[1]);
				close(inert_pipe[1]);
				close(status_pipe[0]);
				return 0;
			}
		}
	}

failure:
	(void)write_all(control_pipe[1], "X", 1);
	(void)kill(child, SIGKILL);
	(void)waitpid(child, NULL, 0);
	close(client_fd);
	close(armed_pipe[0]);
	close(control_pipe[1]);
	close(inert_pipe[1]);
	close(status_pipe[0]);
	return -1;
}

int main(void)
{
	setvbuf(stdout, NULL, _IONBF, 0);
	setvbuf(stderr, NULL, _IONBF, 0);
	if (run_case("nagle-default", 0) < 0)
		return EXIT_FAILURE;
	if (run_case("tcp-nodelay", 1) < 0)
		return EXIT_FAILURE;
	puts("TCP ppoll wakeup regression passed.");
	return EXIT_SUCCESS;
}
