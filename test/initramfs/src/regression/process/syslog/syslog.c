// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE

#include "../../common/test.h"
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <sys/klog.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

enum {
	LOG_CLOSE,
	LOG_OPEN,
	LOG_READ,
	LOG_READ_ALL,
	LOG_READ_CLEAR,
	LOG_CLEAR,
	LOG_CONSOLE_OFF,
	LOG_CONSOLE_ON,
	LOG_CONSOLE_LEVEL,
	LOG_SIZE_UNREAD,
	LOG_SIZE_BUFFER,
};

static int writer_fd;
static char record[8192];
static char snapshot[1024 * 1024];

static void inject(const char *message)
{
	size_t length = strlen(message);
	CHECK_WITH(write(writer_fd, message, strlen(message)),
		   _ret >= 0 && (size_t)_ret == length);
}

static int reader_at_end(void)
{
	int fd = CHECK(open("/dev/kmsg", O_RDONLY | O_NONBLOCK));
	CHECK(lseek(fd, 0, SEEK_END));
	return fd;
}

static int syscall_info_diagnostics_enabled(void)
{
	int fd = open("/proc/cmdline", O_RDONLY);
	if (fd < 0)
		return 0;
	ssize_t count = read(fd, record, sizeof(record) - 1);
	close(fd);
	if (count < 0)
		return 0;
	record[count] = '\0';
	return strstr(record, "asterinas.syscall_diag=1") &&
	       strstr(record, "asterinas.klog_capture=info");
}

static unsigned long long read_marker(int fd, const char *marker,
				      unsigned int *priority)
{
	for (int i = 0; i < 2048; i++) {
		ssize_t count = read(fd, record, sizeof(record) - 1);
		if (count < 0 && errno == EPIPE)
			continue;
		if (count < 0 && errno == EAGAIN) {
			struct pollfd pfd = { .fd = fd, .events = POLLIN };
			int ready;
			do {
				ready = poll(&pfd, 1, 5000);
			} while (ready < 0 && errno == EINTR);
			CHECK_WITH(ready, _ret == 1 && (pfd.revents & POLLIN));
			continue;
		}
		CHECK_WITH(count, _ret > 0);
		record[count] = '\0';
		if (strstr(record, marker)) {
			unsigned long long sequence, timestamp;
			char flags;
			CHECK_WITH(sscanf(record, "%u,%llu,%llu,%c;", priority,
					  &sequence, &timestamp, &flags),
				   _ret == 4);
			CHECK_WITH(record[count - 1], _ret == '\n');
			return sequence;
		}
	}
	fprintf(stderr, "kernel log marker was not found: %s\n", marker);
	exit(EXIT_FAILURE);
}

FN_SETUP(open_log)
{
	CHECK_WITH(klogctl(LOG_SIZE_BUFFER, NULL, 0), _ret > 0);
	writer_fd = CHECK(open("/dev/kmsg", O_WRONLY));
}
END_SETUP()

FN_TEST(action_validation)
{
	TEST_SUCC(klogctl(LOG_OPEN, NULL, -1));
	TEST_SUCC(klogctl(LOG_CLOSE, NULL, -1));
	TEST_ERRNO(klogctl(-1, NULL, 0), EINVAL);
	TEST_ERRNO(klogctl(11, NULL, 0), EINVAL);
	for (int action = LOG_READ; action <= LOG_READ_CLEAR; action++) {
		TEST_ERRNO(klogctl(action, record, -1), EINVAL);
		TEST_ERRNO(klogctl(action, NULL, 0), EINVAL);
		TEST_RES(klogctl(action, record, 0), _ret == 0);
		TEST_ERRNO(klogctl(action, (char *)(uintptr_t)-1, 1), EFAULT);
	}
	TEST_ERRNO(klogctl(LOG_CONSOLE_LEVEL, NULL, 0), EINVAL);
	TEST_ERRNO(klogctl(LOG_CONSOLE_LEVEL, NULL, 9), EINVAL);
	TEST_SUCC(klogctl(LOG_CONSOLE_OFF, NULL, 0));
	TEST_SUCC(klogctl(LOG_CONSOLE_OFF, NULL, 0));
	TEST_SUCC(klogctl(LOG_CONSOLE_ON, NULL, 0));
}
END_TEST()

FN_TEST(independent_readers_and_priority)
{
	int first = reader_at_end();
	int second = reader_at_end();
	unsigned int priority;
	inject("<3>aster-klog-independent");
	unsigned long long first_seq =
		read_marker(first, "aster-klog-independent", &priority);
	TEST_RES(priority, _ret == 11);
	unsigned long long second_seq =
		read_marker(second, "aster-klog-independent", &priority);
	TEST_RES(first_seq == second_seq, _ret);
	int shared = CHECK(dup(first));
	inject("<165>aster-klog-facility\\\t");
	read_marker(shared, "aster-klog-facility", &priority);
	TEST_RES(priority, _ret == 165);
	TEST_RES(strstr(record, "\\x5c\\x09") != NULL, _ret);
	TEST_ERRNO(read(first, record, sizeof(record)), EAGAIN);
	CHECK(close(shared));
	CHECK(close(first));
	CHECK(close(second));
}
END_TEST()

FN_TEST(record_reads_and_seeks)
{
	int fd = reader_at_end();
	TEST_ERRNO(read(fd, record, sizeof(record)), EAGAIN);
	TEST_ERRNO(lseek(fd, 1, SEEK_SET), ESPIPE);
	TEST_ERRNO(lseek(fd, -1, SEEK_END), ESPIPE);
	TEST_ERRNO(lseek(fd, 0, SEEK_CUR), EINVAL);
	TEST_ERRNO(lseek(fd, 0, SEEK_HOLE), EINVAL);
	TEST_ERRNO(lseek(fd, 1, SEEK_HOLE), ESPIPE);
	inject("aster-klog-small-buffer");
	TEST_ERRNO(read(fd, record, 1), EINVAL);
	TEST_ERRNO(read(fd, record, sizeof(record)), EAGAIN);
	TEST_SUCC(lseek(fd, 0, SEEK_SET));
	TEST_RES(read(fd, record, sizeof(record)), _ret > 0);
	TEST_SUCC(lseek(fd, 0, SEEK_DATA));
	char oversized[1025] = {};
	TEST_ERRNO(write(writer_fd, oversized, sizeof(oversized)), EINVAL);
	CHECK(close(fd));
}
END_TEST()

FN_TEST(clear_preserves_device_records)
{
	int fd = reader_at_end();
	unsigned int priority;
	inject("aster-klog-before-clear");
	CHECK(klogctl(LOG_CLEAR, NULL, 0));
	inject("aster-klog-after-clear");
	read_marker(fd, "aster-klog-before-clear", &priority);
	CHECK(lseek(fd, 0, SEEK_DATA));
	read_marker(fd, "aster-klog-after-clear", &priority);
	ssize_t count =
		CHECK(klogctl(LOG_READ_ALL, snapshot, sizeof(snapshot) - 1));
	snapshot[count] = '\0';
	TEST_RES(strstr(snapshot, "aster-klog-before-clear") == NULL, _ret);
	TEST_RES(strstr(snapshot, "aster-klog-after-clear") != NULL, _ret);
	TEST_RES(klogctl(LOG_READ_ALL, record, 1), _ret == 0);
	CHECK(klogctl(LOG_READ_CLEAR, snapshot, sizeof(snapshot)));
	TEST_RES(klogctl(LOG_READ_ALL, snapshot, sizeof(snapshot)), _ret == 0);
	CHECK(close(fd));
}
END_TEST()

FN_TEST(destructive_cursor_is_separate)
{
	int unread;
	while ((unread = CHECK(klogctl(LOG_SIZE_UNREAD, NULL, 0))) > 0)
		CHECK(klogctl(LOG_READ, snapshot,
			      unread < (int)sizeof(snapshot) ?
				      unread :
				      (int)sizeof(snapshot)));
	inject("aster-klog-partial");
	unread = CHECK(klogctl(LOG_SIZE_UNREAD, NULL, 0));
	TEST_RES(klogctl(LOG_READ, record, 1), _ret == 1);
	TEST_RES(klogctl(LOG_SIZE_UNREAD, NULL, 0), _ret == unread - 1);
	CHECK(klogctl(LOG_CLEAR, NULL, 0));
	TEST_RES(klogctl(LOG_SIZE_UNREAD, NULL, 0), _ret == unread - 1);
	TEST_RES(klogctl(LOG_READ, record, sizeof(record)), _ret == unread - 1);
}
END_TEST()

FN_TEST(read_clear_fault_boundary)
{
	CHECK(klogctl(LOG_CLEAR, NULL, 0));
	inject("aster-klog-fault-older-abcdefghijklmnopqrstuvwxyz"
	       "abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyz");
	inject("aster-klog-fault-newer");
	void *unwritable = CHECK_WITH(mmap(NULL, 4096, PROT_NONE,
					   MAP_PRIVATE | MAP_ANONYMOUS, -1, 0),
				      _ret != MAP_FAILED);
	/* Only the newest record fits. A failed copy clears the skipped older
	 * records, but retains the record whose copy failed. */
	TEST_ERRNO(klogctl(LOG_READ_CLEAR, unwritable, 64), EFAULT);
	ssize_t count =
		CHECK(klogctl(LOG_READ_ALL, snapshot, sizeof(snapshot) - 1));
	snapshot[count] = '\0';
	TEST_RES(strstr(snapshot, "aster-klog-fault-older") == NULL, _ret);
	TEST_RES(strstr(snapshot, "aster-klog-fault-newer") != NULL, _ret);
	CHECK(munmap(unwritable, 4096));
}
END_TEST()

FN_TEST(overwritten_cursor)
{
	int fd = reader_at_end();
	/* The bounded Asterinas ring retains 256 whole records. */
	for (int i = 0; i < 300; i++)
		inject("aster-klog-overwrite");
	struct pollfd pfd = { .fd = fd, .events = POLLIN | POLLPRI };
	TEST_RES(poll(&pfd, 1, 0), _ret == 1);
	TEST_RES((pfd.revents & (POLLIN | POLLERR | POLLPRI)),
		 _ret == (POLLIN | POLLERR | POLLPRI));
	TEST_ERRNO(read(fd, record, sizeof(record)), EPIPE);
	TEST_RES(read(fd, record, sizeof(record)), _ret > 0);
	CHECK(close(fd));
}
END_TEST()

static volatile sig_atomic_t alarm_wakeup_fd = -1;

static void alarm_handler(int signal)
{
	(void)signal;
	if (alarm_wakeup_fd >= 0) {
		char ready = 1;
		ssize_t ignored = write(alarm_wakeup_fd, &ready, 1);
		(void)ignored;
	}
}

FN_TEST(blocking_wait_and_signal)
{
	int fd = reader_at_end();
	int ready_pipe[2];
	CHECK(pipe(ready_pipe));
	pid_t child = CHECK(fork());
	if (!child) {
		CHECK(close(ready_pipe[1]));
		char ready;
		CHECK_WITH(read(ready_pipe[0], &ready, 1), _ret == 1);
		inject("aster-klog-wakeup");
		CHECK(close(ready_pipe[0]));
		_exit(0);
	}
	CHECK(close(ready_pipe[0]));
	CHECK_WITH(write(ready_pipe[1], "x", 1), _ret == 1);
	CHECK(close(ready_pipe[1]));
	struct pollfd pfd = { .fd = fd, .events = POLLIN };
	TEST_RES(poll(&pfd, 1, 5000), _ret == 1);
	unsigned int priority;
	read_marker(fd, "aster-klog-wakeup", &priority);
	int status;
	CHECK(waitpid(child, &status, 0));
	TEST_RES(status, _ret == 0);
	CHECK(fcntl(fd, F_SETFL, 0));
	CHECK(lseek(fd, 0, SEEK_END));
	struct sigaction action = { .sa_handler = alarm_handler };
	CHECK(sigemptyset(&action.sa_mask));
	CHECK(sigaction(SIGALRM, &action, NULL));
	alarm(1);
	TEST_ERRNO(read(fd, record, sizeof(record)), EINTR);
	alarm(0);
	CHECK(close(fd));
	int unread;
	while ((unread = CHECK(klogctl(LOG_SIZE_UNREAD, NULL, 0))) > 0)
		CHECK(klogctl(LOG_READ, snapshot,
			      unread < (int)sizeof(snapshot) ?
				      unread :
				      (int)sizeof(snapshot)));
	alarm(1);
	TEST_ERRNO(klogctl(LOG_READ, record, sizeof(record)), EINTR);
	alarm(0);
	action.sa_flags = SA_RESTART;
	CHECK(sigaction(SIGALRM, &action, NULL));
	CHECK(pipe(ready_pipe));
	alarm_wakeup_fd = ready_pipe[1];
	child = CHECK(fork());
	if (!child) {
		CHECK(close(ready_pipe[1]));
		char ready;
		CHECK_WITH(read(ready_pipe[0], &ready, 1), _ret == 1);
		inject("aster-klog-syslog-wakeup");
		CHECK(close(ready_pipe[0]));
		_exit(0);
	}
	CHECK(close(ready_pipe[0]));
	alarm(1);
	int found = 0;
	for (int attempt = 0; attempt < 256 && !found; attempt++) {
		ssize_t count =
			CHECK(klogctl(LOG_READ, record, sizeof(record) - 1));
		record[count] = '\0';
		found = strstr(record, "aster-klog-syslog-wakeup") != NULL;
	}
	alarm(0);
	alarm_wakeup_fd = -1;
	CHECK(close(ready_pipe[1]));
	TEST_RES(found, _ret);
	CHECK(waitpid(child, &status, 0));
	TEST_RES(status, _ret == 0);
}
END_TEST()

FN_TEST(capture_level_retains_info)
{
	SKIP_TEST_IF(!syscall_info_diagnostics_enabled());
	int fd = reader_at_end();
	pid_t parent = getpid();
	pid_t child = CHECK(fork());
	if (!child)
		_exit(0);
	int status;
	CHECK(waitpid(child, &status, 0));
	TEST_RES(status, _ret == 0);
	char marker[128];
	CHECK_WITH(snprintf(marker, sizeof(marker),
			    "syscall_diag lifecycle=clone pid=%ld ",
			    (long)parent),
		   _ret > 0 && _ret < (int)sizeof(marker));
	unsigned int priority;
	read_marker(fd, marker, &priority);
	TEST_RES(priority, _ret == 6);
	CHECK(close(fd));
	puts("KLOG_CAPTURE_INFO_RETAINED=1");
}
END_TEST()

FN_TEST(restricted_readers)
{
	int fd = CHECK(open("/proc/sys/kernel/dmesg_restrict", O_RDWR));
	char saved[2];
	CHECK_WITH(read(fd, saved, sizeof(saved)), _ret == sizeof(saved));
	CHECK(lseek(fd, 0, SEEK_SET));
	CHECK_WITH(write(fd, "1\n", 2), _ret == 2);
	CHECK(lseek(fd, 0, SEEK_SET));
	TEST_RES(read(fd, record, sizeof(record)), _ret == 2);
	TEST_RES(memcmp(record, "1\n", 2), _ret == 0);
	TEST_ERRNO(pwrite(fd, "2\n", 2, 0), EINVAL);
	pid_t child = CHECK(fork());
	if (!child) {
		CHECK(setuid(65534));
		CHECK_WITH(klogctl(LOG_SIZE_BUFFER, NULL, 0),
			   _ret == -1 && errno == EPERM);
		CHECK_WITH(klogctl(11, NULL, 0), _ret == -1 && errno == EPERM);
		CHECK_WITH(open("/dev/kmsg", O_RDONLY),
			   _ret == -1 && errno == EPERM);
		_exit(0);
	}
	int status;
	CHECK(waitpid(child, &status, 0));
	TEST_RES(status, _ret == 0);
	CHECK_WITH(pwrite(fd, "0\n", 2, 0), _ret == 2);
	child = CHECK(fork());
	if (!child) {
		CHECK(setuid(65534));
		CHECK_WITH(klogctl(LOG_SIZE_BUFFER, NULL, 0), _ret > 0);
		CHECK(klogctl(LOG_READ_ALL, snapshot, sizeof(snapshot)));
		int readable = CHECK(open("/dev/kmsg", O_RDONLY | O_NONBLOCK));
		CHECK(close(readable));
		CHECK_WITH(klogctl(LOG_CLEAR, NULL, 0),
			   _ret == -1 && errno == EPERM);
		CHECK_WITH(klogctl(LOG_READ, record, -1),
			   _ret == -1 && errno == EPERM);
		CHECK_WITH(pwrite(fd, "1\n", 2, 0),
			   _ret == -1 && errno == EPERM);
		_exit(0);
	}
	CHECK(waitpid(child, &status, 0));
	TEST_RES(status, _ret == 0);
	CHECK_WITH(pwrite(fd, saved, sizeof(saved), 0), _ret == sizeof(saved));
	CHECK(close(fd));
	CHECK(close(writer_fd));
}
END_TEST()
