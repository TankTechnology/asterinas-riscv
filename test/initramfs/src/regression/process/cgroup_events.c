// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE
#include "../common/test.h"

#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <time.h>
#include <sys/inotify.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

static char base[128], leaf[160], sibling[160], alias[128];
static int notify_fd = -1, watch[3];
static pid_t children[2];
static int release_fd[2] = { -1, -1 };
static int alias_mounted;

static void cleanup(void)
{
	for (int i = 0; i < 2; i++) {
		if (children[i] > 0) {
			kill(children[i], SIGKILL);
			waitpid(children[i], NULL, 0);
		}
		if (release_fd[i] >= 0)
			close(release_fd[i]);
	}
	if (notify_fd >= 0)
		close(notify_fd);
	if (alias_mounted)
		umount(alias);
	rmdir(alias);
	rmdir(leaf);
	rmdir(sibling);
	rmdir(base);
}

static void attribute(char path[256], const char *group, const char *name)
{
	CHECK_WITH(snprintf(path, 256, "%s/%s", group, name), _ret < 256);
}

static void expect_populated(const char *group, int populated)
{
	char path[256], content[128], expected[32];
	attribute(path, group, "cgroup.events");
	int fd = CHECK(open(path, O_RDONLY));
	int count = CHECK(read(fd, content, sizeof(content) - 1));
	content[count] = 0;
	CHECK(close(fd));
	snprintf(expected, sizeof(expected), "populated %d\n", populated);
	CHECK_WITH(strstr(content, expected) != NULL, _ret);
}

static void move_child(int index, const char *group)
{
	char path[256], pid[32];
	attribute(path, group, "cgroup.procs");
	int fd = CHECK(open(path, O_WRONLY));
	int len = snprintf(pid, sizeof(pid), "%d", children[index]);
	CHECK_WITH(write(fd, pid, len), _ret == len);
	CHECK(close(fd));
}

static void spawn_child(int index)
{
	int pipefd[2];
	CHECK(pipe(pipefd));
	children[index] = CHECK(fork());
	if (children[index] == 0) {
		char byte;
		close(pipefd[1]);
		_exit(read(pipefd[0], &byte, 1) == 1 ? 0 : 1);
	}
	CHECK(close(pipefd[0]));
	release_fd[index] = pipefd[1];
}

static void finish_child(int index)
{
	int status;
	CHECK_WITH(write(release_fd[index], "x", 1), _ret == 1);
	CHECK(close(release_fd[index]));
	release_fd[index] = -1;
	CHECK_WITH(waitpid(children[index], &status, 0),
		   _ret == children[index] && WIFEXITED(status) &&
			   WEXITSTATUS(status) == 0);
	children[index] = 0;
}

static int64_t monotonic_ms(void)
{
	struct timespec now;
	CHECK(clock_gettime(CLOCK_MONOTONIC, &now));
	return (int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}

/* Events may arrive separately. Collect the expected nodes by one deadline. */
static void expect_events(unsigned int expected, uint32_t mask)
{
	struct pollfd pollfd = { .fd = notify_fd, .events = POLLIN };
	char buffer[4096] __attribute__((aligned(8)));
	unsigned int seen = 0;
	int64_t deadline = monotonic_ms() + 250;
	while (seen != expected) {
		int64_t remaining = deadline - monotonic_ms();
		CHECK_WITH(remaining, _ret > 0);
		CHECK_WITH(poll(&pollfd, 1, remaining), _ret == 1);
		ssize_t count = read(notify_fd, buffer, sizeof(buffer));
		if (count < 0 && errno == EAGAIN)
			continue;
		CHECK_WITH(count, _ret > 0);
		for (size_t offset = 0; offset < (size_t)count;) {
			struct inotify_event *event = (void *)(buffer + offset);
			unsigned int node = 0;
			for (int i = 0; i < 3; i++)
				if (event->wd == watch[i])
					node = 1U << i;
			CHECK_WITH(node, _ret && !(_ret & ~expected));
			if (event->mask & mask)
				seen |= node;
			offset += sizeof(*event) + event->len;
		}
	}
}

FN_TEST(cgroup_populated_notifications)
{
	char path[256], alias_path[256];
	const char *groups[] = { base, leaf, sibling };
	snprintf(base, sizeof(base), "/sys/fs/cgroup/events-test-%d", getpid());
	snprintf(leaf, sizeof(leaf), "%s/leaf", base);
	snprintf(sibling, sizeof(sibling), "%s/sibling", base);
	snprintf(alias, sizeof(alias), "/tmp/cgroup-events-mount-%d", getpid());
	CHECK(atexit(cleanup));
	CHECK(mkdir(base, 0755));
	CHECK(mkdir(leaf, 0755));
	CHECK(mkdir(sibling, 0755));
	notify_fd = CHECK(inotify_init1(IN_NONBLOCK | IN_CLOEXEC));
	for (int i = 0; i < 3; i++) {
		attribute(path, groups[i], "cgroup.events");
		watch[i] = CHECK(inotify_add_watch(notify_fd, path,
						   IN_MODIFY | IN_DELETE_SELF));
		expect_populated(groups[i], 0);
		TEST_RES(inotify_add_watch(notify_fd, path,
					   IN_MODIFY | IN_DELETE_SELF),
			 _ret == watch[i]);
	}

	/* A second cgroupfs mount must expose the same underlying events inode. */
	CHECK(mkdir(alias, 0755));
	CHECK(mount("none", alias, "cgroup2", MS_NOSUID | MS_NODEV | MS_NOEXEC,
		    NULL));
	alias_mounted = 1;
	CHECK_WITH(snprintf(alias_path, sizeof(alias_path),
			    "%s/events-test-%d/leaf/cgroup.events", alias,
			    getpid()),
		   _ret < (int)sizeof(alias_path));
	TEST_RES(inotify_add_watch(notify_fd, alias_path,
				   IN_MODIFY | IN_DELETE_SELF),
		 _ret == watch[1]);
	CHECK(umount(alias));
	alias_mounted = 0;

	spawn_child(0);
	spawn_child(1);
	move_child(0, leaf);
	expect_events(3, IN_MODIFY);
	expect_populated(base, 1);
	expect_populated(leaf, 1);
	move_child(1, sibling);
	expect_events(4, IN_MODIFY);
	finish_child(0);
	expect_events(2, IN_MODIFY);
	expect_populated(base, 1);
	expect_populated(leaf, 0);
	finish_child(1);
	expect_events(5, IN_MODIFY);
	expect_populated(base, 0);
	expect_populated(sibling, 0);

	/* Explicit removal and implicit attribute deletion must retire watches. */
	CHECK(inotify_rm_watch(notify_fd, watch[2]));
	expect_events(4, IN_IGNORED);
	CHECK(rmdir(leaf));
	expect_events(2, IN_IGNORED);
	CHECK(mkdir(leaf, 0755));
	attribute(path, leaf, "cgroup.events");
	int replacement = CHECK(inotify_add_watch(notify_fd, path, IN_MODIFY));
	TEST_RES(replacement, _ret != watch[1]);
	watch[1] = replacement;
	spawn_child(0);
	move_child(0, leaf);
	expect_events(3, IN_MODIFY);
	finish_child(0);
	expect_events(3, IN_MODIFY);
	cleanup();
	/* The registered cleanup is idempotent. */
	notify_fd = -1;
}
END_TEST()
