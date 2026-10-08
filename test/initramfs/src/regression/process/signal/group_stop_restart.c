/* SPDX-License-Identifier: MPL-2.0 */

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

enum { WAIT_ATTEMPTS = 500, WAIT_STEP_US = 10000 };
enum handler_mode { NO_HANDLER, INTERRUPT_READ, RESTART_READ };

static volatile sig_atomic_t handler_count;
static int handler_ready_fd;

struct ready_message {
	pid_t tid;
	char phase;
};

struct reader_args {
	int data;
	int ready;
	int result;
	enum handler_mode mode;
};

struct read_result {
	int count;
	int error;
	int handled;
	char byte;
};

static void caught_usr1(int number)
{
	(void)number;
	int saved_errno = errno;
	++handler_count;
	struct ready_message ready = { .phase = 'H' };
	// O_NONBLOCK keeps the acknowledgement safe even on a failed test path.
	if (write(handler_ready_fd, &ready, sizeof(ready)) != sizeof(ready))
		handler_count = -1;
	errno = saved_errno;
}

static void *blocking_reader(void *argument)
{
	struct reader_args *args = argument;
	struct ready_message ready = { .tid = syscall(SYS_gettid),
				       .phase = 'R' };
	if (write(args->ready, &ready, sizeof(ready)) != sizeof(ready))
		_exit(2);
	struct read_result result = { 0 };
	// Record the first return; an EINTR retry would conceal restart bugs.
	result.count = read(args->data, &result.byte, 1);
	result.error = result.count < 0 ? errno : 0;
	result.handled = handler_count;
	if (write(args->result, &result, sizeof(result)) != sizeof(result))
		_exit(2);
	int expected = args->mode == INTERRUPT_READ ?
			       result.count == -1 && result.error == EINTR :
			       result.count == 1 && result.byte == 'B';
	_exit(expected ? 0 : 1);
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
		fprintf(stderr, "GROUP_STOP_RESTART FAIL phase=%s pipe\n",
			phase);
		return 0;
	}
	fprintf(stderr, "GROUP_STOP_RESTART FAIL phase=%s timeout\n", phase);
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
	// Consuming waits clear ownership before any cleanup can signal a PID.
	if (kill(*child, SIGKILL) < 0 && errno != ESRCH) {
		fprintf(stderr,
			"GROUP_STOP_RESTART FAIL phase=cleanup-kill errno=%d\n",
			errno);
		return 0;
	}
	int status;
	pid_t result = wait_bounded(child, &status, 0);
	if (result > 0 || !*child)
		return 1;
	fprintf(stderr,
		"GROUP_STOP_RESTART FAIL phase=cleanup-reap result=%d\n",
		result);
	return 0;
}

static int run_case(int sibling_reader, int thread_directed,
		    enum handler_mode mode)
{
	int pipes[3][2] = { { -1, -1 }, { -1, -1 }, { -1, -1 } };
	int *data = pipes[0], *ready = pipes[1], *result_pipe = pipes[2];
	pid_t child = 0;
	int ok = 0;
	for (int index = 0; index < 3; ++index) {
		if (pipe(pipes[index]) < 0) {
			fprintf(stderr,
				"GROUP_STOP_RESTART FAIL phase=pipe errno=%d\n",
				errno);
			goto cleanup;
		}
	}
	child = fork();
	if (child < 0) {
		child = 0;
		fprintf(stderr, "GROUP_STOP_RESTART FAIL phase=fork errno=%d\n",
			errno);
		goto cleanup;
	}
	if (child == 0) {
		close(data[1]);
		close(ready[0]);
		close(result_pipe[0]);
		// This default-action signal only bounds failures; it is never caught.
		alarm(30);
		if (mode != NO_HANDLER) {
			handler_ready_fd = ready[1];
			struct sigaction action = {
				.sa_handler = caught_usr1,
				.sa_flags = mode == RESTART_READ ? SA_RESTART :
								   0,
			};
			sigemptyset(&action.sa_mask);
			if (fcntl(ready[1], F_SETFL, O_NONBLOCK) < 0 ||
			    sigaction(SIGUSR1, &action, NULL) < 0) {
				fprintf(stderr,
					"GROUP_STOP_RESTART FAIL phase=handler-setup errno=%d\n",
					errno);
				_exit(2);
			}
		}
		struct reader_args args = { data[0], ready[1], result_pipe[1],
					    mode };
		if (!sibling_reader)
			blocking_reader(&args);
		pthread_t reader;
		int error =
			pthread_create(&reader, NULL, blocking_reader, &args);
		if (error) {
			fprintf(stderr,
				"GROUP_STOP_RESTART FAIL phase=pthread-create errno=%d\n",
				error);
			_exit(2);
		}
		struct ready_message message = { .tid = getpid(),
						 .phase = 'M' };
		if (write(ready[1], &message, sizeof(message)) !=
		    sizeof(message))
			_exit(2);
		// Keep a spurious pause interruption from hiding the reader's result.
		// The reader exits the process; the parent and alarm bound this loop.
		while (pause() < 0 && errno == EINTR)
			;
		_exit(3);
	}
	close(data[0]);
	data[0] = -1;
	close(ready[1]);
	ready[1] = -1;
	close(result_pipe[1]);
	result_pipe[1] = -1;
	pid_t reader_tid = 0;
	int main_ready = !sibling_reader;
	for (int index = 0; index < 1 + sibling_reader; ++index) {
		struct ready_message message;
		if (!receive(ready[0], &message, sizeof(message), "ready"))
			goto cleanup;
		if (message.phase == 'R')
			reader_tid = message.tid;
		main_ready |= message.phase == 'M' && message.tid == child;
	}
	if (reader_tid <= 0 || !main_ready ||
	    (sibling_reader ? reader_tid == child : reader_tid != child)) {
		fprintf(stderr, "GROUP_STOP_RESTART FAIL phase=ready-bytes\n");
		goto cleanup;
	}
	// Readiness precedes read/pause; allow both threads to enter their waits.
	usleep(100000);
	int sent = thread_directed ?
			   syscall(SYS_tgkill, child, child, SIGSTOP) :
			   kill(child, SIGSTOP);
	if (sent < 0) {
		fprintf(stderr,
			"GROUP_STOP_RESTART FAIL phase=stop-send errno=%d\n",
			errno);
		goto cleanup;
	}
	int status = 0;
	pid_t waited = wait_bounded(&child, &status, WUNTRACED);
	if (waited <= 0 || !WIFSTOPPED(status) || WSTOPSIG(status) != SIGSTOP) {
		fprintf(stderr,
			"GROUP_STOP_RESTART FAIL phase=stop-wait result=%d status=%d\n",
			waited, status);
		goto cleanup;
	}
	if (mode != NO_HANDLER &&
	    syscall(SYS_tgkill, child, reader_tid, SIGUSR1) < 0) {
		fprintf(stderr,
			"GROUP_STOP_RESTART FAIL phase=queue-USR1 errno=%d\n",
			errno);
		goto cleanup;
	}
	if (kill(child, SIGCONT) < 0) {
		fprintf(stderr,
			"GROUP_STOP_RESTART FAIL phase=continue errno=%d\n",
			errno);
		goto cleanup;
	}
	if (mode != NO_HANDLER) {
		// Supply data only after signal delivery, avoiding a read/data race.
		struct ready_message message;
		if (!receive(ready[0], &message, sizeof(message), "handler") ||
		    message.phase != 'H')
			goto cleanup;
	}
	// Collect the reader's errno even if it already exited and this gets EPIPE.
	int supplied = write(data[1], "B", 1) == 1;
	struct read_result result;
	if (!receive(result_pipe[0], &result, sizeof(result), "read-result"))
		goto cleanup;
	printf("GROUP_STOP_RESTART sibling=%d thread_stop=%d handler=%d read=%d "
	       "errno=%d byte=%d handled=%d\n",
	       sibling_reader, thread_directed, mode, result.count,
	       result.error, (unsigned char)result.byte, result.handled);
	waited = wait_bounded(&child, &status, 0);
	int expected = mode == INTERRUPT_READ ?
			       result.count == -1 && result.error == EINTR :
			       supplied && result.count == 1 &&
				       result.error == 0 && result.byte == 'B';
	ok = expected && result.handled == (mode != NO_HANDLER) && waited > 0 &&
	     WIFEXITED(status) && WEXITSTATUS(status) == 0;
	if (!ok)
		fprintf(stderr,
			"GROUP_STOP_RESTART FAIL phase=read-restart supplied=%d wait=%d "
			"status=%d\n",
			supplied, waited, status);
cleanup:
	ok &= reap_owned(&child);
	for (int index = 0; index < 3; ++index)
		for (int end = 0; end < 2; ++end)
			if (pipes[index][end] >= 0)
				close(pipes[index][end]);
	printf("GROUP_STOP_RESTART sibling=%d thread_stop=%d handler=%d pass=%d\n",
	       sibling_reader, thread_directed, mode, ok);
	return ok;
}

int main(void)
{
	struct sigaction ignored = { .sa_handler = SIG_IGN };
	sigemptyset(&ignored.sa_mask);
	if (sigaction(SIGPIPE, &ignored, NULL) < 0)
		return 1;
	int passed = 0;
	for (int sibling = 0; sibling < 2; ++sibling)
		for (int route = 0; route < 2; ++route)
			passed += run_case(sibling, route, NO_HANDLER);
	passed += run_case(1, 1, INTERRUPT_READ);
	passed += run_case(1, 1, RESTART_READ);
	printf("GROUP_STOP_RESTART passed=%d/6\n", passed);
	return passed == 6 ? 0 : 1;
}
