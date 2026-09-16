/* SPDX-License-Identifier: MPL-2.0 */

#define _GNU_SOURCE
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t handler_ran;
static pid_t owned_child;

static void continued(int signal_number)
{
	(void)signal_number;
	handler_ran = 1;
}

static int bounded_wait(pid_t pid, int *status, int options)
{
	for (int i = 0; i < 200; ++i) {
		pid_t result = waitpid(pid, status, options | WNOHANG);
		if (result == pid) {
			// Reaping ends PID ownership even when a setup wait sees an early exit.
			if (WIFEXITED(*status) || WIFSIGNALED(*status))
				owned_child = 0;
			return 1;
		}
		if (result < 0)
			return -1;
		struct timespec delay = { .tv_nsec = 10000000 };
		nanosleep(&delay, NULL);
	}
	return 0;
}

static void cleanup_child(void)
{
	if (owned_child <= 0)
		return;
	kill(owned_child, SIGKILL);
	int status;
	if (bounded_wait(owned_child, &status, 0) != 1)
		_exit(10);
	owned_child = 0;
}

static int test_case(int mode, int thread_directed)
{
	int ready[2], release[2];
	if (pipe(ready) || pipe(release))
		exit(2);
	pid_t child = fork();
	if (child < 0)
		exit(2);
	if (child == 0) {
		close(ready[0]);
		close(release[1]);
		struct sigaction action = { .sa_handler = SIG_DFL };
		sigemptyset(&action.sa_mask);
		if (mode == 1)
			action.sa_handler = SIG_IGN;
		if (mode == 2)
			action.sa_handler = continued;
		if (sigaction(SIGCONT, &action, NULL))
			_exit(3);
		if (mode == 3) {
			sigset_t mask;
			sigemptyset(&mask);
			sigaddset(&mask, SIGCONT);
			if (sigprocmask(SIG_BLOCK, &mask, NULL))
				_exit(4);
		}
		if (write(ready[1], "r", 1) != 1)
			_exit(5);
		char byte;
		ssize_t n;
		do {
			n = read(release[0], &byte, 1);
		} while (n < 0 && errno == EINTR);
		if (n != 1 || byte != 'c' || (mode == 2 && !handler_ran))
			_exit(6);
		if (mode == 3) {
			sigset_t pending, blocked;
			if (sigpending(&pending) ||
			    sigprocmask(SIG_BLOCK, NULL, &blocked) ||
			    sigismember(&pending, SIGCONT) != 1 ||
			    sigismember(&blocked, SIGCONT) != 1)
				_exit(11);
		}
		_exit(0);
	}
	owned_child = child;
	close(ready[1]);
	close(release[0]);
	char byte;
	if (read(ready[0], &byte, 1) != 1)
		exit(7);
	int status = 0;
	// Establish an actual stop; pending STOP/CONT cancellation is a separate
	// test.
	if (kill(child, SIGSTOP) ||
	    bounded_wait(child, &status, WUNTRACED) != 1 ||
	    !WIFSTOPPED(status) || WSTOPSIG(status) != SIGSTOP)
		exit(8);
	int result = thread_directed ?
			     syscall(SYS_tgkill, child, child, SIGCONT) :
			     kill(child, SIGCONT);
	if (result)
		exit(9);
	// The child cannot exit before the pipe is released. Check the wait status,
	// independently of signal-handler delivery and the blocked pending signal.
	int observed = bounded_wait(child, &status, WCONTINUED);
	if (observed == 1 && (WIFEXITED(status) || WIFSIGNALED(status))) {
		owned_child = 0;
		exit(12);
	}
	int resumed = observed == 1 && WIFCONTINUED(status);
	if (resumed && write(release[1], "c", 1) != 1)
		exit(9);
	int reaped = resumed ? bounded_wait(child, &status, 0) : 0;
	int passed = reaped == 1 && WIFEXITED(status) &&
		     WEXITSTATUS(status) == 0;
	if (reaped == 1)
		owned_child = 0;
	else
		cleanup_child();
	close(ready[0]);
	close(release[1]);
	printf("STOP_CONT mode=%s thread_directed=%d pass=%d\n",
	       (const char *[]){ "default", "ignored", "caught",
				 "blocked" }[mode],
	       thread_directed, passed);
	fflush(stdout);
	return passed;
}

int main(void)
{
	if (atexit(cleanup_child))
		return 2;
	alarm(35);
	int passed = 1;
	for (int route = 0; route < 2; ++route)
		for (int mode = 0; mode < 4; ++mode)
			passed &= test_case(mode, route);
	return passed ? 0 : 1;
}
