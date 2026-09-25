// SPDX-License-Identifier: MPL-2.0

// Run an identical bounded ALU loop on the same hart under two kernels.
// Static linking removes the userspace-library version as a variable.
#define _GNU_SOURCE
#include <inttypes.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

#define SAMPLES 3
#define ITERATIONS 20000000ULL

static uint64_t clock_ns(clockid_t id) {
    struct timespec now;
    if (clock_gettime(id, &now) != 0) {
        perror("clock_gettime");
        exit(EXIT_FAILURE);
    }
    return (uint64_t)now.tv_sec * 1000000000ULL + (uint64_t)now.tv_nsec;
}

int main(void) {
    cpu_set_t affinity;
    CPU_ZERO(&affinity);
    CPU_SET(0, &affinity);
    if (sched_setaffinity(0, sizeof(affinity), &affinity) != 0) {
        perror("sched_setaffinity");
        return EXIT_FAILURE;
    }

    for (int sample = 0; sample < SAMPLES; sample++) {
        uint64_t state = 0x9e3779b97f4a7c15ULL;
        int cpu_before = sched_getcpu();
        if (cpu_before != 0) {
            fprintf(stderr, "unexpected CPU before sample: %d\n", cpu_before);
            return EXIT_FAILURE;
        }
        uint64_t wall_before = clock_ns(CLOCK_MONOTONIC);
        uint64_t cpu_time_before = clock_ns(CLOCK_THREAD_CPUTIME_ID);
        for (uint64_t step = 0; step < ITERATIONS; step++) {
            state = state * 6364136223846793005ULL + 1442695040888963407ULL;
            state ^= state >> 27;
        }
        uint64_t cpu_time_after = clock_ns(CLOCK_THREAD_CPUTIME_ID);
        uint64_t wall_after = clock_ns(CLOCK_MONOTONIC);
        int cpu_after = sched_getcpu();
        if (cpu_after != 0 || cpu_time_after <= cpu_time_before ||
            wall_after <= wall_before) {
            fprintf(stderr, "CPU identity or clock changed during sample\n");
            return EXIT_FAILURE;
        }

        uint64_t wall_ns = wall_after - wall_before;
        uint64_t cpu_ns = cpu_time_after - cpu_time_before;
        printf("sample=%d cpu=0 iterations=%" PRIu64 " wall_ns=%" PRIu64
               " thread_cpu_ns=%" PRIu64 " checksum=%016" PRIx64 "\n",
               sample + 1, (uint64_t)ITERATIONS, wall_ns, cpu_ns, state);
    }
    return EXIT_SUCCESS;
}
