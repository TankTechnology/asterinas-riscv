// SPDX-License-Identifier: MPL-2.0

#include <fcntl.h>
#include <string.h>
#include <unistd.h>

#include "../../common/test.h"

// libtirpc needs this sysctl even when no ports have been reserved. Asterinas
// currently has no configurable reserved ports; expose that state read-only.
FN_TEST(empty_reserved_ports_is_readable)
{
	int fd = CHECK(
		open("/proc/sys/net/ipv4/ip_local_reserved_ports", O_RDONLY));
	char buffer[8] = { 0 };

	TEST_RES(read(fd, buffer, sizeof(buffer)), _ret == 1);
	TEST_RES(strcmp(buffer, "\n"), _ret == 0);
	TEST_RES(read(fd, buffer, sizeof(buffer)), _ret == 0);
	TEST_RES(pread(fd, buffer, sizeof(buffer), 1), _ret == 0);
	TEST_RES(pread(fd, buffer, 1, 0), _ret == 1 && buffer[0] == '\n');
	CHECK(close(fd));
}
END_TEST()
