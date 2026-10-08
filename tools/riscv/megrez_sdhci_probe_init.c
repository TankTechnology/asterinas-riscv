// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE

// clang-format off
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mount.h>
#include <linux/fs.h>
#include <sys/reboot.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
// clang-format on

#ifndef EXPECTED_CRC32
#error "EXPECTED_CRC32 must bind the probe to a U-Boot baseline"
#endif

#define PROBE_BYTES (32ULL * 1024ULL * 1024ULL)
#define CHUNK_BYTES (1024U * 1024U)

static uint32_t crc32_table[256];

static void initialize_crc32(void) {
  for (uint32_t index = 0; index < 256; ++index) {
    uint32_t value = index;
    for (unsigned int bit = 0; bit < 8; ++bit) {
      value = (value >> 1) ^ ((value & 1U) ? 0xedb88320U : 0U);
    }
    crc32_table[index] = value;
  }
}

static uint32_t update_crc32(uint32_t crc, const unsigned char *data,
                             size_t length) {
  for (size_t index = 0; index < length; ++index) {
    crc = crc32_table[(crc ^ data[index]) & 0xffU] ^ (crc >> 8);
  }
  return crc;
}

#ifdef SDHCI_PROBE_SELF_TEST

int main(void) {
  static const unsigned char vector[] = "123456789";
  initialize_crc32();
  uint32_t crc =
      update_crc32(0xffffffffU, vector, sizeof(vector) - 1) ^ 0xffffffffU;
  if (crc != 0xcbf43926U) {
    return 1;
  }
  puts("SDHCI_PROBE_SELF_TEST_PASS");
  return 0;
}

#else

static unsigned char transfer_buffer[CHUNK_BYTES];
static int marker_fd = STDOUT_FILENO;

static void emit_marker(const char *format, ...) {
  va_list arguments;
  va_start(arguments, format);
  vdprintf(marker_fd, format, arguments);
  va_end(arguments);
  dprintf(marker_fd, "\n");
}

static _Noreturn void hold(void) {
  for (;;) {
    pause();
  }
}

static _Noreturn void fail_and_reboot(const char *reason) {
  emit_marker("MEGREZ_SDHCI_READ_FAIL reason=%s", reason);
  sync();
  reboot(RB_AUTOBOOT);
  hold();
}

static bool make_directory(const char *path) {
  return mkdir(path, 0755) == 0 || errno == EEXIST;
}

static bool read_exact_at(int fd, unsigned char *buffer, size_t length,
                          off_t offset) {
  size_t completed = 0;
  while (completed < length) {
    ssize_t count = pread(fd, buffer + completed, length - completed,
                          offset + (off_t)completed);
    if (count > 0) {
      completed += (size_t)count;
      continue;
    }
    if (count < 0 && errno == EINTR) {
      continue;
    }
    return false;
  }
  return true;
}

int main(void) {
  if (!make_directory("/dev")) {
    fail_and_reboot("dev-directory");
  }
  // Asterinas mounts and populates a ramfs at /dev before exec. It does not
  // register a Linux "devtmpfs" filesystem type, so this compatibility
  // attempt is deliberately best-effort.
  (void)mount("devtmpfs", "/dev", "devtmpfs", 0, NULL);

  int console = open("/dev/ttyS0", O_WRONLY | O_CLOEXEC | O_NOFOLLOW);
  if (console >= 0) {
    marker_fd = console;
  }

  int device = open("/dev/mmcblk0", O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
  if (device < 0) {
    fail_and_reboot("target-open");
  }
  struct stat device_info;
  if (fstat(device, &device_info) != 0 || !S_ISBLK(device_info.st_mode)) {
    fail_and_reboot("target-not-block-device");
  }
  uint64_t device_bytes = 0;
  if (ioctl(device, BLKGETSIZE64, &device_bytes) != 0 ||
      device_bytes < PROBE_BYTES) {
    fail_and_reboot("target-too-small");
  }

  struct timespec start;
  struct timespec end;
  if (clock_gettime(CLOCK_MONOTONIC, &start) != 0) {
    fail_and_reboot("clock-start");
  }
  emit_marker("MEGREZ_SDHCI_READ_START bytes=%" PRIu64 " uptime=%" PRIu64
              ".%09ld",
              (uint64_t)PROBE_BYTES, (uint64_t)start.tv_sec, start.tv_nsec);

  initialize_crc32();
  uint32_t crc = 0xffffffffU;
  for (uint64_t offset = 0; offset < PROBE_BYTES; offset += CHUNK_BYTES) {
    if (!read_exact_at(device, transfer_buffer, CHUNK_BYTES, (off_t)offset)) {
      fail_and_reboot("read");
    }
    crc = update_crc32(crc, transfer_buffer, CHUNK_BYTES);
  }
  crc ^= 0xffffffffU;
  if (crc != (uint32_t)EXPECTED_CRC32) {
    emit_marker(
        "MEGREZ_SDHCI_READ_FAIL reason=crc-mismatch expected=%08x actual=%08x",
        (uint32_t)EXPECTED_CRC32, crc);
    sync();
    reboot(RB_AUTOBOOT);
    hold();
  }
  if (clock_gettime(CLOCK_MONOTONIC, &end) != 0) {
    fail_and_reboot("clock-end");
  }
  emit_marker("MEGREZ_SDHCI_READ_PASS bytes=%" PRIu64
              " crc32=%08x start=%" PRIu64 ".%09ld end=%" PRIu64 ".%09ld",
              (uint64_t)PROBE_BYTES, crc, (uint64_t)start.tv_sec, start.tv_nsec,
              (uint64_t)end.tv_sec, end.tv_nsec);

  close(device);
  sync();
  reboot(RB_AUTOBOOT);
  fail_and_reboot("reboot-returned");
}

#endif
