// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>

static cpu_set_t expected_mask;
static int thread_failed;

static int check_mask(const char *kind)
{
	cpu_set_t observed_mask;

	CPU_ZERO(&observed_mask);
	if (sched_getaffinity(0, sizeof(observed_mask), &observed_mask) == -1) {
		perror("sched_getaffinity");
		return 1;
	}

	int passed = CPU_EQUAL(&expected_mask, &observed_mask);
	printf("CPU_AFFINITY_INHERIT kind=%s expected_count=%d "
	       "observed_count=%d pass=%d\n",
	       kind, CPU_COUNT(&expected_mask), CPU_COUNT(&observed_mask),
	       passed);
	return !passed;
}

static void *check_thread(void *unused)
{
	(void)unused;
	thread_failed = check_mask("pthread");
	return NULL;
}

int main(void)
{
	cpu_set_t original_mask;
	int result = 1;

	alarm(20);
	setbuf(stdout, NULL);
	CPU_ZERO(&original_mask);
	if (sched_getaffinity(0, sizeof(original_mask), &original_mask) == -1) {
		perror("sched_getaffinity original");
		return 1;
	}
	if (CPU_COUNT(&original_mask) < 2) {
		printf("CPU_AFFINITY_INHERIT skip=single-cpu\n");
		return 77;
	}

	int cpu;
	for (cpu = 0; cpu < CPU_SETSIZE; cpu++) {
		if (CPU_ISSET(cpu, &original_mask))
			break;
	}
	if (cpu == CPU_SETSIZE)
		return 1;

	CPU_ZERO(&expected_mask);
	CPU_SET(cpu, &expected_mask);
	if (sched_setaffinity(0, sizeof(expected_mask), &expected_mask) == -1) {
		perror("sched_setaffinity singleton");
		return 1;
	}
	if (check_mask("parent"))
		goto restore;

	pid_t child = fork();
	if (child == -1) {
		perror("fork");
		goto restore;
	}
	if (child == 0)
		_exit(check_mask("fork"));

	int status;
	pid_t waited;
	do {
		waited = waitpid(child, &status, 0);
	} while (waited == -1 && errno == EINTR);
	if (waited != child || !WIFEXITED(status)) {
		fprintf(stderr, "waitpid did not reap an exited child\n");
		goto restore;
	}
	int fork_failed = WEXITSTATUS(status);

	pthread_t thread;
	int pthread_error = pthread_create(&thread, NULL, check_thread, NULL);
	if (pthread_error != 0) {
		errno = pthread_error;
		perror("pthread_create");
		goto restore;
	}
	pthread_error = pthread_join(thread, NULL);
	if (pthread_error != 0) {
		errno = pthread_error;
		perror("pthread_join");
		goto restore;
	}

	result = fork_failed || thread_failed;

restore:
	if (sched_setaffinity(0, sizeof(original_mask), &original_mask) == -1) {
		perror("sched_setaffinity restore");
		return 1;
	}
	return result;
}
