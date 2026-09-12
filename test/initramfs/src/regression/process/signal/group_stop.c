/* SPDX-License-Identifier: MPL-2.0 */

#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/ptrace.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

enum {
	WAIT_ATTEMPTS = 500,
	WAIT_STEP_US = 10000,
	WORKERS = 3,
	STOP_ROUNDS = 32
};
static int handler_fd;

static void caught_signal(int number)
{
	char byte = (char)number;
	if (write(handler_fd, &byte, 1) != 1)
		_exit(2);
}

static pid_t wait_bounded(pid_t child, int *status, int options)
{
	for (int attempt = 0; attempt < WAIT_ATTEMPTS; ++attempt) {
		pid_t result = waitpid(child, status, options | WNOHANG);
		if (result == child || (result < 0 && errno != EINTR))
			return result;
		usleep(WAIT_STEP_US);
	}
	return 0;
}

static void reap_owned(pid_t child)
{
	assert(kill(child, SIGKILL) == 0 || errno == ESRCH);
	int status;
	assert(wait_bounded(child, &status, 0) == child);
}

static int stop_child(pid_t *child)
{
	assert(kill(*child, SIGSTOP) == 0);
	int status;
	if (wait_bounded(*child, &status, WUNTRACED) != *child)
		return 0;
	if (!WIFSTOPPED(status)) {
		*child = 0;
		return 0;
	}
	return WSTOPSIG(status) == SIGSTOP;
}

static unsigned long long pending_mask(pid_t child, int *fields)
{
	char path[64], line[256];
	snprintf(path, sizeof(path), "/proc/%d/status", child);
	FILE *file = fopen(path, "r");
	assert(file != NULL);
	unsigned long long mask = 0, value;
	while (fgets(line, sizeof(line), file)) {
		if (sscanf(line, "SigPnd: %llx", &value) == 1 ||
		    sscanf(line, "ShdPnd: %llx", &value) == 1) {
			mask |= value;
			++*fields;
		}
	}
	assert(fclose(file) == 0);
	return mask;
}

static int stopped_delivery(int signal_number, int thread_directed)
{
	int ready[2], handled[2];
	assert(pipe(ready) == 0 && pipe(handled) == 0);
	pid_t child = fork();
	assert(child >= 0);
	if (child == 0) {
		close(ready[0]);
		close(handled[0]);
		if (signal_number == SIGUSR1) {
			handler_fd = handled[1];
			struct sigaction action = { .sa_handler =
							    caught_signal };
			sigemptyset(&action.sa_mask);
			assert(sigaction(SIGUSR1, &action, NULL) == 0);
		}
		assert(write(ready[1], "r", 1) == 1);
		for (;;)
			pause();
	}
	close(ready[1]);
	close(handled[1]);
	char byte;
	struct pollfd ready_event = { .fd = ready[0], .events = POLLIN };
	int ok = poll(&ready_event, 1, 1000) == 1 &&
		 (ready_event.revents & POLLIN) &&
		 read(ready[0], &byte, 1) == 1;
	close(ready[0]);
	ok = ok && stop_child(&child);
	if (ok) {
		int result = thread_directed ? syscall(SYS_tgkill, child, child,
						       signal_number) :
					       kill(child, signal_number);
		assert(result == 0);
		if (signal_number == SIGKILL) {
			int status;
			if (wait_bounded(child, &status, 0) == child) {
				ok = WIFSIGNALED(status) &&
				     WTERMSIG(status) == SIGKILL;
				child = 0;
			} else {
				ok = 0;
			}
		} else {
			// An absence assertion needs a bounded observation interval.
			struct pollfd event = { .fd = handled[0],
						.events = POLLIN };
			int quiet = poll(&event, 1, 100) == 0;
			ok = quiet;
			int status;
			pid_t result = waitpid(child, &status, WNOHANG);
			if (result == child) {
				printf("STOPPED_OBSERVATION signal=%d thread=%d quiet=%d alive=0\n",
				       signal_number, thread_directed, quiet);
				child = 0;
				ok = 0;
			} else {
				assert(result == 0);
				int fields = 0;
				int pending = (pending_mask(child, &fields) &
					       (1ULL << (signal_number - 1))) !=
					      0;
				printf("STOPPED_OBSERVATION signal=%d thread=%d quiet=%d alive=1 pending=%d fields=%d\n",
				       signal_number, thread_directed, quiet,
				       pending, fields);
				ok &= fields == 2 && pending;
				assert(kill(child, SIGCONT) == 0);
				if (signal_number == SIGUSR1) {
					ok &= poll(&event, 1, 1000) == 1;
					ok &= (event.revents & POLLIN) != 0;
					if (event.revents & POLLIN) {
						assert(read(handled[0], &byte,
							    1) == 1);
						ok &= byte == SIGUSR1;
					}
				} else if (wait_bounded(child, &status, 0) ==
					   child) {
					ok &= WIFSIGNALED(status) &&
					      WTERMSIG(status) == SIGTERM;
					child = 0;
				} else {
					ok = 0;
				}
			}
		}
	}
	if (child)
		reap_owned(child);
	close(handled[0]);
	printf("STOPPED_DELIVERY signal=%d thread=%d pass=%d\n", signal_number,
	       thread_directed, ok);
	return ok;
}

struct busy_member {
	atomic_ulong counter;
	atomic_int tid;
};

static void *busy_worker(void *argument)
{
	struct busy_member *member = argument;
	atomic_store_explicit(&member->tid, syscall(SYS_gettid),
			      memory_order_release);
	for (;;)
		atomic_fetch_add_explicit(&member->counter, 1,
					  memory_order_relaxed);
	return NULL;
}

static int observed_stop_state(pid_t child, pid_t tid, char expected)
{
	char path[96], line[256], state = 0;
	snprintf(path, sizeof(path), "/proc/%d/task/%d/status", child, tid);
	FILE *file = fopen(path, "r");
	if (!file)
		return 0;
	while (fgets(line, sizeof(line), file))
		sscanf(line, "State: %c", &state);
	int failed = ferror(file);
	if (fclose(file) || failed)
		return 0;
	if (state != expected) {
		printf("GROUP_STOP_STATE tid=%d state=%c expected=%c\n", tid,
		       state, expected);
		return 0;
	}
	snprintf(path, sizeof(path), "/proc/%d/task/%d/stat", child, tid);
	file = fopen(path, "r");
	if (!file)
		return 0;
	char stat_line[4096];
	int read_ok = fgets(stat_line, sizeof(stat_line), file) != NULL;
	if (fclose(file) || !read_ok)
		return 0;
	char *comm_end = strrchr(stat_line, ')');
	state = 0;
	if (comm_end)
		sscanf(comm_end + 1, " %c", &state);
	if (state != expected)
		printf("GROUP_STOP_STAT tid=%d state=%c expected=%c\n", tid,
		       state, expected);
	return state == expected;
}

static int group_participation(void)
{
	struct busy_member *members = mmap(NULL, 4096, PROT_READ | PROT_WRITE,
					   MAP_SHARED | MAP_ANONYMOUS, -1, 0);
	assert(members != MAP_FAILED);
	for (int index = 0; index < WORKERS; ++index) {
		atomic_init(&members[index].counter, 0);
		atomic_init(&members[index].tid, 0);
	}
	pid_t child = fork();
	assert(child >= 0);
	if (child == 0) {
		// An ordinary blocked signal must not make a logically stopped
		// member appear running, even if it wakes internally in the kernel.
		sigset_t blocked;
		sigemptyset(&blocked);
		sigaddset(&blocked, SIGUSR1);
		assert(pthread_sigmask(SIG_BLOCK, &blocked, NULL) == 0);
		pthread_t workers[WORKERS];
		for (int index = 0; index < WORKERS; ++index)
			assert(pthread_create(&workers[index], NULL,
					      busy_worker,
					      &members[index]) == 0);
		for (;;)
			pause();
	}
	int ready = 0;
	for (int attempt = 0; attempt < WAIT_ATTEMPTS && !ready; ++attempt) {
		ready = 1;
		for (int index = 0; index < WORKERS; ++index) {
			ready &= atomic_load_explicit(&members[index].counter,
						      memory_order_relaxed) !=
				 0;
			ready &= atomic_load_explicit(&members[index].tid,
						      memory_order_acquire) > 0;
		}
		if (!ready)
			usleep(WAIT_STEP_US);
	}
	int ok = ready && stop_child(&child);
	int completed = 0;
	for (int round = 0; ok && round < STOP_ROUNDS; ++round) {
		unsigned long stopped[WORKERS];
		for (int index = 0; index < WORKERS; ++index)
			stopped[index] = atomic_load_explicit(
				&members[index].counter, memory_order_relaxed);
		usleep(round == 0 ? 100000 : 1000);
		for (int sample = 0; ok && sample < 16; ++sample) {
			pid_t tid = atomic_load_explicit(
				&members[sample % WORKERS].tid,
				memory_order_acquire);
			assert(tid > 0);
			assert((sample % 2 ? syscall(SYS_tgkill, child, tid,
						     SIGUSR1) :
					     kill(child, SIGUSR1)) == 0);
			ok &= observed_stop_state(child, child, 'T');
			for (int index = 0; index < WORKERS; ++index)
				ok &= observed_stop_state(
					child,
					atomic_load_explicit(
						&members[index].tid,
						memory_order_acquire),
					'T');
		}
		for (int index = 0; index < WORKERS; ++index)
			ok &= stopped[index] ==
			      atomic_load_explicit(&members[index].counter,
						   memory_order_relaxed);
		assert(kill(child, SIGCONT) == 0);
		++completed;
		// Do not wait for user progress between CONT and the next STOP:
		// previous waiters may still be returning from the older episode.
		if (ok && round + 1 < STOP_ROUNDS)
			ok = stop_child(&child);
	}
	if (child)
		reap_owned(child);
	assert(munmap(members, 4096) == 0);
	printf("GROUP_PARTICIPATION workers=%d rounds=%d/%d pass=%d\n", WORKERS,
	       completed, STOP_ROUNDS, ok);
	return ok;
}

static int ptrace_state(void)
{
	int ready[2];
	assert(pipe(ready) == 0);
	pid_t child = fork();
	assert(child >= 0);
	if (!child) {
		close(ready[0]);
		sigset_t blocked;
		sigemptyset(&blocked);
		sigaddset(&blocked, SIGUSR1);
		assert(sigprocmask(SIG_BLOCK, &blocked, NULL) == 0);
		assert(ptrace(PTRACE_TRACEME, 0, NULL, NULL) == 0);
		raise(SIGSTOP);
		assert(write(ready[1], "R", 1) == 1);
		for (;;)
			pause();
	}
	close(ready[1]);
	int status;
	pid_t waited = wait_bounded(child, &status, WUNTRACED);
	int ok = waited == child && WIFSTOPPED(status) &&
		 WSTOPSIG(status) == SIGSTOP;
	if (waited == child && (WIFEXITED(status) || WIFSIGNALED(status)))
		child = 0;
	for (int sample = 0; ok && sample < 64; ++sample) {
		assert(kill(child, sample % 2 ? SIGUSR1 : SIGCONT) == 0);
		ok &= observed_stop_state(child, child, 't');
	}
	if (ok) {
		ok = ptrace(PTRACE_CONT, child, NULL, NULL) == 0;
		// CONT does not release the ptrace hold. After the tracer resumes
		// the task, its pending CONT still causes a signal-delivery stop.
		if (ok) {
			waited = wait_bounded(child, &status, WUNTRACED);
			ok = waited == child && WIFSTOPPED(status) &&
			     WSTOPSIG(status) == SIGCONT;
			printf("GROUP_PTRACE_CONT_DELIVERY observed=%d pass=%d\n",
			       waited == child ? status : -1, ok);
			if (waited == child &&
			    (WIFEXITED(status) || WIFSIGNALED(status)))
				child = 0;
		}
		if (ok)
			ok = ptrace(PTRACE_CONT, child, NULL, NULL) == 0;
		struct pollfd event = { .fd = ready[0], .events = POLLIN };
		char byte;
		ok = ok && poll(&event, 1, 1000) == 1 &&
		     (event.revents & POLLIN) &&
		     read(ready[0], &byte, 1) == 1 && byte == 'R';
	}
	if (child)
		reap_owned(child);
	close(ready[0]);
	printf("GROUP_PTRACE_STATE pass=%d\n", ok);
	return ok;
}

int main(void)
{
	int passed = 0;
	const int signals[] = { SIGUSR1, SIGTERM, SIGKILL };
	for (unsigned int index = 0; index < sizeof(signals) / sizeof(int);
	     ++index)
		for (int route = 0; route < 2; ++route)
			passed += stopped_delivery(signals[index], route);
	passed += group_participation();
	passed += ptrace_state();
	printf("GROUP_STOP passed=%d/8\n", passed);
	return passed == 8 ? 0 : 1;
}
