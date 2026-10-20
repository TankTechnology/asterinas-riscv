// SPDX-License-Identifier: MPL-2.0

// Combine forced CPU migration with kernel copies into untouched user pages.
// This exercises trap return and CPU-local state under concurrent allocation;
// it does not guarantee that a particular page-fault handler itself migrates.
#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

enum { WORKERS = 4, ROUNDS = 512, PAYLOAD_SIZE = 64 };
static int cpus[CPU_SETSIZE];
static int cpu_count;
static size_t page_size;
// Force real TLS loads across syscalls rather than constant-folding the check.
static _Thread_local volatile uintptr_t tls_cookie;

static void *worker(void *argument)
{
	uintptr_t id = (uintptr_t)argument;
	tls_cookie = id + 0x123400;
	int pipefd[2];
	if (pipe(pipefd))
		return (void *)1;
	int passed = 1;
	for (int round = 0; round < ROUNDS; ++round) {
		cpu_set_t mask;
		int cpu = cpus[(id + round) % cpu_count];
		CPU_ZERO(&mask);
		CPU_SET(cpu, &mask);
		if (sched_setaffinity(0, sizeof(mask), &mask) ||
		    sched_getcpu() != cpu) {
			passed = 0;
			break;
		}
		unsigned char expected[PAYLOAD_SIZE];
		for (int index = 0; index < PAYLOAD_SIZE; ++index)
			expected[index] = (unsigned char)(id ^ round ^ index);
		if (write(pipefd[1], expected, sizeof(expected)) !=
		    sizeof(expected)) {
			passed = 0;
			break;
		}
		void *mapping = mmap(NULL, page_size * 2,
				     PROT_READ | PROT_WRITE,
				     MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
		if (mapping == MAP_FAILED) {
			passed = 0;
			break;
		}
		// Neither destination page has been touched in user mode.
		unsigned char *destination =
			(unsigned char *)mapping + page_size - PAYLOAD_SIZE / 2;
		if (read(pipefd[0], destination, sizeof(expected)) !=
			    sizeof(expected) ||
		    memcmp(destination, expected, sizeof(expected)) ||
		    tls_cookie != id + 0x123400 || sched_getcpu() != cpu)
			passed = 0;
		if (munmap(mapping, page_size * 2))
			passed = 0;
		if (!passed)
			break;
		if (sched_yield()) {
			passed = 0;
			break;
		}
	}
	int close_read = close(pipefd[0]);
	int close_write = close(pipefd[1]);
	if (close_read || close_write)
		passed = 0;
	printf("MIGRATE_FAULT worker=%lu pass=%d\n", (unsigned long)id, passed);
	return (void *)(uintptr_t)!passed;
}

int main(void)
{
	setbuf(stdout, NULL);
	alarm(90);
	cpu_set_t allowed;
	if (sched_getaffinity(0, sizeof(allowed), &allowed)) {
		perror("sched_getaffinity");
		return 1;
	}
	for (int cpu = 0; cpu < CPU_SETSIZE; ++cpu)
		if (CPU_ISSET(cpu, &allowed))
			cpus[cpu_count++] = cpu;
	if (cpu_count < 2) {
		puts("MIGRATE_FAULT skip=requires-multiple-cpus");
		return 77;
	}
	long size = sysconf(_SC_PAGESIZE);
	if (size < PAYLOAD_SIZE)
		return 1;
	page_size = (size_t)size;
	pthread_t threads[WORKERS];
	int started = 0, passed = 1;
	for (uintptr_t id = 0; id < WORKERS; ++id) {
		int error =
			pthread_create(&threads[id], NULL, worker, (void *)id);
		if (error) {
			errno = error;
			perror("pthread_create");
			passed = 0;
			break;
		}
		++started;
	}
	for (int id = 0; id < started; ++id) {
		void *result;
		int error = pthread_join(threads[id], &result);
		if (error || result)
			passed = 0;
	}
	alarm(0);
	printf("MIGRATE_FAULT workers=%d rounds=%d cpus=%d pass=%d\n", started,
	       ROUNDS, cpu_count, passed);
	return passed ? 0 : 1;
}
