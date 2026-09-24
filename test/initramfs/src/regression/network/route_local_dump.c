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

#ifdef __asterinas__
struct expected_route {
	uint8_t destination[4];
	uint8_t source[4];
	uint8_t prefix;
	uint8_t type;
	uint8_t scope;
	const char *device;
};

static const struct expected_route expected[] = {
	{ { 127, 0, 0, 0 }, { 127, 0, 0, 1 }, 8, RTN_LOCAL,
	  RT_SCOPE_HOST, "lo" },
	{ { 127, 0, 0, 1 }, { 127, 0, 0, 1 }, 32, RTN_LOCAL,
	  RT_SCOPE_HOST, "lo" },
	{ { 127, 255, 255, 255 }, { 127, 0, 0, 1 }, 32,
	  RTN_BROADCAST, RT_SCOPE_LINK, "lo" },
	{ { 10, 0, 2, 15 }, { 10, 0, 2, 15 }, 32, RTN_LOCAL,
	  RT_SCOPE_HOST, "eth0" },
	{ { 10, 0, 2, 255 }, { 10, 0, 2, 15 }, 32,
	  RTN_BROADCAST, RT_SCOPE_LINK, "eth0" },
};
#endif

static unsigned int inspect_route(struct nlmsghdr *message)
{
	assert(message->nlmsg_len >= NLMSG_LENGTH(sizeof(struct rtmsg)));
	struct rtmsg *route = NLMSG_DATA(message);
	assert(route->rtm_family == AF_INET);
#ifdef __asterinas__
	assert(route->rtm_table == RT_TABLE_LOCAL);
#else
	if (route->rtm_table != RT_TABLE_LOCAL)
		return 0;
#endif
	assert(message->nlmsg_flags & NLM_F_MULTI);

	uint8_t *destination = NULL;
	uint8_t *source = NULL;
	uint8_t *gateway = NULL;
	unsigned int output_index = 0;
	int remaining = RTM_PAYLOAD(message);
	for (struct rtattr *attr = RTM_RTA(route); RTA_OK(attr, remaining);
	     attr = RTA_NEXT(attr, remaining)) {
		if (attr->rta_type == RTA_DST && RTA_PAYLOAD(attr) == 4)
			destination = RTA_DATA(attr);
		if (attr->rta_type == RTA_PREFSRC && RTA_PAYLOAD(attr) == 4)
			source = RTA_DATA(attr);
		if (attr->rta_type == RTA_GATEWAY && RTA_PAYLOAD(attr) == 4)
			gateway = RTA_DATA(attr);
		if (attr->rta_type == RTA_OIF && RTA_PAYLOAD(attr) == 4)
			memcpy(&output_index, RTA_DATA(attr), 4);
	}
	assert(gateway == NULL);
#ifdef __asterinas__
	assert(destination && source);
	for (size_t i = 0; i < sizeof(expected) / sizeof(expected[0]); i++) {
		const struct expected_route *want = &expected[i];
		if (route->rtm_dst_len == want->prefix &&
		    route->rtm_type == want->type &&
		    route->rtm_scope == want->scope &&
		    output_index == if_nametoindex(want->device) &&
		    !memcmp(destination, want->destination, 4) &&
		    !memcmp(source, want->source, 4))
			return 1u << i;
	}
	assert(0 && "unexpected local route");
#else
	(void)destination;
	(void)source;
	(void)output_index;
#endif
	return 0;
}

int main(void)
{
	int fd = socket(AF_NETLINK, SOCK_RAW, NETLINK_ROUTE);
	assert(fd >= 0);
	struct sockaddr_nl address = { .nl_family = AF_NETLINK };
	assert(bind(fd, (struct sockaddr *)&address, sizeof(address)) == 0);
	struct {
		struct nlmsghdr header;
		struct rtmsg route;
		struct rtattr table_attr;
		uint32_t table;
	} request = {
		.header = {
			.nlmsg_len = NLMSG_LENGTH(sizeof(struct rtmsg)) +
				     RTA_LENGTH(sizeof(uint32_t)),
			.nlmsg_type = RTM_GETROUTE,
			.nlmsg_flags = NLM_F_REQUEST | NLM_F_DUMP,
			.nlmsg_seq = 0x5678,
		},
		.route = { .rtm_family = AF_INET },
		.table_attr = {
			.rta_len = RTA_LENGTH(sizeof(uint32_t)),
			.rta_type = RTA_TABLE,
		},
		.table = RT_TABLE_LOCAL,
	};
	assert(send(fd, &request, sizeof(request), 0) == sizeof(request));

	unsigned int found = 0;
	unsigned int count = 0;
	int done = 0;
	for (int iteration = 0; iteration < 8 && !done; iteration++) {
		struct pollfd pfd = { .fd = fd, .events = POLLIN };
		assert(poll(&pfd, 1, 2000) == 1);
		uint8_t buffer[8192];
		ssize_t received = recv(fd, buffer, sizeof(buffer), 0);
		assert(received > 0);
		int remaining = received;
		for (struct nlmsghdr *message = (struct nlmsghdr *)buffer;
		     NLMSG_OK(message, remaining);
		     message = NLMSG_NEXT(message, remaining)) {
			assert(message->nlmsg_seq == request.header.nlmsg_seq);
			if (message->nlmsg_type == NLMSG_DONE) {
				done = 1;
				break;
			}
			assert(message->nlmsg_type == RTM_NEWROUTE);
			found |= inspect_route(message);
			count++;
		}
	}
	assert(done);
#ifdef __asterinas__
	assert(count == sizeof(expected) / sizeof(expected[0]));
	assert(found == (1u << count) - 1);
#else
	(void)found;
	(void)count;
#endif
	assert(close(fd) == 0);
	puts("IPv4 local route dump regression passed.");
	return 0;
}
