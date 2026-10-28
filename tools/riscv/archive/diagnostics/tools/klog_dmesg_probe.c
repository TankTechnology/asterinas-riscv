// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static long long monotonic_ms(void) {
  struct timespec now;
  if (clock_gettime(CLOCK_MONOTONIC, &now) < 0) {
    perror("clock_gettime");
    exit(EXIT_FAILURE);
  }
  return (long long)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}

static void last_record(char *out, size_t out_size, const char *data,
                        size_t size) {
  while (size && (data[size - 1] == '\n' || data[size - 1] == '\r'))
    size--;
  size_t start = size;
  while (start && data[start - 1] != '\n')
    start--;
  if (size - start >= out_size)
    start = size - out_size + 1;
  size_t len = size - start;
  for (size_t i = 0; i < len; i++) {
    unsigned char byte = data[start + i];
    out[i] = byte >= ' ' && byte < 127 ? byte : '.';
  }
  out[len] = '\0';
}

/* Two acknowledged messages prove that the same unmodified dmesg keeps reading.
 * No sleep is used to guess whether the child has reached a blocking read.
 * Pipe reads can contain any number of partial or complete lines: a read-count
 * limit cannot bound the time needed to drain the retained kernel records. */
int main(void) {
  int log_fd = open("/dev/kmsg", O_WRONLY);
  int output[2];
  if (log_fd < 0 || pipe(output) < 0) {
    perror("klog dmesg setup");
    return 1;
  }
  pid_t child = fork();
  if (child < 0) {
    perror("fork");
    return 1;
  }
  if (child == 0) {
    close(output[0]);
    close(log_fd);
    if (dup2(output[1], STDOUT_FILENO) < 0)
      _exit(126);
    close(output[1]);
    execl("/dmesg", "dmesg", "--follow-new", "--raw", NULL);
    perror("exec dmesg");
    _exit(127);
  }
  close(output[1]);
  int passed = 1;
  for (int stage = 0; stage < 2 && passed; stage++) {
    char marker[96], data[16384];
    char last[192] = "";
    snprintf(marker, sizeof(marker), "aster-dmesg-follow-%ld-%d",
             (long)getpid(), stage);
    size_t retained = 0;
    size_t reads = 0, bytes = 0;
    size_t injections = 0;
    int found = 0;
    short last_events = 0;
    const char *reason = "deadline";
    long long started = monotonic_ms();
    long long deadline = started + 5000;
    while (monotonic_ms() < deadline) {
      if (write(log_fd, marker, strlen(marker)) != (ssize_t)strlen(marker)) {
        perror("inject follow marker");
        reason = "write_error";
        passed = 0;
        break;
      }
      injections++;
      struct pollfd wait_for = {.fd = output[0], .events = POLLIN};
      long long remaining = deadline - monotonic_ms();
      if (remaining <= 0)
        break;
      int wait_ms = remaining < 100 ? (int)remaining : 100;
      int ready = poll(&wait_for, 1, wait_ms);
      last_events = wait_for.revents;
      if (ready < 0 && errno == EINTR)
        continue;
      if (ready <= 0) {
        if (ready < 0) {
          reason = "poll_error";
          perror("poll dmesg output");
          break;
        }
        continue;
      }
      ssize_t count =
          read(output[0], data + retained, sizeof(data) - retained - 1);
      if (count < 0 && errno == EINTR)
        continue;
      if (count <= 0) {
        reason = count == 0 ? "eof" : "read_error";
        if (count < 0)
          perror("read dmesg output");
        break;
      }
      reads++;
      bytes += count;
      retained += count;
      data[retained] = 0;
      last_record(last, sizeof(last), data, retained);
      if (strstr(data, marker)) {
        found = 1;
        reason = "marker";
        break;
      }
      if (retained > sizeof(marker)) {
        memmove(data, data + retained - sizeof(marker), sizeof(marker));
        retained = sizeof(marker);
      }
    }
    printf("KLOG_DMESG_FOLLOW stage=%d observed=%d injections=%zu reads=%zu "
           "bytes=%zu elapsed_ms=%lld events=%d reason=%s last_record=%s\n",
           stage, found, injections, reads, bytes, monotonic_ms() - started,
           last_events, reason, last);
    passed = found;
  }
  int status = 0;
  pid_t observed = waitpid(child, &status, WNOHANG);
  printf("KLOG_DMESG_FOLLOW_CHILD before_cleanup=%s status=%d\n",
         observed == 0       ? "running"
         : observed == child ? "exited"
                             : "wait_error",
         status);
  if (observed == child || observed < 0)
    passed = 0;
  if (observed == 0) {
    /* Stop only the exact child created by this probe. SIGKILL makes the
     * cleanup bounded even if the followed command has stopped responding. */
    kill(child, SIGKILL);
    while (waitpid(child, &status, 0) < 0) {
      if (errno == EINTR)
        continue;
      perror("wait dmesg child");
      passed = 0;
      break;
    }
  }
  printf("KLOG_DMESG_FOLLOW_CHILD final_status=%d\n", status);
  close(output[0]);
  close(log_fd);
  return passed ? 0 : 1;
}
