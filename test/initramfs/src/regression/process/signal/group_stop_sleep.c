/* SPDX-License-Identifier: MPL-2.0 */

#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

enum { WAIT_ATTEMPTS = 500, WAIT_STEP_US = 10000, ENTRY_ATTEMPTS = 3 };
enum sleep_api { NANOSLEEP, CLOCK_RELATIVE, CLOCK_ABSOLUTE };
static const char *const api_names[] = { "nanosleep", "clock-relative",
					 "clock-absolute" };
static volatile sig_atomic_t handler_count;

struct ready_message {
	pid_t tid;
	char phase;
};

struct sleep_args {
	int ready;
	int result;
	enum sleep_api api;
	int caught_signal;
	int duration_ms;
};

struct sleep_result {
	struct timespec start;
	struct timespec end;
	int returned;
	int error;
	int handled;
};

static void caught_usr1(int number)
{
	(void)number;
	++handler_count;
}

static void *sleeper(void *argument)
{
	struct sleep_args *args = argument;
	struct sleep_result result = { 0 };
	if (clock_gettime(CLOCK_MONOTONIC, &result.start) < 0)
		_exit(2);
	struct timespec request = { .tv_nsec = args->duration_ms * 1000000L };
	if (args->api == CLOCK_ABSOLUTE) {
		request.tv_sec += result.start.tv_sec;
		request.tv_nsec += result.start.tv_nsec;
		if (request.tv_nsec >= 1000000000L) {
			++request.tv_sec;
			request.tv_nsec -= 1000000000L;
		}
	}
	struct ready_message ready = { .tid = syscall(SYS_gettid),
				       .phase = 'S' };
	if (write(args->ready, &ready, sizeof(ready)) != sizeof(ready))
		_exit(2);
	// Record the first return; caught signals interrupt even with SA_RESTART.
	if (args->api == NANOSLEEP) {
		result.returned = nanosleep(&request, NULL);
		result.error = result.returned < 0 ? errno : 0;
	} else {
		result.returned = clock_nanosleep(
			CLOCK_MONOTONIC,
			args->api == CLOCK_ABSOLUTE ? TIMER_ABSTIME : 0,
			&request, NULL);
		// Unlike nanosleep, clock_nanosleep returns the error number directly.
		result.error = result.returned;
	}
	result.handled = handler_count;
	if (clock_gettime(CLOCK_MONOTONIC, &result.end) < 0 ||
	    write(args->result, &result, sizeof(result)) != sizeof(result))
		_exit(2);
	_exit(result.error == (args->caught_signal ? EINTR : 0) ? 0 : 1);
}

static int receive(int fd, void *buffer, size_t length, const char *phase)
{
	for (int attempt = 0; attempt < WAIT_ATTEMPTS; ++attempt) {
		struct pollfd event = { .fd = fd, .events = POLLIN };
		int result = poll(&event, 1, WAIT_STEP_US / 1000);
		if (result < 0 && errno == EINTR)
			continue;
		if (result == 0)
			continue;
		if (result > 0 && (event.revents & POLLIN) &&
		    read(fd, buffer, length) == (ssize_t)length)
			return 1;
		fprintf(stderr, "GROUP_STOP_SLEEP FAIL phase=%s pipe\n", phase);
		return 0;
	}
	fprintf(stderr, "GROUP_STOP_SLEEP FAIL phase=%s timeout\n", phase);
	return 0;
}

static pid_t wait_bounded(pid_t *child, int *status, int options)
{
	for (int attempt = 0; attempt < WAIT_ATTEMPTS; ++attempt) {
		pid_t result = waitpid(*child, status, options | WNOHANG);
		if (result > 0) {
			if (WIFEXITED(*status) || WIFSIGNALED(*status))
				*child = 0;
			return result;
		}
		if (result < 0 && errno != EINTR) {
			if (errno == ECHILD)
				*child = 0;
			return result;
		}
		usleep(WAIT_STEP_US);
	}
	return 0;
}

static int reap_owned(pid_t *child)
{
	if (!*child)
		return 1;
	// Every consuming wait clears ownership before cleanup can signal a PID.
	if (kill(*child, SIGKILL) < 0 && errno != ESRCH)
		return 0;
	int status;
	return wait_bounded(child, &status, 0) > 0 || !*child;
}

static long long elapsed_ms(struct timespec start, struct timespec end)
{
	return (end.tv_sec - start.tv_sec) * 1000LL +
	       (end.tv_nsec - start.tv_nsec) / 1000000;
}

static int before(struct timespec left, struct timespec right)
{
	return left.tv_sec < right.tv_sec ||
	       (left.tv_sec == right.tv_sec && left.tv_nsec < right.tv_nsec);
}

static int elapsed_at_least(struct timespec start, struct timespec end,
			    int duration_ms)
{
	start.tv_sec += duration_ms / 1000;
	start.tv_nsec += (duration_ms % 1000) * 1000000L;
	if (start.tv_nsec >= 1000000000L) {
		++start.tv_sec;
		start.tv_nsec -= 1000000000L;
	}
	return !before(end, start);
}

static int wait_sleeping(pid_t child, pid_t tid)
{
	char path[96], line[256];
	snprintf(path, sizeof(path), "/proc/%d/task/%d/status", child, tid);
	for (int attempt = 0; attempt < WAIT_ATTEMPTS; ++attempt) {
		FILE *stream = fopen(path, "r");
		if (!stream)
			return 0;
		char state = 0;
		while (fgets(line, sizeof(line), stream))
			sscanf(line, "State: %c", &state);
		int failed = ferror(stream);
		if (fclose(stream) || failed || state == 'Z')
			return 0;
		if (state == 'S')
			return 1;
		usleep(WAIT_STEP_US);
	}
	return 0;
}

// Scheduling can exhaust a finite sleep before the parent even requests STOP.
// Retry only a proven missed setup window, never an interrupted/early return
// or a completion concurrent with or after the STOP request.
static int missed_sleep_window(const struct sleep_result *result,
			       struct timespec requested, int duration_ms)
{
	int missed = before(result->end, requested) && result->returned == 0 &&
		     result->error == 0 && result->handled == 0 &&
		     elapsed_at_least(result->start, result->end, duration_ms);
	fprintf(stderr,
		"GROUP_STOP_SLEEP setup_missed=%d end_minus_request_ms=%lld elapsed_ms=%lld error=%d handled=%d\n",
		missed, elapsed_ms(requested, result->end),
		elapsed_ms(result->start, result->end), result->error,
		result->handled);
	return missed;
}

static int finished_before_stop(int fd, struct timespec requested,
				int duration_ms)
{
	struct pollfd event = { .fd = fd, .events = POLLIN };
	struct sleep_result result;
	if (poll(&event, 1, 0) != 1 || !(event.revents & POLLIN) ||
	    read(fd, &result, sizeof(result)) != sizeof(result))
		return 0;
	return missed_sleep_window(&result, requested, duration_ms);
}

// Return -1 only for a proven missed setup window; it is not a passing case.
static int run_case(enum sleep_api api, int sibling, int caught_signal)
{
	int pipes[2][2] = { { -1, -1 }, { -1, -1 } };
	int *ready = pipes[0], *result_pipe = pipes[1];
	pid_t child = 0;
	int ok = 0;
	int duration_ms = caught_signal ? 500 : 150;
	for (int index = 0; index < 2; ++index)
		if (pipe(pipes[index]) < 0)
			goto cleanup;
	child = fork();
	if (child < 0) {
		child = 0;
		goto cleanup;
	}
	if (child == 0) {
		close(ready[0]);
		close(result_pipe[0]);
		alarm(30);
		if (caught_signal) {
			struct sigaction action = { .sa_handler = caught_usr1,
						    .sa_flags = SA_RESTART };
			sigemptyset(&action.sa_mask);
			if (sigaction(SIGUSR1, &action, NULL) < 0)
				_exit(2);
		}
		struct sleep_args args = { ready[1], result_pipe[1], api,
					   caught_signal, duration_ms };
		if (!sibling)
			sleeper(&args);
		struct ready_message message = { .tid = getpid(),
						 .phase = 'M' };
		if (write(ready[1], &message, sizeof(message)) !=
		    sizeof(message))
			_exit(2);
		pthread_t worker;
		if (pthread_create(&worker, NULL, sleeper, &args))
			_exit(2);
		// The worker exits the process; the alarm and parent bound this pause.
		while (pause() < 0 && errno == EINTR)
			;
		_exit(2);
	}
	close(ready[1]);
	ready[1] = -1;
	close(result_pipe[1]);
	result_pipe[1] = -1;
	pid_t sleeper_tid = 0;
	int main_ready = !sibling;
	for (int index = 0; index < 1 + sibling; ++index) {
		struct ready_message message;
		if (!receive(ready[0], &message, sizeof(message), "ready"))
			goto cleanup;
		if (message.phase == 'S')
			sleeper_tid = message.tid;
		main_ready |= message.phase == 'M' && message.tid == child;
	}
	if (sleeper_tid <= 0 || !main_ready ||
	    (sibling ? sleeper_tid == child : sleeper_tid != child))
		goto cleanup;
	// The worker's ready write precedes its sole blocking call. Observe that
	// call instead of consuming part of its deadline with a guessed delay.
	int sleeping = wait_sleeping(child, sleeper_tid);
	struct timespec stop_requested;
	if (clock_gettime(CLOCK_MONOTONIC, &stop_requested) < 0)
		goto cleanup;
	int status = 0;
	if (!sleeping || syscall(SYS_tgkill, child, child, SIGSTOP) < 0) {
		pid_t exited = wait_bounded(&child, &status, 0);
		if (exited > 0 && WIFEXITED(status) &&
		    WEXITSTATUS(status) == 0 && !caught_signal &&
		    finished_before_stop(result_pipe[0], stop_requested,
					 duration_ms))
			ok = -1;
		goto cleanup;
	}
	pid_t waited = wait_bounded(&child, &status, WUNTRACED);
	if (waited <= 0 || !WIFSTOPPED(status) || WSTOPSIG(status) != SIGSTOP) {
		fprintf(stderr,
			"GROUP_STOP_SLEEP FAIL phase=stop-wait result=%d status=%d\n",
			waited, status);
		if (waited > 0 && WIFEXITED(status) &&
		    WEXITSTATUS(status) == 0 && !caught_signal &&
		    finished_before_stop(result_pipe[0], stop_requested,
					 duration_ms))
			ok = -1;
		goto cleanup;
	}
	if (caught_signal &&
	    syscall(SYS_tgkill, child, sleeper_tid, SIGUSR1) < 0)
		goto cleanup;
	// Without a handler, hold STOP beyond the original sleep deadline.
	usleep(caught_signal ? 100000 : 300000);
	struct timespec continued;
	if (clock_gettime(CLOCK_MONOTONIC, &continued) < 0 ||
	    kill(child, SIGCONT) < 0)
		goto cleanup;
	struct sleep_result result;
	if (!receive(result_pipe[0], &result, sizeof(result), "sleep-result"))
		goto cleanup;
	long long total_ms = elapsed_ms(result.start, result.end);
	long long after_cont_ms = elapsed_ms(continued, result.end);
	printf("GROUP_STOP_SLEEP api=%s sibling=%d caught=%d ret=%d error=%d handled=%d requested_ms=%d elapsed_ms=%lld after_cont_ms=%lld\n",
	       api_names[api], sibling, caught_signal, result.returned,
	       result.error, result.handled, duration_ms, total_ms,
	       after_cont_ms);
	if (result.error == ENOSYS || result.error == EINVAL)
		fprintf(stderr,
			"GROUP_STOP_SLEEP ABI_UNSUPPORTED api=%s error=%d\n",
			api_names[api], result.error);
	waited = wait_bounded(&child, &status, 0);
	if (waited > 0 && WIFEXITED(status) && WEXITSTATUS(status) == 0 &&
	    !caught_signal && before(result.end, stop_requested) &&
	    missed_sleep_window(&result, stop_requested, duration_ms)) {
		ok = -1;
		goto cleanup;
	}
	// Timing is diagnostic: a strict upper limit would depend on QEMU load.
	ok = result.error == (caught_signal ? EINTR : 0) &&
	     result.handled == caught_signal &&
	     !before(result.end, continued) &&
	     (caught_signal ||
	      elapsed_at_least(result.start, result.end, duration_ms)) &&
	     waited > 0 && WIFEXITED(status) && WEXITSTATUS(status) == 0;
cleanup:
	if (!reap_owned(&child)) {
		fprintf(stderr, "GROUP_STOP_SLEEP FAIL phase=cleanup-reap\n");
		ok = 0;
	}
	for (int index = 0; index < 2; ++index)
		for (int end = 0; end < 2; ++end)
			if (pipes[index][end] >= 0)
				close(pipes[index][end]);
	printf("GROUP_STOP_SLEEP api=%s sibling=%d caught=%d pass=%d\n",
	       api_names[api], sibling, caught_signal, ok);
	return ok;
}

int main(void)
{
	// Retry eligibility must not round an early return across a second up.
	struct timespec start = { .tv_nsec = 900000000 };
	assert(!elapsed_at_least(start, (struct timespec){ 1, 49500000 }, 150));
	assert(elapsed_at_least(start, (struct timespec){ 1, 50000000 }, 150));
	assert(elapsed_at_least(start, (struct timespec){ 1, 50100000 }, 150));
	int passed = 0;
	for (enum sleep_api api = NANOSLEEP; api <= CLOCK_ABSOLUTE; ++api) {
		for (int scenario = 0; scenario < 3; ++scenario) {
			int result = -1;
			for (int attempt = 0;
			     result == -1 && attempt < ENTRY_ATTEMPTS;
			     ++attempt) {
				result = run_case(api, scenario != 0,
						  scenario == 2);
				if (result == -1)
					fprintf(stderr,
						"GROUP_STOP_SLEEP RETRY api=%s scenario=%d attempt=%d\n",
						api_names[api], scenario,
						attempt + 1);
			}
			passed += result == 1;
		}
	}
	printf("GROUP_STOP_SLEEP passed=%d/9\n", passed);
	return passed == 9 ? 0 : 1;
}
