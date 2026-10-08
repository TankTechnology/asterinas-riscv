// SPDX-License-Identifier: MPL-2.0

#include <errno.h>
#include <fcntl.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/mount.h>
#include <unistd.h>

#define MOUNT_DIR_TEMPLATE "/tmp/mountinfo_poll.XXXXXX"

static int fail(const char *message)
{
	fprintf(stderr, "%s: %s\n", message, strerror(errno));
	return 1;
}

static bool mountinfo_contains(int mountinfo_fd, const char *mountpoint)
{
	char buffer[16384];
	ssize_t bytes_read;

	if (lseek(mountinfo_fd, 0, SEEK_SET) < 0)
		return false;
	bytes_read = read(mountinfo_fd, buffer, sizeof(buffer) - 1);
	if (bytes_read < 0)
		return false;
	buffer[bytes_read] = '\0';
	return strstr(buffer, mountpoint) != NULL;
}

int main(void)
{
	char mountpoint[] = MOUNT_DIR_TEMPLATE;
	struct epoll_event event = { .events = EPOLLIN | EPOLLET };
	struct epoll_event observed = { 0 };
	int epoll_fd = -1;
	int mountinfo_fd = -1;
	int result = 1;
	bool is_mounted = false;

	if (mkdtemp(mountpoint) == NULL)
		return fail("mkdtemp mountpoint");

	mountinfo_fd = open("/proc/self/mountinfo", O_RDONLY | O_CLOEXEC);
	if (mountinfo_fd < 0) {
		result = fail("open mountinfo");
		goto out;
	}
	event.data.fd = mountinfo_fd;

	epoll_fd = epoll_create1(EPOLL_CLOEXEC);
	if (epoll_fd < 0 ||
	    epoll_ctl(epoll_fd, EPOLL_CTL_ADD, mountinfo_fd, &event) < 0) {
		result = fail("register mountinfo with epoll");
		goto out;
	}

	if (epoll_wait(epoll_fd, &observed, 1, 0) != 1) {
		errno = EIO;
		result = fail("drain initial mountinfo readiness");
		goto out;
	}

	if (mount("tmpfs", mountpoint, "tmpfs", 0, "size=4096") < 0) {
		result = fail("mount tmpfs");
		goto out;
	}
	is_mounted = true;

	if (epoll_wait(epoll_fd, &observed, 1, 1000) != 1 ||
	    !(observed.events & EPOLLERR)) {
		errno = ETIMEDOUT;
		result = fail("wait for mountinfo topology change");
		goto out;
	}
	if (!mountinfo_contains(mountinfo_fd, mountpoint)) {
		errno = ENOENT;
		result = fail("find new mount in mountinfo");
		goto out;
	}

	result = 0;

out:
	if (is_mounted && umount(mountpoint) < 0)
		result = fail("unmount tmpfs");
	if (epoll_fd >= 0)
		close(epoll_fd);
	if (mountinfo_fd >= 0)
		close(mountinfo_fd);
	rmdir(mountpoint);
	return result;
}
