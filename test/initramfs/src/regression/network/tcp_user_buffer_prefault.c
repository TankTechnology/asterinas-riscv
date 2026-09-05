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

int main(void)
{
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
		if (connect(client_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0)
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
		munmap(send_buf, PAYLOAD_LEN);
		close(client_fd);
		_exit(EXIT_SUCCESS);
	}

	int server_fd = accept(listen_fd, NULL, NULL);
	if (server_fd < 0)
		fail("accept");
	struct timeval timeout = {.tv_sec = 5, .tv_usec = 0};
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
		if ((unsigned char)recv_buf[i] != (unsigned char)(i * 17U + 3U)) {
			fprintf(stderr, "payload mismatch at byte %zu\n", i);
			return EXIT_FAILURE;
		}
	}
	munmap(recv_buf, PAYLOAD_LEN);
	close(server_fd);
	close(listen_fd);
	int status;
	if (waitpid(child, &status, 0) != child || !WIFEXITED(status) ||
	    WEXITSTATUS(status) != EXIT_SUCCESS)
		return EXIT_FAILURE;
	puts("TCP user buffer prefault regression passed.");
	return EXIT_SUCCESS;
}
