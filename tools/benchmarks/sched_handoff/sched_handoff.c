// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <pthread.h>
#include <sched.h>
#include <semaphore.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define MAX_ITERATIONS 100000
#define TIMEOUT_SECONDS 20
#define NS_PER_SECOND UINT64_C(1000000000)

enum handoff_mode { HANDOFF_YIELD, HANDOFF_BLOCKING };

struct probe {
	enum handoff_mode mode;
	int cpu_a;
	int cpu_b;
	int confirmed_cpu_a;
	int confirmed_cpu_b;
	unsigned int iterations;
	atomic_int turn;
	sem_t request;
	sem_t response;
	pthread_barrier_t ready;
};

static void fail(const char *operation, int error)
{
	fprintf(stderr, "sched_handoff: %s: %s\n", operation, strerror(error));
	exit(EXIT_FAILURE);
}

static void check_pthread(int error, const char *operation)
{
	if (error != 0)
		fail(operation, error);
}

static void timeout_handler(int signal_number)
{
	(void)signal_number;
	/* Even stderr may be blocked; the exit status is the timeout diagnostic. */
	_exit(124);
}

static int parse_number(const char *text, int minimum, int maximum)
{
	if (*text == '\0')
		fail("empty numeric argument", EINVAL);
	for (const char *digit = text; *digit; ++digit) {
		if (*digit < '0' || *digit > '9')
			fail("invalid numeric argument", EINVAL);
	}

	errno = 0;
	long value = strtol(text, NULL, 10);
	if (errno || value < minimum || value > maximum)
		fail("numeric argument out of range", ERANGE);
	return (int)value;
}

static int pin_thread(int cpu)
{
	cpu_set_t requested;
	CPU_ZERO(&requested);
	CPU_SET(cpu, &requested);
	check_pthread(pthread_setaffinity_np(pthread_self(), sizeof(requested),
					     &requested),
		      "set thread affinity");

	cpu_set_t confirmed;
	CPU_ZERO(&confirmed);
	check_pthread(pthread_getaffinity_np(pthread_self(), sizeof(confirmed),
					     &confirmed),
		      "read thread affinity");
	if (CPU_COUNT(&confirmed) != 1 || !CPU_ISSET(cpu, &confirmed))
		fail("thread affinity readback mismatch", EINVAL);

	int policy;
	struct sched_param parameter;
	check_pthread(pthread_getschedparam(pthread_self(), &policy,
					    &parameter),
		      "read thread scheduling policy");
	if (policy != SCHED_OTHER || parameter.sched_priority != 0)
		fail("thread must use SCHED_OTHER with priority zero", EINVAL);
	return cpu;
}

static void wait_ready(struct probe *probe)
{
	int result = pthread_barrier_wait(&probe->ready);
	if (result != PTHREAD_BARRIER_SERIAL_THREAD)
		check_pthread(result, "wait for startup barrier");
}

static void wait_semaphore(sem_t *semaphore)
{
	while (sem_wait(semaphore) != 0) {
		if (errno != EINTR)
			fail("wait for handoff semaphore", errno);
	}
}

static void post_semaphore(sem_t *semaphore)
{
	if (sem_post(semaphore) != 0)
		fail("post handoff semaphore", errno);
}

static void wait_turn(struct probe *probe, int turn)
{
	while (atomic_load_explicit(&probe->turn, memory_order_acquire) !=
	       turn) {
		if (sched_yield() != 0)
			fail("yield for handoff", errno);
	}
}

static void *respond(void *argument)
{
	struct probe *probe = argument;
	probe->confirmed_cpu_b = pin_thread(probe->cpu_b);
	wait_ready(probe);

	for (unsigned int iteration = 0; iteration < probe->iterations;
	     ++iteration) {
		if (probe->mode == HANDOFF_YIELD) {
			wait_turn(probe, 1);
			atomic_store_explicit(&probe->turn, 0,
					      memory_order_release);
		} else {
			wait_semaphore(&probe->request);
			post_semaphore(&probe->response);
		}
	}
	return NULL;
}

static uint64_t now_ns(void)
{
	struct timespec now;
	if (clock_gettime(CLOCK_MONOTONIC, &now) != 0)
		fail("read monotonic clock", errno);
	return (uint64_t)now.tv_sec * NS_PER_SECOND + (uint64_t)now.tv_nsec;
}

static uint64_t duration_ns(uint64_t start, uint64_t end)
{
	if (end < start)
		fail("monotonic clock moved backwards", ERANGE);
	return end - start;
}

static int compare_samples(const void *left, const void *right)
{
	uint64_t a = *(const uint64_t *)left;
	uint64_t b = *(const uint64_t *)right;
	return (a > b) - (a < b);
}

static uint64_t percentile(const uint64_t *samples, unsigned int count,
			   unsigned int percent)
{
	return samples[(count * percent + 99) / 100 - 1];
}

int main(int argc, char **argv)
{
	if (argc != 5) {
		fprintf(stderr,
			"usage: %s {yield|blocking} CPU_A CPU_B ITERATIONS\n",
			argv[0]);
		return EXIT_FAILURE;
	}

	struct probe probe = { 0 };
	if (strcmp(argv[1], "yield") == 0)
		probe.mode = HANDOFF_YIELD;
	else if (strcmp(argv[1], "blocking") == 0)
		probe.mode = HANDOFF_BLOCKING;
	else
		fail("mode must be yield or blocking", EINVAL);
	probe.cpu_a = parse_number(argv[2], 0, CPU_SETSIZE - 1);
	probe.cpu_b = parse_number(argv[3], 0, CPU_SETSIZE - 1);
	probe.iterations = parse_number(argv[4], 1, MAX_ITERATIONS);
	atomic_init(&probe.turn, 0);

	struct sigaction action = { 0 };
	action.sa_handler = timeout_handler;
	if (sigemptyset(&action.sa_mask) != 0 ||
	    sigaction(SIGALRM, &action, NULL) != 0)
		fail("install timeout handler", errno);
	alarm(TIMEOUT_SECONDS);

	uint64_t *samples = malloc(probe.iterations * sizeof(*samples));
	if (samples == NULL)
		fail("allocate round-trip samples", errno);
	/* Fault in the sample pages before timing; volatile preserves the writes. */
	volatile uint64_t *prepared_samples = samples;
	for (unsigned int index = 0; index < probe.iterations; ++index)
		prepared_samples[index] = 0;
	if (sem_init(&probe.request, 0, 0) != 0 ||
	    sem_init(&probe.response, 0, 0) != 0)
		fail("initialize handoff semaphores", errno);
	check_pthread(pthread_barrier_init(&probe.ready, NULL, 2),
		      "initialize startup barrier");

	pthread_t responder;
	check_pthread(pthread_create(&responder, NULL, respond, &probe),
		      "create responder");
	probe.confirmed_cpu_a = pin_thread(probe.cpu_a);
	wait_ready(&probe);

	unsigned int completed = 0;
	uint64_t start = now_ns();
	for (; completed < probe.iterations; ++completed) {
		uint64_t sample_start = now_ns();
		if (probe.mode == HANDOFF_YIELD) {
			atomic_store_explicit(&probe.turn, 1,
					      memory_order_release);
			wait_turn(&probe, 0);
		} else {
			post_semaphore(&probe.request);
			wait_semaphore(&probe.response);
		}
		samples[completed] = duration_ns(sample_start, now_ns());
	}
	uint64_t elapsed_ns = duration_ns(start, now_ns());
	if (elapsed_ns == 0)
		fail("elapsed time must be positive", ERANGE);

	check_pthread(pthread_join(responder, NULL), "join responder");
	check_pthread(pthread_barrier_destroy(&probe.ready),
		      "destroy startup barrier");
	if (sem_destroy(&probe.request) != 0 ||
	    sem_destroy(&probe.response) != 0)
		fail("destroy handoff semaphores", errno);
	qsort(samples, completed, sizeof(*samples), compare_samples);

	int printed = printf(
		"{\"mode\":\"%s\",\"cpu_a\":%d,\"cpu_b\":%d,"
		"\"confirmed_cpu_a\":%d,\"confirmed_cpu_b\":%d,"
		"\"policy\":\"SCHED_OTHER\",\"iterations\":%u,\"completed\":%u,"
		"\"elapsed_ns\":%" PRIu64 ",\"samples\":%u,"
		"\"min_ns\":%" PRIu64 ",\"p50_ns\":%" PRIu64 ","
		"\"p95_ns\":%" PRIu64 ",\"p99_ns\":%" PRIu64 ","
		"\"max_ns\":%" PRIu64 "}\n",
		argv[1], probe.cpu_a, probe.cpu_b, probe.confirmed_cpu_a,
		probe.confirmed_cpu_b, probe.iterations, completed, elapsed_ns,
		completed, samples[0], percentile(samples, completed, 50),
		percentile(samples, completed, 95),
		percentile(samples, completed, 99), samples[completed - 1]);
	free(samples);
	if (printed < 0 || fflush(stdout) != 0)
		fail("write result", errno);
	alarm(0);
	return EXIT_SUCCESS;
}
