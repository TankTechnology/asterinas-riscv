// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <pthread.h>
#include <unistd.h>

#define MIGRATION_ROUNDS 32

int main()
{
	pid_t pid = getpid();
	cpu_set_t mask;

	if (sched_getaffinity(pid, sizeof(cpu_set_t), &mask) == -1) {
		perror("sched_getaffinity");
		exit(EXIT_FAILURE);
	}

	printf("Current CPU affinity:");
	int cur_cpu_count = 0;
	for (int i = 0; i < CPU_SETSIZE; i++) {
		if (CPU_ISSET(i, &mask)) {
			printf(" %d", i);
			cur_cpu_count++;
		}
	}
	printf("\n");
	if (cur_cpu_count == 0) {
		printf("Error: No CPU affinity set\n");
		exit(EXIT_FAILURE);
	}

	CPU_ZERO(&mask);
	CPU_SET(0, &mask);
	if (sched_setaffinity(pid, sizeof(cpu_set_t), &mask) == -1) {
		perror("sched_setaffinity");
		exit(EXIT_FAILURE);
	}
	if (sched_getcpu() != 0) {
		printf("Error: thread did not migrate to CPU 0\n");
		exit(EXIT_FAILURE);
	}

	long online_cpus = sysconf(_SC_NPROCESSORS_ONLN);
	for (int round = 0; round < MIGRATION_ROUNDS; round++) {
		for (int cpu = 0; cpu < online_cpus; cpu++) {
			CPU_ZERO(&mask);
			CPU_SET(cpu, &mask);
			if (sched_setaffinity(0, sizeof(cpu_set_t), &mask) == -1) {
				perror("sched_setaffinity migration");
				exit(EXIT_FAILURE);
			}
			if (sched_getcpu() != cpu) {
				printf("Error: expected CPU %d, running on CPU %d\n",
				       cpu, sched_getcpu());
				exit(EXIT_FAILURE);
			}
		}
	}
	printf("Observed affinity migration across %ld CPU(s) for %d rounds\n",
	       online_cpus, MIGRATION_ROUNDS);
	printf("RISC-V SMP4 affinity migration regression passed.\n");

	return 0;
}
