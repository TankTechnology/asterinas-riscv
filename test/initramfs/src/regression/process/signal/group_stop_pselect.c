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

struct pselect_result {
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

// The child has no blocking operation between readiness and pselect6.
// Require both sleeping state and the temporary mask, so STOP cannot race
// ahead of syscall entry merely because the child was descheduled.
static int wait_in_pselect(pid_t child)
{
	char path[64], line[256];
	snprintf(path, sizeof(path), "/proc/%d/status", child);
	for (int attempt = 0; attempt < WAIT_ATTEMPTS; ++attempt) {
		FILE *file = fopen(path, "r");
		if (!file)
			return 0;
		char state = 0;
		unsigned long long blocked = ~0ULL;
		while (fgets(line, sizeof(line), file)) {
			sscanf(line, "State: %c", &state);
			sscanf(line, "SigBlk: %llx", &blocked);
		}
		int read_failed = ferror(file);
		if (fclose(file) != 0 || read_failed)
			return 0;
		if (state == 'S' && !(blocked & (1ULL << (SIGUSR1 - 1))))
			return 1;
		if (state == 'Z')
			return 0;
		usleep(WAIT_STEP_US);
	}
	return 0;
}

static void child_main(int ready_fd, int result_fd, int unbounded,
		       int interrupted, int readonly_timeout,
		       int stop_requested)
{
	alarm(5);
	struct sigaction action = { .sa_handler = caught_usr1,
				    .sa_flags = SA_RESTART };
	sigemptyset(&action.sa_mask);
	if (sigaction(SIGUSR1, &action, NULL) < 0)
		_exit(2);
	sigset_t blocked;
	sigemptyset(&blocked);
	sigaddset(&blocked, SIGUSR1);
	if (sigprocmask(SIG_BLOCK, &blocked, NULL) < 0)
		_exit(2);
	uint64_t temporary_mask = 0;
	struct {
		uint64_t *mask;
		size_t size;
	} mask_arg = { &temporary_mask, sizeof(temporary_mask) };

	struct timespec timeout =
		interrupted || stop_requested ?
			(struct timespec){ .tv_sec = 2 } :
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

	struct pselect_result result = { 0 };
	struct timespec started, finished;
	if (clock_gettime(CLOCK_MONOTONIC, &started) < 0)
		_exit(2);
	errno = 0;
	// libc pselect hides timeout copyback; exercise the raw Linux ABI.
	// Linux uses a 64-bit kernel signal set on both x86-64 and RISC-V64.
	result.returned = syscall(SYS_pselect6, 0, NULL, NULL, NULL,
				  timeout_ptr, &mask_arg);
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
		    int stop_requested, int queue_usr1)
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
			   readonly_timeout, stop_requested);
	}
	close(ready[1]);
	ready[1] = -1;
	close(result_pipe[1]);
	result_pipe[1] = -1;
	phase = "ready";
	char byte;
	if (!receive(ready[0], &byte, 1) || byte != 'R')
		goto cleanup;
	phase = "syscall-entry";
	if ((interrupted || stop_requested) && !wait_in_pselect(child))
		goto cleanup;
	if (stop_requested) {
		phase = "stop";
		if (kill(child, SIGSTOP) < 0)
			goto cleanup;
		int status;
		if (wait_bounded(&child, &status, WUNTRACED) <= 0 ||
		    !WIFSTOPPED(status) || WSTOPSIG(status) != SIGSTOP)
			goto cleanup;
		// Deliver under pselect6's temporary mask after STOP, even when failed
		// timeout copyback makes the interrupted call nonrestartable.
		if (queue_usr1 && kill(child, SIGUSR1) < 0)
			goto cleanup;
		if (kill(child, SIGCONT) < 0)
			goto cleanup;
		if (!queue_usr1 && !readonly_timeout) {
			struct pollfd event = { .fd = result_pipe[0],
						.events = POLLIN };
			int observed = poll(&event, 1, 100);
			if (observed < 0)
				goto cleanup;
			premature = observed != 0;
			if (!premature && !wait_in_pselect(child))
				goto cleanup;
		}
	}
	phase = "interrupt";
	if (interrupted && !queue_usr1 && !premature &&
	    kill(child, SIGUSR1) < 0)
		goto cleanup;
	phase = "result";
	struct pselect_result result;
	if (!receive(result_pipe[0], &result, sizeof(result)))
		goto cleanup;
	printf("GROUP_STOP_PSELECT unbounded=%d interrupted=%d readonly=%d stop_requested=%d queue_usr1=%d premature=%d ret=%ld errno=%d handled=%d usr1_blocked=%d elapsed_ns=%lld remaining=%lld.%09ld\n",
	       unbounded, interrupted, readonly_timeout, stop_requested,
	       queue_usr1, premature, result.returned, result.error,
	       result.handled, result.usr1_blocked,
	       (long long)result.elapsed_ns, (long long)result.remaining.tv_sec,
	       result.remaining.tv_nsec);
	ok = !premature && result.handled == interrupted &&
	     result.usr1_blocked == 1;
	// A read-only timeout prevents replay even when STOP has no handler.
	if (interrupted || (stop_requested && readonly_timeout))
		ok &= result.returned == -1 && result.error == EINTR;
	else
		ok &= result.returned == 0 &&
		      result.elapsed_ns >=
			      (stop_requested ? 2000000000LL : 100000000);
	if (!unbounded) {
		if (readonly_timeout) {
			// Failed copyback preserves successful returns and forces EINTR
			// for interruption, without replacing it with EFAULT.
			ok &= result.remaining.tv_sec ==
				      (interrupted || stop_requested ? 2 : 0) &&
			      result.remaining.tv_nsec ==
				      (interrupted || stop_requested ?
					       0 :
					       100000000);
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
		fprintf(stderr, "GROUP_STOP_PSELECT FAIL cleanup\n");
		ok = 0;
	}
	for (int index = 0; index < 2; ++index)
		for (int end = 0; end < 2; ++end)
			if (pipes[index][end] >= 0)
				close(pipes[index][end]);
	printf("GROUP_STOP_PSELECT_CASE unbounded=%d interrupted=%d readonly=%d stop_requested=%d queue_usr1=%d phase=%s pass=%d\n",
	       unbounded, interrupted, readonly_timeout, stop_requested,
	       queue_usr1, phase, ok);
	return ok;
}

int main(void)
{
	int passed = run_case(1, 1, 0, 1, 0);
	for (int interrupted = 0; interrupted < 2; ++interrupted)
		for (int readonly_timeout = 0; readonly_timeout < 2;
		     ++readonly_timeout)
			passed += run_case(0, interrupted, readonly_timeout, 0,
					   0);
	passed += run_case(0, 1, 0, 1, 1);
	passed += run_case(0, 1, 1, 1, 1);
	passed += run_case(0, 0, 0, 1, 0);
	passed += run_case(0, 0, 1, 1, 0);
	printf("GROUP_STOP_PSELECT passed=%d/9\n", passed);
	return passed == 9 ? 0 : 1;
}
