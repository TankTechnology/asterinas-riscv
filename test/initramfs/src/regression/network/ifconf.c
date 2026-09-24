// SPDX-License-Identifier: MPL-2.0

#include <arpa/inet.h>
#include <net/if.h>
#include <stdint.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <unistd.h>

#include "../common/test.h"

FN_TEST(ifconf_ipv4_interfaces)
{
	int fd = CHECK(socket(AF_INET, SOCK_DGRAM, 0));
	struct ifconf config = { .ifc_len = 0, .ifc_buf = NULL };
	TEST_SUCC(ioctl(fd, SIOCGIFCONF, &config));
	TEST_RES(config.ifc_len, _ret >= (int)sizeof(struct ifreq) &&
					 _ret % (int)sizeof(struct ifreq) == 0);

	struct ifreq entries[16];
	memset(entries, 0xa5, sizeof(entries));
	config.ifc_len = sizeof(struct ifreq) - 1;
	config.ifc_req = entries;
	TEST_SUCC(ioctl(fd, SIOCGIFCONF, &config));
	TEST_RES(config.ifc_len, _ret == 0);
	TEST_RES((unsigned char)entries[0].ifr_name[0], _ret == 0xa5);

	memset(entries, 0, sizeof(entries));
	config.ifc_len = sizeof(entries);
	config.ifc_req = entries;
	TEST_SUCC(ioctl(fd, SIOCGIFCONF, &config));
	TEST_RES(config.ifc_len, _ret >= (int)sizeof(struct ifreq) &&
					 _ret <= (int)sizeof(entries) &&
					 _ret % (int)sizeof(struct ifreq) == 0);
	int found_loopback = 0;
	for (int i = 0; i < config.ifc_len / (int)sizeof(struct ifreq); i++) {
		TEST_RES(entries[i].ifr_addr.sa_family, _ret == AF_INET);
		if (strcmp(entries[i].ifr_name, "lo") == 0) {
			struct sockaddr_in *addr =
				(struct sockaddr_in *)&entries[i].ifr_addr;
			found_loopback = ntohl(addr->sin_addr.s_addr) ==
					 INADDR_LOOPBACK;
		}
	}
	TEST_RES(found_loopback, _ret == 1);
	CHECK(close(fd));
}
END_TEST()

FN_TEST(ifconf_bad_pointers)
{
	int fd = CHECK(socket(AF_INET, SOCK_DGRAM, 0));
	struct ifconf config = {
		.ifc_len = sizeof(struct ifreq),
		.ifc_buf = (char *)(uintptr_t)1,
	};
	TEST_ERRNO(ioctl(fd, SIOCGIFCONF, &config), EFAULT);
	CHECK(close(fd));
}
END_TEST()
