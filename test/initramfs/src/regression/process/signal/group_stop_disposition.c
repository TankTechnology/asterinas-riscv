/* SPDX-License-Identifier: MPL-2.0 */

#define _GNU_SOURCE
#include <errno.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

enum { WAIT_ATTEMPTS = 500, WAIT_STEP_US = 10000 };
static sigset_t child_signals;
static volatile sig_atomic_t handler_count;

static void caught_once(int number)
{
	(void)number;
	++handler_count;
}

static int receive_byte(int fd, char expected)
{
	for (int attempt = 0; attempt < WAIT_ATTEMPTS; ++attempt) {
		struct pollfd event = { .fd = fd, .events = POLLIN };
		int result = poll(&event, 1, WAIT_STEP_US / 1000);
		if (result == 0 || (result < 0 && errno == EINTR))
			continue;
		char byte;
		return result > 0 && (event.revents & POLLIN) &&
		       read(fd, &byte, 1) == 1 && byte == expected;
	}
	return 0;
}

static int wait_event(pid_t *child, int options, int code, int status)
{
	for (int attempt = 0; attempt < WAIT_ATTEMPTS; ++attempt) {
		siginfo_t info = { 0 };
		if (waitid(P_PID, *child, &info, options | WEXITED | WNOHANG) <
		    0) {
			if (errno == EINTR)
				continue;
			if (errno == ECHILD)
				*child = 0;
			return 0;
		}
		if (!info.si_pid) {
			usleep(WAIT_STEP_US);
			continue;
		}
		pid_t expected_pid = *child;
		if (info.si_code == CLD_EXITED || info.si_code == CLD_KILLED ||
		    info.si_code == CLD_DUMPED)
			*child = 0;
		int ok = info.si_pid == expected_pid && info.si_code == code &&
			 info.si_status == status;
		printf("GROUP_STOP_DISPOSITION_WAIT pid=%d code=%d status=%d expected=%d,%d pass=%d\n",
		       info.si_pid, info.si_code, info.si_status, code, status,
		       ok);
		return ok;
	}
	return 0;
}

static int notification(pid_t child, int expected, int code, int status,
			const char *phase)
{
	struct timespec timeout =
		expected ? (struct timespec){ .tv_sec = 1 } :
			   (struct timespec){ .tv_nsec = 100000000 };
	siginfo_t info = { 0 };
	errno = 0;
	int result = sigtimedwait(&child_signals, &info, &timeout);
	int error = errno;
	int ok = expected ? result == SIGCHLD && info.si_pid == child &&
				    info.si_code == code &&
				    info.si_status == status :
			    result == -1 && error == EAGAIN;
	printf("GROUP_STOP_DISPOSITION_SIGNAL phase=%s expected=%d ret=%d errno=%d pid=%d code=%d status=%d pass=%d\n",
	       phase, expected, result, error, info.si_pid, info.si_code,
	       info.si_status, ok);
	return ok;
}

static int reap_owned(pid_t *child)
{
	if (!*child)
		return 1;
	if (kill(*child, SIGKILL) < 0 && errno != ESRCH)
		return 0;
	for (int attempt = 0; attempt < WAIT_ATTEMPTS; ++attempt) {
		int status;
		pid_t result = waitpid(*child, &status, WNOHANG);
		if (result == *child || (result < 0 && errno == ECHILD)) {
			*child = 0;
			return 1;
		}
		if (result < 0 && errno != EINTR)
			return 0;
		usleep(WAIT_STEP_US);
	}
	return 0;
}

static void child_main(int request, int response)
{
	// The alarm is fatal: exceeding the deadline never counts as a pass.
	alarm(10);
	if (write(response, "R", 1) != 1 || !receive_byte(request, 'C') ||
	    write(response, "C", 1) != 1 || !receive_byte(request, 'X'))
		_exit(2);
	_exit(0);
}

static int metadata_case(int ignored)
{
	struct sigaction action = {
		.sa_handler = ignored ? SIG_IGN : SIG_DFL,
		.sa_flags = SA_NOCLDSTOP,
	};
	sigemptyset(&action.sa_mask);
	sigaddset(&action.sa_mask, SIGUSR1);
	struct sigaction observed = { 0 };
	int ok = sigaction(SIGCHLD, &action, NULL) == 0 &&
		 sigaction(SIGCHLD, NULL, &observed) == 0;
	int no_cldstop = (observed.sa_flags & SA_NOCLDSTOP) != 0;
	int usr1_masked = sigismember(&observed.sa_mask, SIGUSR1);
	ok &= observed.sa_handler == action.sa_handler && no_cldstop &&
	      usr1_masked == 1;
	// Only specified metadata is compared; libc may add platform flags.
	printf("GROUP_STOP_DISPOSITION_METADATA ignored=%d no_cldstop=%d usr1_masked=%d pass=%d\n",
	       ignored, no_cldstop, usr1_masked, ok);
	return ok;
}

static int events_case(int no_cldstop)
{
	int request[2] = { -1, -1 }, response[2] = { -1, -1 };
	pid_t child = 0;
	int ok = 0;
	const char *phase = "setup";
	struct sigaction action = {
		.sa_handler = SIG_DFL,
		.sa_flags = no_cldstop ? SA_NOCLDSTOP : 0,
	};
	sigemptyset(&action.sa_mask);
	if (sigaction(SIGCHLD, &action, NULL) < 0 || pipe(request) < 0 ||
	    pipe(response) < 0)
		goto cleanup;
	child = fork();
	if (child < 0) {
		child = 0;
		goto cleanup;
	}
	if (!child) {
		close(request[1]);
		close(response[0]);
		child_main(request[0], response[1]);
	}
	close(request[0]);
	request[0] = -1;
	close(response[1]);
	response[1] = -1;
	phase = "ready";
	if (!receive_byte(response[0], 'R'))
		goto cleanup;
	phase = "stop";
	if (kill(child, SIGSTOP) < 0 ||
	    !wait_event(&child, WSTOPPED, CLD_STOPPED, SIGSTOP))
		goto cleanup;
	// Consume STOP before CONT: standard signals can coalesce otherwise.
	ok = notification(child, !no_cldstop, CLD_STOPPED, SIGSTOP, "stop");
	phase = "continue";
	if (kill(child, SIGCONT) < 0 || write(request[1], "C", 1) != 1 ||
	    !receive_byte(response[0], 'C') ||
	    !wait_event(&child, WCONTINUED, CLD_CONTINUED, SIGCONT)) {
		ok = 0;
		goto cleanup;
	}
	ok &= notification(child, !no_cldstop, CLD_CONTINUED, SIGCONT,
			   "continue");
	phase = "exit";
	pid_t exiting_child = child;
	if (write(request[1], "X", 1) != 1 ||
	    !wait_event(&child, 0, CLD_EXITED, 0)) {
		ok = 0;
		goto cleanup;
	}
	// SA_NOCLDSTOP must not suppress exit notification or consume wait reports.
	ok &= notification(exiting_child, 1, CLD_EXITED, 0, "exit");
cleanup:
	if (!reap_owned(&child))
		ok = 0;
	for (int end = 0; end < 2; ++end) {
		if (request[end] >= 0)
			close(request[end]);
		if (response[end] >= 0)
			close(response[end]);
	}
	// A failed case may leave its cleanup SIGCHLD pending. Drain only after
	// reaping; no live children can produce another event for the next case.
	struct timespec zero = { 0 };
	for (int attempt = 0; attempt < 8; ++attempt) {
		int result = sigtimedwait(&child_signals, NULL, &zero);
		if (result == -1 && errno == EAGAIN)
			break;
		ok = 0;
		if (result != SIGCHLD)
			break;
	}
	printf("GROUP_STOP_DISPOSITION_EVENTS no_cldstop=%d phase=%s pass=%d\n",
	       no_cldstop, phase, ok);
	return ok;
}

static int exec_query(int ignored)
{
	struct sigaction observed = { 0 };
	int ok = sigaction(SIGUSR2, NULL, &observed) == 0;
	int no_cldstop = (observed.sa_flags & SA_NOCLDSTOP) != 0;
	int usr1_masked = sigismember(&observed.sa_mask, SIGUSR1);
	ok &= observed.sa_handler == (ignored ? SIG_IGN : SIG_DFL) &&
	      !no_cldstop && usr1_masked == 0;
	printf("GROUP_STOP_DISPOSITION_EXEC ignored=%d no_cldstop=%d usr1_masked=%d pass=%d\n",
	       ignored, no_cldstop, usr1_masked, ok);
	return ok;
}

static int lifecycle_case(const char *program, int ignored)
{
	// SIGUSR2 exercises action inheritance without changing SIGCHLD's
	// auto-reaping behavior while the parent owns a child PID.
	struct sigaction action = {
		.sa_handler = ignored ? SIG_IGN : SIG_DFL,
		.sa_flags = SA_NOCLDSTOP,
	};
	sigemptyset(&action.sa_mask);
	sigaddset(&action.sa_mask, SIGUSR1);
	int response[2] = { -1, -1 };
	pid_t child = 0;
	int ok = 0;
	if (sigaction(SIGUSR2, &action, NULL) < 0 || pipe(response) < 0)
		goto cleanup;
	child = fork();
	if (child < 0) {
		child = 0;
		goto cleanup;
	}
	if (!child) {
		close(response[0]);
		alarm(5);
		struct sigaction observed = { 0 };
		int inherited = sigaction(SIGUSR2, NULL, &observed) == 0 &&
				observed.sa_handler == action.sa_handler &&
				(observed.sa_flags & SA_NOCLDSTOP) &&
				sigismember(&observed.sa_mask, SIGUSR1) == 1;
		printf("GROUP_STOP_DISPOSITION_FORK ignored=%d pass=%d\n",
		       ignored, inherited);
		if (write(response[1], inherited ? "P" : "F", 1) != 1)
			_exit(2);
		close(response[1]);
		execl(program, program, "--exec-query",
		      ignored ? "ignored" : "default", NULL);
		_exit(2);
	}
	close(response[1]);
	response[1] = -1;
	ok = receive_byte(response[0], 'P');
	pid_t exiting_child = child;
	ok &= wait_event(&child, 0, CLD_EXITED, 0);
	ok &= notification(exiting_child, 1, CLD_EXITED, 0, "lifecycle-exit");
cleanup:
	if (!reap_owned(&child))
		ok = 0;
	for (int end = 0; end < 2; ++end)
		if (response[end] >= 0)
			close(response[end]);
	printf("GROUP_STOP_DISPOSITION_LIFECYCLE ignored=%d pass=%d\n", ignored,
	       ok);
	return ok;
}

static int reset_hand_case(void)
{
	struct sigaction action = {
		.sa_handler = caught_once,
		.sa_flags = SA_RESETHAND,
	};
	sigemptyset(&action.sa_mask);
	sigaddset(&action.sa_mask, SIGUSR2);
	sigset_t unblocked;
	sigemptyset(&unblocked);
	sigaddset(&unblocked, SIGUSR1);
	struct sigaction observed = { 0 };
	int ok = sigaction(SIGUSR1, &action, NULL) == 0 &&
		 sigprocmask(SIG_UNBLOCK, &unblocked, NULL) == 0 &&
		 raise(SIGUSR1) == 0 &&
		 sigaction(SIGUSR1, NULL, &observed) == 0;
	int reset_hand = (observed.sa_flags & SA_RESETHAND) != 0;
	int usr2_masked = sigismember(&observed.sa_mask, SIGUSR2);
	// One-shot delivery resets only the handler, unlike exec's full reset.
	ok &= handler_count == 1 && observed.sa_handler == SIG_DFL &&
	      reset_hand && usr2_masked == 1;
	printf("GROUP_STOP_DISPOSITION_RESETHAND handled=%d reset_hand=%d usr2_masked=%d pass=%d\n",
	       (int)handler_count, reset_hand, usr2_masked, ok);
	return ok;
}

int main(int argc, char **argv)
{
	setvbuf(stdout, NULL, _IONBF, 0);
	if (argc == 3 && strcmp(argv[1], "--exec-query") == 0) {
		if (strcmp(argv[2], "ignored") == 0)
			return !exec_query(1);
		if (strcmp(argv[2], "default") == 0)
			return !exec_query(0);
		return 1;
	}
	if (argc != 1)
		return 1;
	sigemptyset(&child_signals);
	sigaddset(&child_signals, SIGCHLD);
	if (sigprocmask(SIG_BLOCK, &child_signals, NULL) < 0)
		return 1;
	// Query SIG_IGN only while there are no children, avoiding auto-reaping.
	int passed = metadata_case(0);
	passed += metadata_case(1);
	passed += events_case(0);
	passed += events_case(1);
	passed += lifecycle_case(argv[0], 0);
	passed += lifecycle_case(argv[0], 1);
	passed += reset_hand_case();
	printf("GROUP_STOP_DISPOSITION passed=%d/7\n", passed);
	return passed == 7 ? 0 : 1;
}
