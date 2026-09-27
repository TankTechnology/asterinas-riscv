// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static int verify_file(const char *path, const char *payload, size_t length)
{
	char buffer[64];
	int fd = open(path, O_RDONLY);
	ssize_t bytes;

	if (fd < 0)
		return -1;
	bytes = read(fd, buffer, sizeof(buffer));
	close(fd);
	return bytes == (ssize_t)length &&
			       memcmp(buffer, payload, length) == 0 ?
		       0 :
		       -1;
}

int main(int argc, char **argv)
{
	const char payload[] = "asterinas-exfat-flush-error-v1\n";
	int fd;
	int result;
	int saved_errno;

	if (argc != 2 ||
	    (strcmp(argv[1], "syncfs") != 0 && strcmp(argv[1], "fsync") != 0 &&
	     strcmp(argv[1], "clean") != 0 && strcmp(argv[1], "verify") != 0)) {
		fputs("usage: exfat_flush_eio syncfs|fsync|clean|verify\n",
		      stderr);
		return 1;
	}
	if (strcmp(argv[1], "verify") == 0) {
		const char *paths[] = {
			"/exfat/asterinas_flush_eio",
			"/exfat/ASTERINAS_FLUSH_EIO",
			"/exfat/école.txt",
			"/exfat/ÉCOLE.TXT",
		};
		for (size_t i = 0; i < sizeof(paths) / sizeof(paths[0]); ++i) {
			if (verify_file(paths[i], payload,
					sizeof(payload) - 1) != 0) {
				printf("ASTERINAS_EXFAT_VERIFY_ERROR path=%s\n",
				       paths[i]);
				return 1;
			}
		}
		if (rename("/exfat/ASTERINAS_FLUSH_EIO",
			   "/exfat/renamed.txt") != 0 ||
		    unlink("/exfat/RENAMED.TXT") != 0 ||
		    unlink("/exfat/ÉCOLE.TXT") != 0) {
			perror("case-insensitive exfat mutation");
			return 1;
		}
		return 0;
	}
	fd = open("/exfat/asterinas_flush_eio", O_CREAT | O_TRUNC | O_RDWR,
		  0600);
	if (fd < 0 ||
	    write(fd, payload, sizeof(payload) - 1) != sizeof(payload) - 1) {
		perror("prepare exfat file");
		return 1;
	}
	if (strcmp(argv[1], "clean") == 0) {
		int unicode_fd;

		if (syncfs(fd) != 0 || fsync(fd) != 0) {
			perror("sync clean exfat file");
			close(fd);
			return 1;
		}
		unicode_fd = open("/exfat/école.txt",
				  O_CREAT | O_TRUNC | O_RDWR, 0600);
		if (unicode_fd < 0 ||
		    write(unicode_fd, payload, sizeof(payload) - 1) !=
			    sizeof(payload) - 1 ||
		    fsync(unicode_fd) != 0 || close(unicode_fd) != 0) {
			perror("write unicode exfat file");
			close(fd);
			return 1;
		}
		return close(fd) == 0 ? 0 : 1;
	}
	errno = 0;
	result = strcmp(argv[1], "syncfs") == 0 ? syncfs(fd) : fsync(fd);
	saved_errno = errno;
	close(fd);
	if (result == -1 && saved_errno == EIO) {
		printf("ASTERINAS_EXFAT_%s_EIO_OK errno=5\n",
		       strcmp(argv[1], "syncfs") == 0 ? "SYNCFS" : "FSYNC");
		return 0;
	}
	printf("ASTERINAS_EXFAT_%s_EIO_ERROR result=%d errno=%d\n",
	       strcmp(argv[1], "syncfs") == 0 ? "SYNCFS" : "FSYNC", result,
	       saved_errno);
	return 1;
}
