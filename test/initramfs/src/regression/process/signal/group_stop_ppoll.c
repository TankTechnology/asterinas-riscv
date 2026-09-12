/* SPDX-License-Identifier: MPL-2.0 */

#define _GNU_SOURCE
#include <errno.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

enum { WAIT_ATTEMPTS = 500, WAIT_STEP_US = 10000 };
static volatile sig_atomic_t handler_count;

struct ppoll_result {
	long returned;
	int error;
	int handled;
	int usr1_blocked;
	int64_t elapsed_ns;
	struct timespec remaining;
};

static void caught_usr1(int number)
{
	(void)number;
	++handler_count;
}

static int receive(int fd, void *buffer, size_t length)
{
	for (int attempt = 0; attempt < WAIT_ATTEMPTS; ++attempt) {
		struct pollfd event = { .fd = fd, .events = POLLIN };
		int result = poll(&event, 1, WAIT_STEP_US / 1000);
		if (result == 0 || (result < 0 && errno == EINTR))
			continue;
		return result > 0 && (event.revents & POLLIN) &&
		       read(fd, buffer, length) == (ssize_t)length;
	}
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
	if (kill(*child, SIGKILL) < 0 && errno != ESRCH)
		return 0;
	int status;
	return wait_bounded(child, &status, 0) > 0 || !*child;
}

static void child_main(int ready_fd, int result_fd, int unbounded,
		       int interrupted, int readonly_timeout, int masked_stop)
{
	alarm(5);
	struct sigaction action = { .sa_handler = caught_usr1,
				    .sa_flags = SA_RESTART };
	sigemptyset(&action.sa_mask);
	if (sigaction(SIGUSR1, &action, NULL) < 0)
		_exit(2);
	sigset_t unblocked;
	sigemptyset(&unblocked);
	sigaddset(&unblocked, SIGUSR1);
	if (sigprocmask(masked_stop ? SIG_BLOCK : SIG_UNBLOCK, &unblocked,
			NULL) < 0)
		_exit(2);
	uint64_t temporary_mask = 0;

	struct timespec timeout =
		interrupted ? (struct timespec){ .tv_sec = 2 } :
			      (struct timespec){ .tv_nsec = 100000000 };
	struct timespec *timeout_ptr = unbounded ? NULL : &timeout;
	if (readonly_timeout) {
		long page_size = sysconf(_SC_PAGESIZE);
		if (page_size <= 0)
			_exit(2);
		timeout_ptr = mmap(NULL, page_size, PROT_READ | PROT_WRITE,
				   MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
		if (timeout_ptr == MAP_FAILED)
			_exit(2);
		*timeout_ptr = timeout;
		if (mprotect(timeout_ptr, page_size, PROT_READ) < 0)
			_exit(2);
	}
	if (write(ready_fd, "R", 1) != 1)
		_exit(2);

	struct ppoll_result result = { 0 };
	struct timespec started, finished;
	if (clock_gettime(CLOCK_MONOTONIC, &started) < 0)
		_exit(2);
	errno = 0;
	// libc ppoll hides timeout copyback; exercise the raw Linux ABI instead.
	// Linux uses a 64-bit kernel signal set on both x86-64 and RISC-V64.
	result.returned = syscall(SYS_ppoll, NULL, 0, timeout_ptr,
				  masked_stop ? &temporary_mask : NULL,
				  sizeof(uint64_t));
	result.error = errno;
	result.handled = handler_count;
	if (clock_gettime(CLOCK_MONOTONIC, &finished) < 0)
		_exit(2);
	result.elapsed_ns =
		(int64_t)(finished.tv_sec - started.tv_sec) * 1000000000 +
		finished.tv_nsec - started.tv_nsec;
	sigset_t restored;
	if (sigprocmask(SIG_SETMASK, NULL, &restored) < 0)
		_exit(2);
	result.usr1_blocked = sigismember(&restored, SIGUSR1);
	if (timeout_ptr)
		result.remaining = *timeout_ptr;
	if (write(result_fd, &result, sizeof(result)) != sizeof(result))
		_exit(2);
	_exit(0);
}

static int run_case(int unbounded, int interrupted, int readonly_timeout,
		    int masked_stop)
{
	int pipes[2][2] = { { -1, -1 }, { -1, -1 } };
	int *ready = pipes[0], *result_pipe = pipes[1];
	pid_t child = 0;
	int ok = 0, premature = 0;
	const char *phase = "setup";
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
		child_main(ready[1], result_pipe[1], unbounded, interrupted,
			   readonly_timeout, masked_stop);
	}
	close(ready[1]);
	ready[1] = -1;
	close(result_pipe[1]);
	result_pipe[1] = -1;
	phase = "ready";
	char byte;
	if (!receive(ready[0], &byte, 1) || byte != 'R')
		goto cleanup;
	// The readiness write precedes syscall entry. Allow the child to block.
	if (interrupted)
		usleep(50000);
	if (unbounded || masked_stop) {
		phase = "stop";
		if (kill(child, SIGSTOP) < 0)
			goto cleanup;
		int status;
		if (wait_bounded(&child, &status, WUNTRACED) <= 0 ||
		    !WIFSTOPPED(status) || WSTOPSIG(status) != SIGSTOP)
			goto cleanup;
		// Deliver under ppoll's temporary mask after STOP, even when failed
		// timeout copyback makes the interrupted call nonrestartable.
		if (masked_stop && kill(child, SIGUSR1) < 0)
			goto cleanup;
		if (kill(child, SIGCONT) < 0)
			goto cleanup;
		if (!masked_stop) {
			struct pollfd event = { .fd = result_pipe[0],
						.events = POLLIN };
			int observed = poll(&event, 1, 100);
			if (observed < 0)
				goto cleanup;
			premature = observed != 0;
		}
	}
	phase = "interrupt";
	if (interrupted && !masked_stop && !premature &&
	    kill(child, SIGUSR1) < 0)
		goto cleanup;
	phase = "result";
	struct ppoll_result result;
	if (!receive(result_pipe[0], &result, sizeof(result)))
		goto cleanup;
	printf("GROUP_STOP_PPOLL unbounded=%d interrupted=%d readonly=%d masked_stop=%d premature=%d ret=%ld errno=%d handled=%d usr1_blocked=%d elapsed_ns=%lld remaining=%lld.%09ld\n",
	       unbounded, interrupted, readonly_timeout, masked_stop, premature,
	       result.returned, result.error, result.handled,
	       result.usr1_blocked, (long long)result.elapsed_ns,
	       (long long)result.remaining.tv_sec, result.remaining.tv_nsec);
	ok = !premature && result.handled == interrupted &&
	     result.usr1_blocked == masked_stop;
	if (interrupted)
		ok &= result.returned == -1 && result.error == EINTR;
	else
		ok &= result.returned == 0 && result.elapsed_ns >= 100000000;
	if (!unbounded) {
		if (readonly_timeout) {
			// Failed copyback preserves successful returns and forces EINTR
			// for interruption, without replacing it with EFAULT.
			ok &= result.remaining.tv_sec ==
				      (interrupted ? 2 : 0) &&
			      result.remaining.tv_nsec ==
				      (interrupted ? 0 : 100000000);
		} else if (interrupted) {
			ok &= result.remaining.tv_sec >= 0 &&
			      result.remaining.tv_nsec >= 0 &&
			      result.remaining.tv_nsec < 1000000000 &&
			      (result.remaining.tv_sec ||
			       result.remaining.tv_nsec) &&
			      result.remaining.tv_sec < 2;
		} else {
			ok &= result.remaining.tv_sec == 0 &&
			      result.remaining.tv_nsec == 0;
		}
	}
	phase = "exit";
	int status;
	ok &= wait_bounded(&child, &status, 0) > 0 && WIFEXITED(status) &&
	      WEXITSTATUS(status) == 0;
cleanup:
	if (!reap_owned(&child)) {
		fprintf(stderr, "GROUP_STOP_PPOLL FAIL cleanup\n");
		ok = 0;
	}
	for (int index = 0; index < 2; ++index)
		for (int end = 0; end < 2; ++end)
			if (pipes[index][end] >= 0)
				close(pipes[index][end]);
	printf("GROUP_STOP_PPOLL_CASE unbounded=%d interrupted=%d readonly=%d masked_stop=%d phase=%s pass=%d\n",
	       unbounded, interrupted, readonly_timeout, masked_stop, phase,
	       ok);
	return ok;
}

int main(void)
{
	int passed = run_case(1, 1, 0, 0);
	for (int interrupted = 0; interrupted < 2; ++interrupted)
		for (int readonly_timeout = 0; readonly_timeout < 2;
		     ++readonly_timeout)
			passed += run_case(0, interrupted, readonly_timeout, 0);
	passed += run_case(0, 1, 1, 1);
	printf("GROUP_STOP_PPOLL passed=%d/6\n", passed);
	return passed == 6 ? 0 : 1;
}
