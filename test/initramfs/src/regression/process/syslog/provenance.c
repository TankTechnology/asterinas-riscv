// SPDX-License-Identifier: MPL-2.0

/* Asterinas-specific diagnostic contract; run with klog_capture=info. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

static void *worker(void *arg)
{
	int fd = *(int *)arg;
	pid_t tid = syscall(SYS_gettid);
	if (write(fd, &tid, sizeof(tid)) != sizeof(tid))
		_exit(2);
	for (;;)
		pause();
}

static int reap(pid_t child, int *status)
{
	for (int i = 0; i < 500; ++i) {
		pid_t result = waitpid(child, status, WNOHANG);
		if (result == child)
			return 1;
		if (result < 0 && errno != EINTR)
			return 0;
		usleep(10000);
	}
	return 0;
}

enum operation { PROCESS_KILL, THREAD_KILL, GROUP_EXIT };

static int check_case(enum operation operation)
{
	int ready[2] = { -1, -1 }, command[2] = { -1, -1 };
	int log_fd = -1, success = 0, status;
	pid_t child = -1, tid = -1;
	if (pipe(ready) || pipe(command))
		goto out;
	log_fd = open("/dev/kmsg", O_RDONLY | O_NONBLOCK);
	if (log_fd < 0 || lseek(log_fd, 0, SEEK_END) < 0)
		goto out;
	child = fork();
	if (child < 0)
		goto out;
	if (!child) {
		close(ready[0]);
		close(command[1]);
		close(log_fd);
		pthread_t thread;
		if (pthread_create(&thread, NULL, worker, &ready[1]))
			_exit(3);
		char byte;
		_exit(read(command[0], &byte, 1) == 1 ? 0 : 4);
	}
	close(ready[1]);
	ready[1] = -1;
	close(command[0]);
	command[0] = -1;
	struct pollfd pfd = { .fd = ready[0], .events = POLLIN };
	if (poll(&pfd, 1, 3000) != 1 || !(pfd.revents & POLLIN) ||
	    read(ready[0], &tid, sizeof(tid)) != sizeof(tid))
		goto out;
	if (operation == GROUP_EXIT) {
		if (write(command[1], "x", 1) != 1)
			goto out;
	} else if (operation == THREAD_KILL) {
		if (syscall(SYS_tgkill, child, tid, SIGKILL))
			goto out;
	} else if (kill(child, SIGKILL)) {
		goto out;
	}
	pid_t target = child;
	if (!reap(child, &status))
		goto out;
	child = -1;
	if (operation == GROUP_EXIT ?
		    !WIFEXITED(status) || WEXITSTATUS(status) :
		    !WIFSIGNALED(status) || WTERMSIG(status) != SIGKILL)
		goto out;

	char expected[128], target_field[64], record[2048];
	snprintf(expected, sizeof(expected), "sender=%d/%ld target=%d/%d ",
		 getpid(), syscall(SYS_gettid), target,
		 operation == THREAD_KILL ? tid : target);
	snprintf(target_field, sizeof(target_field), "target=%d/", target);
	int enqueues = 0, deliveries = 0, records_ok = 1, drained = 0;
	for (int i = 0; i < 256; ++i) {
		ssize_t count = read(log_fd, record, sizeof(record) - 1);
		if (count < 0 && errno == EAGAIN) {
			drained = 1;
			break;
		}
		if (count <= 0)
			goto out; /* An overwritten cursor is not a successful observation. */
		record[count] = '\0';
		char *message = strstr(record, "A_SIGKILL_PROVENANCE ");
		if (!message || !strstr(message, target_field))
			continue;
		unsigned int priority;
		if (sscanf(record, "%u,", &priority) != 1)
			goto out;
		if (strstr(message, "stage=enqueue") &&
		    strstr(message, "origin=user")) {
			++enqueues;
			char *ids = strstr(message, expected);
			char *origin = strstr(message, "origin=user");
			/* Identity must survive a short serial prefix; names are optional. */
			if (!ids || ids >= origin || strlen(message) > 160 ||
			    (priority & 7) != 4)
				records_ok = 0;
		} else if (strstr(message, "stage=delivery")) {
			++deliveries;
			if (!strstr(message, target_field) ||
			    (priority & 7) != 6)
				records_ok = 0;
		} else if (strstr(message, "reason=exit-group-sibling")) {
			/* A debug console can raise capture above the requested info. */
			if ((priority & 7) != 7)
				records_ok = 0;
		}
	}
	success = drained && records_ok &&
		  enqueues == (operation != GROUP_EXIT) && deliveries > 0;
	printf("PROVENANCE case=%d enqueues=%d deliveries=%d contract=%d\n",
	       operation, enqueues, deliveries, records_ok);
out:
	if (child > 0) {
		kill(child, SIGKILL);
		if (!reap(child, &status))
			fprintf(stderr, "PROVENANCE cleanup failed pid=%d\n",
				child);
	}
	for (int i = 0; i < 2; ++i) {
		if (ready[i] >= 0)
			close(ready[i]);
		if (command[i] >= 0)
			close(command[i]);
	}
	if (log_fd >= 0)
		close(log_fd);
	printf("PROVENANCE %s case=%d\n", success ? "PASS" : "FAIL", operation);
	return success;
}

int main(void)
{
	char cmdline[4096];
	int fd = open("/proc/cmdline", O_RDONLY);
	ssize_t size = fd < 0 ? -1 : read(fd, cmdline, sizeof(cmdline) - 1);
	if (fd >= 0)
		close(fd);
	if (size < 0)
		return 1;
	cmdline[size] = '\0';
	if (!strstr(cmdline, "asterinas.klog_capture=info")) {
		puts("PROVENANCE SKIP requires asterinas.klog_capture=info");
		return 0;
	}
	int passed = check_case(PROCESS_KILL);
	passed &= check_case(THREAD_KILL);
	passed &= check_case(GROUP_EXIT);
	return passed ? 0 : 1;
}
