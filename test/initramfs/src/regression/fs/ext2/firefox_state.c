// SPDX-License-Identifier: MPL-2.0

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define BASE_DIR "/ext2/asterinas_firefox_state"
#define CACHE_PATH BASE_DIR "/cache-entry"
#define CYCLES 16
#define PAYLOAD_BYTES 16384
#define PREFERENCES_PATH BASE_DIR "/prefs.js"
#define TEMPORARY_PATH BASE_DIR "/prefs.js.tmp"
#define WAL_PATH BASE_DIR "/places.sqlite-wal"

static void fail(const char *operation)
{
	perror(operation);
	exit(EXIT_FAILURE);
}

static void unlink_optional(const char *path)
{
	if (unlink(path) < 0 && errno != ENOENT)
		fail("unlink");
}

static void write_all(int file_descriptor, const void *payload, size_t length)
{
	const unsigned char *next = payload;

	while (length > 0) {
		ssize_t written = write(file_descriptor, next, length);
		if (written < 0) {
			if (errno == EINTR)
				continue;
			fail("write");
		}
		if (written == 0)
			fail("short write");
		next += written;
		length -= (size_t)written;
	}
}

static void write_and_sync(const char *path, const void *payload, size_t length,
			   int flags)
{
	int file_descriptor = open(path, flags, 0644);
	if (file_descriptor < 0)
		fail("open");
	write_all(file_descriptor, payload, length);
	if (fsync(file_descriptor) < 0)
		fail("fsync file");
	if (close(file_descriptor) < 0)
		fail("close");
}

int main(void)
{
	unsigned char payload[PAYLOAD_BYTES];

	setvbuf(stdout, NULL, _IOLBF, 0);
	unlink_optional(TEMPORARY_PATH);
	unlink_optional(PREFERENCES_PATH);
	unlink_optional(WAL_PATH);
	unlink_optional(CACHE_PATH);
	if (rmdir(BASE_DIR) < 0 && errno != ENOENT)
		fail("rmdir stale profile");
	if (mkdir(BASE_DIR, 0755) < 0)
		fail("mkdir profile");

	int directory_fd = open(BASE_DIR, O_RDONLY | O_DIRECTORY);
	if (directory_fd < 0)
		fail("open profile directory");

	for (int cycle = 0; cycle < CYCLES; ++cycle) {
		for (size_t index = 0; index < sizeof(payload); ++index)
			payload[index] = (unsigned char)((cycle + index) % 251);

		write_and_sync(TEMPORARY_PATH, payload, sizeof(payload),
			       O_CREAT | O_WRONLY | O_TRUNC);
		if (rename(TEMPORARY_PATH, PREFERENCES_PATH) < 0)
			fail("rename preferences");
		if (fsync(directory_fd) < 0)
			fail("fsync directory");

		write_and_sync(WAL_PATH, payload, 4096,
			       O_CREAT | O_WRONLY | O_APPEND);
		write_and_sync(CACHE_PATH, payload, 1024,
			       O_CREAT | O_WRONLY | O_TRUNC);
		unlink_optional(CACHE_PATH);
	}

	unlink_optional(PREFERENCES_PATH);
	unlink_optional(WAL_PATH);
	if (fsync(directory_fd) < 0)
		fail("fsync final directory");
	if (close(directory_fd) < 0)
		fail("close profile directory");
	if (rmdir(BASE_DIR) < 0)
		fail("rmdir profile");
	sync();
	printf("ASTERINAS_EXT2_FIREFOX_WORKLOAD_OK cycles=%d\n", CYCLES);
	return EXIT_SUCCESS;
}
