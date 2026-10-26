// SPDX-License-Identifier: MPL-2.0
//
// Reproducer for the missing SOL_IPV6 options that block dig over IPv6.
//
// Before the fix: IPV6_RECVERR (25), IPV6_DONTFRAG (62) and IPV6_MTU (24)
// all fail with ENOPROTOOPT. After: set/get succeed, the v4 IP_RECVERR
// control keeps working, and SOL_IPV6 on a v4 socket is still rejected
// with ENOPROTOOPT.

#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

static int set_get_bool(const char *name, int optname, int want)
{
    int s = socket(AF_INET6, SOCK_STREAM, 0);
    if (s < 0) {
        printf("V6 %s socket FAIL errno=%d\n", name, errno);
        return 0;
    }

    errno = 0;
    int rc = setsockopt(s, IPPROTO_IPV6, optname, &want, sizeof(want));
    int serr = errno;

    int got = -1;
    socklen_t len = sizeof(got);
    errno = 0;
    int grc = getsockopt(s, IPPROTO_IPV6, optname, &got, &len);
    int gerr = errno;

    printf("%s set rc=%d errno=%d (%s) | get rc=%d errno=%d (%s) value=%d\n",
           name, rc, serr, strerror(serr), grc, gerr, strerror(gerr), got);
    close(s);
    return rc == 0 && grc == 0 && got == want;
}

static int set_get_int(const char *name, int optname, int want)
{
    int s = socket(AF_INET6, SOCK_STREAM, 0);
    if (s < 0) {
        printf("V6 %s socket FAIL errno=%d\n", name, errno);
        return 0;
    }

    errno = 0;
    int rc = setsockopt(s, IPPROTO_IPV6, optname, &want, sizeof(want));
    int serr = errno;

    int got = -1;
    socklen_t len = sizeof(got);
    errno = 0;
    int grc = getsockopt(s, IPPROTO_IPV6, optname, &got, &len);
    int gerr = errno;

    printf("%s set rc=%d errno=%d (%s) | get rc=%d errno=%d (%s) value=%d\n",
           name, rc, serr, strerror(serr), grc, gerr, strerror(gerr), got);
    close(s);
    return rc == 0 && grc == 0 && got == want;
}

int main(void)
{
    int recverr = set_get_bool("ipv6_recverr", IPV6_RECVERR, 1);
    int dontfrag = set_get_bool("ipv6_dontfrag", IPV6_DONTFRAG, 1);
    int mtu = set_get_int("ipv6_mtu", IPV6_MTU, 1280);

    // v4 control: IP_RECVERR must keep working
    int s4 = socket(AF_INET, SOCK_STREAM, 0);
    int one = 1;
    errno = 0;
    int v4rc = setsockopt(s4, IPPROTO_IP, IP_RECVERR, &one, sizeof(one));
    int v4errno = errno;
    printf("v4_ip_recverr rc=%d errno=%d (%s)\n", v4rc, v4errno,
           strerror(v4errno));

    // SOL_IPV6 on a v4 socket must stay ENOPROTOOPT
    errno = 0;
    int v4s6 = setsockopt(s4, IPPROTO_IPV6, IPV6_RECVERR, &one, sizeof(one));
    int v4s6errno = errno;
    printf("v4_sol6_recverr rc=%d errno=%d (%s)\n", v4s6, v4s6errno,
           strerror(v4s6errno));

    int pass_v4 = (v4rc == 0);
    int pass_v4s6 = (v4s6 != 0 && v4s6errno == ENOPROTOOPT);
    int all = recverr && dontfrag && mtu && pass_v4 && pass_v4s6;

    printf("RESULT ipv6_recverr %s\n", recverr ? "PASS" : "FAIL");
    printf("RESULT ipv6_dontfrag %s\n", dontfrag ? "PASS" : "FAIL");
    printf("RESULT ipv6_mtu %s\n", mtu ? "PASS" : "FAIL");
    printf("RESULT v4_ip_recverr %s\n", pass_v4 ? "PASS" : "FAIL");
    printf("RESULT v4_sol6_enoprotoopt %s\n", pass_v4s6 ? "PASS" : "FAIL");
    printf("RESULT all %s\n", all ? "PASS" : "FAIL");

    close(s4);
    return all ? 0 : 1;
}
