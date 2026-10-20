/* SPDX-License-Identifier: MPL-2.0 */
/*
 * Bounded futex/ppoll probe.  The same source is built for the host and
 * riscv64 so that a result can be compared without changing the workload.
 * It intentionally does not depend on Firefox, Xorg, DRM, or a filesystem.
 */
#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <linux/futex.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/eventfd.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#ifndef FUTEX_REQUEUE_PRIVATE
#define FUTEX_REQUEUE_PRIVATE (FUTEX_REQUEUE | FUTEX_PRIVATE_FLAG)
#endif

enum {
#ifndef PROBE_PING_PONG_ROUNDS
#define PROBE_PING_PONG_ROUNDS 100000
#endif
#ifndef PROBE_REQUEUE_ROUNDS
#define PROBE_REQUEUE_ROUNDS 100
#endif
#ifndef PROBE_PPOLL_ROUNDS
#define PROBE_PPOLL_ROUNDS 1000
#endif
#ifndef PROBE_REQUEUE_SETTLE_MS
#define PROBE_REQUEUE_SETTLE_MS 2
#endif
	PING_PONG_ROUNDS = PROBE_PING_PONG_ROUNDS,
	REQUEUE_ROUNDS = PROBE_REQUEUE_ROUNDS,
	REQUEUE_WAITERS = 4,
	PPOLL_ROUNDS = PROBE_PPOLL_ROUNDS,
	WAIT_TIMEOUT_MS = 1000,
};

struct ping_state {
	_Atomic int turn;
	_Atomic int stop;
	_Atomic int completed;
	_Atomic int timeouts;
	_Atomic int errors;
	_Atomic unsigned long waits;
	_Atomic unsigned long wakes;
	int rounds;
	pthread_t waiter;
};

static volatile sig_atomic_t signal_count;

static void on_usr1(int signo)
{
	(void)signo;
	++signal_count;
}

static struct timespec timeout_from_ms(int milliseconds)
{
	struct timespec timeout = {
		.tv_sec = milliseconds / 1000,
		.tv_nsec = (long)(milliseconds % 1000) * 1000000L,
	};
	return timeout;
}

static int emit_marker(const char *marker, size_t length)
{
	return write(STDOUT_FILENO, marker, length) == (ssize_t)length ? 0 : -1;
}

static int futex_wait_private(_Atomic int *word, int expected,
		int timeout_ms)
{
	struct timespec timeout = timeout_from_ms(timeout_ms);
	return (int)syscall(SYS_futex, (int *)word, FUTEX_WAIT_PRIVATE,
			expected, &timeout, NULL, 0);
}

static int futex_wake_private(_Atomic int *word, int count)
{
	return (int)syscall(SYS_futex, (int *)word, FUTEX_WAKE_PRIVATE,
			count, NULL, NULL, 0);
}

static int futex_requeue_private(_Atomic int *source, int wake_count,
		int requeue_count, _Atomic int *destination)
{
	return (int)syscall(SYS_futex, (int *)source,
			FUTEX_REQUEUE_PRIVATE, wake_count, requeue_count,
			(int *)destination, 0);
}

static void *ping_waiter(void *opaque)
{
	struct ping_state *state = opaque;
	(void)emit_marker("FUTEX_H1 waiter-start\n", 22);
	for (int round = 0; round < state->rounds; ++round) {
		while (atomic_load_explicit(&state->turn, memory_order_acquire) != 1 &&
			   !atomic_load_explicit(&state->stop, memory_order_relaxed)) {
			atomic_fetch_add_explicit(&state->waits, 1, memory_order_relaxed);
			int result = futex_wait_private(&state->turn, 0,
					WAIT_TIMEOUT_MS);
			if (result < 0) {
				/* EAGAIN is the expected check-to-sleep race: the producer
				 * changed the word after the user-space load. */
				if (errno == EINTR || errno == EAGAIN)
					continue;
				if (errno == ETIMEDOUT) {
					atomic_fetch_add_explicit(&state->timeouts, 1,
							memory_order_relaxed);
				} else {
					atomic_fetch_add_explicit(&state->errors, 1,
							memory_order_relaxed);
				}
				atomic_store_explicit(&state->stop, 1, memory_order_release);
				return NULL;
			}
		}
		if (atomic_load_explicit(&state->stop, memory_order_acquire))
			return NULL;
		atomic_store_explicit(&state->turn, 0, memory_order_release);
		int woken = futex_wake_private(&state->turn, 1);
		if (woken > 0)
			atomic_fetch_add_explicit(&state->wakes, (unsigned)woken,
					memory_order_relaxed);
		atomic_fetch_add_explicit(&state->completed, 1, memory_order_relaxed);
	}
	return NULL;
}

static void *ping_producer(void *opaque)
{
	struct ping_state *state = opaque;
	(void)emit_marker("FUTEX_H1 producer-start\n", 24);
	for (int round = 0; round < state->rounds &&
			!atomic_load_explicit(&state->stop, memory_order_acquire); ++round) {
		while (atomic_load_explicit(&state->turn, memory_order_acquire) != 0 &&
			   !atomic_load_explicit(&state->stop, memory_order_relaxed))
			sched_yield();
		if (atomic_load_explicit(&state->stop, memory_order_acquire))
			break;
		atomic_store_explicit(&state->turn, 1, memory_order_release);
		int woken = futex_wake_private(&state->turn, 1);
		if (woken > 0)
			atomic_fetch_add_explicit(&state->wakes, (unsigned)woken,
					memory_order_relaxed);
	}
	return NULL;
}

static void *signal_sender(void *opaque)
{
	struct ping_state *state = opaque;
	(void)emit_marker("FUTEX_H1 signaler-start\n", 24);
	for (int i = 0; i < 20000 &&
			!atomic_load_explicit(&state->stop, memory_order_relaxed); ++i) {
		(void)pthread_kill(state->waiter, SIGUSR1);
		struct timespec pause_for = {.tv_nsec = 100000L};
		nanosleep(&pause_for, NULL);
	}
	return NULL;
}

static int run_ping_pong(void)
{
	struct sigaction action = {.sa_handler = on_usr1, .sa_flags = SA_RESTART};
	sigemptyset(&action.sa_mask);
	if (sigaction(SIGUSR1, &action, NULL) < 0)
		return 2;
	struct ping_state state = {
		.turn = 0, .stop = 0, .completed = 0, .timeouts = 0, .errors = 0,
		.waits = 0, .wakes = 0, .rounds = PING_PONG_ROUNDS,
	};
	pthread_t producer, sender;
	if (emit_marker("FUTEX_H1 create-waiter\n", 23) < 0)
		return 2;
	if (pthread_create(&state.waiter, NULL, ping_waiter, &state) != 0) {
		(void)emit_marker("FUTEX_H1 waiter-create-fail\n", 28);
		atomic_store(&state.stop, 1);
		return 2;
	}
	if (emit_marker("FUTEX_H1 waiter-created\n", 24) < 0)
		return 2;
	if (pthread_create(&producer, NULL, ping_producer, &state) != 0) {
		(void)emit_marker("FUTEX_H1 producer-create-fail\n", 30);
		atomic_store(&state.stop, 1);
		return 2;
	}
	if (emit_marker("FUTEX_H1 producer-created\n", 26) < 0)
		return 2;
	if (pthread_create(&sender, NULL, signal_sender, &state) != 0) {
		(void)emit_marker("FUTEX_H1 signaler-create-fail\n", 30);
		atomic_store(&state.stop, 1);
		return 2;
	}
	if (emit_marker("FUTEX_H1 signaler-created\n", 26) < 0)
		return 2;
	pthread_join(producer, NULL);
	pthread_join(sender, NULL);
	/* A producer that completed all rounds is enough to release the waiter. */
	if (atomic_load(&state.completed) != PING_PONG_ROUNDS)
		atomic_store(&state.stop, 1);
	pthread_join(state.waiter, NULL);
	int timeouts = atomic_load(&state.timeouts);
	int errors = atomic_load(&state.errors);
	int completed = atomic_load(&state.completed);
	printf("FUTEX_H1 ping-pong rounds=%d completed=%d timeouts=%d errors=%d "
		"waits=%lu wakes=%lu signals=%d status=%s\n", PING_PONG_ROUNDS,
		completed, timeouts, errors, atomic_load(&state.waits),
		atomic_load(&state.wakes), signal_count,
		completed == PING_PONG_ROUNDS && timeouts == 0 && errors == 0 ?
			"pass" : "fail");
	return completed == PING_PONG_ROUNDS && timeouts == 0 && errors == 0 ? 0 : 1;
}

struct requeue_waiter {
	_Atomic int *source;
	_Atomic int *destination;
	_Atomic int *ready;
	_Atomic int *failed;
};

static void *requeue_wait(void *opaque)
{
	struct requeue_waiter *args = opaque;
	atomic_fetch_add(args->ready, 1);
	int result = futex_wait_private(args->source, 0, WAIT_TIMEOUT_MS);
	if (result < 0 && errno != EINTR)
		atomic_store(args->failed, 1);
	return NULL;
}

static int run_requeue(void)
{
	int failures = 0;
	for (int round = 0; round < REQUEUE_ROUNDS; ++round) {
		_Atomic int source = 0, destination = 0, ready = 0, failed = 0;
		pthread_t threads[REQUEUE_WAITERS];
		struct requeue_waiter args = {&source, &destination, &ready, &failed};
		for (int i = 0; i < REQUEUE_WAITERS; ++i)
			if (pthread_create(&threads[i], NULL, requeue_wait, &args) != 0)
				failures++;
		for (int spin = 0; spin < 1000 && atomic_load(&ready) <
				REQUEUE_WAITERS; ++spin) {
			struct timespec pause_for = {.tv_nsec = 1000000L};
			nanosleep(&pause_for, NULL);
		}
		struct timespec settle = {.tv_nsec = PROBE_REQUEUE_SETTLE_MS * 1000000L};
		nanosleep(&settle, NULL);
		int moved = futex_requeue_private(&source, 0, REQUEUE_WAITERS,
				&destination);
		atomic_store(&destination, 1);
		int woken = futex_wake_private(&destination, REQUEUE_WAITERS);
		if (round == 0)
			printf("FUTEX_H2 detail ready=%d moved=%d woken=%d failed=%d\n",
				atomic_load(&ready), moved, woken, atomic_load(&failed));
		/* FUTEX_REQUEUE returns the directly woken count, not the number
		 * moved to the second queue.  The remaining waiters are accounted
		 * for by the destination wake. */
		if (moved < 0 || woken < 0 || moved + woken < REQUEUE_WAITERS ||
			atomic_load(&failed))
			failures++;
		for (int i = 0; i < REQUEUE_WAITERS; ++i)
			pthread_join(threads[i], NULL);
	}
	printf("FUTEX_H2 requeue rounds=%d waiters=%d failures=%d status=%s\n",
		REQUEUE_ROUNDS, REQUEUE_WAITERS, failures,
		failures == 0 ? "pass" : "fail");
	return failures == 0 ? 0 : 1;
}

struct pipe_writer {
	int fd;
};

static void *pipe_write(void *opaque)
{
	struct pipe_writer *writer = opaque;
	struct timespec pause_for = {.tv_nsec = 1000000L};
	nanosleep(&pause_for, NULL);
	char byte = 'x';
	if (write(writer->fd, &byte, 1) != 1)
		return NULL;
	return NULL;
}

static int run_ppoll(void)
{
	int timeouts = 0, errors = 0;
	for (int round = 0; round < PPOLL_ROUNDS; ++round) {
		int pipe_fds[2];
		if (pipe(pipe_fds) < 0)
			return 2;
		struct pipe_writer writer = {.fd = pipe_fds[1]};
		pthread_t thread;
		if (pthread_create(&thread, NULL, pipe_write, &writer) != 0)
			return 2;
		struct pollfd poller = {.fd = pipe_fds[0], .events = POLLIN};
		struct timespec timeout = timeout_from_ms(WAIT_TIMEOUT_MS);
		int result = ppoll(&poller, 1, &timeout, NULL);
		if (result == 0)
			timeouts++;
		else if (result < 0 && errno != EINTR)
			errors++;
		char byte;
		if (read(pipe_fds[0], &byte, 1) != 1)
			errors++;
		pthread_join(thread, NULL);
		close(pipe_fds[0]);
		close(pipe_fds[1]);
	}
	printf("FUTEX_H3 ppoll-pipe rounds=%d timeouts=%d errors=%d status=%s\n",
		PPOLL_ROUNDS, timeouts, errors,
		timeouts == 0 && errors == 0 ? "pass" : "fail");
	return timeouts == 0 && errors == 0 ? 0 : 1;
}

int main(void)
{
	int console = open("/dev/console", O_RDWR);
	if (console >= 0) {
		(void)dup2(console, STDIN_FILENO);
		(void)dup2(console, STDOUT_FILENO);
		(void)dup2(console, STDERR_FILENO);
	}
	if (emit_marker("FUTEX_PROBE_STAGE start\n", 24) < 0)
		return 2;
	int result = 0;
	if (emit_marker("FUTEX_PROBE_STAGE h1\n", 21) < 0)
		return 2;
	result |= run_ping_pong();
	if (emit_marker("FUTEX_PROBE_STAGE h2\n", 21) < 0)
		return 2;
	result |= run_requeue();
	if (emit_marker("FUTEX_PROBE_STAGE h3\n", 21) < 0)
		return 2;
	result |= run_ppoll();
	printf("FUTEX_LOST_WAKEUP_PROBE status=%s\n", result == 0 ? "pass" : "fail");
	return result;
}
