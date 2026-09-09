// SPDX-License-Identifier: MPL-2.0

// A bounded Linux-reference probe, not browser or physical-board acceptance.
#define _GNU_SOURCE

#include <errno.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>

static volatile sig_atomic_t child_pid = -1;

static void deadline(int signal_number) {
  (void)signal_number;
  static const char message[] =
      "IPC_DIAG {\"version\":1,\"case\":\"unix_epoll_rights_wait\","
      "\"phase\":\"deadline\",\"ok\":false,\"physical\":false}\n";
  ssize_t written = write(STDERR_FILENO, message, sizeof(message) - 1);
  (void)written;
  if (child_pid > 0)
    (void)kill(child_pid, SIGKILL);
  _exit(124);
}

static void cleanup_child(void) {
  if (child_pid <= 0)
    return;
  (void)kill(child_pid, SIGKILL);
  while (waitpid(child_pid, NULL, 0) < 0 && errno == EINTR) {
  }
  child_pid = -1;
}

static void record(const char *phase, bool ok, long result, int error, int fd,
                   pid_t peer_pid) {
  printf("IPC_DIAG {\"version\":1,\"case\":\"unix_epoll_rights_wait\","
         "\"phase\":\"%s\",\"pid\":%ld,\"peer_pid\":%ld,\"fd\":%d,"
         "\"result\":%ld,\"errno\":%d,\"ok\":%s,\"physical\":false}\n",
         phase, (long)getpid(), (long)peer_pid, fd, result, error,
         ok ? "true" : "false");
  if (!ok)
    exit(EXIT_FAILURE);
}

static void setup(bool ok, const char *phase) {
  if (!ok)
    record(phase, false, -1, errno, -1, 0);
}

int main(void) {
  int carrier[2], data_pipe[2];
  char byte = 'F';
  struct sigaction action = {.sa_handler = deadline};

  setvbuf(stdout, NULL, _IOLBF, 0);
  sigemptyset(&action.sa_mask);
  setup(sigaction(SIGALRM, &action, NULL) == 0, "alarm_setup");
  setup(atexit(cleanup_child) == 0, "cleanup_setup");
  alarm(10);
  setup(socketpair(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0,
                   carrier) == 0,
        "socketpair_setup");
  setup(pipe(data_pipe) == 0, "pipe_setup");
  setup(write(data_pipe[1], &byte, 1) == 1, "pipe_seed");
  close(data_pipe[1]);

  // Establish the empty-channel baseline before a sender exists.
  errno = 0;
  ssize_t result = recv(carrier[0], &byte, 1, MSG_DONTWAIT);
  int error = errno;
  record("empty_receive", result == -1 && error == EAGAIN, result, error,
         carrier[0], 0);

  int epoll_fd = epoll_create1(EPOLL_CLOEXEC);
  setup(epoll_fd >= 0, "epoll_create");
  struct epoll_event interest = {.events = EPOLLIN, .data.fd = carrier[0]};
  setup(epoll_ctl(epoll_fd, EPOLL_CTL_ADD, carrier[0], &interest) == 0,
        "epoll_register");

  child_pid = fork();
  setup(child_pid >= 0, "fork");
  if (child_pid == 0) {
    alarm(10);
    close(carrier[0]);
    close(epoll_fd);
    union {
      struct cmsghdr alignment;
      char bytes[CMSG_SPACE(sizeof(int))];
    } control = {0};
    char payload = 'M';
    struct iovec iov = {.iov_base = &payload, .iov_len = 1};
    struct msghdr message = {.msg_iov = &iov,
                             .msg_iovlen = 1,
                             .msg_control = control.bytes,
                             .msg_controllen = sizeof(control.bytes)};
    struct cmsghdr *header = CMSG_FIRSTHDR(&message);
    header->cmsg_level = SOL_SOCKET;
    header->cmsg_type = SCM_RIGHTS;
    header->cmsg_len = CMSG_LEN(sizeof(int));
    memcpy(CMSG_DATA(header), &data_pipe[0], sizeof(int));
    errno = 0;
    result = sendmsg(carrier[1], &message, MSG_NOSIGNAL);
    error = errno;
    record("child_send", result == 1, result, error, carrier[1], getppid());
    close(data_pipe[0]);
    close(carrier[1]);
    _exit(23);
  }

  close(data_pipe[0]);
  close(carrier[1]);
  struct epoll_event ready = {0};
  errno = 0;
  int ready_count = epoll_wait(epoll_fd, &ready, 1, 3000);
  error = errno;
  record("epoll_ready",
         ready_count == 1 && (ready.events & EPOLLIN) &&
             ready.data.fd == carrier[0],
         ready_count, error, carrier[0], child_pid);

  union {
    struct cmsghdr alignment;
    char bytes[CMSG_SPACE(sizeof(int))];
  } control = {0};
  struct iovec iov = {.iov_base = &byte, .iov_len = 1};
  struct msghdr message = {.msg_iov = &iov,
                           .msg_iovlen = 1,
                           .msg_control = control.bytes,
                           .msg_controllen = sizeof(control.bytes)};
  errno = 0;
  result = recvmsg(carrier[0], &message, MSG_DONTWAIT);
  error = errno;
  struct cmsghdr *header = CMSG_FIRSTHDR(&message);
  bool valid_rights = header && header->cmsg_level == SOL_SOCKET &&
                      header->cmsg_type == SCM_RIGHTS &&
                      header->cmsg_len == CMSG_LEN(sizeof(int)) &&
                      CMSG_NXTHDR(&message, header) == NULL;
  record("rights_receive",
         result == 1 && byte == 'M' && valid_rights &&
             !(message.msg_flags & MSG_CTRUNC),
         result, error, carrier[0], child_pid);
  int received_fd;
  memcpy(&received_fd, CMSG_DATA(header), sizeof(received_fd));
  errno = 0;
  result = read(received_fd, &byte, 1);
  error = errno;
  record("descriptor_read", result == 1 && byte == 'F', byte, error,
         received_fd, child_pid);
  close(received_fd);
  close(carrier[0]);
  close(epoll_fd);

  int status = 0;
  pid_t expected_child = child_pid;
  errno = 0;
  pid_t waited = waitpid(expected_child, &status, 0);
  error = errno;
  if (waited == expected_child)
    child_pid = -1;
  bool expected_exit = waited == expected_child && WIFEXITED(status) &&
                       WEXITSTATUS(status) == 23;
  record("child_wait", expected_exit,
         WIFEXITED(status) ? WEXITSTATUS(status) : -1, error, -1,
         expected_child);
  errno = 0;
  waited = waitpid(expected_child, &status, WNOHANG);
  error = errno;
  record("already_reaped", waited == -1 && error == ECHILD, waited, error, -1,
         expected_child);
  alarm(0);
  record("complete", true, 0, 0, -1, expected_child);
  return 0;
}
