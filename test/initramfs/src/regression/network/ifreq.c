// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE
#include <assert.h>
#include <arpa/inet.h>
#include <errno.h>
#include <net/if.h>
#include <net/if_arp.h>
#include <sched.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>

static struct ifreq query(int fd, const char *name, unsigned long command)
{
	struct ifreq request = { 0 };
	assert(strlen(name) < IFNAMSIZ);
	strcpy(request.ifr_name, name);
	assert(ioctl(fd, command, &request) == 0);
	return request;
}

static void check_loopback(int fd)
{
	struct ifreq request = query(fd, "lo", SIOCGIFFLAGS);
	assert(request.ifr_flags & IFF_LOOPBACK);
	assert(request.ifr_flags & IFF_UP);
	request = query(fd, "lo", SIOCGIFMTU);
	assert(request.ifr_mtu >= 1500);
	request = query(fd, "lo", SIOCGIFHWADDR);
	assert(request.ifr_hwaddr.sa_family == ARPHRD_LOOPBACK);
	request = query(fd, "lo", SIOCGIFADDR);
	assert(request.ifr_addr.sa_family == AF_INET);
	assert(((struct sockaddr_in *)&request.ifr_addr)->sin_addr.s_addr ==
	       htonl(INADDR_LOOPBACK));
	request = query(fd, "lo", SIOCGIFNETMASK);
	assert(((struct sockaddr_in *)&request.ifr_netmask)->sin_addr.s_addr ==
	       htonl(0xff000000));
	request = query(fd, "lo", SIOCGIFBRDADDR);
	assert(((struct sockaddr_in *)&request.ifr_broadaddr)->sin_addr.s_addr ==
	       htonl(INADDR_ANY));
	request = query(fd, "lo", SIOCGIFINDEX);
	assert(request.ifr_ifindex == 1);
}

static void check_ethernet(int fd)
{
	struct ifreq request = query(fd, "eth0", SIOCGIFFLAGS);
	assert(request.ifr_flags & IFF_UP);
	assert(request.ifr_flags & IFF_BROADCAST);
	request = query(fd, "eth0", SIOCGIFMTU);
	assert(request.ifr_mtu == 1500);
	request = query(fd, "eth0", SIOCGIFHWADDR);
	assert(request.ifr_hwaddr.sa_family == ARPHRD_ETHER);
	request = query(fd, "eth0", SIOCGIFADDR);
	assert(((struct sockaddr_in *)&request.ifr_addr)->sin_addr.s_addr ==
	       htonl(0x0a00020f));
	request = query(fd, "eth0", SIOCGIFNETMASK);
	assert(((struct sockaddr_in *)&request.ifr_netmask)->sin_addr.s_addr ==
	       htonl(0xffffff00));
	request = query(fd, "eth0", SIOCGIFBRDADDR);
	assert(((struct sockaddr_in *)&request.ifr_broadaddr)->sin_addr.s_addr ==
	       htonl(0x0a0002ff));
	request = query(fd, "eth0", SIOCGIFINDEX);
	assert(request.ifr_ifindex > 1);
}

int main(int argc, char **argv)
{
	assert(argc == 1 ||
	       (argc == 2 && strcmp(argv[1], "--expect-eth0") == 0));
	int fd = socket(AF_INET, SOCK_DGRAM, 0);
	assert(fd >= 0);
	check_loopback(fd);
	if (argc == 2)
		check_ethernet(fd);

	struct ifreq request = { 0 };
	strcpy(request.ifr_name, "missing0");
	assert(ioctl(fd, SIOCGIFFLAGS, &request) == -1);
	assert(errno == ENODEV);
	assert(ioctl(fd, SIOCGIFFLAGS, (void *)1) == -1);
	assert(errno == EFAULT);

	pid_t child = fork();
	assert(child >= 0);
	if (child == 0) {
		if (unshare(CLONE_NEWNET) != 0) {
#ifndef __asterinas__
			if (errno == EPERM)
				_exit(0);
#endif
			_exit(2);
		}
		request = query(fd, "lo", SIOCGIFFLAGS);
		assert(!(request.ifr_flags & IFF_UP));
		strcpy(request.ifr_name, "eth0");
		assert(ioctl(fd, SIOCGIFFLAGS, &request) == -1);
		assert(errno == ENODEV);
		_exit(0);
	}
	int status;
	assert(waitpid(child, &status, 0) == child);
	assert(WIFEXITED(status) && WEXITSTATUS(status) == 0);
	assert(close(fd) == 0);
	puts("interface ioctl regression passed.");
	return 0;
}
