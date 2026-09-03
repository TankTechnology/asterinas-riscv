// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

// Linux uapi: arch/riscv/include/uapi/asm/unistd.h.
#ifndef __NR_riscv_flush_icache
#define __NR_riscv_flush_icache 259
#endif

#define SYS_RISCV_FLUSH_ICACHE_LOCAL 1UL
#define CROSS_HART_ITERATIONS 1024U
#define WAIT_TIMEOUT_SECONDS 5
#define RISCV_ADDI_OPCODE 0x13U
#define RISCV_A0_REGISTER 10U
#define RISCV_RET_INSTRUCTION 0x00008067U

typedef int (*jit_function_t)(void);

struct worker_context {
	void *code;
	int cpu;
	int initial_result;
	pthread_barrier_t ready;
	atomic_uint generation;
	atomic_uint completed_generation;
	atomic_int expected_result;
	atomic_int error;
	atomic_bool stop;
};

static int pin_current_thread(int cpu)
{
	cpu_set_t mask;

	CPU_ZERO(&mask);
	CPU_SET(cpu, &mask);
	return sched_setaffinity(0, sizeof(mask), &mask);
}

static int wait_for_cpu(int cpu)
{
	struct timespec start;
	struct timespec now;

	if (clock_gettime(CLOCK_MONOTONIC, &start) < 0)
		return -1;

	for (;;) {
		int current_cpu = sched_getcpu();

		if (current_cpu < 0)
			return -1;
		if (current_cpu == cpu)
			return 0;
		if (sched_yield() < 0)
			return -1;
		if (clock_gettime(CLOCK_MONOTONIC, &now) < 0)
			return -1;
		if (now.tv_sec - start.tv_sec >= WAIT_TIMEOUT_SECONDS) {
			errno = ETIMEDOUT;
			return -1;
		}
	}
}

static int wait_for_generation(const atomic_uint *generation,
			       unsigned int expected,
			       const atomic_bool *stop)
{
	struct timespec start;
	struct timespec now;

	if (clock_gettime(CLOCK_MONOTONIC, &start) < 0)
		return -1;

	while (atomic_load_explicit(generation, memory_order_acquire) < expected) {
		if (atomic_load_explicit(stop, memory_order_relaxed)) {
			errno = ECANCELED;
			return -1;
		}
		if (sched_yield() < 0)
			return -1;
		if (clock_gettime(CLOCK_MONOTONIC, &now) < 0)
			return -1;
		if (now.tv_sec - start.tv_sec >= WAIT_TIMEOUT_SECONDS) {
			errno = ETIMEDOUT;
			return -1;
		}
	}

	return 0;
}

static void write_return_value(void *code, unsigned int value)
{
	uint32_t instructions[] = {
		(value << 20) | (RISCV_A0_REGISTER << 7) | RISCV_ADDI_OPCODE,
		RISCV_RET_INSTRUCTION,
	};

	memcpy(code, instructions, sizeof(instructions));
}

static int execute_code(void *code)
{
	return ((jit_function_t)code)();
}

static void *execute_on_remote_hart(void *argument)
{
	struct worker_context *context = argument;
	unsigned int generation;

	if (pin_current_thread(context->cpu) < 0)
		atomic_store(&context->error, errno);
	else if (wait_for_cpu(context->cpu) < 0)
		atomic_store(&context->error, errno);
	else if (execute_code(context->code) != context->initial_result)
		atomic_store(&context->error, EILSEQ);

	pthread_barrier_wait(&context->ready);
	if (atomic_load(&context->error) != 0)
		return NULL;

	for (generation = 1; generation <= CROSS_HART_ITERATIONS;
	     generation++) {
		int expected;
		int observed;

		if (wait_for_generation(&context->generation, generation,
					&context->stop) < 0)
			return NULL;
		expected = atomic_load_explicit(&context->expected_result,
						memory_order_relaxed);
		observed = execute_code(context->code);
		if (observed != expected) {
			fprintf(stderr,
				"remote hart returned %d, expected %d at generation %u\n",
				observed, expected, generation);
			atomic_store(&context->error, EILSEQ);
		}
		atomic_store_explicit(&context->completed_generation, generation,
				      memory_order_release);
		if (atomic_load(&context->error) != 0)
			return NULL;
	}

	return NULL;
}

static int select_available_cpus(cpu_set_t *original_mask, int *cpus)
{
	int cpu;
	int count = 0;

	if (sched_getaffinity(0, sizeof(*original_mask), original_mask) < 0)
		return -1;

	for (cpu = 0; cpu < CPU_SETSIZE; cpu++) {
		if (CPU_ISSET(cpu, original_mask))
			cpus[count++] = cpu;
	}

	return count;
}

static int test_local_flush(void *code)
{
	char *code_end = (char *)code + 2 * sizeof(uint32_t);

	write_return_value(code, 123);
	if (syscall(__NR_riscv_flush_icache, code, code_end,
		    SYS_RISCV_FLUSH_ICACHE_LOCAL) < 0) {
		perror("local riscv_flush_icache");
		return -1;
	}
	if (execute_code(code) != 123) {
		fprintf(stderr, "local hart executed stale code\n");
		return -1;
	}

	errno = 0;
	if (syscall(__NR_riscv_flush_icache, code, code_end, 2UL) != -1 ||
	    errno != EINVAL) {
		fprintf(stderr,
			"riscv_flush_icache accepted invalid flags: errno=%d\n",
			errno);
		return -1;
	}

	return 0;
}

static int test_cross_hart_flush(void *code, int remote_cpu)
{
	const int initial_result = 73 + remote_cpu;
	struct worker_context context = {
		.code = code,
		.cpu = remote_cpu,
		.initial_result = initial_result,
	};
	pthread_t worker;
	unsigned int generation;
	char *code_end = (char *)code + 2 * sizeof(uint32_t);
	int pthread_error;
	int result = -1;

	atomic_init(&context.generation, 0);
	atomic_init(&context.completed_generation, 0);
	atomic_init(&context.expected_result, initial_result);
	atomic_init(&context.error, 0);
	atomic_init(&context.stop, false);

	write_return_value(code, initial_result);
	if (syscall(__NR_riscv_flush_icache, code, code_end,
		    SYS_RISCV_FLUSH_ICACHE_LOCAL) < 0) {
		perror("prepare remote-hart instruction stream");
		return -1;
	}

	pthread_error = pthread_barrier_init(&context.ready, NULL, 2);
	if (pthread_error != 0) {
		errno = pthread_error;
		perror("initialize worker barrier");
		return -1;
	}
	pthread_error =
		pthread_create(&worker, NULL, execute_on_remote_hart, &context);
	if (pthread_error != 0) {
		errno = pthread_error;
		perror("create remote-hart worker");
		goto destroy_barrier;
	}
	pthread_barrier_wait(&context.ready);
	if (atomic_load(&context.error) != 0) {
		errno = atomic_load(&context.error);
		perror("failed to pin remote-hart worker");
		goto stop_worker;
	}

	for (generation = 1; generation <= CROSS_HART_ITERATIONS;
	     generation++) {
		unsigned int value = generation % 2 == 0 ? 37 : 911;

		write_return_value(code, value);
		atomic_store_explicit(&context.expected_result, value,
				      memory_order_relaxed);
		if (syscall(__NR_riscv_flush_icache, code, code_end, 0UL) < 0) {
			perror("cross-hart riscv_flush_icache");
			goto stop_worker;
		}
		atomic_store_explicit(&context.generation, generation,
				      memory_order_release);
		if (wait_for_generation(&context.completed_generation, generation,
					&context.stop) < 0) {
			perror("waiting for remote-hart execution");
			goto stop_worker;
		}
		if (atomic_load(&context.error) != 0)
			goto stop_worker;
	}

	result = 0;

stop_worker:
	atomic_store(&context.stop, true);
	atomic_store(&context.generation, CROSS_HART_ITERATIONS + 1);
	pthread_error = pthread_join(worker, NULL);
	if (pthread_error != 0) {
		errno = pthread_error;
		perror("join remote-hart worker");
		result = -1;
	}
destroy_barrier:
	pthread_barrier_destroy(&context.ready);
	return result;
}

int main(int argc, char **argv)
{
	cpu_set_t original_mask;
	int cpus[CPU_SETSIZE];
	long page_size = sysconf(_SC_PAGESIZE);
	void *code;
	bool require_smp4 = false;
	int cpu_count;
	int remote_index;
	int result = EXIT_FAILURE;

	if (argc == 2 && strcmp(argv[1], "--require-smp4") == 0)
		require_smp4 = true;
	else if (argc != 1) {
		fprintf(stderr, "usage: %s [--require-smp4]\n", argv[0]);
		return EXIT_FAILURE;
	}

	if (page_size <= 0) {
		perror("sysconf(_SC_PAGESIZE)");
		return EXIT_FAILURE;
	}
	cpu_count = select_available_cpus(&original_mask, cpus);
	if (cpu_count < 0) {
		perror("sched_getaffinity");
		return EXIT_FAILURE;
	}
	if (require_smp4 && cpu_count != 4) {
		fprintf(stderr,
			"riscv_flush_icache SMP4 requirement failed: available_cpus=%d\n",
			cpu_count);
		return EXIT_FAILURE;
	}
	if (cpu_count < 2) {
		puts("riscv_flush_icache cross-hart skipped: fewer than two CPUs");
		return EXIT_SUCCESS;
	}
	if (pin_current_thread(cpus[0]) < 0) {
		perror("pin local-hart thread");
		return EXIT_FAILURE;
	}
	if (wait_for_cpu(cpus[0]) < 0) {
		perror("verify local-hart placement");
		return EXIT_FAILURE;
	}

	code = mmap(NULL, page_size, PROT_READ | PROT_WRITE | PROT_EXEC,
		    MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (code == MAP_FAILED) {
		perror("mmap executable page");
		goto restore_affinity;
	}
	if (test_local_flush(code) < 0)
		goto unmap_code;
	for (remote_index = 1; remote_index < cpu_count; remote_index++) {
		if (test_cross_hart_flush(code, cpus[remote_index]) < 0)
			goto unmap_code;
	}

	printf("riscv_flush_icache cross-hart passed: cpus=%d local=%d remotes=",
	       cpu_count, cpus[0]);
	for (remote_index = 1; remote_index < cpu_count; remote_index++)
		printf("%s%d", remote_index == 1 ? "" : ",", cpus[remote_index]);
	printf(" generations=%u\n", CROSS_HART_ITERATIONS);
	result = EXIT_SUCCESS;

unmap_code:
	if (munmap(code, page_size) < 0) {
		perror("munmap executable page");
		result = EXIT_FAILURE;
	}
restore_affinity:
	if (sched_setaffinity(0, sizeof(original_mask), &original_mask) < 0) {
		perror("restore CPU affinity");
		result = EXIT_FAILURE;
	}
	return result;
}
