// SPDX-License-Identifier: MPL-2.0

#include <arpa/inet.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

#include "../common/test.h"

// A socket bound to the unspecified address must accept traffic for every
// local address, exactly like Linux's `INADDR_ANY`. Loopback is the address
// every interface set has, so it is the one this test can rely on.

static struct sockaddr_in make_addr(in_addr_t address, in_port_t port)
{
	struct sockaddr_in addr = {
		.sin_family = AF_INET,
		.sin_addr.s_addr = address,
		.sin_port = port,
	};
	return addr;
}

/// Fills `addr` with `fd`'s bound port and the loopback address.
static void loopback_endpoint_of(int fd, struct sockaddr_in *addr)
{
	socklen_t length = sizeof(*addr);
	CHECK(getsockname(fd, (struct sockaddr *)addr, &length));
	addr->sin_addr.s_addr = htonl(INADDR_LOOPBACK);
}

/// Bounds a blocking receive so a dropped datagram fails the test instead of
/// hanging it.
static void bound_the_receive(int fd)
{
	struct timeval timeout = { .tv_sec = 2 };
	CHECK(setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout,
			 sizeof(timeout)));
}

FN_TEST(udp_wildcard_bind_is_reachable)
{
	int server = TEST_SUCC(socket(AF_INET, SOCK_DGRAM, 0));

	struct sockaddr_in bind_addr = make_addr(htonl(INADDR_ANY), 0);
	TEST_SUCC(bind(server, (struct sockaddr *)&bind_addr,
		       sizeof(bind_addr)));
	bound_the_receive(server);

	struct sockaddr_in destination;
	loopback_endpoint_of(server, &destination);

	int client = TEST_SUCC(socket(AF_INET, SOCK_DGRAM, 0));
	TEST_RES(sendto(client, "x", 1, 0, (struct sockaddr *)&destination,
			sizeof(destination)),
		 _ret == 1);

	char buffer[1];
	struct sockaddr_in peer;
	socklen_t peer_length = sizeof(peer);
	TEST_RES(recvfrom(server, buffer, sizeof(buffer), 0,
			  (struct sockaddr *)&peer, &peer_length),
		 _ret == 1);
	TEST_RES(peer.sin_addr.s_addr, _ret == htonl(INADDR_LOOPBACK));

	TEST_SUCC(close(client));
	TEST_SUCC(close(server));
}
END_TEST()

FN_TEST(tcp_wildcard_bind_is_reachable)
{
	int server = TEST_SUCC(socket(AF_INET, SOCK_STREAM, 0));

	struct sockaddr_in bind_addr = make_addr(htonl(INADDR_ANY), 0);
	TEST_SUCC(bind(server, (struct sockaddr *)&bind_addr,
		       sizeof(bind_addr)));
	TEST_SUCC(listen(server, 1));
	bound_the_receive(server);

	struct sockaddr_in destination;
	loopback_endpoint_of(server, &destination);

	int client = TEST_SUCC(socket(AF_INET, SOCK_STREAM, 0));
	TEST_SUCC(connect(client, (struct sockaddr *)&destination,
			  sizeof(destination)));

	int accepted = TEST_SUCC(accept(server, NULL, NULL));

	TEST_SUCC(close(accepted));
	TEST_SUCC(close(client));
	TEST_SUCC(close(server));
}
END_TEST()
