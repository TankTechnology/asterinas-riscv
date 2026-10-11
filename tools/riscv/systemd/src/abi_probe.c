// SPDX-License-Identifier: MPL-2.0

#include <errno.h>
#include <fcntl.h>
#include <linux/filter.h>
#include <linux/netlink.h>
#include <stdio.h>
#include <sys/socket.h>
#include <unistd.h>

static void report(const char *operation, int result)
{
	int error = result < 0 ? errno : 0;
	printf("DEBIAN_ABI operation=%s result=%d errno=%d\n",
	       operation, result < 0 ? -1 : 0, error);
}

int main(void)
{
	int fd = socket(AF_NETLINK, SOCK_RAW | SOCK_CLOEXEC | SOCK_NONBLOCK,
			NETLINK_KOBJECT_UEVENT);
	report("uevent-socket", fd);
	if (fd >= 0) {
		int value = 1;
		report("uevent-passcred", setsockopt(fd, SOL_SOCKET, SO_PASSCRED,
						 &value, sizeof(value)));
		struct sockaddr_nl addr = {
			.nl_family = AF_NETLINK,
			.nl_groups = 1,
		};
		report("uevent-bind", bind(fd, (struct sockaddr *)&addr, sizeof(addr)));
		value = 3;
		report("uevent-membership", setsockopt(fd, SOL_NETLINK,
				 NETLINK_ADD_MEMBERSHIP, &value, sizeof(value)));
		struct sock_filter instruction = BPF_STMT(BPF_RET | BPF_K, ~0U);
		struct sock_fprog filter = { .len = 1, .filter = &instruction };
		report("uevent-filter", setsockopt(fd, SOL_SOCKET, SO_ATTACH_FILTER,
					       &filter, sizeof(filter)));
		close(fd);
	}
	fd = socket(AF_NETLINK, SOCK_RAW | SOCK_CLOEXEC, NETLINK_ROUTE);
	report("route-socket", fd);
	if (fd >= 0)
		close(fd);
	fd = open("/dev/kmsg", O_RDONLY | O_NONBLOCK | O_CLOEXEC);
	report("kmsg-open", fd);
	if (fd >= 0)
		close(fd);
	return 0;
}
