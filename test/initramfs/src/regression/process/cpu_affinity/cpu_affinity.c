// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

int main()
{
	pid_t pid = getpid();
	cpu_set_t mask, original_mask;

	// Get current affinity mask
	if (sched_getaffinity(pid, sizeof(original_mask), &original_mask) ==
	    -1) {
		perror("sched_getaffinity");
		exit(EXIT_FAILURE);
	}

	printf("Current CPU affinity:");
	int cur_cpu_count = 0;
	for (int i = 0; i < CPU_SETSIZE; i++) {
		if (CPU_ISSET(i, &original_mask)) {
			printf(" %d", i);
			cur_cpu_count++;
		}
	}
	printf("\n");
	if (cur_cpu_count == 0) {
		printf("Error: No CPU affinity set\n");
		exit(EXIT_FAILURE);
	}

	for (int cpu = 0; cpu < CPU_SETSIZE; cpu++) {
		if (!CPU_ISSET(cpu, &original_mask))
			continue;

		CPU_ZERO(&mask);
		CPU_SET(cpu, &mask);
		if (sched_setaffinity(pid, sizeof(mask), &mask) == -1) {
			perror("sched_setaffinity");
			exit(EXIT_FAILURE);
		}
		if (sched_getcpu() != cpu) {
			fprintf(stderr,
				"Error: task did not migrate to CPU %d\n", cpu);
			exit(EXIT_FAILURE);
		}
		printf("Migrated to CPU %d\n", cpu);
	}

	if (sched_setaffinity(pid, sizeof(original_mask), &original_mask) ==
	    -1) {
		perror("sched_setaffinity restore");
		exit(EXIT_FAILURE);
	}

	return 0;
}
