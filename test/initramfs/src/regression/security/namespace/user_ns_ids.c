// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <sched.h>
#include <stdio.h>
#include <string.h>
#include <sys/fsuid.h>
#include <unistd.h>

#include "../../common/test.h"

static ssize_t write_file(const char *path, const char *value)
{
	int fd = CHECK(open(path, O_WRONLY));
	size_t len = strlen(value);
	ssize_t written = write(fd, value, len);
	int saved_errno = errno;

	CHECK(close(fd));
	errno = saved_errno;
	return written;
}

FN_TEST(setid_arguments_are_resolved_in_the_current_user_namespace)
{
	uid_t outer_uid = getuid();
	gid_t outer_gid = getgid();
	char uid_map[64];
	char gid_map[64];

	TEST_SUCC(unshare(CLONE_NEWUSER));

	TEST_RES(write_file("/proc/self/setgroups", "deny\n"), _ret == 5);
	int uid_map_len =
		snprintf(uid_map, sizeof(uid_map), "1234 %u 1\n", outer_uid);
	int gid_map_len =
		snprintf(gid_map, sizeof(gid_map), "1234 %u 1\n", outer_gid);
	TEST_RES(write_file("/proc/self/uid_map", uid_map),
		 _ret == uid_map_len);
	TEST_RES(write_file("/proc/self/gid_map", gid_map),
		 _ret == gid_map_len);

	TEST_RES(getuid(), _ret == 1234);
	TEST_RES(getgid(), _ret == 1234);

	/*
	 * setfsuid()/setfsgid() always return the old caller-visible ID.  In this
	 * namespace 1234 maps to a different kernel ID, so these assertions also
	 * catch implementations that leak the global kernel ID on return.
	 */
	TEST_RES(setfsuid((uid_t)-1), _ret == 1234);
	TEST_RES(setfsgid((gid_t)-1), _ret == 1234);
	TEST_RES(setfsuid(1234), _ret == 1234);
	TEST_RES(setfsgid(1234), _ret == 1234);

	/* Unmapped filesystem IDs leave the current IDs unchanged without EINVAL. */
	TEST_RES(setfsuid(1235), _ret == 1234);
	TEST_RES(setfsgid(1235), _ret == 1234);
	TEST_RES(setfsuid((uid_t)-1), _ret == 1234);
	TEST_RES(setfsgid((gid_t)-1), _ret == 1234);

	TEST_ERRNO(setgid(1235), EINVAL);
	TEST_ERRNO(setuid(1235), EINVAL);

	TEST_SUCC(setgid(1234));
	TEST_SUCC(setuid(1234));
	TEST_RES(getgid(), _ret == 1234);
	TEST_RES(getuid(), _ret == 1234);
}
END_TEST()
