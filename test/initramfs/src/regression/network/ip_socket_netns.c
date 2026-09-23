// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE
#include <assert.h>
#include <arpa/inet.h>
#include <errno.h>
#include <sched.h>
#include <stdio.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>

static struct sockaddr_in ipv4(unsigned int address, unsigned short port)
{
	return (struct sockaddr_in){
		.sin_family = AF_INET,
		.sin_port = htons(port),
		.sin_addr.s_addr = htonl(address),
	};
}

int main(void)
{
	int old_udp = socket(AF_INET, SOCK_DGRAM, 0);
	int old_tcp = socket(AF_INET, SOCK_STREAM, 0);
	int old_connect = socket(AF_INET, SOCK_DGRAM, 0);
	assert(old_udp >= 0 && old_tcp >= 0 && old_connect >= 0);

	pid_t child = fork();
	assert(child >= 0);
	if (child == 0) {
		if (unshare(CLONE_NEWNET) != 0) {
#ifndef __asterinas__
			if (errno == EPERM)
				_exit(0);
#endif
			_exit(2);
		}

		struct sockaddr_in eth0 = ipv4(0x0a00020f, 0);
		assert(bind(old_udp, (struct sockaddr *)&eth0, sizeof(eth0)) == 0);
		assert(bind(old_tcp, (struct sockaddr *)&eth0, sizeof(eth0)) == 0);

		int new_udp = socket(AF_INET, SOCK_DGRAM, 0);
		int new_tcp = socket(AF_INET, SOCK_STREAM, 0);
		assert(new_udp >= 0 && new_tcp >= 0);
		assert(bind(new_udp, (struct sockaddr *)&eth0, sizeof(eth0)) == -1);
		assert(errno == EADDRNOTAVAIL);
		assert(bind(new_tcp, (struct sockaddr *)&eth0, sizeof(eth0)) == -1);
		assert(errno == EADDRNOTAVAIL);

		struct sockaddr_in gateway = ipv4(0x0a000202, 9);
		assert(connect(old_connect, (struct sockaddr *)&gateway,
			       sizeof(gateway)) == 0);
		struct sockaddr_in local = { 0 };
		socklen_t local_len = sizeof(local);
		assert(getsockname(old_connect, (struct sockaddr *)&local,
				   &local_len) == 0);
		assert(local.sin_addr.s_addr == eth0.sin_addr.s_addr);
		assert(local.sin_port != 0);

		assert(close(new_udp) == 0);
		assert(close(new_tcp) == 0);
		assert(close(old_udp) == 0);
		assert(close(old_tcp) == 0);
		assert(close(old_connect) == 0);
		_exit(0);
	}

	int status;
	assert(waitpid(child, &status, 0) == child);
	assert(WIFEXITED(status) && WEXITSTATUS(status) == 0);
	assert(close(old_udp) == 0);
	assert(close(old_tcp) == 0);
	assert(close(old_connect) == 0);
	puts("IP socket namespace regression passed.");
	return 0;
}
