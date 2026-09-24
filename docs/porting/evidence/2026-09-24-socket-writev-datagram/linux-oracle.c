// SPDX-License-Identifier: MPL-2.0
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/uio.h>
#include <unistd.h>

int main(void)
{
    int receiver = socket(AF_INET, SOCK_DGRAM, 0);
    int sender = socket(AF_INET, SOCK_DGRAM, 0);
    struct sockaddr_in addr = { .sin_family = AF_INET,
        .sin_addr.s_addr = htonl(INADDR_LOOPBACK) };
    socklen_t addr_len = sizeof(addr);
    char buffer[16];
    if (receiver < 0 || sender < 0 || bind(receiver, (void *)&addr, sizeof(addr)) ||
        getsockname(receiver, (void *)&addr, &addr_len) ||
        connect(sender, (void *)&addr, sizeof(addr))) return 10;

    struct iovec split[] = { { .iov_base = "ab", .iov_len = 2 },
                             { .iov_base = "cd", .iov_len = 2 } };
    errno = 0;
    ssize_t sent = writev(sender, split, 2);
    int send_errno = errno;
    ssize_t received = recv(receiver, buffer, sizeof(buffer), MSG_DONTWAIT);
    int recv_errno = errno;
    ssize_t extra = recv(receiver, buffer + 8, sizeof(buffer) - 8, MSG_DONTWAIT);
    int extra_errno = errno;
    printf("split: writev=%zd errno=%d recv=%zd recv_errno=%d payload=%.*s extra=%zd extra_errno=%d\n",
           sent, send_errno, received, recv_errno,
           received > 0 ? (int)received : 0, buffer, extra, extra_errno);
    if (sent != 4 || received != 4 || memcmp(buffer, "abcd", 4) ||
        extra != -1 || extra_errno != EAGAIN) return 11;

    struct iovec fault[] = { { .iov_base = "XY", .iov_len = 2 },
                             { .iov_base = (void *)1, .iov_len = 1 } };
    errno = 0;
    sent = writev(sender, fault, 2);
    send_errno = errno;
    extra = recv(receiver, buffer, sizeof(buffer), MSG_DONTWAIT);
    extra_errno = errno;
    printf("fault: writev=%zd errno=%d recv=%zd recv_errno=%d\n",
           sent, send_errno, extra, extra_errno);
    if (sent != -1 || send_errno != EFAULT || extra != -1 || extra_errno != EAGAIN)
        return 12;
    int unconnected = socket(AF_INET, SOCK_DGRAM, 0);
    if (unconnected < 0) return 13;
    errno = 0;
    sent = writev(unconnected, fault, 2);
    send_errno = errno;
    printf("unconnected: writev=%zd errno=%d\n", sent, send_errno);
    if (sent != -1 || send_errno != EDESTADDRREQ) return 14;
    close(unconnected);
    close(sender);
    close(receiver);
    return 0;
}
