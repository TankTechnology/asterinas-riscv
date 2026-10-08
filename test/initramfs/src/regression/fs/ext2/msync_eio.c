// SPDX-License-Identifier: MPL-2.0

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <sys/mman.h>
#include <unistd.h>

int main(void)
{
	const char payload[] = "asterinas-msync-error-v1\n";
	int file_fd = open("/ext2/asterinas_msync_eio",
			   O_CREAT | O_TRUNC | O_RDWR, 0600);
	void *mapped;
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
	mapped = mmap(NULL, sizeof(payload) - 1, PROT_READ | PROT_WRITE,
		      MAP_SHARED, file_fd, 0);
	if (mapped == MAP_FAILED) {
		perror("mmap");
		close(file_fd);
		return 1;
	}
	((char *)mapped)[0] = 'A';
	errno = 0;
	result = msync(mapped, sizeof(payload) - 1, MS_SYNC);
	saved_errno = errno;
	munmap(mapped, sizeof(payload) - 1);
	close(file_fd);
	if (result == -1 && saved_errno == EIO) {
		puts("ASTERINAS_EXT2_MSYNC_EIO_OK errno=5");
		return 0;
	}
	printf("ASTERINAS_EXT2_MSYNC_EIO_ERROR result=%d errno=%d\n",
	       result, saved_errno);
	return 1;
}
