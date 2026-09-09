// SPDX-License-Identifier: MPL-2.0

// Exercise the same one-producer/one-consumer handoff shape used when a
// socket-service thread dispatches newly read bytes to an event-loop thread.

#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

#define ROUNDS 4096U

struct handoff {
	pthread_mutex_t mutex;
	pthread_cond_t ready;
	pthread_cond_t consumed;
	uint64_t produced_sequence;
	uint64_t consumed_sequence;
	size_t bytes;
	int failure;
};

static const size_t request_sizes[] = { 92, 40, 121, 521 };

static void fail_on_timeout(int signal)
{
	static const char message[] =
		"pthread_cond_handoff: timed out (possible lost wakeup)\n";
	ssize_t write_result;

	(void)signal;
	write_result = write(STDERR_FILENO, message, sizeof(message) - 1);
	(void)write_result;
	_exit(EXIT_FAILURE);
}

static void *consumer(void *argument)
{
	struct handoff *handoff = argument;

	for (uint64_t sequence = 1; sequence <= ROUNDS; ++sequence) {
		pthread_mutex_lock(&handoff->mutex);
		while (handoff->produced_sequence != sequence)
			pthread_cond_wait(&handoff->ready, &handoff->mutex);

		if (handoff->bytes !=
		    request_sizes[(sequence - 1) % (sizeof(request_sizes) /
						    sizeof(request_sizes[0]))])
			handoff->failure = 1;
		handoff->consumed_sequence = sequence;
		pthread_cond_signal(&handoff->consumed);
		pthread_mutex_unlock(&handoff->mutex);

		if ((sequence & 15) == 0)
			sched_yield();
	}
	return NULL;
}

int main(void)
{
	struct sigaction action = { .sa_handler = fail_on_timeout };
	struct handoff handoff = {
		.mutex = PTHREAD_MUTEX_INITIALIZER,
		.ready = PTHREAD_COND_INITIALIZER,
		.consumed = PTHREAD_COND_INITIALIZER,
	};
	pthread_t thread;

	sigemptyset(&action.sa_mask);
	if (sigaction(SIGALRM, &action, NULL) != 0) {
		perror("sigaction");
		return EXIT_FAILURE;
	}
	alarm(60);
	if (pthread_create(&thread, NULL, consumer, &handoff) != 0) {
		perror("pthread_create");
		return EXIT_FAILURE;
	}

	for (uint64_t sequence = 1; sequence <= ROUNDS; ++sequence) {
		pthread_mutex_lock(&handoff.mutex);
		while (handoff.consumed_sequence != sequence - 1)
			pthread_cond_wait(&handoff.consumed, &handoff.mutex);
		handoff.bytes = request_sizes[(sequence - 1) %
					      (sizeof(request_sizes) /
					       sizeof(request_sizes[0]))];
		handoff.produced_sequence = sequence;
		pthread_cond_signal(&handoff.ready);
		pthread_mutex_unlock(&handoff.mutex);

		if ((sequence & 31) == 0)
			sched_yield();
	}

	pthread_mutex_lock(&handoff.mutex);
	while (handoff.consumed_sequence != ROUNDS)
		pthread_cond_wait(&handoff.consumed, &handoff.mutex);
	pthread_mutex_unlock(&handoff.mutex);

	if (pthread_join(thread, NULL) != 0 || handoff.failure) {
		fprintf(stderr, "pthread_cond_handoff: validation failed\n");
		return EXIT_FAILURE;
	}
	alarm(0);
	printf("pthread_cond_handoff: passed %u handoffs\n", ROUNDS);
	return EXIT_SUCCESS;
}
