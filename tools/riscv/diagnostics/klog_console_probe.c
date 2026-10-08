// SPDX-License-Identifier: MPL-2.0

/* Isolated micro-guest probe; requires boot loglevel=off. After setting level
 * 8, Linux syslog cannot restore the original level 0: its OFF action selects
 * the emergency-only threshold 1. The probe explicitly finishes with that
 * action. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/klog.h>
#include <sys/syscall.h>
#include <unistd.h>

enum { CONSOLE_OFF = 6, CONSOLE_ON = 7, CONSOLE_LEVEL = 8 };

static void require(int condition, const char *operation) {
  if (!condition) {
    fprintf(stderr, "KLOG_CONSOLE_FAIL operation=%s errno=%d\n", operation,
            errno);
    exit(EXIT_FAILURE);
  }
}

static void finish_console_off(void) { (void)klogctl(CONSOLE_OFF, NULL, 0); }

static void retained(int reader, const char *message, unsigned int priority) {
  char record[8192];
  for (int attempt = 0; attempt < 256; attempt++) {
    ssize_t count = read(reader, record, sizeof(record) - 1);
    require(count > 0, "read retained record");
    record[count] = '\0';
    if (!strstr(record, message))
      continue;
    unsigned int actual;
    require(sscanf(record, "%u,", &actual) == 1 && actual == priority,
            "retained record facility and severity");
    return;
  }
  require(0, "retained record bound");
}

static void inject(int writer, int reader, const char *message) {
  char input[128];
  int size = snprintf(input, sizeof(input), "<4>%s", message);
  require(size > 0 && (size_t)size < sizeof(input), "injected message bound");
  require(write(writer, input, size) == size, "inject user record");
  retained(reader, message, 12);
}

int main(void) {
  setvbuf(stdout, NULL, _IOLBF, 0);
  int reader = open("/dev/kmsg", O_RDONLY | O_NONBLOCK);
  int writer = open("/dev/kmsg", O_WRONLY);
  require(reader >= 0 && writer >= 0, "open log");
  require(lseek(reader, 0, SEEK_END) == 0, "seek log tail");
  require(atexit(finish_console_off) == 0, "register cleanup");
  puts("KLOG_CONSOLE_BEGIN");
  require(klogctl(CONSOLE_OFF, NULL, 0) == 0, "disable console");
  puts("KLOG_CONSOLE_PHASE off_begin");
  errno = 0;
  require(syscall(0x7fffffffL) == -1 && errno == ENOSYS,
          "trigger kernel warning");
  /* Exact OSTD warning in kernel/src/syscall/mod.rs dispatch fallback. */
  retained(reader, "Unimplemented syscall number: 2147483647", 4);
  inject(writer, reader, "aster-klog-console-hidden");
  puts("KLOG_CONSOLE_PHASE off_end retained_kernel=1 retained_user=1");
  require(klogctl(CONSOLE_ON, NULL, 0) == 0, "restore boot console level");
  puts("KLOG_CONSOLE_PHASE boot_restored_begin");
  inject(writer, reader, "aster-klog-console-hidden-restored");
  puts("KLOG_CONSOLE_PHASE boot_restored_end retained_user=1");
  require(klogctl(CONSOLE_LEVEL, NULL, 8) == 0, "set console level eight");
  puts("KLOG_CONSOLE_PHASE visible_begin");
  inject(writer, reader, "aster-klog-console-visible");
  puts("KLOG_CONSOLE_PHASE visible_end retained_user=1");
  require(klogctl(CONSOLE_OFF, NULL, 0) == 0, "save level eight and disable");
  puts("KLOG_CONSOLE_PHASE disabled_begin");
  inject(writer, reader, "aster-klog-console-hidden-disabled");
  puts("KLOG_CONSOLE_PHASE disabled_end retained_user=1");
  require(klogctl(CONSOLE_ON, NULL, 0) == 0, "restore level eight");
  puts("KLOG_CONSOLE_PHASE reenabled_begin");
  inject(writer, reader, "aster-klog-console-visible-reenabled");
  puts("KLOG_CONSOLE_PHASE reenabled_end retained_user=1");
  require(klogctl(CONSOLE_OFF, NULL, 0) == 0, "finish console off");
  require(close(reader) == 0 && close(writer) == 0, "close log");
  puts("KLOG_CONSOLE_DONE final_console=off_emergency_only retained=6");
  puts("KLOG_CONSOLE_END");
  puts("KLOG_CONSOLE_CAPTURE kernel=1 hidden=1 visible=1");
  return EXIT_SUCCESS;
}
