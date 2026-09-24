// SPDX-License-Identifier: MPL-2.0

#include <linux/netlink.h>
#include <linux/rtnetlink.h>
#include <net/if.h>
#include <poll.h>
#include <stdint.h>
#include <sys/socket.h>
#include <unistd.h>

#include "../common/test.h"

#define BUFFER_SIZE 8192

static char buffer[BUFFER_SIZE];

static int inspect_ipv4_route(struct nlmsghdr *nlh, unsigned int eth_index)
{
	const struct rtmsg *route = NLMSG_DATA(nlh);
	if (nlh->nlmsg_len < NLMSG_LENGTH(sizeof(*route)) ||
	    route->rtm_family != AF_INET || route->rtm_table != RT_TABLE_MAIN ||
	    route->rtm_type != RTN_UNICAST)
		return 0;

	const unsigned char default_gateway[] = { 10, 0, 2, 2 };
	const unsigned char connected_network[] = { 10, 0, 2, 0 };
	const unsigned char source[] = { 10, 0, 2, 15 };
	const unsigned char *gateway = NULL;
	const unsigned char *destination = NULL;
	const unsigned char *preferred_source = NULL;
	unsigned int output_index = 0;
	int remaining = RTM_PAYLOAD(nlh);

	for (struct rtattr *attr = RTM_RTA(route); RTA_OK(attr, remaining);
	     attr = RTA_NEXT(attr, remaining)) {
		if (attr->rta_type == RTA_GATEWAY &&
		    RTA_PAYLOAD(attr) == sizeof(default_gateway))
			gateway = RTA_DATA(attr);
		if (attr->rta_type == RTA_DST &&
		    RTA_PAYLOAD(attr) == sizeof(connected_network))
			destination = RTA_DATA(attr);
		if (attr->rta_type == RTA_PREFSRC &&
		    RTA_PAYLOAD(attr) == sizeof(source))
			preferred_source = RTA_DATA(attr);
		if (attr->rta_type == RTA_OIF &&
		    RTA_PAYLOAD(attr) == sizeof(output_index))
			memcpy(&output_index, RTA_DATA(attr), sizeof(output_index));
	}

	if (output_index != eth_index)
		return 0;
	if (route->rtm_dst_len == 0 && gateway &&
	    memcmp(gateway, default_gateway, sizeof(default_gateway)) == 0)
		return 1;
	if (route->rtm_dst_len == 24 && destination && preferred_source &&
	    memcmp(destination, connected_network,
		   sizeof(connected_network)) == 0 &&
	    memcmp(preferred_source, source, sizeof(source)) == 0)
		return 2;
	return 0;
}

FN_TEST(get_ipv4_route_dump)
{
	int sock_fd = TEST_SUCC(socket(AF_NETLINK, SOCK_RAW, NETLINK_ROUTE));
	struct sockaddr_nl sa = { .nl_family = AF_NETLINK };
	TEST_SUCC(bind(sock_fd, (struct sockaddr *)&sa, sizeof(sa)));

	struct {
		struct nlmsghdr hdr;
		struct rtmsg route;
		struct rtattr table_attr;
		uint32_t table;
	} request = {
		.hdr = {
			.nlmsg_len = NLMSG_LENGTH(sizeof(struct rtmsg)) +
				     RTA_LENGTH(sizeof(uint32_t)),
			.nlmsg_type = RTM_GETROUTE,
			.nlmsg_flags = NLM_F_REQUEST | NLM_F_DUMP,
			.nlmsg_seq = 0x1234,
		},
		.route = { .rtm_family = AF_INET },
		.table_attr = {
			.rta_len = RTA_LENGTH(sizeof(uint32_t)),
			.rta_type = RTA_TABLE,
		},
		.table = RT_TABLE_MAIN,
	};
	TEST_RES(send(sock_fd, &request, sizeof(request), 0),
		 _ret == (ssize_t)sizeof(request));

	unsigned int eth_index = if_nametoindex("eth0");
	int found = 0;
	int done = 0;
	for (int message = 0; message < 4 && !done; message++) {
		struct pollfd pfd = { .fd = sock_fd, .events = POLLIN };
		if (TEST_RES(poll(&pfd, 1, 2000), _ret == 1) != 1)
			break;
		ssize_t received = TEST_SUCC(recv(sock_fd, buffer, BUFFER_SIZE, 0));
		int remaining = received;
		for (struct nlmsghdr *nlh = (struct nlmsghdr *)buffer;
		     NLMSG_OK(nlh, remaining); nlh = NLMSG_NEXT(nlh, remaining)) {
			TEST_RES(nlh->nlmsg_seq, _ret == request.hdr.nlmsg_seq);
			if (nlh->nlmsg_type == NLMSG_DONE) {
				done = 1;
				break;
			}
			TEST_RES(nlh->nlmsg_type, _ret == RTM_NEWROUTE);
			found |= inspect_ipv4_route(nlh, eth_index);
		}
	}
	TEST_RES(done, _ret == 1);
#ifdef __asterinas__
	TEST_RES(found, _ret == 3);
#endif
	TEST_SUCC(close(sock_fd));
}
END_TEST()
