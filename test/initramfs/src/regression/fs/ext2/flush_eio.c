// SPDX-License-Identifier: MPL-2.0

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <unistd.h>

int main(void)
{
	const char payload[] = "asterinas-flush-error-v1\n";
	int file_fd = open("/ext2/asterinas_flush_eio",
			   O_CREAT | O_TRUNC | O_WRONLY, 0600);
	int saved_errno;
	int result;

	if (file_fd < 0) {
		perror("open");
		return 1;
	}
	if (write(file_fd, payload, sizeof(payload) - 1) !=
	    sizeof(payload) - 1) {
		perror("write");
		close(file_fd);
		return 1;
	}
	errno = 0;
	result = fsync(file_fd);
	saved_errno = errno;
	close(file_fd);
	if (result == -1 && saved_errno == EIO) {
		puts("ASTERINAS_EXT2_FLUSH_EIO_OK errno=5");
		return 0;
	}
	printf("ASTERINAS_EXT2_FLUSH_EIO_ERROR result=%d errno=%d\n", result,
	       saved_errno);
	return 1;
}
