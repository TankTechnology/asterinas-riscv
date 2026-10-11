// SPDX-License-Identifier: MPL-2.0

#include <assert.h>
#include <errno.h>
#include <linux/netlink.h>
#include <stdio.h>
#include <sys/socket.h>
#include <unistd.h>

static unsigned groups(int fd)
{
	struct sockaddr_nl addr = { 0 };
	socklen_t len = sizeof(addr);
	assert(getsockname(fd, (struct sockaddr *)&addr, &len) == 0);
	assert(len == sizeof(addr));
	return addr.nl_groups;
}

int main(void)
{
	int fd = socket(AF_NETLINK, SOCK_RAW | SOCK_CLOEXEC,
			NETLINK_KOBJECT_UEVENT);
	assert(fd >= 0);
	struct sockaddr_nl addr = { .nl_family = AF_NETLINK };
	assert(bind(fd, (struct sockaddr *)&addr, sizeof(addr)) == 0);
	int group = 3;
	assert(setsockopt(fd, SOL_NETLINK, NETLINK_ADD_MEMBERSHIP,
			 &group, sizeof(group)) == 0);
	assert(groups(fd) == 4);
	assert(setsockopt(fd, SOL_NETLINK, NETLINK_ADD_MEMBERSHIP,
			 &group, sizeof(group)) == 0);
	assert(groups(fd) == 4);
	assert(setsockopt(fd, SOL_NETLINK, NETLINK_DROP_MEMBERSHIP,
			 &group, sizeof(group)) == 0);
	assert(groups(fd) == 0);
	group = 0;
	assert(setsockopt(fd, SOL_NETLINK, NETLINK_ADD_MEMBERSHIP,
			 &group, sizeof(group)) == -1);
	assert(errno == EINVAL);
	assert(groups(fd) == 0);
	assert(close(fd) == 0);
	puts("netlink membership regression passed.");
	return 0;
}
