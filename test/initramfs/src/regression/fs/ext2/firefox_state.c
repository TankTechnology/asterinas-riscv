// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#define BASE_DIR "/ext2/asterinas_firefox_state"
#define CACHE_PATH BASE_DIR "/cache-entry"
#define CYCLES 16
#define PAYLOAD_BYTES 16384
#define PREFERENCES_PATH BASE_DIR "/prefs.js"
#define TEMPORARY_PATH BASE_DIR "/prefs.js.tmp"
#define WAL_PATH BASE_DIR "/places.sqlite-wal"
#define MMAP_PATH BASE_DIR "/mapped-state"
#define MMAP_SIZE 4096

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
	unlink_optional(MMAP_PATH);
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

	int mapped_fd = open(MMAP_PATH, O_CREAT | O_RDWR | O_TRUNC, 0644);
	if (mapped_fd < 0)
		fail("open mapped state");
	if (ftruncate(mapped_fd, MMAP_SIZE) < 0)
		fail("truncate mapped state");
	char *mapped = mmap(NULL, MMAP_SIZE, PROT_READ | PROT_WRITE,
			    MAP_SHARED, mapped_fd, 0);
	if (mapped == MAP_FAILED)
		fail("mmap state");
	volatile char initial_byte = mapped[0];
	(void)initial_byte;
	memcpy(mapped + 128, "mmap-persist-v1", sizeof("mmap-persist-v1"));
	if (msync(mapped, MMAP_SIZE, MS_SYNC) < 0)
		fail("msync state");

	char *direct_buf = aligned_alloc(MMAP_SIZE, MMAP_SIZE);
	if (direct_buf == NULL)
		fail("allocate direct buffer");
	int direct_fd = open(MMAP_PATH, O_RDONLY | O_DIRECT);
	if (direct_fd < 0)
		fail("open direct state");
	if (pread(direct_fd, direct_buf, MMAP_SIZE, 0) != MMAP_SIZE)
		fail("read direct state");
	if (memcmp(direct_buf + 128, "mmap-persist-v1",
		   sizeof("mmap-persist-v1")) != 0) {
		fputs("ASTERINAS_EXT2_MMAP_PERSIST_ERROR disk_data_mismatch\n", stderr);
		exit(EXIT_FAILURE);
	}
	if (pwrite(mapped_fd, "buffer", sizeof("buffer"), 64) !=
	    sizeof("buffer"))
		fail("buffered state write");
	if (memcmp(mapped + 128, "mmap-persist-v1",
		   sizeof("mmap-persist-v1")) != 0) {
		fputs("ASTERINAS_EXT2_MMAP_PERSIST_ERROR buffered_write_clobbered_mapping\n", stderr);
		exit(EXIT_FAILURE);
	}
	mapped[256] = 'Q';
	if (msync(mapped, MMAP_SIZE, MS_SYNC) < 0)
		fail("msync state again");
	if (pread(direct_fd, direct_buf, MMAP_SIZE, 0) != MMAP_SIZE)
		fail("read direct state again");
	if (direct_buf[256] != 'Q') {
		fputs("ASTERINAS_EXT2_MMAP_PERSIST_ERROR second_write_missing\n", stderr);
		exit(EXIT_FAILURE);
	}
	if (memcmp(direct_buf + 64, "buffer", sizeof("buffer")) != 0)
		fail("buffered state missing");
	if (ftruncate(mapped_fd, MMAP_SIZE * 2) < 0)
		fail("extend mapped state");
	char *sparse = mmap(NULL, MMAP_SIZE, PROT_READ | PROT_WRITE,
			    MAP_SHARED, mapped_fd, MMAP_SIZE);
	if (sparse == MAP_FAILED)
		fail("mmap sparse state");
	sparse[200] = 'S';
	if (pwrite(mapped_fd, "buffer2", sizeof("buffer2"),
		   MMAP_SIZE + 100) != sizeof("buffer2"))
		fail("buffered sparse write");
	if (sparse[200] != 'S') {
		fputs("ASTERINAS_EXT2_MMAP_PERSIST_ERROR sparse_tail_clobbered\n", stderr);
		exit(EXIT_FAILURE);
	}
	if (msync(sparse, MMAP_SIZE, MS_SYNC) < 0)
		fail("msync sparse state");
	if (pread(direct_fd, direct_buf, MMAP_SIZE, MMAP_SIZE) != MMAP_SIZE)
		fail("read direct sparse state");
	if (direct_buf[200] != 'S' ||
	    memcmp(direct_buf + 100, "buffer2", sizeof("buffer2")) != 0) {
		fputs("ASTERINAS_EXT2_MMAP_PERSIST_ERROR sparse_disk_data_mismatch\n", stderr);
		exit(EXIT_FAILURE);
	}
	if (munmap(sparse, MMAP_SIZE) < 0)
		fail("munmap sparse state");
	free(direct_buf);
	if (close(direct_fd) < 0)
		fail("close direct state");
	if (munmap(mapped, MMAP_SIZE) < 0)
		fail("munmap state");
	if (close(mapped_fd) < 0)
		fail("close mapped state");
	unlink_optional(MMAP_PATH);

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
