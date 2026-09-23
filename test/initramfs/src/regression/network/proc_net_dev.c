// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE
#include <assert.h>
#include <ctype.h>
#include <errno.h>
#include <netinet/in.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <unistd.h>

#define NET_DEV_FIELDS 16

static int read_interface(const char *path, const char *name,
			  unsigned long long fields[NET_DEV_FIELDS])
{
	FILE *file = fopen(path, "r");
	assert(file != NULL);

	char line[4096];
	assert(fgets(line, sizeof(line), file) != NULL);
	assert(strncmp(line, "Inter-|", 7) == 0);
	assert(fgets(line, sizeof(line), file) != NULL);
	assert(strncmp(line, " face |", 7) == 0);

	int found = 0;
	while (fgets(line, sizeof(line), file) != NULL) {
		char *colon = strchr(line, ':');
		assert(colon != NULL);
		*colon = '\0';
		char *entry_name = line;
		while (isspace((unsigned char)*entry_name))
			entry_name++;
		if (strcmp(entry_name, name) != 0)
			continue;

		assert(found == 0);
		found = 1;
		char *next = colon + 1;
		for (int i = 0; i < NET_DEV_FIELDS; i++) {
			while (isspace((unsigned char)*next))
				next++;
			assert(isdigit((unsigned char)*next));
			errno = 0;
			fields[i] = strtoull(next, &next, 10);
			assert(errno == 0);
		}
		while (isspace((unsigned char)*next))
			next++;
		assert(*next == '\0');
	}
	assert(ferror(file) == 0);
	assert(fclose(file) == 0);
	return found;
}

static void send_loopback_packet(void)
{
	int receiver = socket(AF_INET, SOCK_DGRAM, 0);
	assert(receiver >= 0);
	struct sockaddr_in address = {
		.sin_family = AF_INET,
		.sin_port = 0,
		.sin_addr.s_addr = htonl(INADDR_LOOPBACK),
	};
	assert(bind(receiver, (struct sockaddr *)&address, sizeof(address)) ==
	       0);
	socklen_t address_length = sizeof(address);
	assert(getsockname(receiver, (struct sockaddr *)&address,
			   &address_length) == 0);

	const struct timeval timeout = { .tv_sec = 2 };
	assert(setsockopt(receiver, SOL_SOCKET, SO_RCVTIMEO, &timeout,
			  sizeof(timeout)) == 0);
	int sender = socket(AF_INET, SOCK_DGRAM, 0);
	assert(sender >= 0);
	const char payload[] = "proc-net-dev";
	assert(sendto(sender, payload, sizeof(payload), 0,
		      (struct sockaddr *)&address,
		      sizeof(address)) == (ssize_t)sizeof(payload));
	char received[sizeof(payload)];
	assert(recv(receiver, received, sizeof(received), 0) ==
	       (ssize_t)sizeof(payload));
	assert(memcmp(received, payload, sizeof(payload)) == 0);
	assert(close(sender) == 0);
	assert(close(receiver) == 0);
}

static void send_external_packet(void)
{
	int sender = socket(AF_INET, SOCK_DGRAM, 0);
	assert(sender >= 0);
	struct sockaddr_in gateway = {
		.sin_family = AF_INET,
		.sin_port = htons(9),
		.sin_addr.s_addr = htonl(0x0a000202),
	};
	const char payload[] = "proc-net-dev-eth0";
	assert(sendto(sender, payload, sizeof(payload), 0,
		      (struct sockaddr *)&gateway,
		      sizeof(gateway)) == (ssize_t)sizeof(payload));
	assert(close(sender) == 0);
}

int main(int argc, char **argv)
{
	assert(argc == 1 ||
	       (argc == 2 && strcmp(argv[1], "--expect-eth0") == 0));
	int expect_eth0 = argc == 2;
	char link[32];
	ssize_t length = readlink("/proc/net", link, sizeof(link));
	assert(length == (ssize_t)strlen("self/net"));
	assert(memcmp(link, "self/net", (size_t)length) == 0);

	unsigned long long before[NET_DEV_FIELDS];
	unsigned long long after[NET_DEV_FIELDS];
	assert(read_interface("/proc/net/dev", "lo", before));
	assert(read_interface("/proc/self/net/dev", "lo", after));
	assert(before[0] == after[0] && before[8] == after[8]);
	if (expect_eth0) {
		assert(read_interface("/proc/net/dev", "eth0", after));
		unsigned long long eth0_tx_bytes = after[8];
		unsigned long long eth0_tx_packets = after[9];
		// QEMU user networking uses 10.0.2.2 as the gateway.
		send_external_packet();
		assert(read_interface("/proc/net/dev", "eth0", after));
		assert(after[8] > eth0_tx_bytes);
		assert(after[9] > eth0_tx_packets);
	}

	send_loopback_packet();
	assert(read_interface("/proc/net/dev", "lo", after));
	assert(after[0] > before[0]);
	assert(after[1] > before[1]);
	assert(after[8] > before[8]);
	assert(after[9] > before[9]);

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
		unsigned long long fresh[NET_DEV_FIELDS];
		assert(read_interface("/proc/net/dev", "lo", fresh));
		assert(fresh[0] == 0 && fresh[1] == 0);
		assert(fresh[8] == 0 && fresh[9] == 0);
#ifdef __asterinas__
		assert(!read_interface("/proc/net/dev", "eth0", fresh));
#endif
		_exit(0);
	}
	int status;
	assert(waitpid(child, &status, 0) == child);
	assert(WIFEXITED(status) && WEXITSTATUS(status) == 0);
	puts("/proc/net/dev regression passed.");
	return 0;
}
