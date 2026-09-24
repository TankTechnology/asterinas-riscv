// SPDX-License-Identifier: MPL-2.0

#include <arpa/inet.h>
#include <assert.h>
#include <errno.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/uio.h>
#include <unistd.h>

#define PAYLOAD_LEN (4 * 4096 + 137)
#define LARGE_BUFFER_LEN (10 * 1024 * 1024)
#define UDP_RECEIVE_CAPACITY 65536
#define VALID_PREFIX_LEN 4096

static void test_writev_datagram_boundary(void)
{
	int receiver = socket(AF_INET, SOCK_DGRAM | SOCK_NONBLOCK, 0);
	int sender = socket(AF_INET, SOCK_DGRAM, 0);
	assert(receiver >= 0 && sender >= 0);
	struct sockaddr_in address = {
		.sin_family = AF_INET,
		.sin_addr.s_addr = htonl(INADDR_LOOPBACK),
	};
	assert(bind(receiver, (struct sockaddr *)&address, sizeof(address)) == 0);
	socklen_t address_length = sizeof(address);
	assert(getsockname(receiver, (struct sockaddr *)&address,
			   &address_length) == 0);
	assert(connect(sender, (struct sockaddr *)&address,
		       sizeof(address)) == 0);

	struct iovec split[] = {
		{ .iov_base = "ab", .iov_len = 2 },
		{ .iov_base = "cd", .iov_len = 2 },
	};
	errno = 0;
	ssize_t sent = writev(sender, split, 2);
	int send_errno = errno;
	char received[8];
	ssize_t length = -1;
	for (int attempt = 0; attempt < 1000; attempt++) {
		length = recv(receiver, received, sizeof(received), MSG_DONTWAIT);
		if (length >= 0 || errno != EAGAIN)
			break;
		usleep(1000);
	}
	if (sent != 4 || length != 4 || memcmp(received, "abcd", 4)) {
		fprintf(stderr,
			"UDP writev split datagram: sent=%zd send_errno=%d received=%zd recv_errno=%d\n",
			sent, send_errno, length, errno);
		exit(EXIT_FAILURE);
	}
	errno = 0;
	assert(recv(receiver, received, sizeof(received), MSG_DONTWAIT) == -1);
	assert(errno == EAGAIN);

	struct iovec fault[] = {
		{ .iov_base = "XY", .iov_len = 2 },
		{ .iov_base = (void *)1, .iov_len = 1 },
	};
	errno = 0;
	sent = writev(sender, fault, 2);
	send_errno = errno;
	errno = 0;
	length = recv(receiver, received, sizeof(received), MSG_DONTWAIT);
	if (sent != -1 || send_errno != EFAULT || length != -1 ||
	    errno != EAGAIN) {
		fprintf(stderr,
			"UDP writev invalid tail: sent=%zd errno=%d received=%zd recv_errno=%d\n",
			sent, send_errno, length, errno);
		exit(EXIT_FAILURE);
	}
	int unconnected = socket(AF_INET, SOCK_DGRAM, 0);
	assert(unconnected >= 0);
	errno = 0;
	if (writev(unconnected, fault, 2) != -1 || errno != EDESTADDRREQ) {
		fprintf(stderr, "UDP writev unconnected error: errno=%d\n", errno);
		exit(EXIT_FAILURE);
	}

	close(unconnected);
	close(sender);
	close(receiver);
}

int main(void)
{
	alarm(10);
	test_writev_datagram_boundary();

	int receiver = socket(AF_INET, SOCK_DGRAM, 0);
	assert(receiver >= 0);
	struct sockaddr_in receiver_address = {
		.sin_family = AF_INET,
		.sin_addr.s_addr = htonl(INADDR_LOOPBACK),
		.sin_port = 0,
	};
	assert(bind(receiver, (struct sockaddr *)&receiver_address,
		    sizeof(receiver_address)) == 0);
	socklen_t receiver_address_length = sizeof(receiver_address);
	assert(getsockname(receiver, (struct sockaddr *)&receiver_address,
			   &receiver_address_length) == 0);
	assert(receiver_address_length == sizeof(receiver_address));

	int sender = socket(AF_INET, SOCK_DGRAM, 0);
	assert(sender >= 0);

	/* Keep both mappings untouched until their respective socket syscall. */
	unsigned char *send_buffer = mmap(NULL, PAYLOAD_LEN,
					  PROT_READ | PROT_WRITE,
					  MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	assert(send_buffer != MAP_FAILED);
	unsigned char *receive_buffer =
		mmap(NULL, PAYLOAD_LEN, PROT_READ | PROT_WRITE,
		     MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	assert(receive_buffer != MAP_FAILED);

	assert(sendto(sender, send_buffer, PAYLOAD_LEN, 0,
		      (struct sockaddr *)&receiver_address,
		      sizeof(receiver_address)) == PAYLOAD_LEN);

	struct sockaddr_in peer_address = { 0 };
	socklen_t peer_address_length = sizeof(peer_address);
	assert(recvfrom(receiver, receive_buffer, PAYLOAD_LEN, 0,
			(struct sockaddr *)&peer_address,
			&peer_address_length) == PAYLOAD_LEN);
	alarm(0);

	assert(peer_address_length == sizeof(peer_address));
	assert(peer_address.sin_family == AF_INET);
	assert(ntohl(peer_address.sin_addr.s_addr) == INADDR_LOOPBACK);
	for (size_t index = 0; index < PAYLOAD_LEN; index++)
		assert(receive_buffer[index] == 0);

	/* An unreadable tail cannot be reached by one UDP receive. */
	unsigned char *large_buffer = mmap(NULL, LARGE_BUFFER_LEN,
					   PROT_READ | PROT_WRITE,
					   MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	assert(large_buffer != MAP_FAILED);
	assert(mprotect(large_buffer + UDP_RECEIVE_CAPACITY,
			LARGE_BUFFER_LEN - UDP_RECEIVE_CAPACITY,
			PROT_NONE) == 0);

	const char small_payload[] = "udp";
	int failures = 0;
	for (int operation = 0; operation < 8; operation++) {
		if (operation == 4)
			assert(mprotect(large_buffer + VALID_PREFIX_LEN,
					UDP_RECEIVE_CAPACITY - VALID_PREFIX_LEN,
					PROT_NONE) == 0);
		assert(sendto(sender, small_payload, sizeof(small_payload), 0,
			      (struct sockaddr *)&receiver_address,
			      sizeof(receiver_address)) ==
		       sizeof(small_payload));

		ssize_t received;
		if (operation % 4 == 0) {
			received = recvfrom(receiver, large_buffer,
					    LARGE_BUFFER_LEN, 0, NULL, NULL);
		} else if (operation % 4 == 1) {
			struct iovec iov = {
				.iov_base = large_buffer,
				.iov_len = LARGE_BUFFER_LEN,
			};
			struct msghdr msg = {
				.msg_iov = &iov,
				.msg_iovlen = 1,
			};
			received = recvmsg(receiver, &msg, 0);
		} else if (operation % 4 == 2) {
			received =
				read(receiver, large_buffer, LARGE_BUFFER_LEN);
		} else {
			struct iovec iov = {
				.iov_base = large_buffer,
				.iov_len = LARGE_BUFFER_LEN,
			};
			received = readv(receiver, &iov, 1);
		}
		if (received != sizeof(small_payload) ||
		    memcmp(large_buffer, small_payload,
			   sizeof(small_payload))) {
			fprintf(stderr,
				"UDP large-buffer operation %d: received=%zd errno=%d\n",
				operation, received, errno);
			failures++;
		}
	}
	assert(failures == 0);

	/* A packet that reaches the unreadable page must still report EFAULT. */
	assert(sendto(sender, send_buffer, 5000, 0,
		      (struct sockaddr *)&receiver_address,
		      sizeof(receiver_address)) == 5000);
	errno = 0;
	assert(recvfrom(receiver, large_buffer, LARGE_BUFFER_LEN, 0, NULL,
			NULL) == -1);
	assert(errno == EFAULT);
	assert(sendto(sender, send_buffer, 5000, 0,
		      (struct sockaddr *)&receiver_address,
		      sizeof(receiver_address)) == 5000);
	struct iovec fault_iov = {
		.iov_base = large_buffer,
		.iov_len = LARGE_BUFFER_LEN,
	};
	errno = 0;
	assert(readv(receiver, &fault_iov, 1) == -1);
	assert(errno == EFAULT);

	assert(munmap(large_buffer, LARGE_BUFFER_LEN) == 0);

	assert(munmap(receive_buffer, PAYLOAD_LEN) == 0);
	assert(munmap(send_buffer, PAYLOAD_LEN) == 0);
	close(sender);
	close(receiver);
	puts("UDP user buffer prefault regression passed.");
	return EXIT_SUCCESS;
}
