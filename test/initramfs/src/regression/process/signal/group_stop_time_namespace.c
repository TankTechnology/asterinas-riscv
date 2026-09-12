/* SPDX-License-Identifier: MPL-2.0 */

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/futex.h>
#include <linux/sched.h>
#include <poll.h>
#include <sched.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

enum { WAIT_ATTEMPTS = 200, WAIT_STEP_US = 10000, SLEEP_MS = 50 };

static long long elapsed_ns(struct timespec start, struct timespec end)
{
	return (end.tv_sec - start.tv_sec) * 1000000000LL + end.tv_nsec -
	       start.tv_nsec;
}

static int setup_failure(const char *phase)
{
	int error = errno;
	int denied = error == EPERM || error == EACCES;
	printf("TIME_NAMESPACE_SLEEP %s phase=%s errno=%d\n",
	       denied ? "SKIP" : "SETUP_FAILURE", phase, error);
	return denied ? 77 : 1;
}

static int sleep_case(const char *name, clockid_t clock, int absolute,
		      int use_nanosleep)
{
	struct timespec start, end;
	if (clock_gettime(clock, &start) < 0)
		return 0;
	struct timespec request = { .tv_nsec = SLEEP_MS * 1000000L };
	if (absolute) {
		request.tv_sec += start.tv_sec;
		request.tv_nsec += start.tv_nsec;
		if (request.tv_nsec >= 1000000000L) {
			++request.tv_sec;
			request.tv_nsec -= 1000000000L;
		}
	}
	// Flush the active case before sleeping so a parent timeout identifies it.
	printf("TIME_NAMESPACE_SLEEP begin=%s requested_ms=%d\n", name,
	       SLEEP_MS);
	int error;
	if (use_nanosleep)
		error = nanosleep(&request, NULL) < 0 ? errno : 0;
	else
		error = clock_nanosleep(clock, absolute ? TIMER_ABSTIME : 0,
					&request, NULL);
	if (clock_gettime(clock, &end) < 0)
		return 0;
	long long duration_ns = elapsed_ns(start, end);
	int ok = error == 0 && duration_ns >= SLEEP_MS * 1000000LL;
	printf("TIME_NAMESPACE_SLEEP case=%s error=%d elapsed_ms=%lld pass=%d\n",
	       name, error, duration_ns / 1000000, ok);
	return ok;
}

static int futex_case(int absolute, int ready)
{
	struct timespec start, end;
	if (clock_gettime(CLOCK_MONOTONIC, &start) < 0)
		return 0;
	int duration_ms = ready >= 0 ? 400 : SLEEP_MS;
	struct timespec request = { .tv_nsec = duration_ms * 1000000L };
	if (absolute) {
		request.tv_sec += start.tv_sec;
		request.tv_nsec += start.tv_nsec;
		if (request.tv_nsec >= 1000000000L) {
			++request.tv_sec;
			request.tv_nsec -= 1000000000L;
		}
	}
	const char *name = ready >= 0 ? "futex-absolute-restart" :
			   absolute   ? "futex-absolute" :
					"futex-relative";
	printf("TIME_NAMESPACE_SLEEP begin=%s requested_ms=%d\n", name,
	       duration_ms);
	// The parent stops this thread briefly while the deadline is still live.
	if (ready >= 0 && write(ready, "F", 1) != 1)
		return 0;
	int word = 0;
	int operation = absolute ? FUTEX_WAIT_BITSET : FUTEX_WAIT;
	long returned = syscall(SYS_futex, &word,
				operation | FUTEX_PRIVATE_FLAG, 0, &request,
				NULL, FUTEX_BITSET_MATCH_ANY);
	int error = returned < 0 ? errno : 0;
	if (clock_gettime(CLOCK_MONOTONIC, &end) < 0)
		return 0;
	long long duration_ns = elapsed_ns(start, end);
	int ok = returned == -1 && error == ETIMEDOUT &&
		 duration_ns >= duration_ms * 1000000LL;
	printf("TIME_NAMESPACE_SLEEP case=%s error=%d elapsed_ms=%lld pass=%d\n",
	       name, error, duration_ns / 1000000, ok);
	return ok;
}

static int child_main(struct timespec parent_mono, struct timespec parent_boot,
		      int ready)
{
	struct timespec mono, boot;
	if (clock_gettime(CLOCK_MONOTONIC, &mono) < 0 ||
	    clock_gettime(CLOCK_BOOTTIME, &boot) < 0)
		return 1;
	long long mono_delta = elapsed_ns(parent_mono, mono);
	long long boot_delta = elapsed_ns(parent_boot, boot);
	// Verify that the fork joined time_ns_for_children, preventing a false pass.
	int joined =
		mono_delta >= 60000000000LL && mono_delta < 62000000000LL &&
		boot_delta >= 120000000000LL && boot_delta < 122000000000LL;
	printf("TIME_NAMESPACE_SLEEP monotonic_delta_ms=%lld boottime_delta_ms=%lld joined=%d\n",
	       mono_delta / 1000000, boot_delta / 1000000, joined);
	if (!joined)
		return 1;
	int passed = 0;
	passed += sleep_case("nanosleep", CLOCK_MONOTONIC, 0, 1);
	passed += sleep_case("monotonic-relative", CLOCK_MONOTONIC, 0, 0);
	passed += sleep_case("monotonic-absolute", CLOCK_MONOTONIC, 1, 0);
	passed += sleep_case("boottime-relative", CLOCK_BOOTTIME, 0, 0);
	passed += sleep_case("boottime-absolute", CLOCK_BOOTTIME, 1, 0);
	passed += futex_case(0, -1);
	passed += futex_case(1, -1);
	passed += futex_case(1, ready);
	printf("TIME_NAMESPACE_SLEEP passed=%d/8\n", passed);
	return passed == 8 ? 0 : 1;
}

static pid_t wait_bounded(pid_t *child, int *status, int options)
{
	for (int attempt = 0; attempt < WAIT_ATTEMPTS; ++attempt) {
		pid_t result = waitpid(*child, status, WNOHANG | options);
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

static int wait_futex_ready(int fd)
{
	for (int attempt = 0; attempt < WAIT_ATTEMPTS; ++attempt) {
		struct pollfd event = { .fd = fd, .events = POLLIN };
		int result = poll(&event, 1, WAIT_STEP_US / 1000);
		if (result == 0 || (result < 0 && errno == EINTR))
			continue;
		char phase;
		return result > 0 && (event.revents & POLLIN) &&
		       read(fd, &phase, 1) == 1 && phase == 'F';
	}
	return 0;
}

int main(void)
{
	setvbuf(stdout, NULL, _IONBF, 0);
	// The calling parent stays in its original namespace; only its child joins.
	if (unshare(CLONE_NEWTIME) < 0)
		return setup_failure("unshare-NEWTIME");
	// Asterinas currently accepts only one clock record per proc-file write.
	const char *values[] = { "monotonic 60 0\n", "boottime 120 0\n" };
	for (int index = 0; index < 2; ++index) {
		int offsets = open("/proc/self/timens_offsets", O_WRONLY);
		if (offsets < 0)
			return setup_failure("open-timens_offsets");
		size_t length = strlen(values[index]);
		ssize_t written = write(offsets, values[index], length);
		if (written != (ssize_t)length) {
			int error = written < 0 ? errno : EIO;
			close(offsets);
			errno = error;
			return setup_failure("write-timens_offsets");
		}
		if (close(offsets) < 0)
			return setup_failure("close-timens_offsets");
	}
	struct timespec parent_mono, parent_boot;
	if (clock_gettime(CLOCK_MONOTONIC, &parent_mono) < 0 ||
	    clock_gettime(CLOCK_BOOTTIME, &parent_boot) < 0)
		return setup_failure("parent-clock");
	int ready[2];
	if (pipe(ready) < 0)
		return setup_failure("pipe");
	pid_t child = fork();
	if (child < 0) {
		int error = errno;
		close(ready[0]);
		close(ready[1]);
		errno = error;
		return setup_failure("fork");
	}
	if (child == 0) {
		close(ready[0]);
		_exit(child_main(parent_mono, parent_boot, ready[1]));
	}
	close(ready[1]);
	int ok = 0;
	int status = 0;
	pid_t waited = 0;
	if (!wait_futex_ready(ready[0]))
		goto cleanup;
	// Readiness precedes syscall entry; allow it to enter before requesting STOP.
	usleep(50000);
	if (kill(child, SIGSTOP) < 0)
		goto cleanup;
	waited = wait_bounded(&child, &status, WUNTRACED);
	if (waited <= 0 || !WIFSTOPPED(status) || WSTOPSIG(status) != SIGSTOP)
		goto cleanup;
	usleep(100000);
	if (kill(child, SIGCONT) < 0)
		goto cleanup;
	waited = wait_bounded(&child, &status, 0);
	ok = waited > 0 && WIFEXITED(status) && WEXITSTATUS(status) == 0;
cleanup:
	close(ready[0]);
	if (!ok)
		printf("TIME_NAMESPACE_SLEEP FAIL phase=child-wait result=%d status=%d deadline_ms=2000\n",
		       waited, status);
	// Every consuming wait clears ownership before cleanup signals the PID.
	if (child) {
		if ((kill(child, SIGKILL) < 0 && errno != ESRCH) ||
		    wait_bounded(&child, &status, 0) <= 0) {
			printf("TIME_NAMESPACE_SLEEP FAIL phase=cleanup\n");
			ok = 0;
		}
	}
	printf("TIME_NAMESPACE_SLEEP pass=%d\n", ok);
	return ok ? 0 : 1;
}
