// SPDX-License-Identifier: MPL-2.0

#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/uio.h>
#include <sys/wait.h>
#include <unistd.h>

#define PAYLOAD_LEN (4 * 4096 + 137)
#define SEND_CHUNK 257
#define RECV_CHUNK 113
#define LARGE_BUFFER_LEN (10 * 1024 * 1024)
#define TCP_RECEIVE_CAPACITY (128 * 1024)
#define VALID_PREFIX_LEN 4096
#define CROSS_FAULT_LEN (VALID_PREFIX_LEN + 904)

static void fail(const char *what)
{
	perror(what);
	exit(EXIT_FAILURE);
}

static void send_all(int fd, const char *buf, size_t len)
{
	size_t sent = 0;
	while (sent < len) {
		ssize_t n = send(fd, buf + sent, len - sent, 0);
		if (n < 0) {
			fail("send");
		}
		sent += (size_t)n;
	}
}

static void wait_for_payload(int fd)
{
	char peek_buffer[CROSS_FAULT_LEN];
	for (int attempt = 0; attempt < 2000; attempt++) {
		ssize_t n = recv(fd, peek_buffer, sizeof(peek_buffer),
				 MSG_PEEK | MSG_DONTWAIT);
		if (n == sizeof(peek_buffer))
			return;
		if (n < 0 && errno != EAGAIN)
			fail("peek cross-fault payload");
		usleep(1000);
	}
	fprintf(stderr, "cross-fault payload did not arrive\n");
	exit(EXIT_FAILURE);
}

static void test_unix_stream_partial_sendmsg(void)
{
	int sockets[2];
	char good_buffer[] = "unix";
	char received[sizeof(good_buffer) - 1];
	struct iovec iov[2] = {
		{ .iov_base = good_buffer, .iov_len = sizeof(good_buffer) - 1 },
		{ .iov_base = (void *)1, .iov_len = 1 },
	};
	struct msghdr message = {
		.msg_iov = iov,
		.msg_iovlen = 2,
	};

	if (socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) < 0)
		fail("socketpair");
	if (sendmsg(sockets[0], &message, 0) !=
	    (ssize_t)(sizeof(good_buffer) - 1))
		fail("unix stream sendmsg partial iovec");
	if (recv(sockets[1], received, sizeof(received), 0) !=
		    (ssize_t)sizeof(received) ||
	    memcmp(received, good_buffer, sizeof(received)) != 0) {
		fprintf(stderr, "unix stream partial iovec payload mismatch\n");
		exit(EXIT_FAILURE);
	}
	close(sockets[0]);
	close(sockets[1]);
}

int main(void)
{
	alarm(10);
	test_unix_stream_partial_sendmsg();

	struct sockaddr_in addr = {
		.sin_family = AF_INET,
		.sin_addr.s_addr = htonl(INADDR_LOOPBACK),
	};
	socklen_t addr_len = sizeof(addr);
	int listen_fd = socket(AF_INET, SOCK_STREAM, 0);
	if (listen_fd < 0)
		fail("socket");
	if (bind(listen_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0)
		fail("bind");
	if (getsockname(listen_fd, (struct sockaddr *)&addr, &addr_len) < 0)
		fail("getsockname");
	if (listen(listen_fd, 1) < 0)
		fail("listen");

	pid_t child = fork();
	if (child < 0)
		fail("fork");
	if (child == 0) {
		int client_fd = socket(AF_INET, SOCK_STREAM, 0);
		if (client_fd < 0)
			fail("child socket");
		close(listen_fd);
		if (connect(client_fd, (struct sockaddr *)&addr, sizeof(addr)) <
		    0)
			fail("connect");

		char *send_buf = mmap(NULL, PAYLOAD_LEN, PROT_READ | PROT_WRITE,
				      MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
		if (send_buf == MAP_FAILED)
			fail("child mmap");
		for (size_t i = 0; i < PAYLOAD_LEN; i++)
			send_buf[i] = (char)(i * 17U + 3U);
		for (size_t off = 0; off < PAYLOAD_LEN; off += SEND_CHUNK) {
			size_t len = PAYLOAD_LEN - off;
			if (len > SEND_CHUNK)
				len = SEND_CHUNK;
			send_all(client_fd, send_buf + off, len);
		}

		char partial[3];
		size_t received = 0;
		while (received < sizeof(partial)) {
			ssize_t n = recv(client_fd, partial + received,
					 sizeof(partial) - received, 0);
			if (n <= 0)
				fail("child recv partial iovec");
			received += (size_t)n;
		}
		if (memcmp(partial, "abc", sizeof(partial)) != 0) {
			fprintf(stderr, "partial iovec payload mismatch\n");
			return EXIT_FAILURE;
		}
		send_all(client_fd, "xyz", 3);
		for (int operation = 0; operation < 6; operation++) {
			char request;
			if (recv(client_fd, &request, 1, MSG_WAITALL) != 1 ||
			    request != 'A' + operation)
				fail("child large-buffer request");
			send_all(client_fd, "q", 1);
		}
		for (int operation = 0; operation < 3; operation++) {
			char request;
			if (recv(client_fd, &request, 1, MSG_WAITALL) != 1 ||
			    request != 'a' + operation)
				fail("child cross-fault request");
			char cross_data[CROSS_FAULT_LEN];
			memset(cross_data, 'a' + operation, sizeof(cross_data));
			send_all(client_fd, cross_data, sizeof(cross_data));
		}

		munmap(send_buf, PAYLOAD_LEN);
		close(client_fd);
		_exit(EXIT_SUCCESS);
	}

	int server_fd = accept(listen_fd, NULL, NULL);
	if (server_fd < 0)
		fail("accept");
	struct timeval timeout = { .tv_sec = 5, .tv_usec = 0 };
	if (setsockopt(server_fd, SOL_SOCKET, SO_RCVTIMEO, &timeout,
		       sizeof(timeout)) < 0)
		fail("setsockopt");

	/* Keep this anonymous writable mapping untouched until recv() writes it. */
	char *recv_buf = mmap(NULL, PAYLOAD_LEN, PROT_READ | PROT_WRITE,
			      MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (recv_buf == MAP_FAILED)
		fail("parent mmap");
	size_t received = 0;
	while (received < PAYLOAD_LEN) {
		size_t len = PAYLOAD_LEN - received;
		if (len > RECV_CHUNK)
			len = RECV_CHUNK;
		ssize_t n = recv(server_fd, recv_buf + received, len, 0);
		if (n <= 0)
			fail("recv");
		received += (size_t)n;
	}

	for (size_t i = 0; i < PAYLOAD_LEN; i++) {
		if ((unsigned char)recv_buf[i] !=
		    (unsigned char)(i * 17U + 3U)) {
			fprintf(stderr, "payload mismatch at byte %zu\n", i);
			return EXIT_FAILURE;
		}
	}

	char good_buffer[] = "abc";
	struct iovec iov[2] = {
		{ .iov_base = good_buffer, .iov_len = sizeof(good_buffer) - 1 },
		{ .iov_base = (void *)1, .iov_len = 1 },
	};
	struct msghdr message = {
		.msg_iov = iov,
		.msg_iovlen = 2,
	};
	if (sendmsg(server_fd, &message, 0) !=
	    (ssize_t)(sizeof(good_buffer) - 1))
		fail("sendmsg partial iovec");

	char receive_prefix = 0;
	iov[0].iov_base = &receive_prefix;
	iov[0].iov_len = 1;
	errno = 0;
	if (recvmsg(server_fd, &message, 0) != -1 || errno != EFAULT ||
	    receive_prefix != 'x')
		fail("recvmsg partial iovec EFAULT");
	char receive_suffix[3];
	size_t suffix_received = 0;
	while (suffix_received < sizeof(receive_suffix)) {
		ssize_t n = recv(server_fd, receive_suffix + suffix_received,
				 sizeof(receive_suffix) - suffix_received, 0);
		if (n <= 0)
			fail("recv partial iovec suffix");
		suffix_received += (size_t)n;
	}
	if (memcmp(receive_suffix, "xyz", sizeof(receive_suffix)) != 0) {
		fprintf(stderr, "partial iovec retry payload mismatch\n");
		return EXIT_FAILURE;
	}

	/* A single receive cannot reach the inaccessible tail of this mapping. */
	char *large_buffer = mmap(NULL, LARGE_BUFFER_LEN,
				  PROT_READ | PROT_WRITE,
				  MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (large_buffer == MAP_FAILED)
		fail("large-buffer mmap");
	if (mprotect(large_buffer + TCP_RECEIVE_CAPACITY,
		     LARGE_BUFFER_LEN - TCP_RECEIVE_CAPACITY, PROT_NONE) < 0)
		fail("large-buffer mprotect");

	int large_buffer_failures = 0;
	for (int operation = 0; operation < 6; operation++) {
		if (operation == 3 &&
		    mprotect(large_buffer + VALID_PREFIX_LEN,
			     TCP_RECEIVE_CAPACITY - VALID_PREFIX_LEN,
			     PROT_NONE) < 0)
			fail("valid-prefix mprotect");
		char request = 'A' + operation;
		send_all(server_fd, &request, 1);
		errno = 0;
		ssize_t n;
		if (operation % 3 == 0) {
			n = recvfrom(server_fd, large_buffer, LARGE_BUFFER_LEN,
				     0, NULL, NULL);
		} else if (operation % 3 == 1) {
			struct iovec large_iov = {
				.iov_base = large_buffer,
				.iov_len = LARGE_BUFFER_LEN,
			};
			struct msghdr large_message = {
				.msg_iov = &large_iov,
				.msg_iovlen = 1,
			};
			n = recvmsg(server_fd, &large_message, 0);
		} else {
			n = read(server_fd, large_buffer, LARGE_BUFFER_LEN);
		}
		if (n != 1 || large_buffer[0] != 'q') {
			fprintf(stderr,
				"TCP large-buffer operation %d: received=%zd errno=%d\n",
				operation, n, errno);
			large_buffer_failures++;
		}
	}
	if (large_buffer_failures)
		return EXIT_FAILURE;

	for (int operation = 0; operation < 3; operation++) {
		char request = 'a' + operation;
		send_all(server_fd, &request, 1);
		wait_for_payload(server_fd);

		errno = 0;
		ssize_t n;
		if (operation == 0) {
			n = recvfrom(server_fd, large_buffer, LARGE_BUFFER_LEN,
				     0, NULL, NULL);
		} else if (operation == 1) {
			struct iovec cross_iov = {
				.iov_base = large_buffer,
				.iov_len = LARGE_BUFFER_LEN,
			};
			struct msghdr cross_message = {
				.msg_iov = &cross_iov,
				.msg_iovlen = 1,
			};
			n = recvmsg(server_fd, &cross_message, 0);
		} else {
			n = read(server_fd, large_buffer, LARGE_BUFFER_LEN);
		}
		if (n != -1 || errno != EFAULT) {
			fprintf(stderr,
				"TCP cross-fault operation %d: received=%zd errno=%d\n",
				operation, n, errno);
			return EXIT_FAILURE;
		}

		char valid_buffer[CROSS_FAULT_LEN];
		size_t recovered = 0;
		while (recovered < sizeof(valid_buffer)) {
			n = recv(server_fd, valid_buffer + recovered,
				 sizeof(valid_buffer) - recovered, 0);
			if (n <= 0)
				fail("recover cross-fault payload");
			recovered += (size_t)n;
		}
		for (size_t i = 0; i < sizeof(valid_buffer); i++) {
			if (valid_buffer[i] != 'a' + operation) {
				fprintf(stderr,
					"cross-fault payload mismatch at %zu\n",
					i);
				return EXIT_FAILURE;
			}
		}
	}
	munmap(large_buffer, LARGE_BUFFER_LEN);

	munmap(recv_buf, PAYLOAD_LEN);
	close(server_fd);
	close(listen_fd);
	int status;
	if (waitpid(child, &status, 0) != child || !WIFEXITED(status) ||
	    WEXITSTATUS(status) != EXIT_SUCCESS)
		return EXIT_FAILURE;
	alarm(0);
	puts("TCP user buffer prefault regression passed.");
	return EXIT_SUCCESS;
}
