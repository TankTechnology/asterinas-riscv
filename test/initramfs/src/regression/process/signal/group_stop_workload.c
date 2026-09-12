/* SPDX-License-Identifier: MPL-2.0 */

#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdio.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

enum {
	PROCESSES = 2,
	WORKERS = 2,
	ROUNDS = 4,
	ATTEMPTS = 500,
	STEP_US = 10000
};
_Static_assert(ATOMIC_INT_LOCK_FREE == 2, "handler atomics must be lock-free");
_Static_assert(ATOMIC_LONG_LOCK_FREE == 2, "progress must be lock-free");
_Static_assert(ATOMIC_POINTER_LOCK_FREE == 2,
	       "handler pointer must be lock-free");

struct workload {
	atomic_int tid[WORKERS];
	atomic_ulong progress[WORKERS];
	atomic_int handled;
	atomic_int finished;
};

// Each child sets its own pointer before installing the handler or threading.
static _Atomic(struct workload *) current;

static void terminate_workload(int signal_number)
{
	(void)signal_number;
	struct workload *shared =
		atomic_load_explicit(&current, memory_order_acquire);
	atomic_fetch_add_explicit(&shared->handled, 1, memory_order_release);
}

static void *worker(void *argument)
{
	int index = *(int *)argument;
	struct workload *shared =
		atomic_load_explicit(&current, memory_order_acquire);
	atomic_store_explicit(&shared->tid[index], syscall(SYS_gettid),
			      memory_order_release);
	while (!atomic_load_explicit(&shared->handled, memory_order_acquire)) {
		atomic_fetch_add_explicit(&shared->progress[index], 1,
					  memory_order_relaxed);
		if (index == 1)
			usleep(STEP_US);
	}
	atomic_fetch_add_explicit(&shared->finished, 1, memory_order_release);
	return NULL;
}

static void child_workload(struct workload *shared)
{
	atomic_store_explicit(&current, shared, memory_order_release);
	struct sigaction action = { .sa_handler = terminate_workload };
	sigset_t unblocked;
	sigemptyset(&action.sa_mask);
	sigemptyset(&unblocked);
	sigaddset(&unblocked, SIGTERM);
	if (sigaction(SIGTERM, &action, NULL) ||
	    pthread_sigmask(SIG_UNBLOCK, &unblocked, NULL))
		_exit(2);
	pthread_t threads[WORKERS];
	int indices[WORKERS] = { 0, 1 };
	for (int index = 0; index < WORKERS; ++index)
		if (pthread_create(&threads[index], NULL, worker,
				   &indices[index]))
			_exit(3);
	for (int index = 0; index < WORKERS; ++index) {
		int result = EBUSY;
		for (int attempt = 0; attempt < ATTEMPTS && result == EBUSY;
		     ++attempt) {
			result = pthread_tryjoin_np(threads[index], NULL);
			if (result == EBUSY)
				usleep(STEP_US);
		}
		if (result)
			_exit(4);
	}
	_exit(atomic_load(&shared->handled) == 1 &&
			      atomic_load(&shared->finished) == WORKERS ?
		      0 :
		      5);
}

static int wait_event(pid_t *owned, int stopped)
{
	for (int attempt = 0; attempt < ATTEMPTS; ++attempt) {
		int status;
		pid_t result = waitpid(*owned, &status,
				       WNOHANG | (stopped ? WUNTRACED : 0));
		if (result == *owned) {
			if (WIFEXITED(status) || WIFSIGNALED(status))
				*owned = 0;
			return stopped ? WIFSTOPPED(status) &&
						 WSTOPSIG(status) == SIGSTOP :
					 WIFEXITED(status) &&
						 WEXITSTATUS(status) == 0;
		}
		if (result < 0 && errno != EINTR) {
			if (errno == ECHILD)
				*owned = 0;
			return 0;
		}
		usleep(STEP_US);
	}
	return 0;
}

static int observe_task(pid_t child, pid_t tid, char expected, int pending)
{
	char path[96], line[256], state = 0;
	snprintf(path, sizeof(path), "/proc/%d/task/%d/status", child, tid);
	FILE *stream = fopen(path, "r");
	if (!stream)
		return 0;
	unsigned long long thread_pending = 0, shared_pending = 0;
	int fields = 0;
	while (fgets(line, sizeof(line), stream)) {
		sscanf(line, "State: %c", &state);
		fields += sscanf(line, "SigPnd: %llx", &thread_pending) == 1;
		fields += sscanf(line, "ShdPnd: %llx", &shared_pending) == 1;
	}
	int ok = !ferror(stream);
	if (fclose(stream))
		ok = 0;
	unsigned long long mask = pending == 1 ? shared_pending :
						 thread_pending;
	return ok && state == expected &&
	       (!pending || (fields == 2 && (mask & (1ULL << (SIGTERM - 1)))));
}

static int ready(struct workload *shared, pid_t child)
{
	for (int attempt = 0; attempt < ATTEMPTS; ++attempt) {
		int all_ready = 1;
		for (int index = 0; index < WORKERS; ++index)
			all_ready &= atomic_load_explicit(
					     &shared->tid[index],
					     memory_order_acquire) > 0 &&
				     atomic_load(&shared->progress[index]) > 0;
		if (all_ready &&
		    observe_task(child, atomic_load(&shared->tid[1]), 'S', 0))
			return 1;
		usleep(STEP_US);
	}
	return 0;
}

#define REQUIRE(condition, phase)                                                   \
	do {                                                                        \
		if (!(condition)) {                                                 \
			fprintf(stderr,                                             \
				"GROUP_WORKLOAD FAIL round=%d phase=%s errno=%d\n", \
				round, phase, errno);                               \
			goto out;                                                   \
		}                                                                   \
	} while (0)

static int run_round(int round)
{
	pid_t owned[PROCESSES] = { 0 };
	unsigned long snapshot[PROCESSES][WORKERS];
	int ok = 0;
	struct workload *shared = mmap(NULL, sizeof(*shared) * PROCESSES,
				       PROT_READ | PROT_WRITE,
				       MAP_SHARED | MAP_ANONYMOUS, -1, 0);
	if (shared == MAP_FAILED)
		return 0;
	for (int process = 0; process < PROCESSES; ++process) {
		atomic_init(&shared[process].handled, 0);
		atomic_init(&shared[process].finished, 0);
		for (int index = 0; index < WORKERS; ++index) {
			atomic_init(&shared[process].tid[index], 0);
			atomic_init(&shared[process].progress[index], 0);
		}
		pid_t child = fork();
		REQUIRE(child >= 0, "fork");
		if (!child)
			child_workload(&shared[process]);
		owned[process] = child;
	}
	for (int process = 0; process < PROCESSES; ++process)
		REQUIRE(ready(&shared[process], owned[process]),
			"workers ready");
	for (int process = 0; process < PROCESSES; ++process)
		REQUIRE(kill(owned[process], SIGSTOP) == 0, "send stop");
	for (int process = 0; process < PROCESSES; ++process) {
		REQUIRE(wait_event(&owned[process], 1), "confirmed stop");
		for (int index = 0; index < WORKERS; ++index)
			snapshot[process][index] =
				atomic_load(&shared[process].progress[index]);
	}
	for (int process = 0; process < PROCESSES; ++process) {
		pid_t target =
			atomic_load(&shared[process].tid[round % WORKERS]);
		REQUIRE(target != owned[process], "nonleader target");
		REQUIRE((process ? syscall(SYS_tgkill, owned[process], target,
					   SIGTERM) :
				   kill(owned[process], SIGTERM)) == 0,
			"send term");
	}
	// Exercise both workloads concurrently during a bounded absence interval.
	usleep(100000);
	for (int process = 0; process < PROCESSES; ++process) {
		REQUIRE(atomic_load(&shared[process].handled) == 0 &&
				atomic_load(&shared[process].finished) == 0,
			"stopped handler quiet");
		REQUIRE(observe_task(owned[process], owned[process], 'T',
				     process ? 0 : 1),
			"leader stopped and shared term pending");
		for (int index = 0; index < WORKERS; ++index) {
			REQUIRE(atomic_load(&shared[process].progress[index]) ==
					snapshot[process][index],
				"stopped progress quiet");
			REQUIRE(observe_task(
					owned[process],
					atomic_load(&shared[process].tid[index]),
					'T',
					process && index == round % WORKERS ?
						2 :
						0),
				"worker stopped and directed term pending");
		}
	}
	printf("GROUP_WORKLOAD stopped round=%d processes=%d directed_worker=%d pending=1 quiet=1\n",
	       round, PROCESSES, round % WORKERS);
	for (int process = 0; process < PROCESSES; ++process)
		REQUIRE(kill(owned[process], SIGCONT) == 0, "send cont");
	for (int process = 0; process < PROCESSES; ++process) {
		REQUIRE(wait_event(&owned[process], 0),
			"graceful exit deadline");
		REQUIRE(atomic_load(&shared[process].handled) == 1 &&
				atomic_load(&shared[process].finished) ==
					WORKERS,
			"all workers finished");
	}
	ok = 1;
out:
	// Signal every remaining owned child before polling any individual reap.
	for (int process = 0; process < PROCESSES; ++process)
		if (owned[process] && kill(owned[process], SIGKILL) &&
		    errno != ESRCH)
			fprintf(stderr,
				"GROUP_WORKLOAD cleanup kill pid=%d errno=%d\n",
				owned[process], errno);
	for (int process = 0; process < PROCESSES; ++process) {
		if (owned[process])
			wait_event(&owned[process], 0);
		if (owned[process]) {
			fprintf(stderr,
				"GROUP_WORKLOAD cleanup deadline pid=%d\n",
				owned[process]);
			ok = 0;
		}
	}
	if (munmap(shared, sizeof(*shared) * PROCESSES))
		ok = 0;
	printf("GROUP_WORKLOAD round=%d graceful=%d\n", round, ok);
	return ok;
}

int main(void)
{
	setvbuf(stdout, NULL, _IONBF, 0);
	for (int round = 0; round < ROUNDS; ++round)
		if (!run_round(round))
			return 1;
	printf("GROUP_WORKLOAD COMPLETE rounds=%d processes_per_round=%d workers_per_process=%d\n",
	       ROUNDS, PROCESSES, WORKERS);
	return 0;
}
