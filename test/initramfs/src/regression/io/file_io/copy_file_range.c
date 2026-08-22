// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

#include "../../common/test.h"

#define IN_FILE "/tmp/cfr_in"
#define OUT_FILE "/tmp/cfr_out"

static int fd_in = -1;
static int fd_out = -1;

FN_SETUP(prepare)
{
	fd_in = open(IN_FILE, O_RDWR | O_CREAT | O_TRUNC, 0644);
	CHECK(fd_in);
	CHECK(write(fd_in, "hello world", 11));
	CHECK(lseek(fd_in, 0, SEEK_SET));

	fd_out = open(OUT_FILE, O_RDWR | O_CREAT | O_TRUNC, 0644);
	CHECK(fd_out);
}
END_SETUP()

FN_TEST(copy_with_null_offsets)
{
	char buf[16] = { 0 };

	TEST_RES(copy_file_range(fd_in, NULL, fd_out, NULL, 11, 0), _ret == 11);

	/* The file offsets must advance past the copied data. */
	TEST_RES(lseek(fd_in, 0, SEEK_CUR), _ret == 11);
	TEST_RES(lseek(fd_out, 0, SEEK_CUR), _ret == 11);

	CHECK(lseek(fd_out, 0, SEEK_SET));
	TEST_RES(read(fd_out, buf, 11), _ret == 11);
	TEST_RES(buf, strcmp(buf, "hello world") == 0);
}
END_TEST()

FN_TEST(copy_with_explicit_offsets)
{
	char buf[16] = { 0 };
	loff_t off_in = 6;
	loff_t off_out = 0;

	TEST_RES(copy_file_range(fd_in, &off_in, fd_out, &off_out, 5, 0),
		 _ret == 5 && off_in == 11 && off_out == 5);

	/* The file offsets must remain unchanged. */
	TEST_RES(lseek(fd_in, 0, SEEK_CUR), _ret == 11);
	TEST_RES(lseek(fd_out, 0, SEEK_CUR), _ret == 11);

	CHECK(lseek(fd_out, 0, SEEK_SET));
	TEST_RES(read(fd_out, buf, 5), _ret == 5);
	TEST_RES(buf, strcmp(buf, "world") == 0);
}
END_TEST()

FN_TEST(copy_past_eof_is_short)
{
	loff_t off_in = 8;
	loff_t off_out = 0;

	/* Only "rld" (3 bytes) are available from offset 8. */
	TEST_RES(copy_file_range(fd_in, &off_in, fd_out, &off_out, 100, 0),
		 _ret == 3 && off_in == 11 && off_out == 3);
}
END_TEST()

FN_TEST(invalid_args)
{
	loff_t off = 0;
	int dirfd;
	int appendfd;

	TEST_ERRNO(copy_file_range(fd_in, NULL, fd_out, NULL, 1, 1), EINVAL);

	dirfd = open("/tmp", O_RDONLY | O_DIRECTORY);
	CHECK(dirfd);
	TEST_ERRNO(copy_file_range(dirfd, NULL, fd_out, NULL, 1, 0), EISDIR);
	CHECK(close(dirfd));

	appendfd = open(OUT_FILE, O_WRONLY | O_APPEND);
	CHECK(appendfd);
	TEST_ERRNO(copy_file_range(fd_in, &off, appendfd, &off, 1, 0), EBADF);
	CHECK(close(appendfd));
}
END_TEST()

FN_TEST(overlapping_ranges_in_same_file)
{
	loff_t off_in = 0;
	loff_t off_out = 1;

	TEST_ERRNO(copy_file_range(fd_in, &off_in, fd_in, &off_out, 5, 0),
		   EINVAL);
}
END_TEST()

FN_SETUP(cleanup)
{
	CHECK(close(fd_in));
	CHECK(close(fd_out));
	CHECK(unlink(IN_FILE));
	CHECK(unlink(OUT_FILE));
}
END_SETUP()
