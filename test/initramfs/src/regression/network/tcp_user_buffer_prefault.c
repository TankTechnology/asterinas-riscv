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
#include <sys/wait.h>
#include <unistd.h>

#define PAYLOAD_LEN (4 * 4096 + 137)
#define SEND_CHUNK 257
#define RECV_CHUNK 113

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
	if (recvmsg(server_fd, &message, 0) != 1 || receive_prefix != 'x')
		fail("recvmsg partial iovec");
	char receive_suffix[2];
	size_t suffix_received = 0;
	while (suffix_received < sizeof(receive_suffix)) {
		ssize_t n = recv(server_fd, receive_suffix + suffix_received,
				 sizeof(receive_suffix) - suffix_received, 0);
		if (n <= 0)
			fail("recv partial iovec suffix");
		suffix_received += (size_t)n;
	}
	if (memcmp(receive_suffix, "yz", sizeof(receive_suffix)) != 0) {
		fprintf(stderr, "partial iovec receive suffix mismatch\n");
		return EXIT_FAILURE;
	}

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
