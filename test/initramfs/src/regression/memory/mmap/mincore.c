// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE
#include <errno.h>
#include <sys/mman.h>

#include "../../common/test.h"

#define PAGE_SIZE 4096

static unsigned char *mapping;

FN_SETUP(init)
{
	mapping = CHECK_WITH(mmap(NULL, PAGE_SIZE * 3, PROT_READ | PROT_WRITE,
				  MAP_PRIVATE | MAP_ANONYMOUS, -1, 0),
			     _ret != MAP_FAILED);
}
END_SETUP()

FN_TEST(reports_anonymous_page_residency)
{
	unsigned char vec[2] = { 0xff, 0xff };

	TEST_SUCC(mincore(mapping, PAGE_SIZE * 2, vec));
	TEST_RES(vec[0], (_ret & 1) == 0);
	TEST_RES(vec[1], (_ret & 1) == 0);

	mapping[0] = 1;
	TEST_SUCC(mincore(mapping, PAGE_SIZE * 2, vec));
	TEST_RES(vec[0], (_ret & 1) == 1);
	TEST_RES(vec[1], (_ret & 1) == 0);
}
END_TEST()

FN_TEST(validates_arguments)
{
	unsigned char vec[3];

	TEST_ERRNO(mincore(mapping + 1, PAGE_SIZE, vec), EINVAL);
	TEST_ERRNO(mincore(mapping, PAGE_SIZE, NULL), EFAULT);

	TEST_SUCC(munmap(mapping + PAGE_SIZE, PAGE_SIZE));
	TEST_ERRNO(mincore(mapping, PAGE_SIZE * 3, vec), ENOMEM);
}
END_TEST()
