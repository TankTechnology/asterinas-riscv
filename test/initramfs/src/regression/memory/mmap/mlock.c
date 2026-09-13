// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE

#include "../../common/test.h"

#include <stdio.h>
#include <sys/mman.h>
#include <unistd.h>

#define PAGE_SIZE 4096

static long get_locked_kb(void)
{
	FILE *status = fopen("/proc/self/status", "r");
	char line[256];
	long locked_kb = -1;

	if (!status)
		return -1;

	while (fgets(line, sizeof(line), status)) {
		if (sscanf(line, "VmLck:%ld kB", &locked_kb) == 1)
			break;
	}
	fclose(status);
	return locked_kb;
}

FN_TEST(range_locking_updates_accounting)
{
	long before = TEST_RES(get_locked_kb(), _ret >= 0);
	char *mapping = TEST(
		mmap(NULL, PAGE_SIZE * 3, PROT_READ | PROT_WRITE,
		     MAP_PRIVATE | MAP_ANONYMOUS, -1, 0),
		0, _ret != MAP_FAILED);

	TEST_SUCC(mlock(mapping + PAGE_SIZE, PAGE_SIZE));
	TEST_RES(get_locked_kb(), _ret == before + PAGE_SIZE / 1024);

	// Re-locking the middle page must not charge it twice.
	TEST_SUCC(mlock(mapping, PAGE_SIZE * 3));
	TEST_RES(get_locked_kb(), _ret == before + PAGE_SIZE * 3 / 1024);

	TEST_SUCC(munlock(mapping + PAGE_SIZE, PAGE_SIZE));
	TEST_RES(get_locked_kb(), _ret == before + PAGE_SIZE * 2 / 1024);

	TEST_SUCC(munlockall());
	TEST_RES(get_locked_kb(), _ret == 0);
	TEST_SUCC(munmap(mapping, PAGE_SIZE * 3));
}
END_TEST()

FN_TEST(locking_a_range_with_a_hole_updates_the_mapped_prefix)
{
	char *mapping = TEST(
		mmap(NULL, PAGE_SIZE * 3, PROT_READ | PROT_WRITE,
		     MAP_PRIVATE | MAP_ANONYMOUS, -1, 0),
		0, _ret != MAP_FAILED);
	long before = TEST_RES(get_locked_kb(), _ret >= 0);

	TEST_SUCC(munmap(mapping + PAGE_SIZE, PAGE_SIZE));
	TEST_ERRNO(mlock(mapping, PAGE_SIZE * 3), ENOMEM);
	TEST_RES(get_locked_kb(), _ret == before + PAGE_SIZE / 1024);
	TEST_SUCC(munlockall());
	TEST_SUCC(munmap(mapping, PAGE_SIZE * 3));
}
END_TEST()

FN_TEST(map_locked_updates_accounting)
{
	long before = TEST_RES(get_locked_kb(), _ret >= 0);
	char *mapping = TEST(
		mmap(NULL, PAGE_SIZE * 2, PROT_READ | PROT_WRITE,
		     MAP_PRIVATE | MAP_ANONYMOUS | MAP_LOCKED, -1, 0),
		0, _ret != MAP_FAILED);

	TEST_RES(get_locked_kb(), _ret == before + PAGE_SIZE * 2 / 1024);
	TEST_SUCC(munmap(mapping, PAGE_SIZE * 2));
	TEST_RES(get_locked_kb(), _ret == before);
}
END_TEST()

FN_TEST(mlockall_tracks_current_and_future_mappings)
{
	TEST_SUCC(mlockall(MCL_CURRENT));
	long current_locked = TEST_RES(get_locked_kb(), _ret >= 0);
	char *current_only = TEST(
		mmap(NULL, PAGE_SIZE, PROT_READ | PROT_WRITE,
		     MAP_PRIVATE | MAP_ANONYMOUS, -1, 0),
		0, _ret != MAP_FAILED);
	TEST_RES(get_locked_kb(), _ret == current_locked);
	TEST_SUCC(munmap(current_only, PAGE_SIZE));

	TEST_SUCC(munlockall());
	TEST_SUCC(mlockall(MCL_FUTURE));
	long before = TEST_RES(get_locked_kb(), _ret >= 0);
	char *future = TEST(
		mmap(NULL, PAGE_SIZE * 2, PROT_READ | PROT_WRITE,
		     MAP_PRIVATE | MAP_ANONYMOUS, -1, 0),
		0, _ret != MAP_FAILED);
	TEST_RES(get_locked_kb(), _ret == before + PAGE_SIZE * 2 / 1024);
	TEST_SUCC(munmap(future, PAGE_SIZE * 2));
	TEST_SUCC(munlockall());
}
END_TEST()
