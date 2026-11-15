// SPDX-License-Identifier: MPL-2.0

#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/socket.h>
#include <sys/uio.h>
#include <sys/wait.h>
#include <unistd.h>

static void check_empty_receive(int socket_fd)
{
	char buffer[8];
	errno = 0;
	ssize_t received = recvfrom(socket_fd, buffer, sizeof(buffer),
				    MSG_DONTWAIT, NULL, NULL);
	if (received != -1 || errno != EAGAIN) {
		fprintf(stderr, "UDP recvfrom MSG_DONTWAIT: result=%zd errno=%d\n",
			received, errno);
		_exit(EXIT_FAILURE);
	}

	struct iovec iov = { .iov_base = buffer, .iov_len = sizeof(buffer) };
	struct msghdr message = { .msg_iov = &iov, .msg_iovlen = 1 };
	errno = 0;
	received = recvmsg(socket_fd, &message, MSG_DONTWAIT);
	if (received != -1 || errno != EAGAIN) {
		fprintf(stderr, "UDP recvmsg MSG_DONTWAIT: result=%zd errno=%d\n",
			received, errno);
		_exit(EXIT_FAILURE);
	}
	_exit(EXIT_SUCCESS);
}

int main(void)
{
	alarm(5);
	int socket_fd = socket(AF_INET, SOCK_DGRAM, 0);
	if (socket_fd < 0) {
		perror("socket");
		return EXIT_FAILURE;
	}
	struct sockaddr_in address = {
		.sin_family = AF_INET,
		.sin_addr.s_addr = htonl(INADDR_LOOPBACK),
	};
	if (bind(socket_fd, (struct sockaddr *)&address, sizeof(address)) < 0) {
		perror("bind");
		return EXIT_FAILURE;
	}

	pid_t child = fork();
	if (child < 0) {
		perror("fork");
		return EXIT_FAILURE;
	}
	if (child == 0) {
		alarm(2);
		check_empty_receive(socket_fd);
	}

	int status;
	if (waitpid(child, &status, 0) != child) {
		perror("waitpid");
		return EXIT_FAILURE;
	}
	close(socket_fd);
	if (!WIFEXITED(status) || WEXITSTATUS(status) != EXIT_SUCCESS) {
		fprintf(stderr, "UDP MSG_DONTWAIT child status=%d\n", status);
		return EXIT_FAILURE;
	}
	alarm(0);
	puts("UDP MSG_DONTWAIT regression passed.");
	return EXIT_SUCCESS;
}
