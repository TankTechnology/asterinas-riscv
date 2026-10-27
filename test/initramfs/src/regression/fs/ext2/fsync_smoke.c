// SPDX-License-Identifier: MPL-2.0

#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char **argv)
{
	if (argc != 2)
		return 2;
	int fd = open(argv[1], O_WRONLY | O_CREAT | O_TRUNC, 0644);
	if (fd < 0)
		return 1;
	const char payload[] = "fsync-smoke\n";
	if (write(fd, payload, sizeof(payload) - 1) != (ssize_t)(sizeof(payload) - 1))
		return 1;
	if (fsync(fd) < 0)
		return 1;
	return close(fd) == 0 ? 0 : 1;
}
