// SPDX-License-Identifier: MPL-2.0

#include <assert.h>
#include <linux/netlink.h>
#include <linux/rtnetlink.h>
#include <net/if.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

static void check_route(int fd, uint32_t seq, const uint8_t destination[4],
			const uint8_t *gateway, const char *device,
			const uint8_t source_address[4], unsigned int route_type)
{
	struct {
		struct nlmsghdr header;
		struct rtmsg route;
		struct rtattr destination_attr;
		uint8_t destination[4];
	} request = {
		.header = {
			.nlmsg_len = NLMSG_LENGTH(sizeof(struct rtmsg)) +
				     RTA_LENGTH(4),
			.nlmsg_type = RTM_GETROUTE,
			.nlmsg_flags = NLM_F_REQUEST,
			.nlmsg_seq = seq,
		},
		.route = {
			.rtm_family = AF_INET,
			.rtm_dst_len = 32,
			.rtm_flags = RTM_F_LOOKUP_TABLE,
		},
		.destination_attr = {
			.rta_len = RTA_LENGTH(4),
			.rta_type = RTA_DST,
		},
	};
	memcpy(request.destination, destination, 4);
	assert(send(fd, &request, sizeof(request), 0) == sizeof(request));

	struct pollfd pfd = { .fd = fd, .events = POLLIN };
	assert(poll(&pfd, 1, 2000) == 1);
	uint8_t buffer[8192];
	ssize_t received = recv(fd, buffer, sizeof(buffer), 0);
	assert(received >= (ssize_t)NLMSG_LENGTH(sizeof(struct rtmsg)));
	struct nlmsghdr *response = (struct nlmsghdr *)buffer;
	assert(NLMSG_OK(response, received));
	assert(response->nlmsg_type == RTM_NEWROUTE);
	assert(response->nlmsg_seq == seq);
	assert(!(response->nlmsg_flags & NLM_F_MULTI));

	struct rtmsg *route = NLMSG_DATA(response);
	assert(route->rtm_family == AF_INET);
	assert(route->rtm_table == RT_TABLE_MAIN);
	assert(route->rtm_type == route_type);
	assert(route->rtm_dst_len == 32);
	uint32_t output_index = 0;
	const uint8_t *actual_destination = NULL;
	const uint8_t *actual_gateway = NULL;
	const uint8_t *source = NULL;
	int remaining = RTM_PAYLOAD(response);
	for (struct rtattr *attr = RTM_RTA(route); RTA_OK(attr, remaining);
	     attr = RTA_NEXT(attr, remaining)) {
		if (attr->rta_type == RTA_DST && RTA_PAYLOAD(attr) == 4)
			actual_destination = RTA_DATA(attr);
		if (attr->rta_type == RTA_GATEWAY && RTA_PAYLOAD(attr) == 4)
			actual_gateway = RTA_DATA(attr);
		if (attr->rta_type == RTA_PREFSRC && RTA_PAYLOAD(attr) == 4)
			source = RTA_DATA(attr);
		if (attr->rta_type == RTA_OIF && RTA_PAYLOAD(attr) == 4)
			memcpy(&output_index, RTA_DATA(attr), 4);
	}
	assert(output_index == if_nametoindex(device));
	assert(actual_destination && !memcmp(actual_destination, destination, 4));
	assert(source && !memcmp(source, source_address, 4));
	assert((gateway == NULL) == (actual_gateway == NULL));
	if (gateway)
		assert(!memcmp(actual_gateway, gateway, 4));
}

int main(void)
{
	int fd = socket(AF_NETLINK, SOCK_RAW, NETLINK_ROUTE);
	assert(fd >= 0);
	struct sockaddr_nl address = { .nl_family = AF_NETLINK };
	assert(bind(fd, (struct sockaddr *)&address, sizeof(address)) == 0);
	const uint8_t on_link[4] = { 10, 0, 2, 2 };
	const uint8_t off_link[4] = { 198, 51, 100, 10 };
	const uint8_t eth0_address[4] = { 10, 0, 2, 15 };
	const uint8_t loopback[4] = { 127, 0, 0, 1 };
	const uint8_t subnet_broadcast[4] = { 10, 0, 2, 255 };
	const uint8_t global_broadcast[4] = { 255, 255, 255, 255 };
	check_route(fd, 0x1234, on_link, NULL, "eth0", eth0_address,
		    RTN_UNICAST);
	check_route(fd, 0x1235, off_link, on_link, "eth0", eth0_address,
		    RTN_UNICAST);
	check_route(fd, 0x1236, loopback, NULL, "lo", loopback, RTN_LOCAL);
	check_route(fd, 0x1237, eth0_address, NULL, "lo", eth0_address,
		    RTN_LOCAL);
	check_route(fd, 0x1238, subnet_broadcast, NULL, "eth0",
		    eth0_address, RTN_BROADCAST);
	check_route(fd, 0x1239, global_broadcast, NULL, "eth0",
		    eth0_address, RTN_BROADCAST);
	assert(close(fd) == 0);
	puts("IPv4 route lookup regression passed.");
	return 0;
}
