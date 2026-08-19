// SPDX-License-Identifier: MPL-2.0
//
// mlock/munlock smoke test for the Asterinas RISC-V track.
//
// Asterinas has no swap, so mlock/munlock are validated no-ops: a fully mapped
// page must lock/unlock successfully (0), and locking an unmapped address must
// fail with ENOMEM.

#define _GNU_SOURCE
#include <errno.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <unistd.h>

static int fails;

static void say(const char *s) { (void)write(1, s, strlen(s)); }
static void say_line(const char *s) { say(s); say("\n"); }

#define CHECK(cond, msg)                                             \
	do {                                                         \
		if (cond) {                                          \
			say_line("OK: " msg);                        \
		} else {                                             \
			say_line("FAIL: " msg);                      \
			fails++;                                     \
		}                                                    \
	} while (0)

int main(void)
{
	long page = sysconf(_SC_PAGESIZE);
	void *addr = mmap(NULL, page, PROT_READ | PROT_WRITE,
			  MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	CHECK(addr != MAP_FAILED, "mmap one page");

	errno = 0;
	CHECK(syscall(SYS_mlock, addr, page) == 0, "mlock(mapped page)");
	CHECK(errno == 0, "mlock errno clean");

	errno = 0;
	CHECK(syscall(SYS_munlock, addr, page) == 0, "munlock(mapped page)");
	CHECK(errno == 0, "munlock errno clean");

	/* Locking an unmapped address must fail (ENOMEM). */
	errno = 0;
	long r = syscall(SYS_mlock, (void *)0, page);
	CHECK(r == -1 && errno == ENOMEM, "mlock(NULL) -> ENOMEM");

	if (fails) {
		say_line("___MLOCK_FAIL__");
		return 1;
	}
	say_line("__MLOCK_OK__");
	return 0;
}
