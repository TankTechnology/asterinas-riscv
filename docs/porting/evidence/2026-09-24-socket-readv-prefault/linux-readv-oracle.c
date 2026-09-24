// SPDX-License-Identifier: MPL-2.0
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/uio.h>
#include <unistd.h>

static void die(const char *what) {
  perror(what);
  exit(1);
}
static void run_case(const char *name, int op, size_t valid, size_t payload,
                     size_t request) {
  int listener = socket(AF_INET, SOCK_STREAM, 0);
  if (listener < 0)
    die("socket");
  struct sockaddr_in address = {.sin_family = AF_INET,
                                .sin_addr.s_addr = htonl(INADDR_LOOPBACK)};
  if (bind(listener, (void *)&address, sizeof(address)) < 0)
    die("bind");
  socklen_t length = sizeof(address);
  if (getsockname(listener, (void *)&address, &length) < 0)
    die("getsockname");
  if (listen(listener, 1) < 0)
    die("listen");
  int sender = socket(AF_INET, SOCK_STREAM, 0);
  if (sender < 0)
    die("sender socket");
  if (connect(sender, (void *)&address, sizeof(address)) < 0)
    die("connect");
  int receiver = accept(listener, NULL, NULL);
  if (receiver < 0)
    die("accept");
  char payload_buffer[6000];
  memset(payload_buffer, 'x', sizeof(payload_buffer));
  size_t sent = 0;
  while (sent < payload) {
    ssize_t n = send(sender, payload_buffer + sent, payload - sent, 0);
    if (n <= 0)
      die("send");
    sent += (size_t)n;
  }
  char *buffer = mmap(NULL, 8192, PROT_READ | PROT_WRITE,
                      MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
  if (buffer == MAP_FAILED)
    die("mmap");
  if (mprotect(buffer + 4096, 4096, PROT_NONE) < 0)
    die("mprotect");
  int before = -1, after = -1;
  if (ioctl(receiver, FIONREAD, &before) < 0)
    die("before FIONREAD");
  errno = 0;
  ssize_t n;
  if (op == 0)
    n = read(receiver, buffer, request);
  else if (op == 1)
    n = recvfrom(receiver, buffer, request, 0, NULL, NULL);
  else {
    struct iovec iov[2] = {{buffer, valid}, {buffer + 4096, request - valid}};
    struct msghdr msg = {.msg_iov = iov, .msg_iovlen = op == 2 ? 1 : 2};
    if (op == 2)
      iov[0].iov_len = request;
    n = op == 4 ? readv(receiver, iov, 2) : recvmsg(receiver, &msg, 0);
  }
  int saved_errno = errno;
  if (ioctl(receiver, FIONREAD, &after) < 0)
    die("after FIONREAD");
  printf("%s: payload=%zu request=%zu valid=%zu ret=%zd errno=%d queued=%d->%d "
         "first=%c\n",
         name, payload, request, valid, n, saved_errno, before, after,
         buffer[0]);
  close(sender);
  close(receiver);
  close(listener);
  munmap(buffer, 8192);
}
int main(void) {
  run_case("readv-split-three", 4, 1, 3, 3);
  run_case("readv-cross", 4, 4096, 5000, 8192);
  run_case("recvmsg-split-three", 3, 1, 3, 3);
  run_case("recvmsg-split-two", 3, 1, 2, 2);
  run_case("recvmsg-split-three-short", 3, 1, 1, 3);
  run_case("read-cross", 0, 4096, 5000, 8192);
  run_case("recvfrom-cross", 1, 4096, 5000, 8192);
  run_case("recvmsg-single-cross", 2, 4096, 5000, 8192);
  run_case("recvmsg-split-cross", 3, 4096, 5000, 8192);
  run_case("read-short", 0, 4096, 1, 8192);
  run_case("recvfrom-short", 1, 4096, 1, 8192);
  run_case("recvmsg-single-short", 2, 4096, 1, 8192);
  run_case("recvmsg-split-short", 3, 4096, 1, 8192);
  run_case("read-exact", 0, 4096, 4096, 8192);
  run_case("recvfrom-exact", 1, 4096, 4096, 8192);
  run_case("recvmsg-single-exact", 2, 4096, 4096, 8192);
  run_case("recvmsg-split-exact", 3, 4096, 4096, 8192);
}
