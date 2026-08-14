// SPDX-License-Identifier: MPL-2.0
//
// /init for the ET_EXEC repro. Attaches the serial console to stdio and execs
// the dynamically linked /hello binary.

#define _GNU_SOURCE
#include <fcntl.h>
#include <unistd.h>

int main(void)
{
	int fd = open("/dev/console", O_RDWR);
	if (fd < 0)
		fd = open("/dev/ttyS0", O_RDWR);
	if (fd >= 0) {
		(void)dup2(fd, 0);
		(void)dup2(fd, 1);
		(void)dup2(fd, 2);
		if (fd > 2)
			(void)close(fd);
	}

	char *const argv[] = { "/hello", NULL };
	(void)execv("/hello", argv);

	(void)write(1, "init: exec /hello failed\n", 25);
	for (;;)
		(void)pause();
	return 0;
}
