/* SPDX-License-Identifier: MPL-2.0 */

#define _GNU_SOURCE
#include <errno.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <sys/wait.h>
#include <unistd.h>

enum { WAIT_ATTEMPTS = 500, WAIT_STEP_US = 10000, EVENT_CAPACITY = 16 };

static volatile sig_atomic_t event_count;
static volatile sig_atomic_t event_overflow;
static volatile sig_atomic_t event_pid[EVENT_CAPACITY];
static volatile sig_atomic_t event_code[EVENT_CAPACITY];
static volatile sig_atomic_t event_status[EVENT_CAPACITY];

static void child_event(int number, siginfo_t *info, void *context)
{
	(void)number;
	(void)context;
	sig_atomic_t index = event_count;
	if (index == EVENT_CAPACITY) {
		event_overflow = 1;
		return;
	}
	event_pid[index] = info->si_pid;
	event_code[index] = info->si_code;
	event_status[index] = info->si_status;
	// Publish complete slots; SIGCHLD is masked throughout this handler.
	event_count = index + 1;
}

static int syscall_failure(const char *phase)
{
	int error = errno;
	fprintf(stderr, "GROUP_STOP_EVENTS %s phase=%s errno=%d\n",
		(error == ENOSYS || error == EINVAL) ? "ABI_UNSUPPORTED" :
						       "SYSCALL_FAILURE",
		phase, error);
	return 0;
}

static int receive_byte(int fd, char expected, int attempts)
{
	for (int attempt = 0; attempt < attempts; ++attempt) {
		struct pollfd event = { .fd = fd, .events = POLLIN };
		int result = poll(&event, 1, WAIT_STEP_US / 1000);
		if (result < 0 && errno == EINTR)
			continue;
		if (result < 0)
			return syscall_failure("handshake-poll");
		if (result == 0)
			continue;
		char byte;
		if (!(event.revents & POLLIN) || read(fd, &byte, 1) != 1 ||
		    byte != expected)
			break;
		return 1;
	}
	fprintf(stderr, "GROUP_STOP_EVENTS MISMATCH phase=handshake byte=%c\n",
		expected);
	return 0;
}

static void child_main(int request, int response)
{
	if (write(response, "R", 1) != 1)
		_exit(2);
	// A finite idle deadline also bounds the child if its parent fails.
	if (!receive_byte(request, 'P', WAIT_ATTEMPTS * 12) ||
	    write(response, "P", 1) != 1 ||
	    !receive_byte(request, 'X', WAIT_ATTEMPTS * 12))
		_exit(2);
	_exit(0);
}

static int wait_event(pid_t *child, int options, int code, int status,
		      const char *phase)
{
	for (int attempt = 0; attempt < WAIT_ATTEMPTS; ++attempt) {
		siginfo_t info = { 0 };
		if (waitid(P_PID, *child, &info, options | WEXITED | WNOHANG) <
		    0) {
			if (errno == EINTR)
				continue;
			if (errno == ECHILD)
				*child = 0;
			return syscall_failure(phase);
		}
		if (info.si_pid == 0) {
			usleep(WAIT_STEP_US);
			continue;
		}
		pid_t expected_pid = *child;
		if (!(options & WNOWAIT) &&
		    (info.si_code == CLD_EXITED || info.si_code == CLD_KILLED ||
		     info.si_code == CLD_DUMPED))
			*child = 0;
		if (info.si_pid == expected_pid && info.si_code == code &&
		    info.si_status == status)
			return 1;
		fprintf(stderr,
			"GROUP_STOP_EVENTS MISMATCH phase=%s code=%d status=%d expected=%d,%d\n",
			phase, info.si_code, info.si_status, code, status);
		return 0;
	}
	fprintf(stderr, "GROUP_STOP_EVENTS MISMATCH phase=%s timeout\n", phase);
	return 0;
}

static int no_wait_event(pid_t *child, int options, const char *phase)
{
	siginfo_t info = { 0 };
	if (waitid(P_PID, *child, &info,
		   options | WEXITED | WNOWAIT | WNOHANG) < 0) {
		if (errno == ECHILD)
			*child = 0;
		return syscall_failure(phase);
	}
	if (info.si_pid == 0)
		return 1;
	fprintf(stderr,
		"GROUP_STOP_EVENTS MISMATCH phase=%s extra-code=%d status=%d\n",
		phase, info.si_code, info.si_status);
	return 0;
}

static int check_notifications(pid_t child, int first, int expected, int code,
			       int status, const char *phase)
{
	if (expected) {
		for (int attempt = 0;
		     attempt < WAIT_ATTEMPTS && event_count == first; ++attempt)
			usleep(WAIT_STEP_US);
	}
	// Absence and duplicate checks require a bounded observation interval.
	for (int attempt = 0; attempt < 10; ++attempt)
		usleep(WAIT_STEP_US);
	int count = event_count;
	int ok = !event_overflow && count == first + expected;
	for (int index = first; index < count; ++index) {
		ok &= event_pid[index] == child && event_code[index] == code &&
		      event_status[index] == status;
		if (!ok)
			fprintf(stderr,
				"GROUP_STOP_EVENTS MISMATCH phase=%s sigchld-code=%d status=%d\n",
				phase, (int)event_code[index],
				(int)event_status[index]);
	}
	if (!ok)
		fprintf(stderr,
			"GROUP_STOP_EVENTS MISMATCH phase=%s sigchld-count=%d expected=%d overflow=%d\n",
			phase, count - first, expected, (int)event_overflow);
	return ok;
}

static int consume_continued(pid_t *child)
{
	int status;
	pid_t result = waitpid(*child, &status, WCONTINUED | WNOHANG);
	if (result < 0) {
		if (errno == ECHILD)
			*child = 0;
		return syscall_failure("waitpid-WCONTINUED");
	}
	if (result == *child) {
		if (WIFEXITED(status) || WIFSIGNALED(status))
			*child = 0;
		if (WIFCONTINUED(status))
			return 1;
	}
	fprintf(stderr,
		"GROUP_STOP_EVENTS MISMATCH phase=waitpid-WCONTINUED\n");
	return 0;
}

static int reap_owned(pid_t *child)
{
	if (!*child)
		return 1;
	// No handler reaps, and every consuming wait clears ownership on exit.
	if (kill(*child, SIGKILL) < 0 && errno != ESRCH)
		return syscall_failure("cleanup-kill");
	for (int attempt = 0; attempt < WAIT_ATTEMPTS; ++attempt) {
		int status;
		pid_t result = waitpid(*child, &status, WNOHANG);
		if (result == *child || (result < 0 && errno == ECHILD)) {
			*child = 0;
			return 1;
		}
		if (result < 0 && errno != EINTR)
			return syscall_failure("cleanup-wait");
		usleep(WAIT_STEP_US);
	}
	fprintf(stderr, "GROUP_STOP_EVENTS CLEANUP_FAILURE reap-timeout\n");
	return 0;
}

static int run_case(int no_cldstop)
{
	int request[2] = { -1, -1 }, response[2] = { -1, -1 };
	pid_t child = 0;
	int ok = 0;
	struct sigaction action = {
		.sa_sigaction = child_event,
		.sa_flags = SA_SIGINFO | (no_cldstop ? SA_NOCLDSTOP : 0),
	};
	sigemptyset(&action.sa_mask);
	if (sigaction(SIGCHLD, &action, NULL) < 0)
		return syscall_failure("sigaction");
	if (pipe(request) < 0 || pipe(response) < 0) {
		syscall_failure("pipe");
		goto cleanup;
	}
	int first = event_count;
	child = fork();
	if (child < 0) {
		child = 0;
		syscall_failure("fork");
		goto cleanup;
	}
	if (child == 0) {
		close(request[1]);
		close(response[0]);
		child_main(request[0], response[1]);
	}
	close(request[0]);
	request[0] = -1;
	close(response[1]);
	response[1] = -1;
	if (!receive_byte(response[0], 'R', WAIT_ATTEMPTS))
		goto cleanup;
	if (kill(child, SIGSTOP) < 0) {
		syscall_failure("SIGSTOP");
		goto cleanup;
	}
	if (!wait_event(&child, WSTOPPED | WNOWAIT, CLD_STOPPED, SIGSTOP,
			"stop-peek-1") ||
	    !wait_event(&child, WSTOPPED | WNOWAIT, CLD_STOPPED, SIGSTOP,
			"stop-peek-2") ||
	    !wait_event(&child, WSTOPPED, CLD_STOPPED, SIGSTOP,
			"stop-consume") ||
	    !no_wait_event(&child, WSTOPPED, "stop-consumed") ||
	    !check_notifications(child, first, !no_cldstop, CLD_STOPPED,
				 SIGSTOP, "stop-sigchld"))
		goto cleanup;
	first = event_count;
	if (kill(child, SIGCONT) < 0) {
		syscall_failure("SIGCONT");
		goto cleanup;
	}
	if (write(request[1], "P", 1) != 1 ||
	    !receive_byte(response[0], 'P', WAIT_ATTEMPTS))
		goto cleanup;
	if (!wait_event(&child, WCONTINUED | WNOWAIT, CLD_CONTINUED, SIGCONT,
			"continue-peek-1") ||
	    !wait_event(&child, WCONTINUED | WNOWAIT, CLD_CONTINUED, SIGCONT,
			"continue-peek-2") ||
	    !consume_continued(&child) ||
	    !no_wait_event(&child, WCONTINUED, "continue-consumed") ||
	    !check_notifications(child, first, !no_cldstop, CLD_CONTINUED,
				 SIGCONT, "continue-sigchld"))
		goto cleanup;
	first = event_count;
	pid_t exiting_child = child;
	if (write(request[1], "X", 1) != 1 ||
	    !wait_event(&child, 0, CLD_EXITED, 0, "exit-consume") ||
	    !check_notifications(exiting_child, first, 1, CLD_EXITED, 0,
				 "exit-sigchld"))
		goto cleanup;
	ok = 1;
cleanup:
	ok &= reap_owned(&child);
	for (int index = 0; index < 2; ++index) {
		if (request[index] >= 0)
			close(request[index]);
		if (response[index] >= 0)
			close(response[index]);
	}
	printf("GROUP_STOP_EVENTS no_cldstop=%d pass=%d\n", no_cldstop, ok);
	return ok;
}

int main(void)
{
	// Keep failed handshakes on the cleanup path if the child exits early.
	struct sigaction ignored = { .sa_handler = SIG_IGN };
	sigemptyset(&ignored.sa_mask);
	if (sigaction(SIGPIPE, &ignored, NULL) < 0)
		return !syscall_failure("ignore-SIGPIPE");
	sigset_t mask;
	sigemptyset(&mask);
	sigaddset(&mask, SIGCHLD);
	if (sigprocmask(SIG_UNBLOCK, &mask, NULL) < 0)
		return !syscall_failure("unblock-SIGCHLD");
	if (!run_case(0) || !run_case(1))
		return 1;
	printf("GROUP_STOP_EVENTS passed=2/2\n");
	return 0;
}
