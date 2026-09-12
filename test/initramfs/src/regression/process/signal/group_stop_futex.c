/* SPDX-License-Identifier: MPL-2.0 */

#define _GNU_SOURCE
#include <errno.h>
#include <linux/futex.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

enum {
	WAIT_ATTEMPTS = 500,
	WAIT_STEP_US = 10000,
	TIMEOUT_MS = 1000,
	CONTINUE_LIMIT_MS = 500
};
enum wait_api { RELATIVE, ABSOLUTE, UNTIMED };
static const char *const api_names[] = { "wait-relative", "wait-bitset",
					 "wait-untimed" };
static volatile sig_atomic_t handler_count;
static int handler_pipe;

struct notice {
	pid_t tid;
	char phase;
};

struct wait_args {
	int ready;
	int result;
	int *word;
	enum wait_api api;
	int caught_signal;
};

struct wait_result {
	struct timespec end;
	int returned;
	int error;
	int handled;
};

static void caught_usr1(int number)
{
	(void)number;
	int saved_errno = errno;
	++handler_count;
	struct notice notice = { .phase = 'H' };
	if (write(handler_pipe, &notice, sizeof(notice)) != sizeof(notice))
		_exit(2);
	errno = saved_errno;
}

static void *waiter(void *argument)
{
	struct wait_args *args = argument;
	struct timespec timeout = { .tv_sec = TIMEOUT_MS / 1000 };
	if (args->caught_signal)
		timeout.tv_sec *= 2;
	if (args->api == ABSOLUTE) {
		struct timespec now;
		if (clock_gettime(CLOCK_MONOTONIC, &now) < 0)
			_exit(2);
		timeout.tv_sec += now.tv_sec;
		timeout.tv_nsec = now.tv_nsec;
	}
	struct notice notice = { .tid = syscall(SYS_gettid), .phase = 'W' };
	if (write(args->ready, &notice, sizeof(notice)) != sizeof(notice))
		_exit(2);
	struct wait_result result = { 0 };
	result.returned =
		syscall(SYS_futex, args->word,
			args->api == ABSOLUTE ? FUTEX_WAIT_BITSET : FUTEX_WAIT,
			0, args->api == UNTIMED ? NULL : &timeout, NULL,
			FUTEX_BITSET_MATCH_ANY);
	result.error = result.returned < 0 ? errno : 0;
	result.handled = handler_count;
	if (clock_gettime(CLOCK_MONOTONIC, &result.end) < 0 ||
	    write(args->result, &result, sizeof(result)) != sizeof(result))
		_exit(2);
	_exit(0);
}

static int receive(int fd, void *buffer, size_t length, const char *phase)
{
	for (int attempt = 0; attempt < WAIT_ATTEMPTS; ++attempt) {
		struct pollfd event = { .fd = fd, .events = POLLIN };
		int result = poll(&event, 1, WAIT_STEP_US / 1000);
		if ((result < 0 && errno == EINTR) || result == 0)
			continue;
		if (result > 0 && (event.revents & POLLIN) &&
		    read(fd, buffer, length) == (ssize_t)length)
			return 1;
		fprintf(stderr, "GROUP_STOP_FUTEX FAIL phase=%s pipe\n", phase);
		return 0;
	}
	fprintf(stderr, "GROUP_STOP_FUTEX FAIL phase=%s timeout\n", phase);
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
	// Consuming waits clear ownership before cleanup can signal a PID.
	if (kill(*child, SIGKILL) < 0 && errno != ESRCH)
		return 0;
	int status;
	return wait_bounded(child, &status, 0) > 0 || !*child;
}

static long long elapsed_ns(struct timespec start, struct timespec end)
{
	return (end.tv_sec - start.tv_sec) * 1000000000LL + end.tv_nsec -
	       start.tv_nsec;
}

static int run_case(enum wait_api api, int sibling, int caught_signal)
{
	int pipes[2][2] = { { -1, -1 }, { -1, -1 } };
	int *ready = pipes[0], *result_pipe = pipes[1];
	int *word = MAP_FAILED;
	pid_t child = 0;
	int ok = 0;
	word = mmap(NULL, sizeof(*word), PROT_READ | PROT_WRITE,
		    MAP_SHARED | MAP_ANONYMOUS, -1, 0);
	if (word == MAP_FAILED)
		goto cleanup;
	*word = 0;
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
		if (caught_signal) {
			handler_pipe = ready[1];
			struct sigaction action = { .sa_handler = caught_usr1,
						    .sa_flags = SA_RESTART };
			sigemptyset(&action.sa_mask);
			if (sigaction(SIGUSR1, &action, NULL) < 0)
				_exit(2);
		}
		struct wait_args args = { ready[1], result_pipe[1], word, api,
					  caught_signal };
		if (!sibling)
			waiter(&args);
		pthread_t worker;
		if (pthread_create(&worker, NULL, waiter, &args))
			_exit(2);
		struct notice notice = { .tid = getpid(), .phase = 'M' };
		if (write(ready[1], &notice, sizeof(notice)) != sizeof(notice))
			_exit(2);
		// The worker exits the process; parent and alarm bound this pause.
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
		struct notice notice;
		if (!receive(ready[0], &notice, sizeof(notice), "ready"))
			goto cleanup;
		if (notice.phase == 'W')
			waiter_tid = notice.tid;
		main_ready |= notice.phase == 'M' && notice.tid == child;
	}
	if (waiter_tid <= 0 || !main_ready ||
	    (sibling ? waiter_tid == child : waiter_tid != child))
		goto cleanup;
	// The handshake precedes futex/pause; allow both to enter the kernel.
	usleep(50000);
	if (syscall(SYS_tgkill, child, child, SIGSTOP) < 0)
		goto cleanup;
	int status = 0;
	pid_t waited = wait_bounded(&child, &status, WUNTRACED);
	if (waited <= 0 || !WIFSTOPPED(status) || WSTOPSIG(status) != SIGSTOP) {
		fprintf(stderr,
			"GROUP_STOP_FUTEX FAIL phase=stop-wait result=%d status=%d\n",
			waited, status);
		goto cleanup;
	}
	if (caught_signal &&
	    syscall(SYS_tgkill, child, waiter_tid, SIGUSR1) < 0)
		goto cleanup;
	usleep(caught_signal ? 100000 : (TIMEOUT_MS + 200) * 1000);
	struct timespec continued;
	if (clock_gettime(CLOCK_MONOTONIC, &continued) < 0 ||
	    kill(child, SIGCONT) < 0)
		goto cleanup;
	int woken = 0;
	if (api == UNTIMED) {
		struct notice notice;
		if (!receive(ready[0], &notice, sizeof(notice), "handler") ||
		    notice.phase != 'H')
			goto cleanup;
		// The handler handshake precedes sigreturn; wait for re-enrollment.
		for (int attempt = 0; attempt < WAIT_ATTEMPTS; ++attempt) {
			struct pollfd event = { .fd = result_pipe[0],
						.events = POLLIN };
			if (poll(&event, 1, 0) != 0)
				break;
			woken = syscall(SYS_futex, word, FUTEX_WAKE, 1);
			if (woken != 0)
				break;
			usleep(WAIT_STEP_US);
		}
	} else if (!caught_signal) {
		// Linux restart_syscall preserves the original deadline across STOP.
		// A reset relative timeout is still queued and incorrectly wakes here.
		usleep(100000);
		woken = syscall(SYS_futex, word, FUTEX_WAKE, 1);
	}
	struct wait_result result;
	if (!receive(result_pipe[0], &result, sizeof(result), "futex-result"))
		goto cleanup;
	long long after_cont_ns = elapsed_ns(continued, result.end);
	printf("GROUP_STOP_FUTEX api=%s sibling=%d caught=%d ret=%d error=%d "
	       "handled=%d woken=%d after_cont_ms=%lld\n",
	       api_names[api], sibling, caught_signal, result.returned,
	       result.error, result.handled, woken, after_cont_ns / 1000000);
	int expected_error = api == UNTIMED ? 0 :
			     caught_signal  ? EINTR :
					      ETIMEDOUT;
	// If the parent wakes late, ETIMEDOUT alone can hide a reset deadline.
	// This allows 500 ms of scheduling delay, below the reset 1000 ms wait;
	// extreme QEMU load can therefore fail an otherwise correct kernel.
	int deadline_ok = caught_signal || api == UNTIMED ||
			  (after_cont_ns >= 0 &&
			   after_cont_ns < CONTINUE_LIMIT_MS * 1000000LL);
	waited = wait_bounded(&child, &status, 0);
	ok = result.error == expected_error &&
	     result.returned == (expected_error ? -1 : 0) &&
	     result.handled == caught_signal && woken == (api == UNTIMED) &&
	     deadline_ok && waited > 0 && WIFEXITED(status) &&
	     WEXITSTATUS(status) == 0;
cleanup:
	if (!reap_owned(&child)) {
		fprintf(stderr, "GROUP_STOP_FUTEX FAIL phase=cleanup-reap\n");
		ok = 0;
	}
	for (int index = 0; index < 2; ++index)
		for (int end = 0; end < 2; ++end)
			if (pipes[index][end] >= 0)
				close(pipes[index][end]);
	if (word != MAP_FAILED)
		munmap(word, sizeof(*word));
	printf("GROUP_STOP_FUTEX api=%s sibling=%d caught=%d pass=%d\n",
	       api_names[api], sibling, caught_signal, ok);
	return ok;
}

int main(void)
{
	setbuf(stdout, NULL);
	// Invalid clock flags must fail even when the value would not block.
	int word = 1;
	int returned = syscall(SYS_futex, &word,
			       FUTEX_WAIT | FUTEX_CLOCK_REALTIME, 0, NULL);
	int error = returned < 0 ? errno : 0;
	int passed = returned == -1 && error == ENOSYS;
	printf("GROUP_STOP_FUTEX realtime-null ret=%d error=%d pass=%d\n",
	       returned, error, passed);
	for (enum wait_api api = RELATIVE; api <= ABSOLUTE; ++api)
		for (int sibling = 0; sibling <= 1; ++sibling) {
			passed += run_case(api, sibling, 0);
			passed += run_case(api, sibling, 1);
		}
	passed += run_case(UNTIMED, 0, 1);
	passed += run_case(UNTIMED, 1, 1);
	printf("GROUP_STOP_FUTEX passed=%d/11\n", passed);
	return passed == 11 ? 0 : 1;
}
