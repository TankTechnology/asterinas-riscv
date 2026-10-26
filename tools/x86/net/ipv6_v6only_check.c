// SPDX-License-Identifier: MPL-2.0
//
// Reproducer for the missing SOL_IPV6 options. Run inside the Debian guest.
//
// Expected before the fix: setsockopt/getsockopt(IPPROTO_IPV6, IPV6_V6ONLY)
// both fail with EOPNOTSUPP. Expected after: set/get succeed, the default
// reads back as 0, the v4 control keeps working, and SOL_IPV6 on a v4 socket
// is rejected with ENOPROTOOPT.

#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

int main(void)
{
    int val = 1;
    int got = -1;
    socklen_t len = sizeof(got);

    // --- IPv6 socket: set and read back IPV6_V6ONLY ---
    int s6 = socket(AF_INET6, SOCK_STREAM, 0);
    if (s6 < 0) {
        printf("V6 socket FAIL errno=%d (%s)\n", errno, strerror(errno));
        return 1;
    }

    errno = 0;
    int rc = setsockopt(s6, IPPROTO_IPV6, IPV6_V6ONLY, &val, sizeof(val));
    printf("SET v6only rc=%d errno=%d (%s)\n", rc, errno, strerror(errno));

    errno = 0;
    int grc = getsockopt(s6, IPPROTO_IPV6, IPV6_V6ONLY, &got, &len);
    printf("GET v6only rc=%d errno=%d (%s) value=%d\n", grc, errno,
           strerror(errno), got);

    // --- default value on a fresh socket must read back as 0 ---
    int s6b = socket(AF_INET6, SOCK_STREAM, 0);
    int dflt = -1;
    socklen_t dlen = sizeof(dflt);
    errno = 0;
    int drc = getsockopt(s6b, IPPROTO_IPV6, IPV6_V6ONLY, &dflt, &dlen);
    printf("DEFAULT v6only rc=%d errno=%d (%s) value=%d\n", drc, errno,
           strerror(errno), dflt);

    // --- v4 control: a known-good SOL_SOCKET option ---
    int s4 = socket(AF_INET, SOCK_STREAM, 0);
    int one = 1;
    errno = 0;
    int crc = setsockopt(s4, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    printf("CTRL v4_reuseaddr rc=%d errno=%d (%s)\n", crc, errno,
           strerror(errno));

    // --- SOL_IPV6 on a v4 socket must be ENOPROTOOPT ---
    errno = 0;
    int v4rc = setsockopt(s4, IPPROTO_IPV6, IPV6_V6ONLY, &val, sizeof(val));
    int v4errno = errno;
    printf("V4_SOL6 rc=%d errno=%d (%s)\n", v4rc, v4errno,
           strerror(v4errno));

    int pass_set = (rc == 0);
    int pass_get = (grc == 0 && got == 1);
    int pass_default = (drc == 0 && dflt == 0);
    int pass_ctrl = (crc == 0);
    int pass_v4_sol6 = (v4rc != 0 && v4errno == ENOPROTOOPT);
    int all = pass_set && pass_get && pass_default && pass_ctrl && pass_v4_sol6;

    printf("RESULT v6only_set %s\n", pass_set ? "PASS" : "FAIL");
    printf("RESULT v6only_get %s\n", pass_get ? "PASS" : "FAIL");
    printf("RESULT v6only_default0 %s\n", pass_default ? "PASS" : "FAIL");
    printf("RESULT v4_ctrl %s\n", pass_ctrl ? "PASS" : "FAIL");
    printf("RESULT v4_sol6_enoprotoopt %s\n", pass_v4_sol6 ? "PASS" : "FAIL");
    printf("RESULT all %s\n", all ? "PASS" : "FAIL");

    close(s6);
    close(s6b);
    close(s4);
    return all ? 0 : 1;
}
