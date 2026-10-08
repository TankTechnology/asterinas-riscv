/* SPDX-License-Identifier: MPL-2.0 */

#define _GNU_SOURCE
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <ucontext.h>
#include <unistd.h>

#if !defined(__x86_64__) && !(defined(__riscv) && __riscv_xlen == 64)
#error "signal_restart_context requires an x86-64 or RISC-V 64 ucontext layout"
#endif

enum { WAIT_ATTEMPTS = 500, WAIT_STEP_US = 10000, USER_RETURN_VALUE = -512 };
static volatile sig_atomic_t handler_count;

struct context_result {
	long restart_return;
	long signal_return;
	int restart_error;
	int signal_error;
	int handled;
};

static void change_return_value(int number, siginfo_t *info, void *context)
{
	(void)number;
	(void)info;
	++handler_count;
	// Only the first delivery edits the context, bounding erroneous re-entry.
	if (handler_count != 1)
		return;
	ucontext_t *saved = context;
#if defined(__x86_64__)
	saved->uc_mcontext.gregs[REG_RAX] = USER_RETURN_VALUE;
#elif defined(__riscv) && __riscv_xlen == 64
	saved->uc_mcontext.__gregs[REG_A0] = (unsigned long)USER_RETURN_VALUE;
#endif
}

static void child_main(int output)
{
	struct context_result result = { 0 };
	// A fresh child has no saved restart operation.
	errno = 0;
	result.restart_return = syscall(SYS_restart_syscall);
	result.restart_error = errno;
	struct sigaction action = { .sa_sigaction = change_return_value,
				    .sa_flags = SA_SIGINFO };
	sigemptyset(&action.sa_mask);
	sigset_t mask;
	sigemptyset(&mask);
	sigaddset(&mask, SIGUSR1);
	if (sigaction(SIGUSR1, &action, NULL) < 0 ||
	    sigprocmask(SIG_UNBLOCK, &mask, NULL) < 0)
		_exit(2);
	pid_t pid = getpid();
	pid_t tid = syscall(SYS_gettid);
	errno = 0;
	// -512 is user context here, not an ERESTARTSYS result from the kernel.
	// The libc syscall wrapper converts this restored value to errno 512.
	result.signal_return = syscall(SYS_tgkill, pid, tid, SIGUSR1);
	result.signal_error = errno;
	result.handled = handler_count;
	if (write(output, &result, sizeof(result)) != sizeof(result))
		_exit(2);
	_exit(0);
}

static pid_t wait_bounded(pid_t *child, int *status)
{
	for (int attempt = 0; attempt < WAIT_ATTEMPTS; ++attempt) {
		pid_t result = waitpid(*child, status, WNOHANG);
		if (result > 0) {
			if (WIFEXITED(*status) || WIFSIGNALED(*status))
				*child = 0;
			return result;
		}
		if (result < 0 && errno != EINTR) {
			if (errno == ECHILD)
				*child = 0;
			return result;
		}
		usleep(WAIT_STEP_US);
	}
	return 0;
}

int main(void)
{
	int result_pipe[2];
	if (pipe(result_pipe) < 0)
		return 1;
	pid_t child = fork();
	if (child < 0) {
		close(result_pipe[0]);
		close(result_pipe[1]);
		return 1;
	}
	if (child == 0) {
		close(result_pipe[0]);
		child_main(result_pipe[1]);
	}
	close(result_pipe[1]);
	int status = 0;
	pid_t waited = wait_bounded(&child, &status);
	int ok = 0;
	if (waited > 0 && WIFEXITED(status) && WEXITSTATUS(status) == 0) {
		struct context_result result;
		// The writer has exited, so this read cannot wait for more data.
		if (read(result_pipe[0], &result, sizeof(result)) ==
		    sizeof(result)) {
			printf("SIGNAL_RESTART_CONTEXT direct_ret=%ld direct_errno=%d restored_ret=%ld restored_errno=%d handled=%d\n",
			       result.restart_return, result.restart_error,
			       result.signal_return, result.signal_error,
			       result.handled);
			ok = result.restart_return == -1 &&
			     result.restart_error == EINTR &&
			     result.signal_return == -1 &&
			     result.signal_error == -USER_RETURN_VALUE &&
			     result.handled == 1;
		}
	} else {
		fprintf(stderr,
			"SIGNAL_RESTART_CONTEXT FAIL phase=child-wait result=%d status=%d\n",
			waited, status);
	}
	// Consuming waits clear ownership before cleanup can signal a reused PID.
	if (child) {
		if ((kill(child, SIGKILL) < 0 && errno != ESRCH) ||
		    wait_bounded(&child, &status) <= 0) {
			fprintf(stderr,
				"SIGNAL_RESTART_CONTEXT FAIL phase=cleanup\n");
			ok = 0;
		}
	}
	close(result_pipe[0]);
	printf("SIGNAL_RESTART_CONTEXT pass=%d\n", ok);
	return ok ? 0 : 1;
}
