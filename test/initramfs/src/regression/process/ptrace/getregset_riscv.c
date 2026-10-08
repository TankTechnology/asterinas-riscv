// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE

#include <asm/ptrace.h>
#include <elf.h>
#include <errno.h>
#include <signal.h>
#include <stddef.h>
#include <stdint.h>
#include <sys/ptrace.h>
#include <sys/uio.h>
#include <sys/wait.h>
#include <unistd.h>

#include "../../common/test.h"
#include "../../common/yama_ptrace_scope.h"

FN_TEST(getregset_riscv_prstatus)
{
	pid_t child = TEST_SUCC(fork());
	if (child == 0) {
		CHECK(ptrace(PTRACE_TRACEME, 0, 0, 0));
		CHECK(raise(SIGSTOP));
		for (;;)
			pause();
	}

	int status;
	TEST_RES(waitpid(child, &status, 0),
		 _ret == child && WIFSTOPPED(status) &&
			 WSTOPSIG(status) == SIGSTOP);

	struct user_regs_struct regs = { 0 };
	struct iovec iov = { .iov_base = &regs, .iov_len = sizeof(regs) };
	TEST_SUCC(ptrace(PTRACE_GETREGSET, child, (void *)NT_PRSTATUS, &iov));
	TEST_RES(iov.iov_len, _ret == sizeof(regs));
	TEST_RES(regs.pc, _ret != 0);
	TEST_RES(regs.sp, _ret != 0);

	unsigned long first_two[2] = { 0 };
	iov.iov_base = first_two;
	iov.iov_len = sizeof(first_two);
	TEST_SUCC(ptrace(PTRACE_GETREGSET, child, (void *)NT_PRSTATUS, &iov));
	TEST_RES(iov.iov_len, _ret == sizeof(first_two));
	TEST_RES(first_two[0], _ret == regs.pc);
	TEST_RES(first_two[1], _ret == regs.ra);

	struct {
		struct user_regs_struct regs;
		unsigned long guard;
	} large = { .guard = 0x5a5a5a5a5a5a5a5aUL };
	iov.iov_base = &large;
	iov.iov_len = sizeof(large);
	TEST_SUCC(ptrace(PTRACE_GETREGSET, child, (void *)NT_PRSTATUS, &iov));
	TEST_RES(iov.iov_len, _ret == sizeof(regs));
	TEST_RES(large.guard, _ret == 0x5a5a5a5a5a5a5a5aUL);
	TEST_RES(large.regs.pc, _ret == regs.pc);

	iov.iov_len = 1;
	TEST_ERRNO(ptrace(PTRACE_GETREGSET, child, (void *)NT_PRSTATUS, &iov),
		   EINVAL);
	iov.iov_len = sizeof(first_two);
	TEST_ERRNO(ptrace(PTRACE_GETREGSET, child, (void *)0xdead, &iov),
		   EINVAL);
	iov.iov_base = NULL;
	TEST_ERRNO(ptrace(PTRACE_GETREGSET, child, (void *)NT_PRSTATUS, &iov),
		   EFAULT);

	TEST_SUCC(ptrace(PTRACE_CONT, child, 0, 0));
	TEST_ERRNO(ptrace(PTRACE_GETREGSET, child, (void *)NT_PRSTATUS, &iov),
		   ESRCH);
	iov.iov_len = 1;
	TEST_ERRNO(ptrace(PTRACE_GETREGSET, child, (void *)0xdead, &iov),
		   ESRCH);
	TEST_ERRNO(ptrace(PTRACE_GETREGSET, child, (void *)NT_PRSTATUS,
			  (void *)-1),
		   ESRCH);
	TEST_SUCC(kill(child, SIGKILL));
	TEST_RES(waitpid(child, &status, 0),
		 _ret == child && WIFSIGNALED(status) &&
			 WTERMSIG(status) == SIGKILL);
}
END_TEST()

FN_TEST(attach_getregset_riscv_prstatus)
{
	SKIP_TEST_IF(read_yama_scope() == YAMA_SCOPE_NO_ATTACH);

	pid_t child = TEST_SUCC(fork());
	if (child == 0) {
		for (;;)
			pause();
	}

	TEST_SUCC(ptrace(PTRACE_ATTACH, child, 0, 0));
	int status;
	TEST_RES(waitpid(child, &status, 0),
		 _ret == child && WIFSTOPPED(status) &&
			 WSTOPSIG(status) == SIGSTOP);

	struct user_regs_struct regs = { 0 };
	struct iovec iov = { .iov_base = &regs, .iov_len = sizeof(regs) };
	TEST_SUCC(ptrace(PTRACE_GETREGSET, child, (void *)NT_PRSTATUS, &iov));
	TEST_RES(iov.iov_len, _ret == sizeof(regs));
	TEST_RES(regs.pc, _ret != 0);
	TEST_RES(regs.sp, _ret != 0);

	TEST_SUCC(ptrace(PTRACE_DETACH, child, 0, 0));
	TEST_SUCC(kill(child, SIGKILL));
	TEST_RES(waitpid(child, &status, 0),
		 _ret == child && WIFSIGNALED(status) &&
			 WTERMSIG(status) == SIGKILL);
}
END_TEST()
