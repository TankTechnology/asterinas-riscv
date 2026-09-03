// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <unistd.h>

#define NUM_HARTS 4
#define NUM_HELPERS 12
#define PRIME_ITERATIONS 1024
#define CLAIM_ATTEMPTS 10000
#define RISCV_FLUSH_ICACHE_SYSCALL 259

typedef int (*jit_fn_t)(void);

struct worker_state {
	int cpu;
	int error;
	int owns_hart;
};

static uint32_t *jit_code;
static atomic_int phase;
static atomic_int coordinator_cpu;
static atomic_uint claimed_mask;
static atomic_uint ready_mask;
static atomic_uint done_mask;
static atomic_uint failed_mask;

static int pin_to_cpu(int cpu)
{
	cpu_set_t mask;

	CPU_ZERO(&mask);
	CPU_SET(cpu, &mask);
	if (sched_setaffinity(0, sizeof(mask), &mask) < 0)
		return -1;

	for (int i = 0; i < 100; i++) {
		if (sched_getcpu() == cpu)
			return 0;
		sched_yield();
	}

	errno = ETIMEDOUT;
	return -1;
}

static void emit_return_value(int value)
{
	/* addi a0, zero, value; ret */
	jit_code[0] = ((uint32_t)value << 20) | (10U << 7) | 0x13U;
	jit_code[1] = 0x00008067U;
	atomic_thread_fence(memory_order_seq_cst);
}

static int flush_all_harts(void)
{
	return syscall(RISCV_FLUSH_ICACHE_SYSCALL, jit_code,
		       (char *)jit_code + 2 * sizeof(*jit_code), 0);
}

static int call_jit(void)
{
	return ((jit_fn_t)jit_code)();
}

static void *worker_main(void *arg)
{
	struct worker_state *state = arg;

	while (atomic_load_explicit(&phase, memory_order_acquire) == 0)
		sched_yield();

	unsigned target_mask = (1U << NUM_HARTS) - 1;
	int coordinator =
		atomic_load_explicit(&coordinator_cpu, memory_order_relaxed);
	target_mask &= ~(1U << coordinator);

	for (int attempt = 0; attempt < CLAIM_ATTEMPTS; attempt++) {
		if ((atomic_load_explicit(&claimed_mask, memory_order_acquire) &
		     target_mask) == target_mask)
			return NULL;

		int cpu = sched_getcpu();
		if (cpu < 0 || cpu >= NUM_HARTS || cpu == coordinator) {
			usleep(1000);
			continue;
		}

		unsigned bit = 1U << cpu;
		unsigned previous = atomic_fetch_or_explicit(
			&claimed_mask, bit, memory_order_acq_rel);
		if (previous & bit) {
			usleep(1000);
			continue;
		}

		state->cpu = cpu;
		state->owns_hart = 1;
		if (pin_to_cpu(cpu) < 0) {
			state->error = errno;
		} else {
			for (int i = 0; i < PRIME_ITERATIONS; i++) {
				if (call_jit() != 1) {
					state->error = EILSEQ;
					break;
				}
			}
		}

		if (state->error != 0)
			atomic_fetch_or_explicit(&failed_mask, bit,
						 memory_order_release);
		atomic_fetch_or_explicit(&ready_mask, bit,
					 memory_order_release);

		while (atomic_load_explicit(&phase, memory_order_acquire) == 1)
			sched_yield();

		if (state->error == 0 && call_jit() != 2)
			state->error = ESTALE;
		if (state->error != 0)
			atomic_fetch_or_explicit(&failed_mask, bit,
						 memory_order_release);
		atomic_fetch_or_explicit(&done_mask, bit, memory_order_release);
		return NULL;
	}

	return NULL;
}

static int require_smp4(int argc, char **argv)
{
	if (argc == 1)
		return 0;
	if (argc == 2 && strcmp(argv[1], "--require-smp4") == 0)
		return 1;

	fprintf(stderr, "usage: %s [--require-smp4]\n", argv[0]);
	exit(EXIT_FAILURE);
}

int main(int argc, char **argv)
{
	const int required = require_smp4(argc, argv);
	cpu_set_t original_mask;
	pthread_t helpers[NUM_HELPERS];
	struct worker_state states[NUM_HELPERS] = { 0 };
	long online_cpus = sysconf(_SC_NPROCESSORS_ONLN);

	if (sched_getaffinity(0, sizeof(original_mask), &original_mask) < 0) {
		perror("sched_getaffinity");
		return EXIT_FAILURE;
	}

	int have_four_harts = online_cpus >= NUM_HARTS;
	for (int cpu = 0; cpu < NUM_HARTS; cpu++)
		have_four_harts &= CPU_ISSET(cpu, &original_mask);

	if (!have_four_harts) {
		if (required) {
			fprintf(stderr,
				"RISC-V SMP4 icache regression requires CPUs 0-3 "
				"(online=%ld)\n",
				online_cpus);
			return EXIT_FAILURE;
		}
		printf("RISC-V cross-hart icache regression skipped: "
		       "CPUs 0-3 are not online\n");
		return EXIT_SUCCESS;
	}

	jit_code = mmap(NULL, 4096, PROT_READ | PROT_WRITE | PROT_EXEC,
			MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (jit_code == MAP_FAILED) {
		perror("mmap executable JIT page");
		return EXIT_FAILURE;
	}

	emit_return_value(1);
	if (flush_all_harts() < 0) {
		perror("initial riscv_flush_icache");
		return EXIT_FAILURE;
	}

	/*
	 * Create helpers before narrowing the coordinator's affinity. This lets
	 * the scheduler place them naturally on all four harts, so the test does
	 * not depend on forced migration by sched_setaffinity().
	 */
	for (int i = 0; i < NUM_HELPERS; i++) {
		states[i].cpu = -1;
		int ret = pthread_create(&helpers[i], NULL, worker_main,
					 &states[i]);
		if (ret != 0) {
			fprintf(stderr, "pthread_create failed: %s\n",
				strerror(ret));
			return EXIT_FAILURE;
		}
	}

	int coordinator = sched_getcpu();
	if (coordinator < 0 || coordinator >= NUM_HARTS) {
		coordinator = 0;
		if (pin_to_cpu(coordinator) < 0) {
			perror("pin coordinator to CPU0");
			return EXIT_FAILURE;
		}
	} else if (pin_to_cpu(coordinator) < 0) {
		perror("pin coordinator to its current CPU");
		return EXIT_FAILURE;
	}

	unsigned all_mask = (1U << NUM_HARTS) - 1;
	unsigned target_mask = all_mask & ~(1U << coordinator);
	atomic_store_explicit(&coordinator_cpu, coordinator,
			      memory_order_relaxed);
	atomic_store_explicit(&claimed_mask, 1U << coordinator,
			      memory_order_relaxed);
	atomic_store_explicit(&phase, 1, memory_order_release);

	int failed = 0;
	for (int attempt = 0; attempt < CLAIM_ATTEMPTS; attempt++) {
		if ((atomic_load_explicit(&ready_mask, memory_order_acquire) &
		     target_mask) == target_mask)
			break;
		usleep(1000);
		if (attempt == CLAIM_ATTEMPTS - 1) {
			fprintf(stderr,
				"helpers did not occupy every remote hart: "
				"coordinator=%d claimed=%#x ready=%#x\n",
				coordinator, atomic_load(&claimed_mask),
				atomic_load(&ready_mask));
			failed = 1;
		}
	}

	emit_return_value(2);
	if (flush_all_harts() < 0) {
		perror("cross-hart riscv_flush_icache");
		failed = 1;
	}
	atomic_store_explicit(&phase, 2, memory_order_release);

	for (int i = 0; i < NUM_HELPERS; i++) {
		int ret = pthread_join(helpers[i], NULL);
		if (ret != 0) {
			fprintf(stderr, "pthread_join failed: %s\n",
				strerror(ret));
			failed = 1;
		}
		if (states[i].owns_hart && states[i].error != 0) {
			fprintf(stderr, "CPU%d worker failed: %s\n",
				states[i].cpu, strerror(states[i].error));
			failed = 1;
		}
	}

	if ((atomic_load_explicit(&done_mask, memory_order_acquire) &
	     target_mask) != target_mask ||
	    atomic_load_explicit(&failed_mask, memory_order_acquire) != 0)
		failed = 1;

	if (sched_setaffinity(0, sizeof(original_mask), &original_mask) < 0) {
		perror("restore CPU affinity");
		failed = 1;
	}
	munmap(jit_code, 4096);

	if (failed)
		return EXIT_FAILURE;

	printf("RISC-V cross-hart icache: CPU%d updated JIT code; "
	       "remote mask %#x observed the new instruction\n",
	       coordinator, target_mask);
	return EXIT_SUCCESS;
}
