// SPDX-License-Identifier: MPL-2.0

#include <arpa/inet.h>
#include <assert.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

int main(void)
{
	int receiver = socket(AF_INET6, SOCK_DGRAM, 0);
	assert(receiver >= 0);

	int v6only = -1;
	socklen_t option_length = sizeof(v6only);
	assert(getsockopt(receiver, IPPROTO_IPV6, IPV6_V6ONLY, &v6only,
			  &option_length) == 0);
	assert(option_length == sizeof(v6only));
	assert(v6only == 0);

	v6only = 1;
	assert(setsockopt(receiver, IPPROTO_IPV6, IPV6_V6ONLY, &v6only,
			  sizeof(v6only)) == 0);
	v6only = -1;
	option_length = sizeof(v6only);
	assert(getsockopt(receiver, IPPROTO_IPV6, IPV6_V6ONLY, &v6only,
			  &option_length) == 0);
	assert(v6only == 1);

	struct sockaddr_in6 mapped_loopback = {
		.sin6_family = AF_INET6,
		.sin6_port = htons(9),
	};
	assert(inet_pton(AF_INET6, "::ffff:127.0.0.1",
			 &mapped_loopback.sin6_addr) == 1);
	const char strict_payload = 'x';
	errno = 0;
	assert(sendto(receiver, &strict_payload, sizeof(strict_payload), 0,
		      (struct sockaddr *)&mapped_loopback,
		      sizeof(mapped_loopback)) == -1);
	assert(errno == EAFNOSUPPORT);

	v6only = 0;
	assert(setsockopt(receiver, IPPROTO_IPV6, IPV6_V6ONLY, &v6only,
			  sizeof(v6only)) == 0);
	v6only = -1;
	option_length = sizeof(v6only);
	assert(getsockopt(receiver, IPPROTO_IPV6, IPV6_V6ONLY, &v6only,
			  &option_length) == 0);
	assert(v6only == 0);

	struct sockaddr_in6 receiver_address = {
		.sin6_family = AF_INET6,
		.sin6_addr = IN6ADDR_LOOPBACK_INIT,
		.sin6_port = 0,
	};
	assert(bind(receiver, (struct sockaddr *)&receiver_address,
		    sizeof(receiver_address)) == 0);

	socklen_t receiver_address_length = sizeof(receiver_address);
	assert(getsockname(receiver, (struct sockaddr *)&receiver_address,
			   &receiver_address_length) == 0);
	assert(receiver_address_length == sizeof(receiver_address));
	assert(receiver_address.sin6_family == AF_INET6);
	assert(IN6_IS_ADDR_LOOPBACK(&receiver_address.sin6_addr));
	assert(receiver_address.sin6_port != 0);

	v6only = 1;
	errno = 0;
	assert(setsockopt(receiver, IPPROTO_IPV6, IPV6_V6ONLY, &v6only,
			  sizeof(v6only)) == -1);
	assert(errno == EINVAL);

	int sender = socket(AF_INET6, SOCK_DGRAM, 0);
	assert(sender >= 0);

	const struct timeval timeout = { .tv_sec = 2, .tv_usec = 0 };
	assert(setsockopt(receiver, SOL_SOCKET, SO_RCVTIMEO, &timeout,
			  sizeof(timeout)) == 0);

	const char payload[] = "asterinas-ipv6-udp";
	assert(sendto(sender, payload, sizeof(payload), 0,
		      (struct sockaddr *)&receiver_address,
		      sizeof(receiver_address)) == (ssize_t)sizeof(payload));

	char received[sizeof(payload)] = { 0 };
	struct sockaddr_in6 peer_address = { 0 };
	socklen_t peer_address_length = sizeof(peer_address);
	assert(recvfrom(receiver, received, sizeof(received), 0,
			(struct sockaddr *)&peer_address,
			&peer_address_length) == (ssize_t)sizeof(payload));
	assert(memcmp(payload, received, sizeof(payload)) == 0);
	assert(peer_address_length == sizeof(peer_address));
	assert(peer_address.sin6_family == AF_INET6);
	assert(IN6_IS_ADDR_LOOPBACK(&peer_address.sin6_addr));
	assert(peer_address.sin6_port != 0);

	close(sender);
	close(receiver);
	puts("ipv6_udp: PASS");
	return 0;
}
