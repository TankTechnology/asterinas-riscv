// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <linux/netlink.h>
#include <sched.h>
#include <stdio.h>
#include <sys/socket.h>
#include <unistd.h>

#define SHARED_PORT 0x55455654U

static struct sockaddr_nl uevent_addr(void)
{
	return (struct sockaddr_nl){
		.nl_family = AF_NETLINK,
		.nl_pid = SHARED_PORT,
		.nl_groups = 1,
	};
}

static int bind_uevent(void)
{
	int fd = socket(AF_NETLINK, SOCK_RAW, NETLINK_KOBJECT_UEVENT);
	assert(fd >= 0);
	struct sockaddr_nl addr = uevent_addr();
	assert(bind(fd, (struct sockaddr *)&addr, sizeof(addr)) == 0);
	struct sockaddr_nl bound = { 0 };
	socklen_t bound_len = sizeof(bound);
	assert(getsockname(fd, (struct sockaddr *)&bound, &bound_len) == 0);
	assert(bound_len == sizeof(bound));
	assert(bound.nl_pid == SHARED_PORT && bound.nl_groups == 1);
	return fd;
}

int main(void)
{
	int old_fd = bind_uevent();
	assert(unshare(CLONE_NEWNET) == 0);
	int new_fd = bind_uevent();

	// A duplicate within one namespace must still be rejected.
	int duplicate_fd = socket(AF_NETLINK, SOCK_RAW, NETLINK_KOBJECT_UEVENT);
	assert(duplicate_fd >= 0);
	struct sockaddr_nl addr = uevent_addr();
	assert(bind(duplicate_fd, (struct sockaddr *)&addr, sizeof(addr)) == -1);
	assert(errno == EADDRINUSE);

	assert(close(duplicate_fd) == 0);
	assert(close(new_fd) == 0);
	assert(close(old_fd) == 0);
	puts("netlink uevent port namespace regression passed.");
	return 0;
}
