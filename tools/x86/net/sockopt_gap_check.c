// SPDX-License-Identifier: MPL-2.0
//
// Reproducer / acceptance test for two missing socket options found by the
// strace failure scan (e1f2c865f):
//
//   SOL_IP      / IP_MTU_DISCOVER  (opt 10)  -- dig sets it to 5 (OMIT)
//   SOL_NETLINK / NETLINK_EXT_ACK  (opt 11)  -- ss sets it to 1
//
// Before the fix both setsockopt() calls fail with ENOPROTOOPT.
//
// Linux semantics are pinned against a host Linux 7.2.6 measurement:
//   * IP_MTU_DISCOVER accepts 0..=5 (4 included; the kernel only bounds the
//     range DONT..OMIT), rejects 6 and -1 with EINVAL, default is 1 (WANT);
//   * NETLINK_EXT_ACK is a boolean: any non-zero int reads back as 1, the
//     default is 0;
//   * SOL_IP options are ACCEPTED on an AF_INET6 socket (Linux routes the
//     level to ip_setsockopt), so the v6 check asserts acceptance, not
//     ENOPROTOOPT. See docs/porting/2026-10-27-sockopt-gaps.md.

#include <arpa/inet.h>
#include <errno.h>
#include <linux/netlink.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

static int failures;

static void check(const char *name, int cond, const char *detail)
{
    printf("SG %s %s %s\n", name, cond ? "PASS" : "FAIL", detail);
    if (!cond)
        failures++;
}

static int get_int(int fd, int level, int optname, int *out)
{
    socklen_t len = sizeof(*out);
    errno = 0;
    int rc = getsockopt(fd, level, optname, out, &len);
    return rc == 0 ? 0 : errno;
}

static int set_int(int fd, int level, int optname, int val)
{
    errno = 0;
    return setsockopt(fd, level, optname, &val, sizeof(val)) == 0 ? 0 : errno;
}

static void test_mtu_discover(void)
{
    int s = socket(AF_INET, SOCK_DGRAM, 0);
    if (s < 0) {
        check("v4_socket", 0, "cannot create socket");
        return;
    }

    int dflt = -1;
    int err = get_int(s, IPPROTO_IP, IP_MTU_DISCOVER, &dflt);
    char buf[160];
    snprintf(buf, sizeof(buf), "errno=%d default=%d (linux default 1)", err,
             dflt);
    check("mtu_discover_default", err == 0 && dflt == 1, buf);

    for (int v = 0; v <= 5; v++) {
        int serr = set_int(s, IPPROTO_IP, IP_MTU_DISCOVER, v);
        int got = -1;
        int gerr = get_int(s, IPPROTO_IP, IP_MTU_DISCOVER, &got);
        snprintf(buf, sizeof(buf), "set_errno=%d get_errno=%d got=%d", serr,
                 gerr, got);
        char name[48];
        snprintf(name, sizeof(name), "mtu_discover_%d", v);
        check(name, serr == 0 && gerr == 0 && got == v, buf);
    }

    int border[] = { 6, -1 };
    for (unsigned i = 0; i < sizeof(border) / sizeof(border[0]); i++) {
        int serr = set_int(s, IPPROTO_IP, IP_MTU_DISCOVER, border[i]);
        snprintf(buf, sizeof(buf), "set_errno=%d (want EINVAL=%d)", serr,
                 EINVAL);
        char name[48];
        snprintf(name, sizeof(name), "mtu_discover_reject_%d", border[i]);
        check(name, serr == EINVAL, buf);
    }

    close(s);
}

static void test_v6_sol_ip(void)
{
    int s = socket(AF_INET6, SOCK_DGRAM, 0);
    if (s < 0) {
        check("v6_socket", 0, "cannot create socket");
        return;
    }

    int serr = set_int(s, IPPROTO_IP, IP_MTU_DISCOVER, 1);
    int got = -1;
    int gerr = get_int(s, IPPROTO_IP, IP_MTU_DISCOVER, &got);
    char buf[160];
    snprintf(buf, sizeof(buf),
             "set_errno=%d get_errno=%d got=%d (linux accepts SOL_IP on v6)",
             serr, gerr, got);
    check("v6_sol_ip_accepted_linux_parity", serr == 0 && gerr == 0 && got == 1,
          buf);

    close(s);
}

static void test_netlink_ext_ack(void)
{
    int s = socket(AF_NETLINK, SOCK_RAW, NETLINK_ROUTE);
    if (s < 0) {
        check("netlink_socket", 0, "cannot create NETLINK_ROUTE socket");
        return;
    }

    int dflt = -1;
    int err = get_int(s, SOL_NETLINK, NETLINK_EXT_ACK, &dflt);
    char buf[160];
    snprintf(buf, sizeof(buf), "errno=%d default=%d (linux default 0)", err,
             dflt);
    check("ext_ack_default", err == 0 && dflt == 0, buf);

    int cases[] = { 1, 0, 2, -1 };
    int want[] = { 1, 0, 1, 1 };
    for (unsigned i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
        int serr = set_int(s, SOL_NETLINK, NETLINK_EXT_ACK, cases[i]);
        int got = -1;
        int gerr = get_int(s, SOL_NETLINK, NETLINK_EXT_ACK, &got);
        snprintf(buf, sizeof(buf), "set_errno=%d get_errno=%d got=%d", serr,
                 gerr, got);
        char name[48];
        snprintf(name, sizeof(name), "ext_ack_set_%d", cases[i]);
        check(name, serr == 0 && gerr == 0 && got == want[i], buf);
    }

    close(s);
}

int main(void)
{
    test_mtu_discover();
    test_v6_sol_ip();
    test_netlink_ext_ack();

    if (failures == 0) {
        printf("SG RESULT all PASS\n");
        return 0;
    }
    printf("SG RESULT all FAIL failures=%d\n", failures);
    return 1;
}
