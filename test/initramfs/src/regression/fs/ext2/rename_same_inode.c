// SPDX-License-Identifier: MPL-2.0

#include <errno.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include "../../common/test.h"

#define BASE_DIR "/ext2/rename_same_inode"
#define SRC_DIR BASE_DIR "/src"
#define DST_DIR BASE_DIR "/dst"
#define SRC_NAME SRC_DIR "/source"
#define DST_NAME DST_DIR "/target"
#define SAME_SOURCE BASE_DIR "/same_source"
#define SAME_TARGET BASE_DIR "/same_target"

FN_TEST(rename_hardlinks_in_one_directory_keeps_both_names)
{
	TEST_SUCC(mkdir(BASE_DIR, 0755));
	int fd = TEST_SUCC(open(SAME_SOURCE, O_CREAT | O_EXCL | O_WRONLY, 0644));
	TEST_SUCC(close(fd));
	TEST_SUCC(link(SAME_SOURCE, SAME_TARGET));

	struct stat before;
	TEST_SUCC(stat(SAME_SOURCE, &before));
	TEST_RES(before.st_nlink, _ret == 2);
	TEST_SUCC(rename(SAME_SOURCE, SAME_TARGET));

	struct stat source_after;
	struct stat target_after;
	int source_status = TEST_RES(stat(SAME_SOURCE, &source_after), _ret == 0);
	int target_status = TEST_RES(stat(SAME_TARGET, &target_after), _ret == 0);
	if (source_status == 0 && target_status == 0) {
		TEST_RES(source_after.st_ino, _ret == before.st_ino);
		TEST_RES(target_after.st_ino, _ret == before.st_ino);
		TEST_RES(source_after.st_nlink, _ret == 2);
		TEST_RES(target_after.st_nlink, _ret == 2);
	}

	CHECK_WITH(unlink(SAME_SOURCE), _ret == 0 || errno == ENOENT);
	CHECK_WITH(unlink(SAME_TARGET), _ret == 0 || errno == ENOENT);
	TEST_SUCC(rmdir(BASE_DIR));
}
END_TEST()

FN_TEST(rename_hardlinks_to_same_inode_keeps_both_names)
{
	TEST_SUCC(mkdir(BASE_DIR, 0755));
	TEST_SUCC(mkdir(SRC_DIR, 0755));
	TEST_SUCC(mkdir(DST_DIR, 0755));

	int fd = TEST_SUCC(open(SRC_NAME, O_CREAT | O_EXCL | O_WRONLY, 0644));
	TEST_RES(write(fd, "kept", 4), _ret == 4);
	TEST_SUCC(close(fd));
	TEST_SUCC(link(SRC_NAME, DST_NAME));

	struct stat source_before;
	struct stat target_before;
	TEST_SUCC(stat(SRC_NAME, &source_before));
	TEST_SUCC(stat(DST_NAME, &target_before));
	TEST_RES(source_before.st_ino, _ret == target_before.st_ino);
	TEST_RES(source_before.st_nlink, _ret == 2);

	TEST_SUCC(rename(SRC_NAME, DST_NAME));

	struct stat source_after;
	struct stat target_after;
	int source_status = TEST_RES(stat(SRC_NAME, &source_after), _ret == 0);
	int target_status = TEST_RES(stat(DST_NAME, &target_after), _ret == 0);
	if (source_status == 0 && target_status == 0) {
		TEST_RES(source_after.st_ino, _ret == source_before.st_ino);
		TEST_RES(target_after.st_ino, _ret == target_before.st_ino);
		TEST_RES(source_after.st_nlink, _ret == 2);
		TEST_RES(target_after.st_nlink, _ret == 2);
	}

	CHECK_WITH(unlink(SRC_NAME), _ret == 0 || errno == ENOENT);
	CHECK_WITH(unlink(DST_NAME), _ret == 0 || errno == ENOENT);
	TEST_SUCC(rmdir(SRC_DIR));
	TEST_SUCC(rmdir(DST_DIR));
	TEST_SUCC(rmdir(BASE_DIR));
}
END_TEST()
