// SPDX-License-Identifier: MPL-2.0

#include <assert.h>
#include <linux/netlink.h>
#include <linux/rtnetlink.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <sys/socket.h>
#include <unistd.h>

int main(void)
{
	int fd = socket(AF_NETLINK, SOCK_RAW, NETLINK_ROUTE);
	assert(fd >= 0);
	struct sockaddr_nl address = { .nl_family = AF_NETLINK };
	assert(bind(fd, (struct sockaddr *)&address, sizeof(address)) == 0);

	struct {
		struct nlmsghdr header;
		struct rtmsg route;
	} request = {
		.header = {
			.nlmsg_len = NLMSG_LENGTH(sizeof(struct rtmsg)),
			.nlmsg_type = RTM_GETROUTE,
			.nlmsg_flags = NLM_F_REQUEST | NLM_F_DUMP,
			.nlmsg_seq = 0x9abc,
		},
		.route = { .rtm_family = AF_INET },
	};
	assert(send(fd, &request, sizeof(request), 0) == sizeof(request));

	unsigned int main_count = 0;
	unsigned int local_count = 0;
	int done = 0;
	for (int iteration = 0; iteration < 16 && !done; iteration++) {
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
			assert(message->nlmsg_len >=
			       NLMSG_LENGTH(sizeof(struct rtmsg)));
			assert(message->nlmsg_flags & NLM_F_MULTI);
			struct rtmsg *route = NLMSG_DATA(message);
			assert(route->rtm_family == AF_INET);
			if (route->rtm_table == RT_TABLE_MAIN)
				main_count++;
			else if (route->rtm_table == RT_TABLE_LOCAL)
				local_count++;
#ifdef __asterinas__
			else
				assert(0 && "unexpected IPv4 route table");
#endif
		}
	}
	assert(done);
#ifdef __asterinas__
	assert(main_count == 2);
	assert(local_count == 5);
#else
	assert(main_count > 0 && local_count > 0);
#endif
	assert(close(fd) == 0);
	puts("IPv4 all route tables regression passed.");
	return 0;
}
