// SPDX-License-Identifier: MPL-2.0

#include <arpa/inet.h>
#include <assert.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <unistd.h>

#define PAYLOAD_LEN (4 * 4096 + 137)

int main(void)
{
	alarm(10);

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

	assert(munmap(receive_buffer, PAYLOAD_LEN) == 0);
	assert(munmap(send_buffer, PAYLOAD_LEN) == 0);
	close(sender);
	close(receiver);
	puts("UDP user buffer prefault regression passed.");
	return EXIT_SUCCESS;
}
