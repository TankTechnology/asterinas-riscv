/* SPDX-License-Identifier: MPL-2.0 */

#define _GNU_SOURCE
#include <stdio.h>

#if defined(__x86_64__)
#include <errno.h>
#include <poll.h>
#include <signal.h>
#include <sys/ptrace.h>
#include <sys/syscall.h>
#include <sys/user.h>
#include <sys/wait.h>
#include <unistd.h>

enum { WAIT_ATTEMPTS = 500, WAIT_STEP_US = 10000 };
static volatile sig_atomic_t handler_count;

struct suspend_result {
	int returned;
	int error;
	int handled;
	int usr1_blocked;
	int usr2_blocked;
};

static void caught_usr1(int number)
{
	(void)number;
	++handler_count;
}

static void child_main(int ready, int output)
{
	struct sigaction action = { .sa_handler = caught_usr1 };
	sigemptyset(&action.sa_mask);
	sigset_t original, temporary;
	sigemptyset(&original);
	sigaddset(&original, SIGUSR1);
	sigaddset(&original, SIGUSR2);
	if (sigaction(SIGUSR1, &action, NULL) < 0 ||
	    sigprocmask(SIG_BLOCK, &original, NULL) < 0 ||
	    sigprocmask(SIG_SETMASK, NULL, &temporary) < 0)
		_exit(2);
	sigdelset(&temporary, SIGUSR1);
	if (ptrace(PTRACE_TRACEME, 0, NULL, NULL) < 0) {
		fprintf(stderr,
			"SIGSUSPEND_PTRACE FAIL phase=TRACEME errno=%d\n",
			errno);
		_exit(2);
	}
	if (raise(SIGSTOP) < 0 || write(ready, "R", 1) != 1)
		_exit(2);
	errno = 0;
	struct suspend_result result = { 0 };
	result.returned = sigsuspend(&temporary);
	result.error = errno;
	result.handled = handler_count;
	sigset_t restored;
	if (sigprocmask(SIG_SETMASK, NULL, &restored) < 0)
		_exit(2);
	result.usr1_blocked = sigismember(&restored, SIGUSR1);
	result.usr2_blocked = sigismember(&restored, SIGUSR2);
	if (write(output, &result, sizeof(result)) != sizeof(result))
		_exit(2);
	_exit(0);
}

static pid_t wait_bounded(pid_t *child, int *status, int options)
{
	for (int attempt = 0; attempt < WAIT_ATTEMPTS; ++attempt) {
		pid_t result = waitpid(*child, status, options | WNOHANG);
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

static int expect_stop(pid_t *child, int signal, const char *phase)
{
	int status = 0;
	pid_t result = wait_bounded(child, &status, WUNTRACED);
	if (result > 0 && WIFSTOPPED(status) && WSTOPSIG(status) == signal)
		return 1;
	fprintf(stderr, "SIGSUSPEND_PTRACE FAIL phase=%s wait=%d status=%d\n",
		phase, result, status);
	return 0;
}

static int receive_ready(int fd)
{
	for (int attempt = 0; attempt < WAIT_ATTEMPTS; ++attempt) {
		struct pollfd event = { .fd = fd, .events = POLLIN };
		int result = poll(&event, 1, WAIT_STEP_US / 1000);
		if (result < 0 && errno == EINTR)
			continue;
		if (result == 0)
			continue;
		char byte;
		return result > 0 && (event.revents & POLLIN) &&
		       read(fd, &byte, 1) == 1 && byte == 'R';
	}
	return 0;
}

static int reap_owned(pid_t *child)
{
	if (!*child)
		return 1;
	// A traced child may report a stop before its final SIGKILL exit.
	if (kill(*child, SIGKILL) < 0 && errno != ESRCH)
		return 0;
	for (int attempt = 0; attempt < WAIT_ATTEMPTS; ++attempt) {
		int status;
		pid_t result = waitpid(*child, &status, WNOHANG);
		if (result > 0 && (WIFEXITED(status) || WIFSIGNALED(status))) {
			*child = 0;
			return 1;
		}
		if (result < 0 && errno == ECHILD) {
			*child = 0;
			return 1;
		}
		if (result < 0 && errno != EINTR)
			return 0;
		usleep(WAIT_STEP_US);
	}
	return 0;
}

int main(void)
{
	int pipes[2][2] = { { -1, -1 }, { -1, -1 } };
	int *ready = pipes[0], *output = pipes[1];
	pid_t child = 0;
	int ok = 0;
	const char *phase = "pipe";
	for (int index = 0; index < 2; ++index)
		if (pipe(pipes[index]) < 0)
			goto cleanup;
	phase = "fork";
	child = fork();
	if (child < 0) {
		child = 0;
		goto cleanup;
	}
	if (child == 0) {
		close(ready[0]);
		close(output[0]);
		child_main(ready[1], output[1]);
	}
	close(ready[1]);
	ready[1] = -1;
	close(output[1]);
	output[1] = -1;
	phase = "initial-stop";
	if (!expect_stop(&child, SIGSTOP, phase))
		goto cleanup;
	phase = "initial-CONT";
	if (ptrace(PTRACE_CONT, child, NULL, NULL) < 0)
		goto cleanup;
	phase = "ready";
	if (!receive_ready(ready[0]))
		goto cleanup;
	usleep(50000);
	phase = "send-USR1";
	if (kill(child, SIGUSR1) < 0)
		goto cleanup;
	phase = "delivery-stop";
	if (!expect_stop(&child, SIGUSR1, phase))
		goto cleanup;
	struct user_regs_struct regs;
	phase = "GETREGS";
	if (ptrace(PTRACE_GETREGS, child, NULL, &regs) < 0)
		goto cleanup;
	printf("SIGSUSPEND_PTRACE before_rax=%lld orig_rax=%llu\n",
	       (long long)regs.rax, regs.orig_rax);
	phase = "suspended-syscall";
	if (regs.orig_rax != SYS_rt_sigsuspend)
		goto cleanup;
	// Removing the restart result must not remove deferred mask restoration.
	regs.rax = 0;
	phase = "SETREGS";
	if (ptrace(PTRACE_SETREGS, child, NULL, &regs) < 0)
		goto cleanup;
	phase = "suppress-USR1";
	if (ptrace(PTRACE_CONT, child, NULL, NULL) < 0)
		goto cleanup;
	int status = 0;
	phase = "exit";
	if (wait_bounded(&child, &status, 0) <= 0 || !WIFEXITED(status) ||
	    WEXITSTATUS(status) != 0)
		goto cleanup;
	struct suspend_result result;
	phase = "result";
	// The child exited and closed its writer, so this read cannot block.
	if (read(output[0], &result, sizeof(result)) != sizeof(result))
		goto cleanup;
	printf("SIGSUSPEND_PTRACE ret=%d errno=%d handled=%d usr1_blocked=%d usr2_blocked=%d\n",
	       result.returned, result.error, result.handled,
	       result.usr1_blocked, result.usr2_blocked);
	ok = result.returned == 0 && result.error == 0 && result.handled == 0 &&
	     result.usr1_blocked == 1 && result.usr2_blocked == 1;
cleanup:
	if (!ok)
		fprintf(stderr, "SIGSUSPEND_PTRACE FAIL phase=%s errno=%d\n",
			phase, errno);
	if (!reap_owned(&child)) {
		fprintf(stderr, "SIGSUSPEND_PTRACE FAIL phase=cleanup\n");
		ok = 0;
	}
	for (int index = 0; index < 2; ++index)
		for (int end = 0; end < 2; ++end)
			if (pipes[index][end] >= 0)
				close(pipes[index][end]);
	printf("SIGSUSPEND_PTRACE pass=%d\n", ok);
	return ok ? 0 : 1;
}
#else
int main(void)
{
	// Asterinas currently exposes ptrace register writes only on x86-64.
	printf("SIGSUSPEND_PTRACE SKIP architecture-register-access\n");
	return 77;
}
#endif
