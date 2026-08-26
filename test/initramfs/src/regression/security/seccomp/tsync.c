// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE
#include <errno.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <pthread.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

#define BLOCK_ERRNO 99

struct worker {
	pthread_barrier_t ready;
	pthread_barrier_t done;
	long nr;
	long result;
	pid_t tid;
};

static int install_errno_filter(long nr, unsigned int flags)
{
	struct sock_filter insns[] = {
		BPF_STMT(BPF_LD | BPF_W | BPF_ABS,
			 offsetof(struct seccomp_data, nr)),
		BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, nr, 0, 1),
		BPF_STMT(BPF_RET | BPF_K,
			 SECCOMP_RET_ERRNO | (BLOCK_ERRNO & SECCOMP_RET_DATA)),
		BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
	};
	struct sock_fprog prog = {
		.len = sizeof(insns) / sizeof(insns[0]),
		.filter = insns,
	};

	return syscall(SYS_seccomp, SECCOMP_SET_MODE_FILTER, flags, &prog);
}

static void *probe_worker(void *arg)
{
	struct worker *worker = arg;

	worker->tid = syscall(SYS_gettid);
	pthread_barrier_wait(&worker->ready);
	pthread_barrier_wait(&worker->done);
	errno = 0;
	worker->result = syscall(worker->nr);
	if (worker->result == -1)
		worker->result = -errno;
	return NULL;
}

static void *divergent_worker(void *arg)
{
	struct worker *worker = arg;

	worker->tid = syscall(SYS_gettid);
	if (install_errno_filter(SYS_getppid, 0) != 0)
		worker->result = -errno;
	pthread_barrier_wait(&worker->ready);
	pthread_barrier_wait(&worker->done);
	if (worker->result == 0) {
		errno = 0;
		worker->result = syscall(SYS_getppid);
		if (worker->result == -1)
			worker->result = -errno;
	}
	return NULL;
}

static int run_sync_case(unsigned int flags, int sibling_blocked)
{
	struct worker worker = { .nr = SYS_getpid };
	pthread_t thread;
	long caller_result;
	int rc = 1;

	pthread_barrier_init(&worker.ready, NULL, 2);
	pthread_barrier_init(&worker.done, NULL, 2);
	if (pthread_create(&thread, NULL, probe_worker, &worker) != 0)
		goto out;
	pthread_barrier_wait(&worker.ready);
	if (install_errno_filter(SYS_getpid, flags) != 0)
		goto release;
	errno = 0;
	caller_result = syscall(SYS_getpid);
	if (caller_result != -1 || errno != BLOCK_ERRNO)
		goto release;
	pthread_barrier_wait(&worker.done);
	pthread_join(thread, NULL);
	thread = 0;
	if (sibling_blocked ? worker.result == -BLOCK_ERRNO : worker.result > 0)
		rc = 0;
out:
	pthread_barrier_destroy(&worker.ready);
	pthread_barrier_destroy(&worker.done);
	return rc;
release:
	pthread_barrier_wait(&worker.done);
	pthread_join(thread, NULL);
	goto out;
}

static int test_divergent_policy_is_atomic(void)
{
	struct worker worker = { 0 };
	pthread_t thread;
	long rc;
	int failed = 1;

	pthread_barrier_init(&worker.ready, NULL, 2);
	pthread_barrier_init(&worker.done, NULL, 2);
	if (pthread_create(&thread, NULL, divergent_worker, &worker) != 0)
		goto out;
	pthread_barrier_wait(&worker.ready);
	rc = install_errno_filter(SYS_getpid, SECCOMP_FILTER_FLAG_TSYNC);
	/* Linux returns the TID whose existing filter tree cannot synchronize. */
	if (rc != worker.tid)
		goto release;
	/* Failure must not install the candidate filter on the caller. */
	if (syscall(SYS_getpid) <= 0)
		goto release;
	pthread_barrier_wait(&worker.done);
	pthread_join(thread, NULL);
	thread = 0;
	if (worker.result == -BLOCK_ERRNO)
		failed = 0;
out:
	pthread_barrier_destroy(&worker.ready);
	pthread_barrier_destroy(&worker.done);
	return failed;
release:
	pthread_barrier_wait(&worker.done);
	pthread_join(thread, NULL);
	goto out;
}

static int run_isolated(int (*test)(void), const char *name)
{
	pid_t child = fork();
	int status;

	if (child == 0) {
		if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0)
			_exit(2);
		_exit(test());
	}
	if (child < 0 || waitpid(child, &status, 0) != child ||
	    !WIFEXITED(status) || WEXITSTATUS(status) != 0) {
		fprintf(stderr, "seccomp TSYNC: %s failed\n", name);
		return 1;
	}
	printf("seccomp TSYNC: %s passed\n", name);
	return 0;
}

static int test_tsync(void)
{
	return run_sync_case(SECCOMP_FILTER_FLAG_TSYNC, 1);
}

static int test_thread_local(void)
{
	return run_sync_case(0, 0);
}

static int test_unknown_flags(void)
{
	if (install_errno_filter(SYS_getpid, 1U << 31) != -1)
		return 1;
	return errno == EINVAL ? 0 : 1;
}

static int test_clone_inherits_filter(void)
{
	struct worker worker = { .nr = SYS_getppid };
	pthread_t thread;

	if (install_errno_filter(SYS_getppid, 0) != 0)
		return 1;
	pthread_barrier_init(&worker.ready, NULL, 2);
	pthread_barrier_init(&worker.done, NULL, 2);
	if (pthread_create(&thread, NULL, probe_worker, &worker) != 0)
		return 1;
	pthread_barrier_wait(&worker.ready);
	pthread_barrier_wait(&worker.done);
	pthread_join(thread, NULL);
	pthread_barrier_destroy(&worker.ready);
	pthread_barrier_destroy(&worker.done);
	return worker.result == -BLOCK_ERRNO ? 0 : 1;
}

static int test_strict_cannot_replace_filter(void)
{
	if (install_errno_filter(SYS_getpid, 0) != 0)
		return 1;
	errno = 0;
	if (syscall(SYS_seccomp, SECCOMP_SET_MODE_STRICT, 0, NULL) != -1 ||
	    errno != EINVAL)
		return 1;
	errno = 0;
	return syscall(SYS_getpid) == -1 && errno == BLOCK_ERRNO ? 0 : 1;
}

int main(void)
{
	int failures = 0;

	failures += run_isolated(test_tsync, "sibling synchronization");
	failures += run_isolated(test_thread_local, "thread-local install");
	failures += run_isolated(test_unknown_flags, "unknown flags");
	failures += run_isolated(test_clone_inherits_filter,
				 "clone inherits filter");
	failures += run_isolated(test_strict_cannot_replace_filter,
				 "strict cannot replace filter");
	failures += run_isolated(test_divergent_policy_is_atomic,
				 "divergent policy atomic failure");
	return failures ? EXIT_FAILURE : EXIT_SUCCESS;
}
