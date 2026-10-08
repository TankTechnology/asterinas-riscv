// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE

#include <fcntl.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <sys/wait.h>
#include <unistd.h>

#include "../common/test.h"

#define ZERO_DEVICE "/dev/zero"
#define PAGE_SIZE 4096

static int all_zero(const uint8_t *buf, size_t len)
{
	for (size_t i = 0; i < len; ++i) {
		if (buf[i] != 0)
			return 0;
	}
	return 1;
}

FN_TEST(identity_and_mode)
{
	struct stat st;
	int fd = TEST_SUCC(open(ZERO_DEVICE, O_RDONLY));

	TEST_RES(fstat(fd, &st),
		 S_ISCHR(st.st_mode) && major(st.st_rdev) == 1 &&
			 minor(st.st_rdev) == 5 && (st.st_mode & 0777) == 0666);
	TEST_SUCC(close(fd));
}
END_TEST()

FN_TEST(read_only_shared_mapping)
{
	int fd = TEST_SUCC(open(ZERO_DEVICE, O_RDONLY));
	uint8_t *mapped =
		TEST_SUCC(mmap(NULL, PAGE_SIZE, PROT_READ, MAP_SHARED, fd, 0));

	TEST_RES(all_zero(mapped, PAGE_SIZE), _ret == 1);
	TEST_ERRNO(mprotect(mapped, PAGE_SIZE, PROT_READ | PROT_WRITE), EACCES);

	TEST_SUCC(munmap(mapped, PAGE_SIZE));
	TEST_SUCC(close(fd));
}
END_TEST()

FN_TEST(private_mapping_is_copy_on_write)
{
	int fd = TEST_SUCC(open(ZERO_DEVICE, O_RDONLY));
	uint8_t *first = TEST_SUCC(
		mmap(NULL, PAGE_SIZE, PROT_READ, MAP_PRIVATE, fd, PAGE_SIZE));
	uint8_t *second = TEST_SUCC(
		mmap(NULL, PAGE_SIZE, PROT_READ, MAP_PRIVATE, fd, PAGE_SIZE));

	TEST_RES(all_zero(first, PAGE_SIZE), _ret == 1);
	TEST_RES(all_zero(second, PAGE_SIZE), _ret == 1);
	TEST_SUCC(mprotect(first, PAGE_SIZE, PROT_READ | PROT_WRITE));
	memcpy(first, "private", sizeof("private"));
	TEST_RES(memcmp(first, "private", sizeof("private")), _ret == 0);
	TEST_RES(all_zero(second, PAGE_SIZE), _ret == 1);

	TEST_SUCC(munmap(first, PAGE_SIZE));
	TEST_SUCC(munmap(second, PAGE_SIZE));
	TEST_SUCC(close(fd));
}
END_TEST()

FN_TEST(shared_mapping_survives_fork)
{
	int fd = TEST_SUCC(open(ZERO_DEVICE, O_RDWR));
	uint8_t *mapped = TEST_SUCC(mmap(
		NULL, PAGE_SIZE, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0));

	TEST_RES(all_zero(mapped, PAGE_SIZE), _ret == 1);
	pid_t pid = TEST_SUCC(fork());
	if (pid == 0) {
		memcpy(mapped, "shared", sizeof("shared"));
		_exit(EXIT_SUCCESS);
	}

	int status;
	TEST_RES(waitpid(pid, &status, 0),
		 _ret == pid && WIFEXITED(status) &&
			 WEXITSTATUS(status) == EXIT_SUCCESS);
	TEST_RES(memcmp(mapped, "shared", sizeof("shared")), _ret == 0);

	TEST_SUCC(munmap(mapped, PAGE_SIZE));
	TEST_SUCC(close(fd));
}
END_TEST()
