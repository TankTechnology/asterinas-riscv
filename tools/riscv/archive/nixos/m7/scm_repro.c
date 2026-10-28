// SPDX-License-Identifier: MPL-2.0
//
// M7 minimal repro: UNIX-domain SCM_RIGHTS file-descriptor passing +
// SO_PEERCRED.
//
// This is the exact kernel surface nix-daemon depends on for its multi-user
// protocol: a server accepts an AF_UNIX client, reads the client's credentials
// with SO_PEERCRED, and receives a live file descriptor with SCM_RIGHTS.
// Linux contracts: https://man7.org/linux/man-pages/man7/unix.7.html and
// https://man7.org/linux/man-pages/man7/socket.7.html.
//
// The repro runs three checks and prints a fixed marker for each so a driver
// can assert the result unambiguously:
//   1. a parent creates a listening AF_UNIX socket before fork.
//   2. the child connects and SO_PEERCRED reports the child's pid; strict mode
//      also requires the child to drop to uid/gid 65534 before connecting.
//   3. the child sends a temporary file with SCM_RIGHTS and the parent reads
//      the known payload through the received descriptor.

#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static const uid_t DISTINCT_UID = 65534;
static const gid_t DISTINCT_GID = 65534;
static const int TRANSACTION_TIMEOUT_MS = 5000;
static const int CLEANUP_TIMEOUT_MS = 250;
static const int CLEANUP_POLL_INTERVAL_MS = 10;
static const unsigned int ABSTRACT_BIND_ATTEMPTS = 8;
static pid_t supervised_child_pid = -1;

enum test_fault {
  TEST_FAULT_NONE,
  TEST_FAULT_EXIT_BEFORE_CONNECT,
  TEST_FAULT_STALL_AFTER_CONNECT,
};

struct options {
  bool require_distinct_ids;
  enum test_fault test_fault;
};

static void write_stdout_or_exit(const char *text) {
  size_t remaining = strlen(text);
  while (remaining > 0) {
    ssize_t written = write(STDOUT_FILENO, text, remaining);
    if (written < 0 && errno == EINTR)
      continue;
    if (written <= 0)
      _exit(1);
    text += written;
    remaining -= (size_t)written;
  }
}

static int elapsed_milliseconds(const struct timespec *started_at) {
  struct timespec now;
  if (clock_gettime(CLOCK_MONOTONIC, &now) < 0)
    return -1;

  int64_t elapsed_nanoseconds =
      ((int64_t)now.tv_sec - (int64_t)started_at->tv_sec) * 1000000000LL +
      ((int64_t)now.tv_nsec - (int64_t)started_at->tv_nsec);
  if (elapsed_nanoseconds < 0)
    return -1;
  return (int)(elapsed_nanoseconds / 1000000LL);
}

static void reap_supervised_child_until_deadline(pid_t child_pid) {
  struct timespec started_at;
  if (clock_gettime(CLOCK_MONOTONIC, &started_at) < 0) {
    int status;
    (void)waitpid(child_pid, &status, WNOHANG);
    return;
  }

  for (;;) {
    int elapsed_ms = elapsed_milliseconds(&started_at);
    if (elapsed_ms < 0 || elapsed_ms >= CLEANUP_TIMEOUT_MS)
      return;

    int status;
    pid_t waited = waitpid(child_pid, &status, WNOHANG);
    if (waited == child_pid || (waited < 0 && errno == ECHILD))
      return;
    if (waited < 0 && errno != EINTR)
      return;

    int remaining_ms = CLEANUP_TIMEOUT_MS - elapsed_ms;
    int sleep_ms = remaining_ms < CLEANUP_POLL_INTERVAL_MS
                       ? remaining_ms
                       : CLEANUP_POLL_INTERVAL_MS;
    if (poll(NULL, 0, sleep_ms) < 0 && errno != EINTR)
      return;
  }
}

static void terminate_and_reap_supervised_child(void) {
  pid_t child_pid = supervised_child_pid;
  if (child_pid <= 0)
    return;

  supervised_child_pid = -1;
  int saved_errno = errno;
  if (kill(child_pid, SIGKILL) < 0) {
    int kill_errno = errno;
    dprintf(STDOUT_FILENO, "__M7_CHILD_CLEANUP_KILL_FAIL__ errno=%d (%s)\n",
            kill_errno, strerror(kill_errno));
  }
  reap_supervised_child_until_deadline(child_pid);
  errno = saved_errno;
}

static void fail(const char *why) {
  int saved_errno = errno;
  terminate_and_reap_supervised_child();
  dprintf(STDOUT_FILENO, "__M7_%s_FAIL__ errno=%d (%s)\n", why, saved_errno,
          strerror(saved_errno));
  exit(1);
}

static void protocol_fail(const char *why) {
  errno = EPROTO;
  fail(why);
}

static struct options parse_options(int argc, char **argv) {
  struct options options = {0};
  for (int index = 1; index < argc; ++index) {
    if (strcmp(argv[index], "--require-distinct-ids") == 0) {
      options.require_distinct_ids = true;
    } else if (strcmp(argv[index], "--test-exit-before-connect") == 0) {
      options.test_fault = TEST_FAULT_EXIT_BEFORE_CONNECT;
    } else if (strcmp(argv[index], "--test-stall-after-connect") == 0) {
      options.test_fault = TEST_FAULT_STALL_AFTER_CONNECT;
    } else {
      dprintf(STDOUT_FILENO,
              "usage: %s [--require-distinct-ids] "
              "[--test-exit-before-connect|--test-stall-after-connect]\n",
              argv[0]);
      exit(2);
    }
  }
  if (options.require_distinct_ids && geteuid() != 0) {
    write_stdout_or_exit("__M7_DISTINCT_IDS_REQUIRES_ROOT__\n");
    exit(2);
  }
  return options;
}

static socklen_t initialize_abstract_address(struct sockaddr_un *address,
                                             unsigned int attempt) {
  memset(address, 0, sizeof(*address));
  address->sun_family = AF_UNIX;

  int name_length =
      snprintf(&address->sun_path[1], sizeof(address->sun_path) - 1,
               "asterinas-m7-%" PRIdMAX "-%u", (intmax_t)getpid(), attempt);
  if (name_length <= 0 || (size_t)name_length >= sizeof(address->sun_path) - 1)
    protocol_fail("SOCKET_NAME");

  return (socklen_t)(offsetof(struct sockaddr_un, sun_path) + 1 +
                     (size_t)name_length);
}

static int create_listener(struct sockaddr_un *address,
                           socklen_t *address_length) {
  for (unsigned int attempt = 0; attempt < ABSTRACT_BIND_ATTEMPTS; ++attempt) {
    *address_length = initialize_abstract_address(address, attempt);
    int listener = socket(AF_UNIX, SOCK_STREAM, 0);
    if (listener < 0)
      fail("LISTENER_SOCKET");
    if (bind(listener, (const struct sockaddr *)address, *address_length) ==
        0) {
      if (listen(listener, 1) < 0)
        fail("LISTENER_LISTEN");
      dprintf(STDOUT_FILENO, "__M7_LISTEN_OK__ attempt=%u\n", attempt);
      return listener;
    }

    int saved_errno = errno;
    close(listener);
    if (saved_errno != EADDRINUSE) {
      errno = saved_errno;
      fail("LISTENER_BIND");
    }
  }

  errno = EADDRINUSE;
  fail("LISTENER_BIND_RETRIES");
  return -1;
}

static int connect_to_listener(const struct sockaddr_un *address,
                               socklen_t address_length) {
  int connection = socket(AF_UNIX, SOCK_STREAM, 0);
  if (connection < 0)
    fail("CHILD_SOCKET");
  if (connect(connection, (const struct sockaddr *)address, address_length) < 0)
    fail("CHILD_CONNECT");
  return connection;
}

static int create_payload_file(void) {
  char path[] = "/tmp/asterinas-scm-repro-XXXXXX";
  int descriptor = mkstemp(path);
  if (descriptor < 0)
    fail("CHILD_OPEN");
  if (unlink(path) < 0) {
    int saved_errno = errno;
    close(descriptor);
    errno = saved_errno;
    fail("CHILD_UNLINK");
  }

  const char payload[] = "scm-rights-ok";
  if (write(descriptor, payload, sizeof(payload) - 1) !=
      (ssize_t)(sizeof(payload) - 1))
    fail("CHILD_WRITE");
  if (lseek(descriptor, 0, SEEK_SET) < 0)
    fail("CHILD_SEEK");
  return descriptor;
}

static void send_descriptor(int connection, int descriptor) {
  char byte = 'x';
  struct iovec iov = {.iov_base = &byte, .iov_len = 1};
  char control[CMSG_SPACE(sizeof(int))];
  memset(control, 0, sizeof(control));
  struct msghdr message;
  memset(&message, 0, sizeof(message));
  message.msg_iov = &iov;
  message.msg_iovlen = 1;
  message.msg_control = control;
  message.msg_controllen = sizeof(control);

  struct cmsghdr *header = CMSG_FIRSTHDR(&message);
  header->cmsg_level = SOL_SOCKET;
  header->cmsg_type = SCM_RIGHTS;
  header->cmsg_len = CMSG_LEN(sizeof(int));
  memcpy(CMSG_DATA(header), &descriptor, sizeof(descriptor));

  if (sendmsg(connection, &message, 0) != 1)
    fail("CHILD_SENDMSG");
}

static void arm_parent_death_signal(pid_t expected_parent_pid) {
  if (prctl(PR_SET_PDEATHSIG, SIGKILL) < 0)
    fail("CHILD_PDEATHSIG");
  if (getppid() != expected_parent_pid)
    protocol_fail("CHILD_PARENT_CHANGED");
}

static void drop_child_ids(pid_t expected_parent_pid) {
  if (setgid(DISTINCT_GID) < 0)
    fail("CHILD_SETGID");
  arm_parent_death_signal(expected_parent_pid);
  if (setuid(DISTINCT_UID) < 0)
    fail("CHILD_SETUID");
  arm_parent_death_signal(expected_parent_pid);
  if (getegid() != DISTINCT_GID || geteuid() != DISTINCT_UID)
    protocol_fail("CHILD_IDS");
}

static void run_child(int listener, const struct sockaddr_un *address,
                      socklen_t address_length, const struct options *options,
                      pid_t expected_parent_pid) {
  supervised_child_pid = -1;
  arm_parent_death_signal(expected_parent_pid);
  close(listener);
  if (options->test_fault == TEST_FAULT_EXIT_BEFORE_CONNECT) {
    write_stdout_or_exit("__M7_TEST_EXIT_BEFORE_CONNECT__\n");
    _exit(EXIT_FAILURE);
  }
  if (options->require_distinct_ids)
    drop_child_ids(expected_parent_pid);
  int connection = connect_to_listener(address, address_length);
  if (options->test_fault == TEST_FAULT_STALL_AFTER_CONNECT) {
    write_stdout_or_exit("__M7_TEST_STALL_AFTER_CONNECT__\n");
    for (;;)
      pause();
  }
  int descriptor = create_payload_file();
  send_descriptor(connection, descriptor);
  close(descriptor);
  close(connection);

  char marker[64];
  int marker_length =
      snprintf(marker, sizeof(marker),
               "__M7_CHILD_SEND_OK__ pid=%" PRIdMAX "\n", (intmax_t)getpid());
  if (marker_length <= 0 || (size_t)marker_length >= sizeof(marker))
    protocol_fail("CHILD_MARKER");
  write_stdout_or_exit(marker);
  _exit(0);
}

static int remaining_transaction_timeout_ms(const struct timespec *started_at) {
  struct timespec now;
  if (clock_gettime(CLOCK_MONOTONIC, &now) < 0)
    fail("TRANSACTION_CLOCK");

  int64_t elapsed_nanoseconds =
      ((int64_t)now.tv_sec - (int64_t)started_at->tv_sec) * 1000000000LL +
      ((int64_t)now.tv_nsec - (int64_t)started_at->tv_nsec);
  if (elapsed_nanoseconds < 0)
    protocol_fail("TRANSACTION_CLOCK_ORDER");
  int64_t remaining = TRANSACTION_TIMEOUT_MS - elapsed_nanoseconds / 1000000LL;
  return remaining > 0 ? (int)remaining : 0;
}

static short poll_for_transaction_event(int descriptor, short events,
                                        const struct timespec *started_at,
                                        const char *timeout_failure,
                                        const char *poll_failure) {
  for (;;) {
    int timeout_ms = remaining_transaction_timeout_ms(started_at);
    if (timeout_ms == 0) {
      errno = ETIMEDOUT;
      fail(timeout_failure);
    }

    struct pollfd poll_descriptor = {
        .fd = descriptor,
        .events = events,
    };
    int ready = poll(&poll_descriptor, 1, timeout_ms);
    if (ready == 0) {
      errno = ETIMEDOUT;
      fail(timeout_failure);
    }
    if (ready < 0) {
      if (errno == EINTR)
        continue;
      fail(poll_failure);
    }
    return poll_descriptor.revents;
  }
}

static int accept_connection(int listener, const struct timespec *started_at) {
  for (;;) {
    short events = poll_for_transaction_event(listener, POLLIN, started_at,
                                              "ACCEPT_TIMEOUT", "ACCEPT_POLL");
    if ((events & POLLIN) == 0)
      protocol_fail("ACCEPT_POLL_EVENTS");

    int connection = accept(listener, NULL, NULL);
    if (connection >= 0)
      return connection;
    if (errno != EINTR)
      fail("ACCEPT");
  }
}

static void validate_peer_credentials(int connection, pid_t expected_pid,
                                      bool require_distinct_ids) {
  struct ucred credentials;
  socklen_t credentials_length = sizeof(credentials);
  if (getsockopt(connection, SOL_SOCKET, SO_PEERCRED, &credentials,
                 &credentials_length) < 0)
    fail("PEERCRED");
  if (credentials_length != sizeof(credentials) ||
      credentials.pid != expected_pid)
    protocol_fail("PEERCRED_CONTENT");

  if (!require_distinct_ids) {
    dprintf(STDOUT_FILENO,
            "__M7_PEERCRED_PID_OK__ pid=%" PRIdMAX " distinct_ids=0\n",
            (intmax_t)credentials.pid);
    return;
  }

  if (credentials.uid != DISTINCT_UID || credentials.gid != DISTINCT_GID)
    protocol_fail("PEERCRED_DISTINCT_IDS");
  dprintf(STDOUT_FILENO,
          "__M7_PEERCRED_OK__ pid=%" PRIdMAX " uid=%" PRIuMAX " gid=%" PRIuMAX
          " distinct_ids=1\n",
          (intmax_t)credentials.pid, (uintmax_t)credentials.uid,
          (uintmax_t)credentials.gid);
}

static int receive_descriptor(int connection,
                              const struct timespec *started_at) {
  char byte = 0;
  struct iovec iov = {.iov_base = &byte, .iov_len = 1};
  char control[CMSG_SPACE(sizeof(int))];
  struct msghdr message;

  ssize_t received;
  for (;;) {
    short events = poll_for_transaction_event(connection, POLLIN, started_at,
                                              "RECV_TIMEOUT", "RECV_POLL");
    if ((events & POLLIN) == 0)
      protocol_fail("RECV_POLL_EVENTS");

    byte = 0;
    memset(control, 0, sizeof(control));
    memset(&message, 0, sizeof(message));
    message.msg_iov = &iov;
    message.msg_iovlen = 1;
    message.msg_control = control;
    message.msg_controllen = sizeof(control);
    received = recvmsg(connection, &message, MSG_DONTWAIT);
    if (received >= 0)
      break;
    if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)
      continue;
    fail("RECVMSG");
  }
  if (received != 1 || byte != 'x')
    protocol_fail("RECV_DATA");
  if ((message.msg_flags & MSG_CTRUNC) != 0)
    protocol_fail("RECV_CTRUNC");

  for (struct cmsghdr *header = CMSG_FIRSTHDR(&message); header;
       header = CMSG_NXTHDR(&message, header)) {
    if (header->cmsg_level != SOL_SOCKET || header->cmsg_type != SCM_RIGHTS)
      continue;
    if (header->cmsg_len < CMSG_LEN(sizeof(int)))
      protocol_fail("SHORT_SCM_RIGHTS");

    int descriptor;
    memcpy(&descriptor, CMSG_DATA(header), sizeof(descriptor));
    return descriptor;
  }
  protocol_fail("NO_SCM_RIGHTS");
  return -1;
}

static void validate_payload(int descriptor) {
  char buffer[32] = {0};
  ssize_t length = read(descriptor, buffer, sizeof(buffer) - 1);
  if (length < 0)
    fail("READ_FD");
  if (strcmp(buffer, "scm-rights-ok") != 0)
    protocol_fail("FD_CONTENT");

  dprintf(STDOUT_FILENO, "__M7_SCM_RIGHTS_OK__ read_back=[%s]\n", buffer);
}

static void wait_for_child(pid_t child_pid, const struct timespec *started_at) {
  int status = 0;
  for (;;) {
    pid_t waited = waitpid(child_pid, &status, WNOHANG);
    if (waited == child_pid) {
      supervised_child_pid = -1;
      if (!WIFEXITED(status) || WEXITSTATUS(status) != 0)
        protocol_fail("CHILD_STATUS");
      return;
    }
    if (waited < 0) {
      if (errno == EINTR)
        continue;
      fail("WAITPID");
    }

    int remaining_ms = remaining_transaction_timeout_ms(started_at);
    if (remaining_ms == 0) {
      errno = ETIMEDOUT;
      fail("CHILD_TIMEOUT");
    }
    int sleep_ms = remaining_ms < 50 ? remaining_ms : 50;
    if (poll(NULL, 0, sleep_ms) < 0 && errno != EINTR)
      fail("CHILD_WAIT_POLL");
  }
}

int main(int argc, char **argv) {
  struct options options = parse_options(argc, argv);
  write_stdout_or_exit(">>> M7 init: SCM_RIGHTS + SO_PEERCRED repro <<<\n");

  struct sockaddr_un address;
  socklen_t address_length;
  int listener = create_listener(&address, &address_length);

  struct timespec transaction_started_at;
  if (clock_gettime(CLOCK_MONOTONIC, &transaction_started_at) < 0)
    fail("TRANSACTION_CLOCK");
  pid_t parent_pid = getpid();
  pid_t child_pid = fork();
  if (child_pid < 0)
    fail("FORK");
  if (child_pid == 0)
    run_child(listener, &address, address_length, &options, parent_pid);
  supervised_child_pid = child_pid;

  int connection = accept_connection(listener, &transaction_started_at);
  close(listener);
  validate_peer_credentials(connection, child_pid,
                            options.require_distinct_ids);
  int descriptor = receive_descriptor(connection, &transaction_started_at);
  close(connection);
  validate_payload(descriptor);
  close(descriptor);
  wait_for_child(child_pid, &transaction_started_at);

  write_stdout_or_exit(">>> M7 repro done <<<\n");
  return 0;
}
