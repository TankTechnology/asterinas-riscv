// SPDX-License-Identifier: MPL-2.0

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/uio.h>
#include <sys/wait.h>
#include <unistd.h>

#define MAX_FILLED_BYTES (8 * 1024 * 1024)

static void fail(const char *operation)
{
	perror(operation);
	exit(EXIT_FAILURE);
}

static void fill_send_buffer(int sender, const char *payload, size_t size)
{
	int flags = fcntl(sender, F_GETFL);
	if (flags < 0 || fcntl(sender, F_SETFL, flags | O_NONBLOCK) < 0)
		fail("fcntl nonblocking");

	size_t total = 0;
	for (;;) {
		ssize_t sent = send(sender, payload, size, MSG_NOSIGNAL);
		if (sent > 0) {
			total += (size_t)sent;
			if (total > MAX_FILLED_BYTES) {
				fprintf(stderr, "TCP send buffer did not fill\n");
				exit(EXIT_FAILURE);
			}
			continue;
		}
		if (sent == -1 && errno == EAGAIN)
			break;
		fail("fill TCP send buffer");
	}

	if (fcntl(sender, F_SETFL, flags) < 0)
		fail("fcntl blocking");
}

static void check_per_call_nonblocking(int sender, const char *payload,
				      size_t size)
{
	struct iovec iov = { .iov_base = (void *)payload, .iov_len = size };
	struct msghdr message = { .msg_iov = &iov, .msg_iovlen = 1 };
	size_t total = 0;
	for (;;) {
		ssize_t sent = sendmsg(sender, &message,
				       MSG_DONTWAIT | MSG_NOSIGNAL);
		if (sent > 0) {
			total += (size_t)sent;
			if (total > MAX_FILLED_BYTES) {
				fprintf(stderr, "TCP MSG_DONTWAIT never returned EAGAIN\n");
				_exit(EXIT_FAILURE);
			}
			continue;
		}
		if (sent == -1 && errno == EAGAIN)
			_exit(EXIT_SUCCESS);
		fprintf(stderr, "TCP MSG_DONTWAIT: result=%zd errno=%d\n",
			sent, errno);
		_exit(EXIT_FAILURE);
	}
}

int main(void)
{
	alarm(20);
	struct sockaddr_in address = {
		.sin_family = AF_INET,
		.sin_addr.s_addr = htonl(INADDR_LOOPBACK),
	};
	socklen_t address_len = sizeof(address);
	int listener = socket(AF_INET, SOCK_STREAM, 0);
	if (listener < 0 ||
	    bind(listener, (struct sockaddr *)&address, address_len) < 0 ||
	    getsockname(listener, (struct sockaddr *)&address, &address_len) < 0 ||
	    listen(listener, 1) < 0)
		fail("prepare TCP listener");
	int sender = socket(AF_INET, SOCK_STREAM, 0);
	if (sender < 0 ||
	    connect(sender, (struct sockaddr *)&address, address_len) < 0)
		fail("connect TCP sender");
	int receiver = accept(listener, NULL, NULL);
	if (receiver < 0)
		fail("accept TCP receiver");
	close(listener);

	char payload[4096];
	memset(payload, 'x', sizeof(payload));
	fill_send_buffer(sender, payload, sizeof(payload));

	pid_t child = fork();
	if (child < 0)
		fail("fork");
	if (child == 0) {
		alarm(4);
		check_per_call_nonblocking(sender, payload, sizeof(payload));
	}
	int status;
	if (waitpid(child, &status, 0) != child)
		fail("waitpid");
	close(sender);
	close(receiver);
	if (!WIFEXITED(status) || WEXITSTATUS(status) != EXIT_SUCCESS) {
		fprintf(stderr, "TCP MSG_DONTWAIT child status=%d\n", status);
		return EXIT_FAILURE;
	}
	alarm(0);
	puts("TCP MSG_DONTWAIT send regression passed.");
	return EXIT_SUCCESS;
}
