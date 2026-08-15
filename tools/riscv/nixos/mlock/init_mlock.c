// SPDX-License-Identifier: MPL-2.0
//
// /init for the mlock/munlock smoke test.

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

	char *const argv[] = { "/mlock_smoke", NULL };
	(void)execv("/mlock_smoke", argv);

	(void)write(1, "init: exec /mlock_smoke failed\n", 30);
	for (;;)
		(void)pause();
	return 0;
}
