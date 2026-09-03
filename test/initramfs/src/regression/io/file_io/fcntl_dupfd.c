// SPDX-License-Identifier: MPL-2.0

#include <fcntl.h>
#include <unistd.h>

#include "../../common/test.h"

static int fd;

FN_SETUP(open_source)
{
	fd = CHECK(open("/dev/null", O_RDONLY));
}
END_SETUP()

// Regression test for issue #97: the lower bound may equal the source FD.
FN_TEST(dupfd_accepts_source_fd_as_minimum)
{
	int duplicated_fd = TEST_RES(fcntl(fd, F_DUPFD, fd), _ret > fd);

	if (duplicated_fd >= 0) {
		TEST_RES(fcntl(duplicated_fd, F_GETFD),
			 (_ret & FD_CLOEXEC) == 0);
		TEST_SUCC(close(duplicated_fd));
	}
}
END_TEST()

FN_TEST(dupfd_cloexec_accepts_source_fd_as_minimum)
{
	int duplicated_fd = TEST_RES(fcntl(fd, F_DUPFD_CLOEXEC, fd), _ret > fd);

	if (duplicated_fd >= 0) {
		TEST_RES(fcntl(duplicated_fd, F_GETFD),
			 (_ret & FD_CLOEXEC) != 0);
		TEST_SUCC(close(duplicated_fd));
	}
}
END_TEST()

FN_SETUP(close_source)
{
	CHECK(close(fd));
}
END_SETUP()
