/* SPDX-License-Identifier: MPL-2.0 */

#define _GNU_SOURCE
#include <errno.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <sys/ptrace.h>
#include <sys/wait.h>
#include <unistd.h>

enum { WAIT_ATTEMPTS = 500, WAIT_STEP_US = 10000 };
enum test_case {
	CONT_GROUP_STOP,
	CONT_DELIVERY_STOP,
	DETACH_GROUP_STOP,
	REPEAT_GROUP_STOP,
	GROUP_NOTIFICATION,
	ATTACH_GROUP_STOP,
	TRACER_EXIT_GROUP_STOP
};
static const char *const case_names[] = {
	"cont-group-stop",	  "sigcont-delivery-stop", "detach-group-stop",
	"repeat-without-sigcont", "group-notification",	   "attach-group-stop",
	"tracer-exit-group-stop"
};

static int failure(const char *phase)
{
	fprintf(stderr, "GROUP_STOP_PTRACE FAIL phase=%s errno=%d\n", phase,
		errno);
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

static int expect_stop(pid_t *child, int number, const char *phase)
{
	int status = 0;
	pid_t result = wait_bounded(child, &status, WUNTRACED);
	if (result > 0 && WIFSTOPPED(status) && WSTOPSIG(status) == number)
		return 1;
	fprintf(stderr,
		"GROUP_STOP_PTRACE FAIL phase=%s waited=%d status=%d expected=%d\n",
		phase, result, status, number);
	return 0;
}

static int expect_siginfo(pid_t child, int number, int group_stop,
			  const char *phase)
{
	siginfo_t info = { 0 };
	errno = 0;
	long result = ptrace(PTRACE_GETSIGINFO, child, NULL, &info);
	int error = errno;
	if (group_stop ? result == -1 && error == EINVAL :
			 result == 0 && info.si_signo == number)
		return 1;
	fprintf(stderr,
		"GROUP_STOP_PTRACE FAIL phase=%s getsiginfo=%ld errno=%d signal=%d group=%d\n",
		phase, result, error, info.si_signo, group_stop);
	return 0;
}

static int receive_progress(int fd)
{
	for (int attempt = 0; attempt < WAIT_ATTEMPTS; ++attempt) {
		struct pollfd event = { .fd = fd, .events = POLLIN };
		int result = poll(&event, 1, WAIT_STEP_US / 1000);
		if (result == 0 || (result < 0 && errno == EINTR))
			continue;
		char byte;
		if (result > 0 && (event.revents & POLLIN) &&
		    read(fd, &byte, 1) == 1 && byte == 'P')
			return 1;
		return failure("progress-pipe");
	}
	return failure("progress-timeout");
}

static int expect_no_progress(int fd)
{
	// Stops are acknowledged before this bounded absence observation.
	for (int attempt = 0; attempt < 10; ++attempt) {
		struct pollfd event = { .fd = fd, .events = POLLIN };
		int result = poll(&event, 1, 100);
		if (result < 0 && errno == EINTR)
			continue;
		if (result == 0)
			return 1;
		return failure("unexpected-progress");
	}
	return failure("quiet-observation-interrupted");
}

static int reap_owned(pid_t *child)
{
	if (!*child)
		return 1;
	if (kill(*child, SIGKILL) < 0 && errno != ESRCH)
		return failure("cleanup-kill");
	// Drain ptrace stops without losing PID ownership before terminal wait.
	for (int attempt = 0; attempt < WAIT_ATTEMPTS; ++attempt) {
		int status;
		pid_t result = waitpid(*child, &status, WNOHANG | WUNTRACED);
		if (result > 0 && (WIFEXITED(status) || WIFSIGNALED(status))) {
			*child = 0;
			return 1;
		}
		if (result < 0 && errno == ECHILD) {
			*child = 0;
			return 1;
		}
		if (result < 0 && errno != EINTR)
			return failure("cleanup-wait");
		if (result > 0 && WIFSTOPPED(status)) {
			if (ptrace(PTRACE_CONT, *child, NULL,
				   (void *)(long)SIGKILL) < 0 &&
			    errno != ESRCH)
				return failure("cleanup-ptrace");
		}
		usleep(WAIT_STEP_US);
	}
	return failure("cleanup-timeout");
}

static int expect_notification(int code, int status)
{
	sigset_t signals;
	sigemptyset(&signals);
	sigaddset(&signals, SIGCHLD);
	struct timespec timeout = { .tv_sec = 2 };
	siginfo_t info = { 0 };
	int result = sigtimedwait(&signals, &info, &timeout);
	if (result == SIGCHLD && info.si_code == code &&
	    info.si_status == status)
		return 1;
	fprintf(stderr,
		"GROUP_STOP_PTRACE FAIL notification=%d code=%d status=%d expected=%d/%d errno=%d\n",
		result, info.si_code, info.si_status, code, status, errno);
	return 0;
}

static int expect_no_notification(void)
{
	sigset_t signals;
	sigemptyset(&signals);
	sigaddset(&signals, SIGCHLD);
	struct timespec timeout = { .tv_nsec = 100000000 };
	siginfo_t info = { 0 };
	int result = sigtimedwait(&signals, &info, &timeout);
	if (result == -1 && errno == EAGAIN)
		return 1;
	fprintf(stderr,
		"GROUP_STOP_PTRACE FAIL duplicate-notification=%d code=%d status=%d errno=%d\n",
		result, info.si_code, info.si_status, errno);
	return 0;
}

static int run_case(enum test_case test)
{
	int progress[2] = { -1, -1 };
	pid_t child = 0;
	int ok = 0;
	sigset_t old_mask;
	int changed_mask = 0;
	if (test == GROUP_NOTIFICATION) {
		sigset_t signals;
		sigemptyset(&signals);
		sigaddset(&signals, SIGCHLD);
		if (sigprocmask(SIG_BLOCK, &signals, &old_mask) < 0)
			goto cleanup;
		changed_mask = 1;
	}
	if (pipe(progress) < 0)
		goto cleanup;
	child = fork();
	if (child < 0) {
		child = 0;
		goto cleanup;
	}
	if (child == 0) {
		close(progress[0]);
		alarm(30);
		if (ptrace(PTRACE_TRACEME, 0, NULL, NULL) < 0 ||
		    raise(SIGSTOP) != 0 || write(progress[1], "P", 1) != 1)
			_exit(2);
		if (test == REPEAT_GROUP_STOP &&
		    (raise(SIGSTOP) != 0 || write(progress[1], "P", 1) != 1))
			_exit(2);
		_exit(0);
	}
	close(progress[1]);
	progress[1] = -1;
	if (!expect_stop(&child, SIGSTOP, "delivery-stop") ||
	    !expect_siginfo(child, SIGSTOP, 0, "delivery-siginfo"))
		goto cleanup;
	if (test == GROUP_NOTIFICATION &&
	    !expect_notification(CLD_TRAPPED, SIGSTOP))
		goto cleanup;
	if (test == CONT_DELIVERY_STOP) {
		if (kill(child, SIGCONT) < 0 ||
		    !expect_no_progress(progress[0]) ||
		    !expect_siginfo(child, SIGSTOP, 0,
				    "sigcont-keeps-ptrace-stop"))
			goto cleanup;
		if (ptrace(PTRACE_CONT, child, NULL, NULL) < 0 ||
		    !expect_stop(&child, SIGCONT, "sigcont-delivery") ||
		    !expect_siginfo(child, SIGCONT, 0, "sigcont-siginfo") ||
		    ptrace(PTRACE_CONT, child, NULL, NULL) < 0)
			goto cleanup;
	} else {
		if (ptrace(PTRACE_CONT, child, NULL, (void *)(long)SIGSTOP) <
			    0 ||
		    !expect_stop(&child, SIGSTOP, "group-stop") ||
		    !expect_siginfo(child, SIGSTOP, 1, "group-siginfo"))
			goto cleanup;
		// The tracer is also the real parent. Hold the child in this trap
		// throughout the observation so exit/CONT cannot create another event.
		if (test == GROUP_NOTIFICATION &&
		    (!expect_notification(CLD_STOPPED, SIGSTOP) ||
		     !expect_no_notification()))
			goto cleanup;
		if (test != DETACH_GROUP_STOP) {
			// Traditional ptrace resumes execution without any SIGCONT.
			if (ptrace(PTRACE_CONT, child, NULL, NULL) < 0)
				goto cleanup;
		} else {
			if (ptrace(PTRACE_DETACH, child, NULL, NULL) < 0 ||
			    !expect_no_progress(progress[0]))
				goto cleanup;
			if (kill(child, SIGCONT) < 0)
				goto cleanup;
		}
	}
	if (!receive_progress(progress[0]))
		goto cleanup;
	if (test == REPEAT_GROUP_STOP) {
		// A new stop must complete even though no SIGCONT ended the first.
		if (!expect_stop(&child, SIGSTOP, "repeat-delivery-stop") ||
		    !expect_siginfo(child, SIGSTOP, 0,
				    "repeat-delivery-siginfo") ||
		    ptrace(PTRACE_CONT, child, NULL, (void *)(long)SIGSTOP) <
			    0 ||
		    !expect_stop(&child, SIGSTOP, "repeat-group-stop") ||
		    !expect_siginfo(child, SIGSTOP, 1,
				    "repeat-group-siginfo") ||
		    ptrace(PTRACE_CONT, child, NULL, NULL) < 0 ||
		    !receive_progress(progress[0]))
			goto cleanup;
	}
	int status = 0;
	if (wait_bounded(&child, &status, 0) <= 0 || !WIFEXITED(status) ||
	    WEXITSTATUS(status) != 0) {
		failure("exit");
		goto cleanup;
	}
	ok = 1;
cleanup:
	if (!reap_owned(&child))
		ok = 0;
	for (int index = 0; index < 2; ++index)
		if (progress[index] >= 0)
			close(progress[index]);
	if (changed_mask && sigprocmask(SIG_SETMASK, &old_mask, NULL) < 0)
		ok = 0;
	printf("GROUP_STOP_PTRACE %s case=%s\n", ok ? "PASS" : "FAIL",
	       case_names[test]);
	return ok;
}

static int run_lifecycle_case(enum test_case test)
{
	int progress[2] = { -1, -1 };
	pid_t child = 0;
	pid_t tracer = 0;
	int ok = 0;
	if (pipe(progress) < 0)
		goto cleanup;
	child = fork();
	if (child < 0) {
		child = 0;
		goto cleanup;
	}
	if (child == 0) {
		close(progress[0]);
		alarm(30);
		if (raise(SIGSTOP) != 0 || write(progress[1], "P", 1) != 1)
			_exit(2);
		_exit(0);
	}
	close(progress[1]);
	progress[1] = -1;
	if (!expect_stop(&child, SIGSTOP, "untraced-group-stop"))
		goto cleanup;

	if (test == TRACER_EXIT_GROUP_STOP) {
		// Keep the tracee's real parent alive to own its terminal wait.
		tracer = fork();
		if (tracer < 0) {
			tracer = 0;
			goto cleanup;
		}
		if (tracer == 0) {
			close(progress[0]);
			alarm(10);
			if (ptrace(PTRACE_ATTACH, child, NULL, NULL) < 0 ||
			    !expect_stop(&child, SIGSTOP,
					 "tracer-attach-stop") ||
			    !expect_siginfo(child, SIGSTOP, 1,
					    "tracer-attach-group-siginfo"))
				_exit(2);
			// Exiting releases ptrace ownership but preserves the group stop.
			_exit(0);
		}
		int status = 0;
		if (wait_bounded(&tracer, &status, 0) <= 0 ||
		    !WIFEXITED(status) || WEXITSTATUS(status) != 0) {
			failure("tracer-exit");
			goto cleanup;
		}
		if (!expect_no_progress(progress[0]) ||
		    kill(child, SIGCONT) < 0)
			goto cleanup;
	} else {
		// ATTACH first reports the existing group stop, then its queued SIGSTOP.
		if (ptrace(PTRACE_ATTACH, child, NULL, NULL) < 0 ||
		    !expect_stop(&child, SIGSTOP, "attach-stop") ||
		    !expect_siginfo(child, SIGSTOP, 1,
				    "attach-group-siginfo") ||
		    !expect_no_progress(progress[0]) ||
		    ptrace(PTRACE_CONT, child, NULL, NULL) < 0 ||
		    !expect_stop(&child, SIGSTOP, "attach-delivery-stop") ||
		    !expect_siginfo(child, SIGSTOP, 0,
				    "attach-delivery-siginfo") ||
		    ptrace(PTRACE_CONT, child, NULL, NULL) < 0)
			goto cleanup;
	}
	if (!receive_progress(progress[0]))
		goto cleanup;
	int status = 0;
	if (wait_bounded(&child, &status, 0) <= 0 || !WIFEXITED(status) ||
	    WEXITSTATUS(status) != 0) {
		failure("lifecycle-exit");
		goto cleanup;
	}
	ok = 1;
cleanup:
	// Stop the tracer first so it cannot consume the tracee's terminal wait.
	if (!reap_owned(&tracer))
		ok = 0;
	if (!reap_owned(&child))
		ok = 0;
	for (int index = 0; index < 2; ++index)
		if (progress[index] >= 0)
			close(progress[index]);
	printf("GROUP_STOP_PTRACE %s case=%s\n", ok ? "PASS" : "FAIL",
	       case_names[test]);
	return ok;
}

int main(void)
{
	setbuf(stdout, NULL);
	int ok = 1;
	for (enum test_case test = CONT_GROUP_STOP; test <= GROUP_NOTIFICATION;
	     ++test)
		ok &= run_case(test);
	for (enum test_case test = ATTACH_GROUP_STOP;
	     test <= TRACER_EXIT_GROUP_STOP; ++test)
		ok &= run_lifecycle_case(test);
	return ok ? 0 : 1;
}
