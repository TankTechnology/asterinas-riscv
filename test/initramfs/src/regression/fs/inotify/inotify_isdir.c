// SPDX-License-Identifier: MPL-2.0

#include <errno.h>
#include <fcntl.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/inotify.h>
#include <sys/stat.h>
#include <unistd.h>

#define TEST_DIR_TEMPLATE "/tmp/inotify_isdir.XXXXXX"
#define TEST_CHILD "child"

static int fail(const char *message)
{
	fprintf(stderr, "%s: %s\n", message, strerror(errno));
	return 1;
}

int main(void)
{
	char buffer[sizeof(struct inotify_event) + 32]
		__attribute__((aligned(__alignof__(struct inotify_event))));
	char directory[] = TEST_DIR_TEMPLATE;
	char child_path[sizeof(TEST_DIR_TEMPLATE) + sizeof(TEST_CHILD)];
	int inotify_fd = -1;
	int result = 1;

	if (mkdtemp(directory) == NULL)
		return fail("mkdtemp watch directory");
	if (snprintf(child_path, sizeof(child_path), "%s/%s", directory,
		     TEST_CHILD) < 0)
		goto out;

	inotify_fd = inotify_init1(IN_CLOEXEC);
	if (inotify_fd < 0) {
		result = fail("inotify_init1");
		goto out;
	}

	int watch_descriptor =
		inotify_add_watch(inotify_fd, directory, IN_CREATE | IN_ISDIR);
	if (watch_descriptor < 0) {
		result = fail("inotify_add_watch(IN_CREATE | IN_ISDIR)");
		goto out;
	}

	if (mkdir(child_path, 0700) < 0) {
		result = fail("mkdir watched child");
		goto out;
	}

	ssize_t bytes_read = read(inotify_fd, buffer, sizeof(buffer));
	if (bytes_read < (ssize_t)sizeof(struct inotify_event)) {
		result = fail("read inotify event");
		goto out;
	}

	struct inotify_event *event = (struct inotify_event *)buffer;
	bool is_expected_event = event->wd == watch_descriptor &&
				 (event->mask & (IN_CREATE | IN_ISDIR)) ==
					 (IN_CREATE | IN_ISDIR) &&
				 event->len > 0 &&
				 strcmp(event->name, TEST_CHILD) == 0;
	if (!is_expected_event) {
		fprintf(stderr,
			"unexpected inotify event: wd=%d mask=0x%x name=%s\n",
			event->wd, event->mask,
			event->len > 0 ? event->name : "<none>");
		goto out;
	}

	result = 0;

out:
	if (inotify_fd >= 0)
		close(inotify_fd);
	rmdir(child_path);
	rmdir(directory);
	return result;
}
