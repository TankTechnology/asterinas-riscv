// SPDX-License-Identifier: MPL-2.0
//
// NIXOS-N3 net probe: raw UDP DNS query to the QEMU slirp resolver
// (10.0.2.3:53) and a TCP connect to the slirp gateway, to isolate whether
// guest outbound networking works below the TLS layer.

#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

int main(void) {
    setbuf(stdout, NULL);

    // Minimal DNS query for cache.nixos.org (A record).
    static const unsigned char query[] = {
        0x12, 0x34, 0x01, 0x00, 0x00, 0x01, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00,
        5, 'c', 'a', 'c', 'h', 'e', 5, 'n', 'i', 'x', 'o', 's', 3, 'o', 'r', 'g', 0,
        0x00, 0x01, 0x00, 0x01,
    };

    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) {
        printf("__NETPROBE__ dns socket failed errno=%d\n", errno);
        return 1;
    }
    struct timeval tv = { .tv_sec = 5 };
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    struct sockaddr_in dns = {
        .sin_family = AF_INET,
        .sin_port = htons(53),
        .sin_addr.s_addr = htonl(0x0a000203), // 10.0.2.3
    };

    // No ARP warm-up: with the kernel ARP pending-queue fix, the first packet
    // to an unresolved next hop must be delivered after the ARP exchange.
    ssize_t sent = sendto(fd, query, sizeof(query), 0,
                          (struct sockaddr *)&dns, sizeof(dns));
    printf("__NETPROBE__ dns sendto=%zd errno=%d\n", sent, sent < 0 ? errno : 0);

    unsigned char buf[512];
    ssize_t n = recv(fd, buf, sizeof(buf), 0);
    printf("__NETPROBE__ dns recv=%zd errno=%d (%s)\n", n, n < 0 ? errno : 0,
           n < 0 ? strerror(errno) : "ok");
    if (n >= 4)
        printf("__NETPROBE__ dns answer rcode=%d ancount=%d\n", buf[3] & 0xf,
               (buf[6] << 8) | buf[7]);
    close(fd);

    // TCP connect to the slirp gateway (should refuse or accept fast).
    int tfd = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in gw = {
        .sin_family = AF_INET,
        .sin_port = htons(80),
        .sin_addr.s_addr = htonl(0x0a000202), // 10.0.2.2
    };
    struct timeval ctv = { .tv_sec = 5 };
    setsockopt(tfd, SOL_SOCKET, SO_RCVTIMEO, &ctv, sizeof(ctv));
    setsockopt(tfd, SOL_SOCKET, SO_SNDTIMEO, &ctv, sizeof(ctv));
    int rc = connect(tfd, (struct sockaddr *)&gw, sizeof(gw));
    printf("__NETPROBE__ tcp 10.0.2.2:80 connect rc=%d errno=%d (%s)\n", rc,
           rc < 0 ? errno : 0, rc < 0 ? strerror(errno) : "ok");
    close(tfd);

    return 0;
}
