/* SPDX-License-Identifier: MPL-2.0 */

#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

static void send_signal(int signal_number, int thread_directed)
{
	int result = thread_directed ? syscall(SYS_tgkill, getpid(), getpid(),
					       signal_number) :
				       kill(getpid(), signal_number);
	assert(result == 0);
}

static void check_case(int stop_signal, int reverse, int first_route,
		       int second_route, int ignored)
{
	sigset_t mask, pending;
	sigemptyset(&mask);
	sigaddset(&mask, stop_signal);
	sigaddset(&mask, SIGCONT);
	sigaddset(&mask, SIGUSR1);
	assert(sigprocmask(SIG_BLOCK, &mask, NULL) == 0);
	if (ignored) {
		struct sigaction action = { .sa_handler = SIG_IGN };
		sigemptyset(&action.sa_mask);
		assert(sigaction(stop_signal, &action, NULL) == 0);
		assert(sigaction(SIGCONT, &action, NULL) == 0);
	}
	int first = reverse ? SIGCONT : stop_signal;
	int second = reverse ? stop_signal : SIGCONT;
	send_signal(SIGUSR1, first_route);
	send_signal(first, first_route);
	assert(sigpending(&pending) == 0);
	assert(sigismember(&pending, first) == 1);
	send_signal(second, second_route);
	assert(sigpending(&pending) == 0);
	assert(sigismember(&pending, first) == 0);
	assert(sigismember(&pending, second) == 1);
	assert(sigismember(&pending, SIGUSR1) == 1);
	// Raw synchronous consumption must not execute STOP/CONT actions.
	sigset_t consumed;
	sigemptyset(&consumed);
	sigaddset(&consumed, second);
	struct timespec timeout = { 0 };
	assert(sigtimedwait(&consumed, NULL, &timeout) == second);
	assert(sigpending(&pending) == 0);
	assert(sigismember(&pending, second) == 0);
	assert(sigismember(&pending, SIGUSR1) == 1);
}

// Returns -1 after a timeout, 0 for failure, and 1 for success. Reap the owned
// child before proceeding, including one stopped by a broken kernel.
static int wait_case(pid_t child)
{
	int status = 0;
	pid_t waited = 0;
	for (int attempt = 0; attempt < 500; ++attempt) {
		waited = waitpid(child, &status, WNOHANG | WUNTRACED);
		if (waited == child)
			break;
		assert(waited == 0 || errno == EINTR);
		usleep(10000);
	}
	if (waited == child && !WIFSTOPPED(status))
		return WIFEXITED(status) && WEXITSTATUS(status) == 0;
	int timed_out = waited != child;
	assert(kill(child, SIGKILL) == 0 || errno == ESRCH);
	do {
		waited = waitpid(child, &status, 0);
	} while (waited < 0 && errno == EINTR);
	assert(waited == child);
	if (timed_out)
		fprintf(stderr,
			"PENDING_CANCEL child timed out and was reaped\n");
	return timed_out ? -1 : 0;
}

static int run_case(int stop, unsigned int flags)
{
	int reverse = (flags >> 3) & 1;
	int first = (flags >> 2) & 1;
	int second = (flags >> 1) & 1;
	int ignored = flags & 1;
	pid_t child = fork();
	assert(child >= 0);
	if (child == 0) {
		check_case(stop, reverse, first, second, ignored);
		_exit(0);
	}
	int ok = wait_case(child);
	printf("PENDING_CANCEL stop=%d reverse=%d routes=%d,%d ignored=%d pass=%d\n",
	       stop, reverse, first, second, ignored, ok);
	return ok;
}

struct sibling_case {
	int first;
	int request[2];
	int response[2];
};

static void *check_sibling(void *argument)
{
	struct sibling_case *test = argument;
	pid_t tid = syscall(SYS_gettid);
	assert(write(test->response[1], &tid, sizeof(tid)) == sizeof(tid));
	for (int cancelled = 0; cancelled < 2; ++cancelled) {
		char request;
		assert(read(test->request[0], &request, 1) == 1);
		sigset_t pending;
		assert(sigpending(&pending) == 0);
		assert(sigismember(&pending, test->first) == !cancelled);
		assert(sigismember(&pending, SIGUSR1) == 1);
		assert(write(test->response[1], &request, 1) == 1);
	}
	return NULL;
}

static void check_sibling_case(int stop, int reverse)
{
	struct sibling_case test = { .first = reverse ? SIGCONT : stop };
	sigset_t mask;
	sigemptyset(&mask);
	sigaddset(&mask, stop);
	sigaddset(&mask, SIGCONT);
	sigaddset(&mask, SIGUSR1);
	assert(pthread_sigmask(SIG_BLOCK, &mask, NULL) == 0);
	assert(pipe(test.request) == 0);
	assert(pipe(test.response) == 0);
	pthread_t worker;
	assert(pthread_create(&worker, NULL, check_sibling, &test) == 0);
	pid_t tid;
	assert(read(test.response[0], &tid, sizeof(tid)) == sizeof(tid));
	assert(syscall(SYS_tgkill, getpid(), tid, SIGUSR1) == 0);
	assert(syscall(SYS_tgkill, getpid(), tid, test.first) == 0);
	for (int cancelled = 0; cancelled < 2; ++cancelled) {
		if (cancelled)
			send_signal(reverse ? stop : SIGCONT, 1);
		char request = 0;
		assert(write(test.request[1], &request, 1) == 1);
		assert(read(test.response[0], &request, 1) == 1);
	}
	assert(pthread_join(worker, NULL) == 0);
	// The isolated child exits with its remaining signals still blocked.
}

int main(void)
{
	const int stop_signals[] = { SIGTSTP, SIGTTIN, SIGTTOU };
	int passed = 0;
	for (size_t stop = 0; stop < sizeof(stop_signals) / sizeof(int); ++stop)
		for (unsigned int flags = 0; flags < 16; ++flags) {
			int ok = run_case(stop_signals[stop], flags);
			if (ok < 0)
				return 1;
			passed += ok;
		}
	printf("PENDING_CANCEL passed=%d/48\n", passed);
	int sibling_passed = 0;
	for (size_t stop = 0; stop < sizeof(stop_signals) / sizeof(int);
	     ++stop) {
		for (int reverse = 0; reverse < 2; ++reverse) {
			pid_t child = fork();
			assert(child >= 0);
			if (child == 0) {
				check_sibling_case(stop_signals[stop], reverse);
				_exit(0);
			}
			int ok = wait_case(child);
			if (ok < 0)
				return 1;
			sibling_passed += ok;
		}
	}
	printf("SIBLING_CANCEL passed=%d/6\n", sibling_passed);
	return passed == 48 && sibling_passed == 6 ? 0 : 1;
}
