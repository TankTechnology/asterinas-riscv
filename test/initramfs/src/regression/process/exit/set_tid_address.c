// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE
#include <errno.h>
#include <linux/futex.h>
#include <sched.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define CHILD_STACK_SIZE (1024 * 1024)
#define OLD_TID_SENTINEL ((int)0x13579bdf)
#define SCENARIO_RETRY 2
#define MAX_WAKE_ATTEMPTS 100

enum scenario_kind {
	REPLACE_CLEAR_CHILD_TID,
	DISARM_CLEAR_CHILD_TID,
	INVALID_CLEAR_CHILD_TID,
};

struct scenario {
	enum scenario_kind kind;
	_Atomic int ready;
	_Atomic int allow_exit;
	_Atomic int old_tid;
	_Atomic int new_tid;
	_Atomic int child_error;
};

static int futex_wait(_Atomic int *address, int expected)
{
	int ret;

	do {
		ret = syscall(SYS_futex, address, FUTEX_WAIT, expected, NULL, NULL,
			      0);
	} while (ret < 0 && errno == EINTR);

	return ret;
}

static int futex_wait_bounded(_Atomic int *address, int expected)
{
	struct timespec timeout = { .tv_sec = 1 };
	int ret;

	do {
		ret = syscall(SYS_futex, address, FUTEX_WAIT, expected, &timeout,
			      NULL, 0);
	} while (ret < 0 && errno == EINTR);

	return ret;
}

static int futex_wake(_Atomic int *address)
{
	return syscall(SYS_futex, address, FUTEX_WAKE, 1, NULL, NULL, 0);
}

static void wait_until_set(_Atomic int *value)
{
	while (atomic_load_explicit(value, memory_order_acquire) == 0) {
		if (futex_wait(value, 0) < 0 && errno != EAGAIN) {
			perror("futex wait");
			exit(EXIT_FAILURE);
		}
	}
}

static int child_main(void *argument)
{
	struct scenario *scenario = argument;
	const struct timespec waiter_settle = { .tv_nsec = 10 * 1000 * 1000 };
	pid_t tid = syscall(SYS_gettid);
	void *tidptr;
	long ret;

	switch (scenario->kind) {
	case REPLACE_CLEAR_CHILD_TID:
		atomic_store_explicit(&scenario->new_tid, tid,
				      memory_order_relaxed);
		tidptr = &scenario->new_tid;
		break;
	case DISARM_CLEAR_CHILD_TID:
		tidptr = NULL;
		break;
	case INVALID_CLEAR_CHILD_TID:
		tidptr = (void *)(uintptr_t)1;
		break;
	default:
		return EXIT_FAILURE;
	}

	ret = syscall(SYS_set_tid_address, tidptr);
	if (ret != tid)
		atomic_store_explicit(&scenario->child_error, 1,
				      memory_order_relaxed);

	atomic_store_explicit(&scenario->ready, 1, memory_order_release);
	if (futex_wake(&scenario->ready) < 0)
		atomic_store_explicit(&scenario->child_error, 1,
				      memory_order_relaxed);

	wait_until_set(&scenario->allow_exit);
	if (scenario->kind == REPLACE_CLEAR_CHILD_TID) {
		/* Give the parent time to enqueue the futex waiter before exit. */
		while (nanosleep(&waiter_settle, NULL) < 0 && errno == EINTR) {
		}
	}
	return atomic_load_explicit(&scenario->child_error, memory_order_relaxed)
		       ? EXIT_FAILURE
		       : EXIT_SUCCESS;
}

static int run_scenario(enum scenario_kind kind)
{
	struct scenario *scenario;
	void *stack;
	void *stack_top;
	pid_t child;
	int expected_new_tid = 0;
	int raced_with_exit = 0;
	int status;
	int result = EXIT_FAILURE;

	scenario = calloc(1, sizeof(*scenario));
	stack = malloc(CHILD_STACK_SIZE);
	if (scenario == NULL || stack == NULL) {
		perror("allocate clone state");
		goto out;
	}

	scenario->kind = kind;
	atomic_store_explicit(&scenario->old_tid, OLD_TID_SENTINEL,
			      memory_order_relaxed);
	stack_top = (char *)stack + CHILD_STACK_SIZE;

	child = clone(child_main, stack_top,
		      CLONE_VM | CLONE_CHILD_CLEARTID | SIGCHLD, scenario, NULL,
		      NULL, &scenario->old_tid);
	if (child < 0) {
		perror("clone");
		goto out;
	}

	wait_until_set(&scenario->ready);
	if (atomic_load_explicit(&scenario->old_tid, memory_order_relaxed) !=
	    OLD_TID_SENTINEL) {
		fprintf(stderr, "old clear_child_tid was modified before exit\n");
		goto kill_child;
	}
	if (kind == REPLACE_CLEAR_CHILD_TID)
		expected_new_tid = atomic_load_explicit(&scenario->new_tid,
						memory_order_relaxed);

	atomic_store_explicit(&scenario->allow_exit, 1, memory_order_release);
	if (futex_wake(&scenario->allow_exit) < 0) {
		perror("wake child");
		goto kill_child;
	}

	if (kind == REPLACE_CLEAR_CHILD_TID) {
		int wait_result =
			futex_wait_bounded(&scenario->new_tid, expected_new_tid);

		if (wait_result < 0 && errno == EAGAIN) {
			/*
			 * The child exited between FUTEX_WAKE(allow_exit) and this
			 * FUTEX_WAIT. The clear was observed, but no waiter existed to
			 * prove the wake. Retry with a fresh child.
			 */
			raced_with_exit = 1;
		} else if (wait_result < 0) {
			perror("wait for clear_child_tid wake");
			goto kill_child;
		}
	}

	if (waitpid(child, &status, 0) != child) {
		perror("waitpid");
		goto out;
	}
	if (!WIFEXITED(status) || WEXITSTATUS(status) != EXIT_SUCCESS) {
		fprintf(stderr, "child failed in scenario %d\n", kind);
		goto out;
	}
	if (atomic_load_explicit(&scenario->old_tid, memory_order_relaxed) !=
	    OLD_TID_SENTINEL) {
		fprintf(stderr, "old clear_child_tid was cleared in scenario %d\n",
			kind);
		goto out;
	}
	if (kind == REPLACE_CLEAR_CHILD_TID &&
	    atomic_load_explicit(&scenario->new_tid, memory_order_relaxed) != 0) {
		fprintf(stderr, "replacement clear_child_tid was not cleared\n");
		goto out;
	}

	result = raced_with_exit ? SCENARIO_RETRY : EXIT_SUCCESS;
	goto out;

kill_child:
	kill(child, SIGKILL);
	waitpid(child, NULL, 0);
out:
	free(stack);
	free(scenario);
	return result;
}

static int pin_to_one_cpu(cpu_set_t *original_mask)
{
	cpu_set_t one_cpu;
	int cpu;

	if (sched_getaffinity(0, sizeof(*original_mask), original_mask) < 0)
		return -1;

	CPU_ZERO(&one_cpu);
	for (cpu = 0; cpu < CPU_SETSIZE; cpu++) {
		if (CPU_ISSET(cpu, original_mask)) {
			CPU_SET(cpu, &one_cpu);
			return sched_setaffinity(0, sizeof(one_cpu), &one_cpu);
		}
	}

	errno = EINVAL;
	return -1;
}

int main(void)
{
	cpu_set_t original_mask;
	int attempt;
	int replacement_result = SCENARIO_RETRY;
	int result = EXIT_SUCCESS;

	if (pin_to_one_cpu(&original_mask) < 0) {
		perror("pin test to one CPU");
		return EXIT_FAILURE;
	}

	for (attempt = 0;
	     attempt < MAX_WAKE_ATTEMPTS && replacement_result == SCENARIO_RETRY;
	     attempt++)
		replacement_result = run_scenario(REPLACE_CLEAR_CHILD_TID);
	if (replacement_result != EXIT_SUCCESS) {
		fprintf(stderr, "did not observe clear_child_tid futex wake\n");
		result = EXIT_FAILURE;
	}
	if (run_scenario(DISARM_CLEAR_CHILD_TID) != EXIT_SUCCESS)
		result = EXIT_FAILURE;
	if (run_scenario(INVALID_CLEAR_CHILD_TID) != EXIT_SUCCESS)
		result = EXIT_FAILURE;

	if (sched_setaffinity(0, sizeof(original_mask), &original_mask) < 0) {
		perror("restore CPU affinity");
		result = EXIT_FAILURE;
	}

	if (result == EXIT_SUCCESS)
		puts("set_tid_address replacement and exit semantics passed");
	return result;
}
