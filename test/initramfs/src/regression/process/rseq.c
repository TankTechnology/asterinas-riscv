// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <unistd.h>

/* Linux's original rseq ABI permits a 32-byte allocation aligned to 32 bytes.
 * The syscall signature is not a field following that allocation.
 * https://github.com/torvalds/linux/blob/v6.12/include/uapi/linux/rseq.h
 */
#define RSEQ_ABI_SIZE 32
#define RSEQ_UNREGISTER 1
#define RSEQ_SIGNATURE 0x12345678U

/* glibc exports a nonzero size when it owns a registration. Keep this weak
 * so the test also links against libcs which do not export this interface.
 */
extern const unsigned int __rseq_size __attribute__((weak));

static struct {
	uint32_t cpu_id_start;
	uint32_t cpu_id;
	uint64_t rseq_cs;
	uint32_t flags;
	uint32_t reserved[3];
	unsigned char canary[RSEQ_ABI_SIZE];
} area __attribute__((aligned(RSEQ_ABI_SIZE)));

static void reset_area(void)
{
	memset(&area, 0, sizeof(area));
	area.cpu_id = UINT32_MAX;
	memset(area.canary, 0xa5, sizeof(area.canary));
}

static void test_registration_bounds(void)
{
	reset_area();
	unsigned char saved[sizeof(area)];
	memcpy(saved, &area, sizeof(area));
	errno = 0;
	long result =
		syscall(SYS_rseq, &area, RSEQ_ABI_SIZE, 0, RSEQ_SIGNATURE);
	int error = errno;
	int canary_intact = !memcmp(area.canary, saved + RSEQ_ABI_SIZE,
				    sizeof(area.canary));
	printf("rseq registration: result=%ld errno=%d canary_intact=%d\n",
	       result, error, canary_intact);
	/* Linux may reject a different area when glibc owns the registration. */
	assert(result == 0 ||
	       (result == -1 &&
		(error == ENOSYS || error == EBUSY ||
		 (error == EINVAL && &__rseq_size && __rseq_size))));
	if (result == 0) {
		assert(syscall(SYS_rseq, &area, RSEQ_ABI_SIZE, RSEQ_UNREGISTER,
			       RSEQ_SIGNATURE) == 0);
	} else {
		assert(memcmp(saved, &area, sizeof(area)) == 0);
	}
	assert(canary_intact);
	assert(memcmp(area.canary, saved + RSEQ_ABI_SIZE,
		      sizeof(area.canary)) == 0);
}

static void expect_enosys(void *address, size_t size, unsigned int flags)
{
	unsigned char saved[sizeof(area)];
	memcpy(saved, &area, sizeof(area));
	errno = 0;
	long result = syscall(SYS_rseq, address, size, flags, RSEQ_SIGNATURE);
	int error = errno;
	printf("rseq fallback: len=%zu flags=%u result=%ld errno=%d\n", size,
	       flags, result, error);
	assert(result == -1 && error == ENOSYS);
	assert(memcmp(saved, &area, sizeof(area)) == 0);
}

/* Asterinas must reject all requests until it supports the restart protocol.
 * This explicit mode also tests that invalid arguments cannot cause accesses.
 */
static void test_unavailable(void)
{
	reset_area();
	expect_enosys(&area, RSEQ_ABI_SIZE, 0);
	expect_enosys(&area, RSEQ_ABI_SIZE, 0);
	expect_enosys(&area, RSEQ_ABI_SIZE, RSEQ_UNREGISTER);
	expect_enosys(&area, RSEQ_ABI_SIZE, 2);
	expect_enosys(&area, RSEQ_ABI_SIZE, RSEQ_UNREGISTER | 2);
	expect_enosys(NULL, RSEQ_ABI_SIZE, 0);
	expect_enosys(&area, RSEQ_ABI_SIZE - 1, 0);
	expect_enosys((char *)&area + 1, RSEQ_ABI_SIZE, 0);
	long page_size = sysconf(_SC_PAGESIZE);
	assert(page_size > 0);
	void *unreadable = mmap(NULL, page_size, PROT_NONE,
				MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	assert(unreadable != MAP_FAILED);
	expect_enosys(unreadable, RSEQ_ABI_SIZE, 0);
	assert(munmap(unreadable, page_size) == 0);
}

static void *test_thread(void *require_enosys)
{
	test_registration_bounds();
	if (*(int *)require_enosys)
		test_unavailable();
	return NULL;
}

int main(int argc, char **argv)
{
	setbuf(stdout, NULL);
	_Static_assert(__builtin_offsetof(__typeof__(area), canary) ==
			       RSEQ_ABI_SIZE,
		       "canary must immediately follow the ABI allocation");
	assert(argc == 1 ||
	       (argc == 2 && strcmp(argv[1], "--require-enosys") == 0));
	int require_enosys = argc == 2;
#ifdef __asterinas__
	require_enosys = 1;
#endif
	test_thread(&require_enosys);
	/* Exercise libc's registration attempts during thread startup and exit. */
	pthread_t thread;
	assert(pthread_create(&thread, NULL, test_thread, &require_enosys) ==
	       0);
	assert(pthread_join(thread, NULL) == 0);
	puts("rseq: PASS");
	return 0;
}
