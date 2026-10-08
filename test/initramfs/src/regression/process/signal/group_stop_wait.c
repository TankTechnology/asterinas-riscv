/* SPDX-License-Identifier: MPL-2.0 */

#define _GNU_SOURCE
#include <errno.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

enum { WAIT_ATTEMPTS = 500, WAIT_STEP_US = 10000 };
static volatile sig_atomic_t handler_count;

struct ready_message {
	pid_t tid;
	char phase;
};

struct wait_args {
	int ready;
	int result;
	int use_sigsuspend;
};

struct wait_result {
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

static void *signal_waiter(void *argument)
{
	struct wait_args *args = argument;
	sigset_t blocked, temporary;
	sigemptyset(&blocked);
	sigaddset(&blocked, SIGUSR2);
	if (args->use_sigsuspend)
		sigaddset(&blocked, SIGUSR1);
	if (sigprocmask(SIG_BLOCK, &blocked, NULL) < 0)
		_exit(2);
	if (!args->use_sigsuspend) {
		sigemptyset(&blocked);
		sigaddset(&blocked, SIGUSR1);
		if (sigprocmask(SIG_UNBLOCK, &blocked, NULL) < 0)
			_exit(2);
	}
	if (sigprocmask(SIG_SETMASK, NULL, &temporary) < 0)
		_exit(2);
	sigdelset(&temporary, SIGUSR1);
	struct ready_message ready = { .tid = syscall(SYS_gettid),
				       .phase = 'W' };
	if (write(args->ready, &ready, sizeof(ready)) != sizeof(ready))
		_exit(2);
	// libc sigsuspend invokes rt_sigsuspend with the architecture's mask size.
	struct wait_result result = { 0 };
	result.returned = args->use_sigsuspend ? sigsuspend(&temporary) :
						 pause();
	result.error = errno;
	result.handled = handler_count;
	sigset_t restored;
	if (sigprocmask(SIG_SETMASK, NULL, &restored) < 0)
		_exit(2);
	result.usr1_blocked = sigismember(&restored, SIGUSR1);
	result.usr2_blocked = sigismember(&restored, SIGUSR2);
	if (write(args->result, &result, sizeof(result)) != sizeof(result))
		_exit(2);
	_exit(0);
}

static int receive(int fd, void *buffer, size_t length, const char *phase)
{
	for (int attempt = 0; attempt < WAIT_ATTEMPTS; ++attempt) {
		struct pollfd event = { .fd = fd, .events = POLLIN };
		int result = poll(&event, 1, WAIT_STEP_US / 1000);
		if (result < 0 && errno == EINTR)
			continue;
		if (result == 0)
			continue;
		if (result > 0 && (event.revents & POLLIN) &&
		    read(fd, buffer, length) == (ssize_t)length)
			return 1;
		fprintf(stderr, "GROUP_STOP_WAIT FAIL phase=%s pipe\n", phase);
		return 0;
	}
	fprintf(stderr, "GROUP_STOP_WAIT FAIL phase=%s timeout\n", phase);
	return 0;
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

static int reap_owned(pid_t *child)
{
	if (!*child)
		return 1;
	// Every consuming wait clears ownership before cleanup signals a PID.
	if (kill(*child, SIGKILL) < 0 && errno != ESRCH)
		return 0;
	int status;
	return wait_bounded(child, &status, 0) > 0 || !*child;
}

static int run_case(int use_sigsuspend, int sibling, int queued_while_stopped)
{
	int pipes[2][2] = { { -1, -1 }, { -1, -1 } };
	int *ready = pipes[0], *result_pipe = pipes[1];
	pid_t child = 0;
	int ok = 0;
	for (int index = 0; index < 2; ++index)
		if (pipe(pipes[index]) < 0)
			goto cleanup;
	child = fork();
	if (child < 0) {
		child = 0;
		goto cleanup;
	}
	if (child == 0) {
		close(ready[0]);
		close(result_pipe[0]);
		alarm(30);
		struct sigaction action = { .sa_handler = caught_usr1 };
		sigemptyset(&action.sa_mask);
		if (sigaction(SIGUSR1, &action, NULL) < 0)
			_exit(2);
		struct wait_args args = { ready[1], result_pipe[1],
					  use_sigsuspend };
		if (!sibling)
			signal_waiter(&args);
		pthread_t worker;
		if (pthread_create(&worker, NULL, signal_waiter, &args))
			_exit(2);
		struct ready_message message = { .tid = getpid(),
						 .phase = 'M' };
		if (write(ready[1], &message, sizeof(message)) !=
		    sizeof(message))
			_exit(2);
		// The worker exits the process; the alarm and parent bound this pause.
		while (pause() < 0 && errno == EINTR)
			;
		_exit(2);
	}
	close(ready[1]);
	ready[1] = -1;
	close(result_pipe[1]);
	result_pipe[1] = -1;
	pid_t waiter_tid = 0;
	int main_ready = !sibling;
	for (int index = 0; index < 1 + sibling; ++index) {
		struct ready_message message;
		if (!receive(ready[0], &message, sizeof(message), "ready"))
			goto cleanup;
		if (message.phase == 'W')
			waiter_tid = message.tid;
		main_ready |= message.phase == 'M' && message.tid == child;
	}
	if (waiter_tid <= 0 || !main_ready ||
	    (sibling ? waiter_tid == child : waiter_tid != child))
		goto cleanup;
	// Readiness precedes the wait; allow both threads to enter their syscalls.
	usleep(50000);
	if (syscall(SYS_tgkill, child, child, SIGSTOP) < 0)
		goto cleanup;
	int status = 0;
	pid_t waited = wait_bounded(&child, &status, WUNTRACED);
	if (waited <= 0 || !WIFSTOPPED(status) || WSTOPSIG(status) != SIGSTOP) {
		fprintf(stderr,
			"GROUP_STOP_WAIT FAIL phase=stop-wait result=%d status=%d\n",
			waited, status);
		goto cleanup;
	}
	if (queued_while_stopped &&
	    syscall(SYS_tgkill, child, waiter_tid, SIGUSR1) < 0)
		goto cleanup;
	if (kill(child, SIGCONT) < 0)
		goto cleanup;
	int premature = 0;
	if (!queued_while_stopped) {
		// STOP/CONT alone must not complete pause or restore sigsuspend's mask.
		struct pollfd event = { .fd = result_pipe[0],
					.events = POLLIN };
		int observed = poll(&event, 1, 100);
		if (observed < 0)
			goto cleanup;
		premature = observed != 0;
		if (!premature &&
		    syscall(SYS_tgkill, child, waiter_tid, SIGUSR1) < 0)
			goto cleanup;
	}
	struct wait_result result;
	if (!receive(result_pipe[0], &result, sizeof(result), "wait-result"))
		goto cleanup;
	printf("GROUP_STOP_WAIT sigsuspend=%d sibling=%d queued=%d premature=%d ret=%d errno=%d handled=%d usr1_blocked=%d usr2_blocked=%d\n",
	       use_sigsuspend, sibling, queued_while_stopped, premature,
	       result.returned, result.error, result.handled,
	       result.usr1_blocked, result.usr2_blocked);
	waited = wait_bounded(&child, &status, 0);
	ok = !premature && result.returned == -1 && result.error == EINTR &&
	     result.handled == 1 && result.usr1_blocked == use_sigsuspend &&
	     result.usr2_blocked == 1 && waited > 0 && WIFEXITED(status) &&
	     WEXITSTATUS(status) == 0;
cleanup:
	if (!reap_owned(&child)) {
		fprintf(stderr, "GROUP_STOP_WAIT FAIL phase=cleanup\n");
		ok = 0;
	}
	for (int index = 0; index < 2; ++index)
		for (int end = 0; end < 2; ++end)
			if (pipes[index][end] >= 0)
				close(pipes[index][end]);
	printf("GROUP_STOP_WAIT sigsuspend=%d sibling=%d queued=%d pass=%d\n",
	       use_sigsuspend, sibling, queued_while_stopped, ok);
	return ok;
}

int main(void)
{
	int passed = 0;
	for (int use_sigsuspend = 0; use_sigsuspend < 2; ++use_sigsuspend)
		for (int sibling = 0; sibling < 2; ++sibling)
			passed += run_case(use_sigsuspend, sibling, 0);
	for (int sibling = 0; sibling < 2; ++sibling)
		passed += run_case(1, sibling, 1);
	printf("GROUP_STOP_WAIT passed=%d/6\n", passed);
	return passed == 6 ? 0 : 1;
}
