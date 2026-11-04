// SPDX-License-Identifier: MPL-2.0
// Syscall microbenchmark for the Asterinas RISC-V board.
//
// Self-contained (no libc extensions beyond POSIX), compiled on the board with
// `gcc -O2 -pthread`. Prints one `BENCH <name> <ns-per-op>` line per case so the
// output can be scraped over a serial console.

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#include <sys/syscall.h>

// musl's headers do not ship <linux/futex.h>; only this opcode is used.
#ifndef FUTEX_WAKE
#define FUTEX_WAKE 1
#endif

static int64_t elapsed_ns(struct timespec *a, struct timespec *b)
{
	return (b->tv_sec - a->tv_sec) * 1000000000LL + (b->tv_nsec - a->tv_nsec);
}

static void report(const char *name, int64_t total_ns, long iters)
{
	printf("BENCH %s %.1f\n", name, (double)total_ns / (double)iters);
	fflush(stdout);
}

#define BENCH(name, iters, body)                                            \
	do {                                                                \
		struct timespec t0, t1;                                     \
		long i;                                                     \
		clock_gettime(CLOCK_MONOTONIC, &t0);                        \
		for (i = 0; i < (iters); i++) {                             \
			body;                                               \
		}                                                           \
		clock_gettime(CLOCK_MONOTONIC, &t1);                        \
		report(name, elapsed_ns(&t0, &t1), (iters));                \
	} while (0)

static volatile long sink;

static void bench_getpid(void)
{
	BENCH("getpid", 2000000L, sink += getpid());
}

static void bench_getppid(void)
{
	BENCH("getppid", 2000000L, sink += getppid());
}

static void bench_clock_gettime(void)
{
	struct timespec ts;
	BENCH("clock_gettime", 2000000L, clock_gettime(CLOCK_MONOTONIC, &ts));
}

static void bench_open_close(void)
{
	BENCH("open+close", 200000L, {
		int fd = open("/etc/hostname", O_RDONLY);
		if (fd >= 0)
			close(fd);
	});
}

static void bench_stat(void)
{
	struct stat st;
	BENCH("stat", 200000L, stat("/etc/hostname", &st));
}

static void bench_pipe_rw(void)
{
	int fds[2];
	char buf[64] = { 0 };
	if (pipe(fds) < 0) {
		perror("pipe");
		return;
	}
	fcntl(fds[0], F_SETFL, O_NONBLOCK);
	fcntl(fds[1], F_SETFL, O_NONBLOCK);
	BENCH("pipe write+read 64B", 100000L, {
		if (write(fds[1], buf, sizeof(buf)) == (ssize_t)sizeof(buf))
			if (read(fds[0], buf, sizeof(buf)) != (ssize_t)sizeof(buf))
				sink++;
	});
	close(fds[0]);
	close(fds[1]);
}

static void bench_pipe_create(void)
{
	BENCH("pipe create+close", 100000L, {
		int fds[2];
		if (pipe(fds) == 0) {
			close(fds[0]);
			close(fds[1]);
		}
	});
}

static void bench_socket_create(void)
{
	BENCH("unix socket create+close", 100000L, {
		int fd = socket(AF_UNIX, SOCK_STREAM, 0);
		if (fd >= 0)
			close(fd);
	});
}

static int futex_word;

static void bench_futex(void)
{
	BENCH("futex wait(0)/wake", 100000L, {
		syscall(SYS_futex, &futex_word, FUTEX_WAKE, 1, NULL, NULL, 0);
	});
}

static void bench_fork(void)
{
	BENCH("fork+wait", 2000L, {
		pid_t pid = fork();
		if (pid == 0)
			_exit(0);
		if (pid > 0)
			waitpid(pid, NULL, 0);
	});
}

int main(void)
{
	setvbuf(stdout, NULL, _IONBF, 0);
	printf("BENCH_BEGIN pid=%d\n", (int)getpid());

	bench_getpid();
	bench_getppid();
	bench_clock_gettime();
	bench_open_close();
	bench_stat();
	bench_pipe_rw();
	bench_pipe_create();
	bench_socket_create();
	bench_futex();
	bench_fork();

	printf("BENCH_END sink=%ld\n", sink);
	return 0;
}
