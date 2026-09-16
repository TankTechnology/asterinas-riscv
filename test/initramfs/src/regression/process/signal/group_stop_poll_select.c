/* SPDX-License-Identifier: MPL-2.0 */

#define _GNU_SOURCE
#include <errno.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <sys/mman.h>
#include <sys/select.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#if defined(SYS_poll) && defined(SYS_select)
enum { ATTEMPTS = 500, STEP_US = 10000, TIMEOUT_MS = 300 };
enum scenario {
	EXPIRE,
	INTERRUPT,
	STOP_EXPIRE,
	STOP_READY,
	STOP_INTERRUPT,
	STOP_UNBOUNDED,
	STOP_TWICE,
	STOP_EXPIRED_READY
};
static volatile sig_atomic_t handled;

struct observation {
	long returned;
	int error, handled, ready;
	int64_t elapsed_ns;
	struct timeval remaining;
};

static void handler(int signal)
{
	(void)signal;
	++handled;
}

static int receive(int fd, void *data, size_t size)
{
	for (int i = 0; i < ATTEMPTS; ++i) {
		struct pollfd event = { .fd = fd, .events = POLLIN };
		int result = poll(&event, 1, STEP_US / 1000);
		if (result == 0 || (result < 0 && errno == EINTR))
			continue;
		return result > 0 && (event.revents & POLLIN) &&
		       read(fd, data, size) == (ssize_t)size;
	}
	return 0;
}

static int wait_child(pid_t *child, int *status, int options)
{
	for (int i = 0; i < ATTEMPTS; ++i) {
		pid_t result = waitpid(*child, status, options | WNOHANG);
		if (result > 0) {
			if (WIFEXITED(*status) || WIFSIGNALED(*status))
				*child = 0;
			return 1;
		}
		if (result < 0 && errno != EINTR) {
			if (errno == ECHILD)
				*child = 0;
			return 0;
		}
		usleep(STEP_US);
	}
	return 0;
}

// After the readiness write the child's only blocking operation is the raw
// syscall under test. Require actual sleep, not merely the readiness byte.
static int wait_sleeping(pid_t child)
{
	char path[64], line[256];
	snprintf(path, sizeof(path), "/proc/%d/status", child);
	for (int i = 0; i < ATTEMPTS; ++i) {
		FILE *file = fopen(path, "r");
		if (!file)
			return 0;
		char state = 0;
		while (fgets(line, sizeof(line), file))
			sscanf(line, "State: %c", &state);
		int failed = ferror(file);
		if (fclose(file) || failed || state == 'Z')
			return 0;
		if (state == 'S')
			return 1;
		usleep(STEP_US);
	}
	return 0;
}

static void child_main(int use_select, enum scenario scenario, int readonly,
		       int ready_fd, int output_fd, int input_fd)
{
	alarm(5);
	struct sigaction action = { .sa_handler = handler,
				    .sa_flags = SA_RESTART };
	sigemptyset(&action.sa_mask);
	if (sigaction(SIGUSR1, &action, NULL))
		_exit(2);
	struct timeval *timeout = mmap(NULL, 4096, PROT_READ | PROT_WRITE,
				       MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (timeout == MAP_FAILED)
		_exit(2);
	*timeout = (struct timeval){ .tv_usec = TIMEOUT_MS * 1000 };
	if (readonly && mprotect(timeout, 4096, PROT_READ))
		_exit(2);
	fd_set input;
	FD_ZERO(&input);
	FD_SET(input_fd, &input);
	struct pollfd event = { .fd = input_fd,
				.events = POLLIN,
				.revents = POLLERR };
	struct timespec start, end;
	if (clock_gettime(CLOCK_MONOTONIC, &start) ||
	    write(ready_fd, "R", 1) != 1)
		_exit(2);
	struct observation result = { 0 };
	errno = 0;
	if (use_select)
		result.returned =
			syscall(SYS_select, input_fd + 1, &input, NULL, NULL,
				scenario == STOP_UNBOUNDED ? NULL : timeout);
	else
		result.returned =
			syscall(SYS_poll, &event, 1,
				scenario == STOP_UNBOUNDED ? -1 : TIMEOUT_MS);
	result.error = errno;
	result.handled = handled;
	result.ready = use_select ? FD_ISSET(input_fd, &input) : event.revents;
	result.remaining = *timeout;
	if (clock_gettime(CLOCK_MONOTONIC, &end))
		_exit(2);
	result.elapsed_ns = (int64_t)(end.tv_sec - start.tv_sec) * 1000000000 +
			    end.tv_nsec - start.tv_nsec;
	if (write(output_fd, &result, sizeof(result)) != sizeof(result))
		_exit(2);
	_exit(0);
}

static int run_case(int use_select, enum scenario scenario, int readonly)
{
	int pipes[3][2] = { { -1, -1 }, { -1, -1 }, { -1, -1 } };
	pid_t child = 0;
	int ok = 0, status;
	const char *phase = "setup";
	for (int i = 0; i < 3; ++i)
		if (pipe(pipes[i]))
			goto cleanup;
	child = fork();
	if (child < 0) {
		child = 0;
		goto cleanup;
	}
	if (!child) {
		close(pipes[0][0]);
		close(pipes[1][0]);
		close(pipes[2][1]);
		child_main(use_select, scenario, readonly, pipes[0][1],
			   pipes[1][1], pipes[2][0]);
	}
	for (int i = 0; i < 2; ++i) {
		close(pipes[i][1]);
		pipes[i][1] = -1;
	}
	close(pipes[2][0]);
	pipes[2][0] = -1;
	phase = "entry";
	char byte;
	if (!receive(pipes[0][0], &byte, 1) || byte != 'R' ||
	    !wait_sleeping(child))
		goto cleanup;
	if (scenario >= STOP_EXPIRE) {
		phase = "stop";
		// Consume a visible part of select's timeout before stopping; a
		// restart with the full original duration must not pass copyback.
		if (scenario == STOP_READY)
			usleep(100000);
		if (kill(child, SIGSTOP) ||
		    !wait_child(&child, &status, WUNTRACED) ||
		    !WIFSTOPPED(status) || WSTOPSIG(status) != SIGSTOP)
			goto cleanup;
		if (scenario == STOP_TWICE) {
			// A second interruption enters through restart_syscall for poll;
			// it must save the consumed restart block again, not lose it.
			if (kill(child, SIGCONT) || !wait_sleeping(child) ||
			    kill(child, SIGSTOP) ||
			    !wait_child(&child, &status, WUNTRACED) ||
			    !WIFSTOPPED(status) || WSTOPSIG(status) != SIGSTOP)
				goto cleanup;
		}
		// Poll includes time stopped; select replays its pre-stop remaining
		// duration. Both must rescan ready descriptors after continuation.
		if (scenario == STOP_EXPIRE || scenario == STOP_TWICE ||
		    scenario == STOP_EXPIRED_READY)
			usleep((TIMEOUT_MS + 100) * 1000);
		if ((scenario == STOP_READY || scenario == STOP_UNBOUNDED ||
		     scenario == STOP_EXPIRED_READY) &&
		    write(pipes[2][1], "D", 1) != 1)
			goto cleanup;
		if (scenario == STOP_INTERRUPT && kill(child, SIGUSR1))
			goto cleanup;
		if (kill(child, SIGCONT))
			goto cleanup;
	}
	if (scenario == INTERRUPT && kill(child, SIGUSR1))
		goto cleanup;
	phase = "result";
	struct observation result;
	if (!receive(pipes[1][0], &result, sizeof(result)))
		goto cleanup;
	int caught = scenario == INTERRUPT || scenario == STOP_INTERRUPT;
	int interrupted = caught ||
			  (use_select && readonly && scenario >= STOP_EXPIRE);
	int ready = scenario == STOP_READY || scenario == STOP_UNBOUNDED ||
		    scenario == STOP_EXPIRED_READY;
	ok = result.handled == caught;
	if (interrupted)
		ok &= result.returned == -1 && result.error == EINTR;
	else if (ready)
		ok &= result.returned == 1 && result.ready == POLLIN;
	else
		ok &= result.returned == 0 && result.ready == 0;
	if (interrupted && !use_select)
		ok &= result.ready == 0;
	if (use_select && scenario != STOP_UNBOUNDED) {
		if (readonly)
			ok &= result.remaining.tv_sec == 0 &&
			      result.remaining.tv_usec == TIMEOUT_MS * 1000;
		else if (!caught && !ready)
			ok &= result.remaining.tv_sec == 0 &&
			      result.remaining.tv_usec == 0;
		else
			ok &= result.remaining.tv_sec == 0 &&
			      result.remaining.tv_usec >= 0 &&
			      result.remaining.tv_usec < TIMEOUT_MS * 1000;
	}
	if (use_select && !readonly && scenario == STOP_READY)
		ok &= result.remaining.tv_usec < 220000;
	if (scenario == EXPIRE)
		ok &= result.elapsed_ns >= (int64_t)(TIMEOUT_MS - 10) * 1000000;
	if ((scenario == STOP_EXPIRE || scenario == STOP_TWICE) &&
	    !interrupted) {
		int64_t min_ns = (int64_t)(TIMEOUT_MS + 100) * 1000000;
		if (use_select)
			min_ns += (int64_t)(TIMEOUT_MS - 20) * 1000000;
		ok &= result.elapsed_ns >= min_ns;
		// A generous bound still distinguishes restarting poll's full
		// duration (about 700 ms) from preserving its deadline (400 ms).
		if (!use_select)
			ok &= result.elapsed_ns < 600000000;
	}
	printf("GROUP_STOP_POLL_SELECT select=%d scenario=%d readonly=%d ret=%ld errno=%d handled=%d ready=%d elapsed_ns=%lld remaining=%ld.%06ld pass=%d\n",
	       use_select, scenario, readonly, result.returned, result.error,
	       result.handled, result.ready, (long long)result.elapsed_ns,
	       result.remaining.tv_sec, result.remaining.tv_usec, ok);
	phase = "exit";
	ok &= wait_child(&child, &status, 0) && WIFEXITED(status) &&
	      WEXITSTATUS(status) == 0;
cleanup:
	if (child) {
		if ((kill(child, SIGKILL) && errno != ESRCH) ||
		    !wait_child(&child, &status, 0))
			ok = 0;
	}
	for (int i = 0; i < 3; ++i)
		for (int j = 0; j < 2; ++j)
			if (pipes[i][j] >= 0)
				close(pipes[i][j]);
	if (!ok)
		printf("GROUP_STOP_POLL_SELECT_FAILURE select=%d scenario=%d readonly=%d phase=%s\n",
		       use_select, scenario, readonly, phase);
	return ok;
}
#endif

int main(void)
{
	setbuf(stdout, NULL);
#if defined(SYS_poll) && defined(SYS_select)
	alarm(30);
	int passed = 0, total = 0;
	for (int select = 0; select <= 1; ++select)
		for (int scenario = EXPIRE; scenario <= STOP_EXPIRED_READY;
		     ++scenario) {
			passed += run_case(select, scenario, 0);
			++total;
		}
	for (int scenario = EXPIRE; scenario <= STOP_INTERRUPT; ++scenario) {
		passed += run_case(1, scenario, 1);
		++total;
	}
	printf("GROUP_STOP_POLL_SELECT passed=%d/%d\n", passed, total);
	return passed == total ? 0 : 1;
#else
	puts("GROUP_STOP_POLL_SELECT SKIP: architecture uses ppoll/pselect6");
	return 0;
#endif
}
