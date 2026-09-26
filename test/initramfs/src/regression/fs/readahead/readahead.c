// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <unistd.h>

#include "../../common/test.h"

#define TEST_FILE "/tmp/readahead-test"

static int readable_fd;
static int writable_fd;

FN_SETUP(readahead)
{
	readable_fd = CHECK(open(TEST_FILE, O_CREAT | O_RDONLY, 0600));
	writable_fd = CHECK(open(TEST_FILE, O_WRONLY));
}
END_SETUP()

FN_TEST(readahead)
{
	TEST_SUCC(readahead(readable_fd, 0, 4096));
	TEST_ERRNO(readahead(-1, 0, 4096), EBADF);
	TEST_ERRNO(readahead(-1, -1, 4096), EBADF);
	TEST_ERRNO(readahead(writable_fd, 0, 4096), EBADF);
	TEST_ERRNO(readahead(readable_fd, -1, 4096), EINVAL);
}
END_TEST()

FN_SETUP(cleanup)
{
	CHECK(close(readable_fd));
	CHECK(close(writable_fd));
	CHECK(unlink(TEST_FILE));
}
END_SETUP()
