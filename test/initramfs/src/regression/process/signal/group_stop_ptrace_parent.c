/* SPDX-License-Identifier: MPL-2.0 */

#define _GNU_SOURCE
#include <errno.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <sys/ptrace.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

enum { ATTEMPTS = 300, STEP_US = 10000 };

static int fail(const char *phase)
{
	fprintf(stderr, "PTRACE_PARENT FAIL phase=%s errno=%d\n", phase, errno);
	return 0;
}

static int send_byte(int fd, char byte)
{
	return write(fd, &byte, 1) == 1 || fail("send");
}

static int receive_byte(int fd, char expected)
{
	for (int i = 0; i < ATTEMPTS; ++i) {
		struct pollfd event = { .fd = fd, .events = POLLIN };
		int result = poll(&event, 1, STEP_US / 1000);
		if (!result || (result < 0 && errno == EINTR))
			continue;
		char byte;
		return (result > 0 && (event.revents & POLLIN) &&
			read(fd, &byte, 1) == 1 && byte == expected) ||
		       fail("receive");
	}
	return fail("receive-timeout");
}

static int event(pid_t *owned, int options, int code, int status,
		 const char *phase)
{
	pid_t pid = *owned;
	for (int i = 0; i < ATTEMPTS; ++i) {
		siginfo_t info = { 0 };
		if (waitid(P_PID, pid, &info, options | WNOHANG) < 0) {
			if (errno == EINTR)
				continue;
			return fail(phase);
		}
		if (info.si_pid) {
			if (!(options & WNOWAIT) &&
			    (info.si_code == CLD_EXITED ||
			     info.si_code == CLD_KILLED ||
			     info.si_code == CLD_DUMPED))
				*owned = 0;
			if (info.si_pid == pid && info.si_code == code &&
			    info.si_status == status)
				return 1;
			fprintf(stderr,
				"PTRACE_PARENT FAIL phase=%s pid=%d code=%d status=%d expected=%d/%d/%d\n",
				phase, info.si_pid, info.si_code,
				info.si_status, pid, code, status);
			return 0;
		}
		usleep(STEP_US);
	}
	return fail(phase);
}

static int no_event(pid_t pid, int options, const char *phase)
{
	siginfo_t info = { 0 };
	return (waitid(P_PID, pid, &info, options | WNOHANG | WNOWAIT) == 0 &&
		!info.si_pid) ||
	       fail(phase);
}

static int cleanup(pid_t *pid)
{
	if (!*pid)
		return 1;
	if (kill(*pid, SIGKILL) < 0 && errno != ESRCH)
		return fail("cleanup-kill");
	for (int i = 0; i < ATTEMPTS; ++i) {
		int status;
		pid_t result = waitpid(*pid, &status, WNOHANG);
		if ((result > 0 &&
		     (WIFEXITED(status) || WIFSIGNALED(status))) ||
		    (result < 0 && errno == ECHILD)) {
			*pid = 0;
			return 1;
		}
		if (result < 0 && errno != EINTR)
			return fail("cleanup-wait");
		usleep(STEP_US);
	}
	return fail("cleanup-timeout");
}

static int tracer_main(pid_t child, int to_parent, int from_parent,
		       int parent_first, int tracer_continued)
{
	alarm(20);
	if (ptrace(PTRACE_ATTACH, child, NULL, NULL) < 0 ||
	    !event(&child, WSTOPPED, CLD_TRAPPED, SIGSTOP, "attach-delivery") ||
	    ptrace(PTRACE_CONT, child, NULL, (void *)(long)SIGSTOP) < 0 ||
	    !event(&child, WSTOPPED | WNOWAIT, CLD_TRAPPED, SIGSTOP,
		   "tracer-group-peek"))
		return 2;
	if (!parent_first &&
	    !event(&child, WSTOPPED, CLD_TRAPPED, SIGSTOP, "tracer-first"))
		return 2;
	if (!send_byte(to_parent, 'G') || !receive_byte(from_parent, 'T'))
		return 2;
	if (parent_first && !event(&child, WSTOPPED, CLD_TRAPPED, SIGSTOP,
				   "tracer-after-parent"))
		return 2;
	if (tracer_continued &&
	    (!event(&child, WCONTINUED | WNOWAIT, CLD_CONTINUED, SIGCONT,
		    "tracer-continue-peek") ||
	     !event(&child, WCONTINUED, CLD_CONTINUED, SIGCONT,
		    "tracer-continue-consume") ||
	     !send_byte(to_parent, 'C') || !receive_byte(from_parent, 'U')))
		return 2;
	if (!no_event(child, WSTOPPED, "tracer-consumed") ||
	    ptrace(PTRACE_CONT, child, NULL, NULL) < 0 ||
	    !event(&child, WSTOPPED, CLD_TRAPPED, SIGCONT,
		   "continue-delivery") ||
	    ptrace(PTRACE_CONT, child, NULL, NULL) < 0 ||
	    !send_byte(to_parent, 'D'))
		return 2;
	// The real parent must not reap this exit before the tracer consumes it.
	if (!event(&child, WEXITED | WNOWAIT, CLD_EXITED, 42,
		   "tracer-exit-peek") ||
	    !send_byte(to_parent, 'E') || !receive_byte(from_parent, 'Z') ||
	    !event(&child, WEXITED, CLD_EXITED, 42, "tracer-exit-consume") ||
	    !send_byte(to_parent, 'F'))
		return 2;
	return 0;
}

static int run_case(int parent_first, int tracer_continued)
{
	int ready[2] = { -1, -1 }, command[2] = { -1, -1 };
	int to_parent[2] = { -1, -1 }, to_tracer[2] = { -1, -1 };
	pid_t child = 0, tracer = 0;
	int ok = 0;
	if (pipe(ready) || pipe(command) || pipe(to_parent) || pipe(to_tracer))
		goto out;
	child = fork();
	if (child < 0) {
		child = 0;
		goto out;
	}
	if (!child) {
		alarm(20);
		close(ready[0]);
		close(command[1]);
		for (int i = 0; i < 2; ++i) {
			close(to_parent[i]);
			close(to_tracer[i]);
		}
		_exit(send_byte(ready[1], 'R') &&
				      receive_byte(command[0], 'X') ?
			      42 :
			      2);
	}
	close(ready[1]);
	ready[1] = -1;
	close(command[0]);
	command[0] = -1;
	if (!receive_byte(ready[0], 'R'))
		goto out;
	tracer = fork();
	if (tracer < 0) {
		tracer = 0;
		goto out;
	}
	if (!tracer) {
		close(ready[0]);
		close(command[1]);
		close(to_parent[0]);
		close(to_tracer[1]);
		_exit(tracer_main(child, to_parent[1], to_tracer[0],
				  parent_first, tracer_continued));
	}
	close(to_parent[1]);
	to_parent[1] = -1;
	close(to_tracer[0]);
	to_tracer[0] = -1;
	if (!receive_byte(to_parent[0], 'G') ||
	    !event(&child, WSTOPPED | WNOWAIT, CLD_STOPPED, SIGSTOP,
		   "parent-group-peek") ||
	    !event(&child, WSTOPPED | WNOWAIT, CLD_STOPPED, SIGSTOP,
		   "parent-group-peek-again") ||
	    !event(&child, WSTOPPED, CLD_STOPPED, SIGSTOP,
		   "parent-group-consume") ||
	    !no_event(child, WSTOPPED, "parent-consumed") ||
	    kill(child, SIGCONT) < 0)
		goto out;
	if (tracer_continued) {
		if (!send_byte(to_tracer[1], 'T') ||
		    !receive_byte(to_parent[0], 'C') ||
		    !no_event(child, WCONTINUED,
			      "continue-already-consumed-by-tracer") ||
		    !send_byte(to_tracer[1], 'U'))
			goto out;
	} else if (!event(&child, WCONTINUED | WNOWAIT, CLD_CONTINUED, SIGCONT,
			  "parent-continue-peek") ||
		   !event(&child, WCONTINUED, CLD_CONTINUED, SIGCONT,
			  "parent-continue-consume") ||
		   !send_byte(to_tracer[1], 'T')) {
		goto out;
	}
	if (!receive_byte(to_parent[0], 'D') || !send_byte(command[1], 'X') ||
	    !receive_byte(to_parent[0], 'E') ||
	    !no_event(child, WEXITED, "parent-exit-hidden") ||
	    !send_byte(to_tracer[1], 'Z') || !receive_byte(to_parent[0], 'F'))
		goto out;
	// Peek first so a diagnostic mismatch cannot silently lose PID ownership.
	if (!event(&child, WEXITED | WNOWAIT, CLD_EXITED, 42,
		   "parent-exit-peek") ||
	    !event(&child, WEXITED, CLD_EXITED, 42, "parent-exit-consume"))
		goto out;
	child = 0;
	if (!event(&tracer, WEXITED | WNOWAIT, CLD_EXITED, 0,
		   "tracer-terminal-peek") ||
	    !event(&tracer, WEXITED, CLD_EXITED, 0, "tracer-terminal-consume"))
		goto out;
	tracer = 0;
	ok = 1;
out:
	// The supervisor owns both children. Remove the tracer before reaping its
	// tracee, including failure paths; no subreaper or orphan cleanup is needed.
	if (!cleanup(&tracer))
		ok = 0;
	if (!cleanup(&child))
		ok = 0;
	for (int i = 0; i < 2; ++i) {
		if (ready[i] >= 0)
			close(ready[i]);
		if (command[i] >= 0)
			close(command[i]);
		if (to_parent[i] >= 0)
			close(to_parent[i]);
		if (to_tracer[i] >= 0)
			close(to_tracer[i]);
	}
	printf("PTRACE_PARENT parent_first=%d tracer_continued=%d pass=%d\n",
	       parent_first, tracer_continued, ok);
	return ok;
}

static void *report_tid(void *arg)
{
	int fd = *(int *)arg;
	pid_t tid = syscall(SYS_gettid);
	if (write(fd, &tid, sizeof(tid)) != sizeof(tid))
		_exit(2);
	for (;;)
		pause();
	return NULL;
}

static int nonleader_continue(void)
{
	int ready[2] = { -1, -1 };
	pid_t child = 0, tracee = 0;
	int ok = 0;
	if (pipe(ready))
		goto out;
	child = fork();
	if (child < 0) {
		child = 0;
		goto out;
	}
	if (!child) {
		alarm(20);
		close(ready[0]);
		pthread_t worker;
		if (pthread_create(&worker, NULL, report_tid, &ready[1]))
			_exit(2);
		for (;;)
			pause();
	}
	close(ready[1]);
	ready[1] = -1;
	struct pollfd readable = { .fd = ready[0], .events = POLLIN };
	if (poll(&readable, 1, ATTEMPTS * STEP_US / 1000) <= 0 ||
	    read(ready[0], &tracee, sizeof(tracee)) != sizeof(tracee) ||
	    tracee <= 0 || tracee == child)
		goto out;
	if (ptrace(PTRACE_ATTACH, tracee, NULL, NULL) < 0 ||
	    !event(&tracee, WSTOPPED, CLD_TRAPPED, SIGSTOP,
		   "nonleader-attach") ||
	    ptrace(PTRACE_CONT, tracee, NULL, (void *)(long)SIGSTOP) < 0 ||
	    !event(&tracee, WSTOPPED, CLD_TRAPPED, SIGSTOP,
		   "nonleader-group-stop") ||
	    kill(child, SIGCONT) < 0 ||
	    !event(&tracee, WCONTINUED | WNOWAIT, CLD_CONTINUED, SIGCONT,
		   "nonleader-continue-peek"))
		goto out;
	// Check waitpid's return identity as well as waitid's siginfo identity.
	int status;
	if (waitpid(tracee, &status, WCONTINUED | WNOHANG) != tracee ||
	    !WIFCONTINUED(status) ||
	    !no_event(child, WCONTINUED, "nonleader-shared-consumed")) {
		fail("nonleader-continue-consume");
		goto out;
	}
	if (ptrace(PTRACE_DETACH, tracee, NULL, NULL) < 0)
		goto out;
	tracee = 0;
	ok = 1;
out:
	// On failure, reap the traced nonleader before its owning process. Killing
	// the entire process also releases a group stop left by an earlier failure.
	if (child > 0 && kill(child, SIGKILL) < 0 && errno != ESRCH)
		ok = 0;
	if (tracee > 0 && !cleanup(&tracee))
		ok = 0;
	if (!cleanup(&child))
		ok = 0;
	for (int i = 0; i < 2; ++i)
		if (ready[i] >= 0)
			close(ready[i]);
	printf("PTRACE_PARENT nonleader-continued pass=%d\n", ok);
	return ok;
}

int main(void)
{
	setbuf(stdout, NULL);
	if (signal(SIGPIPE, SIG_IGN) == SIG_ERR)
		return 1;
	int ok = run_case(0, 0);
	ok &= run_case(1, 0);
	ok &= run_case(0, 1);
	ok &= nonleader_continue();
	return ok ? 0 : 1;
}
