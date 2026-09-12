/* SPDX-License-Identifier: MPL-2.0 */

#define _GNU_SOURCE
#include <errno.h>
#include <poll.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/ptrace.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

enum { ATTEMPTS = 300, STEP_US = 10000, STACK_SIZE = 262144 };
_Static_assert(ATOMIC_INT_LOCK_FREE == 2, "shared counters must be lock-free");

struct shared {
	atomic_int worker_tid;
	atomic_int joined_tid;
	atomic_int entered;
};

struct worker_args {
	struct shared *shared;
	int command;
	void *stack;
};

static int failed(const char *phase)
{
	fprintf(stderr, "GROUP_LIFECYCLE FAIL phase=%s errno=%d\n", phase,
		errno);
	return 0;
}

#define REQUIRE(condition, phase)      \
	do {                           \
		if (!(condition)) {    \
			failed(phase); \
			goto out;      \
		}                      \
	} while (0)

static int wait_event(pid_t *owned, int options, int code, int value)
{
	pid_t tid = *owned;
	for (int i = 0; i < ATTEMPTS; ++i) {
		siginfo_t info = { 0 };
		if (waitid(P_PID, tid, &info, options | WNOHANG) < 0) {
			if (errno == EINTR)
				continue;
			if (errno == ECHILD)
				*owned = 0;
			return 0;
		}
		if (info.si_pid) {
			if (!(options & WNOWAIT) &&
			    (info.si_code == CLD_EXITED ||
			     info.si_code == CLD_KILLED ||
			     info.si_code == CLD_DUMPED))
				*owned = 0;
			if (info.si_pid == tid && info.si_code == code &&
			    info.si_status == value)
				return 1;
			fprintf(stderr,
				"GROUP_LIFECYCLE event=%d/%d/%d expected=%d/%d/%d\n",
				info.si_pid, info.si_code, info.si_status, tid,
				code, value);
			return 0;
		}
		usleep(STEP_US);
	}
	return 0;
}

static int no_stop(pid_t child)
{
	siginfo_t info = { 0 };
	return waitid(P_PID, child, &info, WSTOPPED | WNOHANG | WNOWAIT) == 0 &&
	       !info.si_pid;
}

static int wait_state(pid_t child, pid_t tid, char expected)
{
	char path[96], line[256];
	snprintf(path, sizeof(path), "/proc/%d/task/%d/status", child, tid);
	for (int i = 0; i < ATTEMPTS; ++i) {
		FILE *stream = fopen(path, "r");
		if (!stream)
			return 0;
		char state = 0;
		while (fgets(line, sizeof(line), stream))
			if (sscanf(line, "State: %c", &state) == 1)
				break;
		fclose(stream);
		if (state == expected)
			return 1;
		usleep(STEP_US);
	}
	return 0;
}

static int wait_positive(atomic_int *value)
{
	for (int i = 0; i < ATTEMPTS; ++i) {
		int current = atomic_load(value);
		if (current)
			return current;
		usleep(STEP_US);
	}
	return 0;
}

static int joined_thread(void *arg)
{
	struct shared *shared = arg;
	atomic_store(&shared->entered, 1);
	for (;;)
		atomic_signal_fence(memory_order_seq_cst);
	return 0;
}

static void *worker(void *arg)
{
	struct worker_args *args = arg;
	atomic_store(&args->shared->worker_tid, syscall(SYS_gettid));
	char command;
	if (read(args->command, &command, 1) != 1)
		_exit(2);
	if (command == 'X')
		syscall(SYS_exit, 42);
	if (command != 'C')
		_exit(2);
	int tid = clone(joined_thread, (char *)args->stack + STACK_SIZE,
			CLONE_VM | CLONE_FS | CLONE_FILES | CLONE_SIGHAND |
				CLONE_THREAD,
			args->shared);
	atomic_store(&args->shared->joined_tid, tid);
	for (;;)
		pause();
	return NULL;
}

static int reap(pid_t *owned)
{
	if (!*owned)
		return 1;
	for (int i = 0; i < ATTEMPTS; ++i) {
		int status;
		pid_t got = waitpid(*owned, &status, WNOHANG);
		if ((got == *owned &&
		     (WIFEXITED(status) || WIFSIGNALED(status))) ||
		    (got < 0 && errno == ECHILD)) {
			*owned = 0;
			return 1;
		}
		if (got < 0 && errno != EINTR)
			return 0;
		usleep(STEP_US);
	}
	return 0;
}

static int lifecycle(int joining)
{
	pid_t child = 0, tracee = 0;
	int command[2] = { -1, -1 }, ok = 0;
	struct shared *shared = mmap(NULL, sizeof(*shared),
				     PROT_READ | PROT_WRITE,
				     MAP_SHARED | MAP_ANONYMOUS, -1, 0);
	REQUIRE(shared != MAP_FAILED, "shared-map");
	atomic_init(&shared->worker_tid, 0);
	atomic_init(&shared->joined_tid, 0);
	atomic_init(&shared->entered, 0);
	REQUIRE(pipe(command) == 0, "pipe");
	child = fork();
	if (child < 0) {
		child = 0;
		REQUIRE(0, "fork");
	}
	if (!child) {
		close(command[1]);
		alarm(20);
		void *stack = mmap(NULL, STACK_SIZE, PROT_READ | PROT_WRITE,
				   MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
		if (stack == MAP_FAILED)
			_exit(2);
		struct worker_args args = { shared, command[0], stack };
		pthread_t thread;
		if (pthread_create(&thread, NULL, worker, &args))
			_exit(2);
		for (;;)
			pause();
	}
	close(command[0]);
	command[0] = -1;
	tracee = wait_positive(&shared->worker_tid);
	REQUIRE(tracee > 0 && tracee != child, "worker-ready");
	REQUIRE(ptrace(PTRACE_ATTACH, tracee, NULL, NULL) == 0, "attach");
	REQUIRE(wait_event(&tracee, WSTOPPED, CLD_TRAPPED, SIGSTOP),
		"attach-stop");
	if (joining) {
		REQUIRE(ptrace(PTRACE_CONT, tracee, NULL,
			       (void *)(long)SIGSTOP) == 0,
			"inject-stop");
		REQUIRE(wait_event(&tracee, WSTOPPED, CLD_TRAPPED, SIGSTOP),
			"group-trap");
		REQUIRE(wait_event(&child, WSTOPPED, CLD_STOPPED, SIGSTOP),
			"group-complete");
		REQUIRE(write(command[1], "C", 1) == 1, "clone-command");
		REQUIRE(ptrace(PTRACE_CONT, tracee, NULL, NULL) == 0,
			"run-creator");
		int joined = wait_positive(&shared->joined_tid);
		REQUIRE(joined > 0, "clone-return");
		REQUIRE(wait_state(child, joined, 'T'), "joined-parked");
		REQUIRE(!atomic_load(&shared->entered), "no-first-user-entry");
		REQUIRE(no_stop(child), "no-duplicate-stop");
		REQUIRE(kill(child, SIGCONT) == 0, "continue-joined");
		REQUIRE(wait_positive(&shared->entered) == 1, "joined-resumes");
	} else {
		REQUIRE(ptrace(PTRACE_SETOPTIONS, tracee, NULL,
			       (void *)PTRACE_O_TRACEEXIT) == 0,
			"trace-exit");
		REQUIRE(ptrace(PTRACE_CONT, tracee, NULL, NULL) == 0,
			"run-exiter");
		REQUIRE(write(command[1], "X", 1) == 1, "exit-command");
		REQUIRE(wait_event(&tracee, WSTOPPED, CLD_TRAPPED,
				   SIGTRAP | (PTRACE_EVENT_EXIT << 8)),
			"exit-trap");
		REQUIRE(kill(child, SIGSTOP) == 0, "stop-with-exiter");
		REQUIRE(wait_state(child, child, 'T'), "leader-acknowledged");
		REQUIRE(no_stop(child), "no-premature-stop");
		REQUIRE(ptrace(PTRACE_CONT, tracee, NULL, NULL) == 0,
			"finish-exit");
		REQUIRE(wait_event(&tracee, WEXITED, CLD_EXITED, 42),
			"worker-exit");
		REQUIRE(wait_event(&child, WSTOPPED, CLD_STOPPED, SIGSTOP),
			"exit-completes-stop");
		REQUIRE(no_stop(child), "no-double-exit-report");
		REQUIRE(kill(child, SIGCONT) == 0, "continue-survivor");
	}
	ok = 1;
out:
	// The supervisor owns the process and its traced nonleader. Always release
	// the nonleader's exit before reaping the process, including failed phases.
	if (child > 0 && kill(child, SIGKILL) < 0 && errno != ESRCH)
		ok = 0;
	if (tracee > 0 && !reap(&tracee))
		ok = failed("reap-tracee");
	if (!reap(&child))
		ok = failed("reap-child");
	for (int i = 0; i < 2; ++i)
		if (command[i] >= 0)
			close(command[i]);
	if (shared != MAP_FAILED)
		munmap(shared, sizeof(*shared));
	printf("GROUP_LIFECYCLE case=%s pass=%d\n",
	       joining ? "join-stopped" : "pending-exit", ok);
	return ok;
}

static volatile sig_atomic_t wait_expired;

static void expire_wait(int number)
{
	(void)number;
	wait_expired = 1;
}

static int blocking_parent_wait(int continued)
{
	pid_t parent = getpid(), child = 0, controller = 0;
	int ok = 0, handler_installed = 0;
	struct sigaction old_action, action = { .sa_handler = expire_wait };
	atomic_int *ready = mmap(NULL, sizeof(*ready), PROT_READ | PROT_WRITE,
				 MAP_SHARED | MAP_ANONYMOUS, -1, 0);
	REQUIRE(ready != MAP_FAILED, "blocking-map");
	atomic_init(ready, 0);
	sigemptyset(&action.sa_mask);
	REQUIRE(sigaction(SIGALRM, &action, &old_action) == 0,
		"blocking-alarm-handler");
	handler_installed = 1;
	child = fork();
	if (child < 0) {
		child = 0;
		REQUIRE(0, "blocking-child-fork");
	}
	if (!child) {
		for (;;)
			pause();
	}
	if (continued) {
		REQUIRE(kill(child, SIGSTOP) == 0, "blocking-initial-stop");
		REQUIRE(wait_event(&child, WSTOPPED, CLD_STOPPED, SIGSTOP),
			"blocking-initial-wait");
	}
	controller = fork();
	if (controller < 0) {
		controller = 0;
		REQUIRE(0, "controller-fork");
	}
	if (!controller) {
		// After the release store below, the parent performs only waitid. A
		// sleeping state observed after that store confirms blocking entry.
		if (wait_positive(ready) != 1 ||
		    !wait_state(parent, parent, 'S'))
			_exit(2);
		if (kill(child, continued ? SIGCONT : SIGSTOP) < 0)
			_exit(2);
		// Its exit must not accidentally supply the target child's wakeup.
		while (atomic_load(ready) != 2)
			usleep(STEP_US);
		_exit(0);
	}
	wait_expired = 0;
	alarm(5);
	siginfo_t info = { 0 };
	atomic_store(ready, 1);
	int result =
		waitid(P_PID, child, &info, continued ? WCONTINUED : WSTOPPED);
	int needed_alarm = wait_expired;
	alarm(0);
	atomic_store(ready, 2);
	REQUIRE(result == 0 && !needed_alarm && info.si_pid == child &&
			info.si_code ==
				(continued ? CLD_CONTINUED : CLD_STOPPED) &&
			info.si_status == (continued ? SIGCONT : SIGSTOP),
		"blocking-child-event");
	REQUIRE(wait_event(&controller, WEXITED, CLD_EXITED, 0),
		"controller-exit");
	ok = 1;
out:
	if (handler_installed) {
		alarm(0);
		if (sigaction(SIGALRM, &old_action, NULL) < 0)
			ok = 0;
	}
	if (controller > 0 && kill(controller, SIGKILL) < 0 && errno != ESRCH)
		ok = 0;
	if (!reap(&controller))
		ok = failed("reap-controller");
	if (child > 0 && kill(child, SIGKILL) < 0 && errno != ESRCH)
		ok = 0;
	if (!reap(&child))
		ok = failed("reap-blocking-child");
	if (ready != MAP_FAILED)
		munmap(ready, sizeof(*ready));
	printf("GROUP_LIFECYCLE case=blocking-%s pass=%d\n",
	       continued ? "continue" : "stop", ok);
	return ok;
}

int main(void)
{
	setbuf(stdout, NULL);
	if (signal(SIGPIPE, SIG_IGN) == SIG_ERR)
		return 1;
	int ok = lifecycle(0);
	ok &= lifecycle(1);
	ok &= blocking_parent_wait(0);
	ok &= blocking_parent_wait(1);
	return ok ? 0 : 1;
}
