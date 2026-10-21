// SPDX-License-Identifier: MPL-2.0

#include <arpa/inet.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

#include "../common/test.h"

static int check_reply_source(in_addr_t bind_address)
{
	int server = CHECK(socket(AF_INET, SOCK_DGRAM, 0));
	int client = CHECK(socket(AF_INET, SOCK_DGRAM, 0));
	struct timeval timeout = { .tv_sec = 1 };
	CHECK(setsockopt(server, SOL_SOCKET, SO_RCVTIMEO, &timeout,
			 sizeof(timeout)));
	CHECK(setsockopt(client, SOL_SOCKET, SO_RCVTIMEO, &timeout,
			 sizeof(timeout)));
	struct sockaddr_in address = {
		.sin_family = AF_INET,
		.sin_addr.s_addr = htonl(bind_address),
	};
	CHECK(bind(server, (struct sockaddr *)&address, sizeof(address)));
	socklen_t length = sizeof(address);
	CHECK(getsockname(server, (struct sockaddr *)&address, &length));
	address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
	CHECK_WITH(sendto(client, "ping", 4, 0, (struct sockaddr *)&address,
			  sizeof(address)),
		   _ret == 4);
	struct sockaddr_in peer;
	length = sizeof(peer);
	char buffer[4];
	CHECK_WITH(recvfrom(server, buffer, sizeof(buffer), 0,
			    (struct sockaddr *)&peer, &length),
		   _ret == 4);
	CHECK_WITH(sendto(server, buffer, sizeof(buffer), 0,
			  (struct sockaddr *)&peer, length),
		   _ret == 4);
	length = sizeof(peer);
	CHECK_WITH(recvfrom(client, buffer, sizeof(buffer), 0,
			    (struct sockaddr *)&peer, &length),
		   _ret == 4);
	CHECK_WITH(ntohl(peer.sin_addr.s_addr), _ret == INADDR_LOOPBACK);
	CHECK_WITH(peer.sin_port, _ret == address.sin_port);
	CHECK_WITH(memcmp(buffer, "ping", 4), _ret == 0);
	CHECK(close(client));
	CHECK(close(server));
	return 0;
}

// Run on a guest with both Ethernet and loopback: wildcard sockets are owned
// by the default interface but must choose a loopback source for local replies.
FN_TEST(wildcard_udp_loopback_reply)
{
	TEST_RES(check_reply_source(INADDR_ANY), _ret == 0);
	TEST_RES(check_reply_source(INADDR_LOOPBACK), _ret == 0);
}
END_TEST()
