// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <semaphore.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdio.h>
#include <time.h>
#include <unistd.h>

/* A blocked, default-ignored SIGCHLD must not be discarded by the generic
 * cancellation probe between sigtimedwait's explicit dequeue attempts. */
enum { ITERATIONS = 10000 };
_Static_assert(ATOMIC_INT_LOCK_FREE == 2, "shared counters must be lock-free");
_Static_assert(ATOMIC_LONG_LOCK_FREE == 2,
	       "shared timestamps must be lock-free");
static atomic_uint request, done;
static atomic_int stop, send_error;
static atomic_ulong sent_ns;
/* Alarm diagnostics must not acquire a stdio lock held by either thread. */
static atomic_uint phase, completed, sender_phase;
static atomic_ulong iteration_start_ns;
static sem_t request_ready, send_completed;

static void write_counter(const char *label, size_t length, unsigned long value)
{
	char digits[32];
	size_t begin = sizeof(digits);
	do {
		digits[--begin] = '0' + value % 10;
		value /= 10;
	} while (value);
	if (write(STDERR_FILENO, label, length) != (ssize_t)length ||
	    write(STDERR_FILENO, digits + begin, sizeof(digits) - begin) !=
		    (ssize_t)(sizeof(digits) - begin))
		_exit(142);
}

static void timed_out(int signal)
{
	(void)signal;
	/* Restore default termination before printing. A blocked diagnostic write
	 * must not outlive this one-second failure-reporting grace period. */
	alarm(1);
	/* These individually atomic counters are an advisory, not coherent, snapshot. */
#define REPORT_COUNTER(label, counter) \
	write_counter(label, sizeof(label) - 1, atomic_load(&(counter)))
	REPORT_COUNTER("SIGWAIT_RACE_TIMEOUT phase=", phase);
	REPORT_COUNTER(" request=", request);
	REPORT_COUNTER(" done=", done);
	REPORT_COUNTER(" completed=", completed);
	REPORT_COUNTER(" sender_phase=", sender_phase);
#undef REPORT_COUNTER
	unsigned long start = atomic_load(&iteration_start_ns);
	struct timespec ts;
	if (start && !clock_gettime(CLOCK_MONOTONIC, &ts)) {
		unsigned long now =
			(unsigned long)ts.tv_sec * 1000000000UL + ts.tv_nsec;
		write_counter(" wait_age_ns=", sizeof(" wait_age_ns=") - 1,
			      now - start);
	}
	if (write(STDERR_FILENO, "\n", 1) != 1)
		_exit(142);
	_exit(142);
}

static unsigned long now_ns(void)
{
	struct timespec ts;
	if (clock_gettime(CLOCK_MONOTONIC, &ts))
		_exit(2);
	return (unsigned long)ts.tv_sec * 1000000000UL + ts.tv_nsec;
}

static void *sender(void *unused)
{
	(void)unused;
	for (;;) {
		atomic_store(&sender_phase, 0);
		if (sem_wait(&request_ready)) {
			atomic_store(&send_error, errno);
			return NULL;
		}
		if (atomic_load(&stop))
			return NULL;
		unsigned int next = atomic_load(&request);
		atomic_store(&sender_phase, 1);
		if (kill(getpid(), SIGCHLD))
			atomic_store(&send_error, errno);
		atomic_store(&sender_phase, 2);
		atomic_store(&sent_ns, now_ns());
		atomic_store(&done, next);
		if (sem_post(&send_completed)) {
			atomic_store(&send_error, errno);
			return NULL;
		}
	}
}

int main(void)
{
	sigset_t mask;
	sigemptyset(&mask);
	sigaddset(&mask, SIGCHLD);
	if (pthread_sigmask(SIG_SETMASK, &mask, NULL))
		return 2;
	struct sigaction action = { .sa_handler = timed_out,
				    .sa_flags = SA_RESETHAND | SA_NODEFER };
	sigemptyset(&action.sa_mask);
	if (sigaction(SIGALRM, &action, NULL))
		return 2;
	/* Bounds both the wait loop and thread cleanup, including native runs. */
	alarm(15);
	/* Block while idle: yield polling couples a correctness gate to CPU
	 * placement and scheduler time slices instead of signal delivery. The
	 * sender still races our sigtimedwait after each request is published. */
	if (sem_init(&request_ready, 0, 0) || sem_init(&send_completed, 0, 0))
		return 2;
	pthread_t thread;
	if (pthread_create(&thread, NULL, sender, NULL))
		return 2;
	int failed = 0;
	unsigned int passed = 0;
	unsigned long test_start = now_ns(), max_wait = 0;
	for (unsigned int i = 1; i <= ITERATIONS; ++i) {
		unsigned long start = now_ns();
		atomic_store(&iteration_start_ns, start);
		atomic_store(&request, i);
		if (sem_post(&request_ready))
			return 2;
		struct timespec timeout = { .tv_sec = 1 };
		siginfo_t info;
		atomic_store(&phase, 1);
		int result = sigtimedwait(&mask, &info, &timeout);
		int error = errno;
		unsigned long end = now_ns();
		if (end - start > max_wait)
			max_wait = end - start;
		atomic_store(&phase, 2);
		struct timespec completion_deadline;
		if (clock_gettime(CLOCK_REALTIME, &completion_deadline))
			return 2;
		completion_deadline.tv_nsec += 50000000L;
		if (completion_deadline.tv_nsec >= 1000000000L) {
			++completion_deadline.tv_sec;
			completion_deadline.tv_nsec -= 1000000000L;
		}
		int completion =
			sem_timedwait(&send_completed, &completion_deadline);
		int completion_error = completion ? errno : 0;
		if (result != SIGCHLD || completion ||
		    atomic_load(&done) != i || atomic_load(&send_error)) {
			sigset_t pending;
			if (sigpending(&pending))
				return 2;
			printf("SIGWAIT_RACE iteration=%u result=%d error=%d done=%u "
			       "completion=%d completion_error=%d send_error=%d "
			       "sent_after_ns=%lu elapsed_ns=%lu pending=%d\n",
			       i, result, error, atomic_load(&done), completion,
			       completion_error, atomic_load(&send_error),
			       atomic_load(&sent_ns) - start, end - start,
			       sigismember(&pending, SIGCHLD));
			failed = 1;
			break;
		}
		++passed;
		atomic_store(&completed, passed);
		if (passed % 1000 == 0) {
			printf("SIGWAIT_RACE_PROGRESS passed=%u elapsed_ns=%lu max_wait_ns=%lu\n",
			       passed, now_ns() - test_start, max_wait);
			fflush(stdout);
		}
	}
	atomic_store(&stop, 1);
	if (sem_post(&request_ready))
		return 2;
	atomic_store(&phase, 3);
	if (pthread_join(thread, NULL))
		return 2;
	if (sem_destroy(&request_ready) || sem_destroy(&send_completed))
		return 2;
	sigset_t restored;
	if (pthread_sigmask(SIG_SETMASK, NULL, &restored) ||
	    sigismember(&restored, SIGCHLD) != 1)
		failed = 1;
	alarm(0);
	printf("SIGWAIT_RACE passed=%u failed=%d\n", passed, failed);
	return failed;
}
