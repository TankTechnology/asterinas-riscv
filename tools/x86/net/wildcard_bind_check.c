// SPDX-License-Identifier: MPL-2.0
//
// Minimal reproducer for the wildcard-bind bug. Run inside the Debian guest
// with:
//
//   wb bind      bind(0.0.0.0:0), sendto DNS, recvfrom  (must time out before
//                the fix; the socket is pinned to lo while the query leaves
//                through eth0)
//   wb nobind    implicit bind via sendto                (must already work)
//
// Exits 0 when a reply arrived, 1 on setup errors, 2 on receive timeout.

#include <arpa/inet.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

static const unsigned char dns_query[] = {
    0x12, 0x34, 0x01, 0x00, 0x00, 0x01, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
    3, 'd', 'e', 'b', 6, 'd', 'e', 'b', 'i', 'a', 'n', 3, 'o', 'r', 'g', 0x00,
    0x00, 0x01, 0x00, 0x01,
};

static int run(int do_bind)
{
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) {
        perror("socket");
        return 1;
    }

    struct timeval tv = { .tv_sec = 3, .tv_usec = 0 };
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    if (do_bind) {
        struct sockaddr_in local = { 0 };
        local.sin_family = AF_INET;
        local.sin_addr.s_addr = htonl(INADDR_ANY);
        local.sin_port = 0;
        if (bind(fd, (struct sockaddr *)&local, sizeof(local)) < 0) {
            perror("bind");
            return 1;
        }
        socklen_t len = sizeof(local);
        getsockname(fd, (struct sockaddr *)&local, &len);
        printf("local=%s:%d\n", inet_ntoa(local.sin_addr), ntohs(local.sin_port));
    }

    struct sockaddr_in server = { 0 };
    server.sin_family = AF_INET;
    server.sin_port = htons(53);
    inet_pton(AF_INET, "10.0.2.3", &server.sin_addr);

    // Retry: the first UDP packet after boot can be lost while the neighbour
    // entry is unresolved (a separate issue from the bind bug), so a single
    // send would make the control case flaky.
    unsigned char buf[1024];
    for (int attempt = 1; attempt <= 3; attempt++) {
        if (sendto(fd, dns_query, sizeof(dns_query), 0,
                   (struct sockaddr *)&server, sizeof(server)) < 0) {
            perror("sendto");
            return 1;
        }

        ssize_t n = recvfrom(fd, buf, sizeof(buf), 0, NULL, NULL);
        if (n >= 0) {
            printf("attempt=%d reply=%zd bytes id=0x%02x%02x\n",
                   attempt, n, buf[0], buf[1]);
            close(fd);
            return 0;
        }
        printf("attempt=%d recvfrom errno=%d (%s)\n", attempt, errno,
               strerror(errno));
    }

    close(fd);
    return 2;
}

int main(int argc, char **argv)
{
    int do_bind = argc > 1 && strcmp(argv[1], "bind") == 0;
    printf("CASE %s\n", do_bind ? "bind" : "nobind");
    int rc = run(do_bind);
    printf("RESULT %s %s\n", do_bind ? "bind" : "nobind",
           rc == 0 ? "PASS" : "FAIL");
    return rc == 0 ? 0 : 1;
}
