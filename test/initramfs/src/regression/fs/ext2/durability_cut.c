// SPDX-License-Identifier: MPL-2.0

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define TEMPORARY_PATH "/ext2/asterinas_durability_cut.tmp"
#define FINAL_PATH "/ext2/asterinas_durability_cut"

static void fail(const char *operation)
{
	perror(operation);
	exit(EXIT_FAILURE);
}

int main(int argc, char **argv)
{
	const char payload[] = "asterinas-durability-cut-v1\n";
	int file_fd;
	int directory_fd;

	if (argc != 2 || (strcmp(argv[1], "file") != 0 &&
			  strcmp(argv[1], "directory") != 0)) {
		fprintf(stderr, "usage: durability_cut file|directory\n");
		return EXIT_FAILURE;
	}

	file_fd = open(TEMPORARY_PATH, O_CREAT | O_EXCL | O_WRONLY, 0600);
	if (file_fd < 0)
		fail("open temporary file");
	if (write(file_fd, payload, sizeof(payload) - 1) != sizeof(payload) - 1)
		fail("write temporary file");
	if (fsync(file_fd) < 0)
		fail("fsync temporary file");
	if (close(file_fd) < 0)
		fail("close temporary file");
	if (strcmp(argv[1], "file") == 0) {
		puts("ASTERINAS_EXT2_CUT_READY stage=file_fsync");
		fflush(stdout);
		sleep(600);
		return EXIT_FAILURE;
	}

	if (rename(TEMPORARY_PATH, FINAL_PATH) < 0)
		fail("rename file");
	directory_fd = open("/ext2", O_RDONLY | O_DIRECTORY);
	if (directory_fd < 0)
		fail("open directory");
	if (fsync(directory_fd) < 0)
		fail("fsync directory");
	if (close(directory_fd) < 0)
		fail("close directory");
	puts("ASTERINAS_EXT2_CUT_READY stage=directory_fsync");
	fflush(stdout);
	sleep(600);
	return EXIT_FAILURE;
}
