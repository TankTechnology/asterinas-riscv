// SPDX-License-Identifier: MPL-2.0
//
// NIXOS-N1 netlink probe: exercises AF_NETLINK socket creation, bind,
// send/recv of nlmsg requests, NETLINK_KOBJECT_UEVENT group membership, and
// NETLINK_ROUTE RTM_GETLINK/RTM_GETADDR/RTM_NEWADDR against the Asterinas
// kernel. Prints fixed markers so the QEMU driver can attribute failures.
//
// Markers:
//   __NL_UEVENT_OK__     KOBJECT_UEVENT socket + bind + membership options OK
//   __NL_GETLINK_OK__    RTM_GETLINK dump returned lo and eth0
//   __NL_GETADDR_OK__    RTM_GETADDR dump returned 127.0.0.1/8 on lo
//   __NL_NEWADDR_OK__    RTM_NEWADDR of an existing address ACKed -EEXIST
//   __NL_FAIL:<step>     a step failed (errno printed)

#define _GNU_SOURCE
#include <errno.h>
#include <linux/if_link.h>
#include <linux/netlink.h>
#include <linux/rtnetlink.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#ifndef NETLINK_KOBJECT_UEVENT
#define NETLINK_KOBJECT_UEVENT 15
#endif
#ifndef NETLINK_LIST_MEMBERSHIPS
#define NETLINK_LIST_MEMBERSHIPS 9
#endif
#ifndef NETLINK_PKTINFO
#define NETLINK_PKTINFO 3
#endif
#ifndef NETLINK_EXT_ACK
#define NETLINK_EXT_ACK 11
#endif
#ifndef SOL_NETLINK
#define SOL_NETLINK 270
#endif
#ifndef SO_PROTOCOL
#define SO_PROTOCOL 38
#endif

static int failures;

static void fail(const char *step) {
    printf("__NL_FAIL:%s errno=%d (%s)\n", step, errno, strerror(errno));
    failures++;
}

// Sends one netlink request and drains the response until NLMSG_DONE or an
// NLMSG_ERROR. `on_msg` is invoked for each non-ERROR message.
static int nl_dump(int fd, struct nlmsghdr *req,
                   int (*on_msg)(struct nlmsghdr *, void *), void *arg,
                   int *ack_err) {
    struct sockaddr_nl kern = { .nl_family = AF_NETLINK };
    req->nlmsg_flags |= NLM_F_REQUEST;
    req->nlmsg_seq = 1;

    if (sendto(fd, req, req->nlmsg_len, 0,
               (struct sockaddr *)&kern, sizeof(kern)) < 0)
        return -1;

    if (ack_err)
        *ack_err = -2; // sentinel: no ACK seen

    for (;;) {
        char buf[16384];
        ssize_t n = recv(fd, buf, sizeof(buf), 0);
        if (n < 0)
            return -1;

        int done = 0;
        for (struct nlmsghdr *h = (struct nlmsghdr *)buf; NLMSG_OK(h, n);
             h = NLMSG_NEXT(h, n)) {
            if (h->nlmsg_type == NLMSG_DONE) {
                done = 1;
                break;
            }
            if (h->nlmsg_type == NLMSG_ERROR) {
                struct nlmsgerr *e = NLMSG_DATA(h);
                if (ack_err)
                    *ack_err = e->error;
                if (e->error != 0) {
                    errno = -e->error;
                    return -1;
                }
                done = 1;
                break;
            }
            if (on_msg && on_msg(h, arg) != 0) {
                done = 1;
                break;
            }
        }
        if (done)
            return 0;
    }
}

struct link_stats {
    int count;
    int saw_lo;
    int saw_eth0;
};

static int on_link(struct nlmsghdr *h, void *arg) {
    struct link_stats *st = arg;
    struct ifinfomsg *ifi = NLMSG_DATA(h);
    int attrlen = h->nlmsg_len - NLMSG_LENGTH(sizeof(*ifi));
    const char *name = "?";

    for (struct rtattr *a = IFLA_RTA(ifi); RTA_OK(a, attrlen);
         a = RTA_NEXT(a, attrlen)) {
        if (a->rta_type == IFLA_IFNAME)
            name = RTA_DATA(a);
    }

    printf("link: index=%d name=%s flags=0x%x\n", ifi->ifi_index, name,
           ifi->ifi_flags);
    st->count++;
    if (strcmp(name, "lo") == 0)
        st->saw_lo = 1;
    if (strcmp(name, "eth0") == 0)
        st->saw_eth0 = 1;
    return 0;
}

struct addr_stats {
    int count;
    int saw_lo_v4;
};

static int on_addr(struct nlmsghdr *h, void *arg) {
    struct addr_stats *st = arg;
    struct ifaddrmsg *ifa = NLMSG_DATA(h);
    int attrlen = h->nlmsg_len - NLMSG_LENGTH(sizeof(*ifa));
    unsigned char *addr = NULL;
    int addrlen = 0;

    for (struct rtattr *a = IFA_RTA(ifa); RTA_OK(a, attrlen);
         a = RTA_NEXT(a, attrlen)) {
        if (a->rta_type == IFA_LOCAL || a->rta_type == IFA_ADDRESS) {
            addr = RTA_DATA(a);
            addrlen = RTA_PAYLOAD(a);
            if (a->rta_type == IFA_LOCAL)
                break;
        }
    }

    printf("addr: family=%d index=%d prefix=%d addr=", ifa->ifa_family,
           ifa->ifa_index, ifa->ifa_prefixlen);
    if (addr) {
        for (int i = 0; i < addrlen; i++)
            printf("%s%d", i ? "." : "", addr[i]);
    } else {
        printf("<none>");
    }
    printf("\n");

    st->count++;
    if (ifa->ifa_family == AF_INET && addrlen == 4 && addr &&
        addr[0] == 127 && addr[1] == 0 && addr[2] == 0 && addr[3] == 1 &&
        ifa->ifa_prefixlen == 8)
        st->saw_lo_v4 = 1;
    return 0;
}

static void test_uevent(void) {
    int fd = socket(AF_NETLINK, SOCK_RAW | SOCK_CLOEXEC, NETLINK_KOBJECT_UEVENT);
    if (fd < 0) {
        fail("uevent:socket");
        return;
    }

    struct sockaddr_nl addr = {
        .nl_family = AF_NETLINK,
        .nl_groups = 1,
    };
    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        fail("uevent:bind");
        goto out;
    }

    int proto = -1;
    socklen_t len = sizeof(proto);
    if (getsockopt(fd, SOL_SOCKET, SO_PROTOCOL, &proto, &len) < 0 ||
        proto != NETLINK_KOBJECT_UEVENT) {
        printf("uevent: SO_PROTOCOL=%d\n", proto);
        fail("uevent:so_protocol");
        goto out;
    }

    // systemd's sd_netlink_open queries the membership-list size with a NULL
    // buffer; this must succeed and report the required size.
    len = 0;
    if (getsockopt(fd, SOL_NETLINK, NETLINK_LIST_MEMBERSHIPS, NULL, &len) < 0) {
        fail("uevent:list_memberships_size_query");
        goto out;
    }
    printf("uevent: membership size query -> optlen=%u\n", len);

    unsigned groups[8];
    len = sizeof(groups);
    if (getsockopt(fd, SOL_NETLINK, NETLINK_LIST_MEMBERSHIPS, groups, &len) < 0) {
        fail("uevent:list_memberships");
        goto out;
    }
    printf("uevent: memberships:");
    for (unsigned i = 0; i < len / sizeof(unsigned); i++)
        printf(" %u", groups[i]);
    printf("\n");
    if (len < sizeof(unsigned) || groups[0] != 1) {
        errno = 0;
        fail("uevent:list_memberships_value");
        goto out;
    }

    int one = 1, got = 0;
    if (setsockopt(fd, SOL_NETLINK, NETLINK_PKTINFO, &one, sizeof(one)) < 0) {
        fail("uevent:set_pktinfo");
        goto out;
    }
    len = sizeof(got);
    if (getsockopt(fd, SOL_NETLINK, NETLINK_PKTINFO, &got, &len) < 0 || got != 1) {
        printf("uevent: PKTINFO readback=%d\n", got);
        fail("uevent:get_pktinfo");
        goto out;
    }
    if (setsockopt(fd, SOL_NETLINK, NETLINK_EXT_ACK, &one, sizeof(one)) < 0) {
        fail("uevent:set_ext_ack");
        goto out;
    }

    printf("__NL_UEVENT_OK__\n");
out:
    close(fd);
}

static void test_getlink(int fd) {
    struct {
        struct nlmsghdr h;
        struct ifinfomsg ifi;
    } req;
    memset(&req, 0, sizeof(req));
    req.h.nlmsg_len = NLMSG_LENGTH(sizeof(req.ifi));
    req.h.nlmsg_type = RTM_GETLINK;
    req.h.nlmsg_flags = NLM_F_DUMP;
    req.ifi.ifi_family = AF_UNSPEC;

    struct link_stats st = {0};
    if (nl_dump(fd, &req.h, on_link, &st, NULL) < 0) {
        fail("route:getlink");
        return;
    }
    printf("getlink: %d links (lo=%d eth0=%d)\n", st.count, st.saw_lo,
           st.saw_eth0);
    if (st.saw_lo && st.saw_eth0)
        printf("__NL_GETLINK_OK__\n");
    else {
        errno = 0;
        fail("route:getlink_missing_iface");
    }
}

static void test_getaddr(int fd) {
    struct {
        struct nlmsghdr h;
        struct ifaddrmsg ifa;
    } req;
    memset(&req, 0, sizeof(req));
    req.h.nlmsg_len = NLMSG_LENGTH(sizeof(req.ifa));
    req.h.nlmsg_type = RTM_GETADDR;
    req.h.nlmsg_flags = NLM_F_DUMP;
    req.ifa.ifa_family = AF_UNSPEC;

    struct addr_stats st = {0};
    if (nl_dump(fd, &req.h, on_addr, &st, NULL) < 0) {
        fail("route:getaddr");
        return;
    }
    printf("getaddr: %d addrs (lo_v4=%d)\n", st.count, st.saw_lo_v4);
    if (st.saw_lo_v4)
        printf("__NL_GETADDR_OK__\n");
    else {
        errno = 0;
        fail("route:getaddr_missing_lo");
    }
}

static void test_newaddr(int fd) {
    // Re-adding the loopback address must be ACKed with -EEXIST (Linux
    // behavior; this is what systemd's loopback setup does).
    char buf[64];
    memset(buf, 0, sizeof(buf));

    struct nlmsghdr *h = (struct nlmsghdr *)buf;
    h->nlmsg_len = NLMSG_LENGTH(sizeof(struct ifaddrmsg)) + RTA_LENGTH(4);
    h->nlmsg_type = RTM_NEWADDR;
    h->nlmsg_flags = NLM_F_ACK | NLM_F_CREATE | NLM_F_EXCL;

    struct ifaddrmsg *ifa = NLMSG_DATA(h);
    ifa->ifa_family = AF_INET;
    ifa->ifa_prefixlen = 8;
    ifa->ifa_scope = RT_SCOPE_HOST;
    ifa->ifa_index = 1;

    struct rtattr *attr =
        (struct rtattr *)(buf + NLMSG_ALIGN(NLMSG_LENGTH(sizeof(*ifa))));
    attr->rta_type = IFA_LOCAL;
    attr->rta_len = RTA_LENGTH(4);
    memcpy(RTA_DATA(attr), "\x7f\x00\x00\x01", 4);

    int ack_err;
    if (nl_dump(fd, h, NULL, NULL, &ack_err) < 0) {
        if (ack_err == -EEXIST) {
            printf("newaddr: ACK -EEXIST as expected\n");
            printf("__NL_NEWADDR_OK__\n");
            return;
        }
        printf("newaddr: ack_err=%d\n", ack_err);
        fail("route:newaddr");
        return;
    }
    printf("newaddr: ack_err=%d (accepted)\n", ack_err);
    fail("route:newaddr_should_eexist");
}

int main(void) {
    setbuf(stdout, NULL);
    printf(">>> N1 netlink probe start <<<\n");

    test_uevent();

    int rfd = socket(AF_NETLINK, SOCK_RAW | SOCK_CLOEXEC, NETLINK_ROUTE);
    if (rfd < 0) {
        fail("route:socket");
    } else {
        struct sockaddr_nl addr = { .nl_family = AF_NETLINK };
        if (bind(rfd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
            fail("route:bind");
        } else {
            test_getlink(rfd);
            test_getaddr(rfd);
            test_newaddr(rfd);
        }
        close(rfd);
    }

    printf(failures ? ">>> N1 probe: %d FAILURE(S) <<<\n"
                    : ">>> N1 probe: ALL OK <<<\n",
           failures);
    return failures ? 1 : 0;
}
