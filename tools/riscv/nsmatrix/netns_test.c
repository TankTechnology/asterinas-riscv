#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/netlink.h>
#include <linux/rtnetlink.h>
#include <net/if.h>
#include <sched.h>
#include <stdio.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>
#include <netinet/in.h>
#include <arpa/inet.h>

#define CHECK(cond, msg) do { if (!(cond)) { printf("FAIL: %s (errno=%d %s)\n", msg, errno, strerror(errno)); _exit(1); } printf("ok: %s\n", msg); } while (0)

/* Linux UAPI: the loopback interface is index 1 in every network namespace.
   systemd's loopback_setup() relies on this fixed value. */
#define LOOPBACK_IFINDEX 1

static int nl_open(void)
{
    int fd = socket(AF_NETLINK, SOCK_RAW, NETLINK_ROUTE);
    if (fd < 0) return -1;
    struct sockaddr_nl sa = { .nl_family = AF_NETLINK };
    if (bind(fd, (void *)&sa, sizeof(sa)) < 0) { close(fd); return -1; }
    return fd;
}

/* Dumps link names/flags via RTM_GETLINK. Returns count, fills names/flags. */
static int dump_links(int fd, char names[][16], unsigned *flags, int max)
{
    struct {
        struct nlmsghdr h;
        struct rtgenmsg g;
    } req = {
        .h = { .nlmsg_len = sizeof(req), .nlmsg_type = RTM_GETLINK,
               .nlmsg_flags = NLM_F_REQUEST | NLM_F_DUMP },
        .g = { .rtgen_family = AF_PACKET },
    };
    if (send(fd, &req, sizeof(req), 0) < 0) return -1;

    int count = 0;
    char buf[8192];
    for (;;) {
        ssize_t n = recv(fd, buf, sizeof(buf), 0);
        if (n <= 0) break;
        for (struct nlmsghdr *h = (void *)buf; NLMSG_OK(h, n); h = NLMSG_NEXT(h, n)) {
            if (h->nlmsg_type == NLMSG_DONE) return count;
            if (h->nlmsg_type == NLMSG_ERROR) {
                struct nlmsgerr *e = NLMSG_DATA(h);
                errno = e->error ? -e->error : 0;
                return e->error ? -1 : count;
            }
            if (h->nlmsg_type != RTM_NEWLINK) continue;
            struct ifinfomsg *ifi = NLMSG_DATA(h);
            struct rtattr *rta = (void *)(ifi + 1);
            int len = h->nlmsg_len - NLMSG_LENGTH(sizeof(*ifi));
            for (; RTA_OK(rta, len); rta = RTA_NEXT(rta, len)) {
                if (rta->rta_type == IFLA_IFNAME && count < max) {
                    snprintf(names[count], 16, "%s", (char *)RTA_DATA(rta));
                    flags[count] = ifi->ifi_flags;
                    count++;
                }
            }
        }
    }
    return count;
}

static int find_link(char names[][16], int count, const char *name)
{
    for (int i = 0; i < count; i++)
        if (strcmp(names[i], name) == 0) return i;
    return -1;
}

/* Sets interface flags via RTM_NEWLINK (ip link set <ifindex> up/down). */
static int set_link_up(int fd, int ifindex, int up)
{
    struct {
        struct nlmsghdr h;
        struct ifinfomsg ifi;
    } req = {
        .h = { .nlmsg_len = sizeof(req), .nlmsg_type = RTM_NEWLINK,
               .nlmsg_flags = NLM_F_REQUEST | NLM_F_ACK },
        .ifi = { .ifi_family = AF_UNSPEC, .ifi_index = ifindex,
                 .ifi_flags = up ? IFF_UP : 0, .ifi_change = IFF_UP },
    };
    if (send(fd, &req, sizeof(req), 0) < 0) return -1;
    char buf[512];
    ssize_t n = recv(fd, buf, sizeof(buf), 0);
    if (n <= 0) return -1;
    struct nlmsghdr *h = (void *)buf;
    if (h->nlmsg_type == NLMSG_ERROR) {
        struct nlmsgerr *e = NLMSG_DATA(h);
        if (e->error) { errno = -e->error; return -1; }
    }
    return 0;
}

/* ifindex of lo in the current netns via a fresh dump: lo is reported in
   order; we need the index, so re-dump with indices. */
static int lo_index(int fd)
{
    struct {
        struct nlmsghdr h;
        struct rtgenmsg g;
    } req = {
        .h = { .nlmsg_len = sizeof(req), .nlmsg_type = RTM_GETLINK,
               .nlmsg_flags = NLM_F_REQUEST | NLM_F_DUMP },
        .g = { .rtgen_family = AF_PACKET },
    };
    if (send(fd, &req, sizeof(req), 0) < 0) return -1;
    int found = -1;
    char buf[8192];
    for (;;) {
        ssize_t n = recv(fd, buf, sizeof(buf), 0);
        if (n <= 0) break;
        for (struct nlmsghdr *h = (void *)buf; NLMSG_OK(h, n); h = NLMSG_NEXT(h, n)) {
            if (h->nlmsg_type == NLMSG_DONE) return found;
            if (h->nlmsg_type != RTM_NEWLINK) continue;
            struct ifinfomsg *ifi = NLMSG_DATA(h);
            struct rtattr *rta = (void *)(ifi + 1);
            int len = h->nlmsg_len - NLMSG_LENGTH(sizeof(*ifi));
            for (; RTA_OK(rta, len); rta = RTA_NEXT(rta, len))
                if (rta->rta_type == IFLA_IFNAME && strcmp(RTA_DATA(rta), "lo") == 0)
                    found = ifi->ifi_index;
        }
    }
    return found;
}

static void ns_child(void)
{
    CHECK(unshare(CLONE_NEWUSER) == 0, "child unshare(NEWUSER)");
    CHECK(unshare(CLONE_NEWNET) == 0, "child unshare(NEWNET)");

    int nl = nl_open();
    CHECK(nl >= 0, "child netlink socket");

    char names[8][16];
    unsigned flags[8];
    int count = dump_links(nl, names, flags, 8);
    CHECK(count >= 0, "child RTM_GETLINK dump");
    for (int i = 0; i < count; i++)
        printf("child sees link: %s flags=0x%x\n", names[i], flags[i]);
    CHECK(count == 1, "child sees exactly one link");
    int lo = find_link(names, count, "lo");
    CHECK(lo >= 0, "child sees lo");
    CHECK(find_link(names, count, "eth0") < 0 && find_link(names, count, "virtio") < 0,
          "child does not see host interfaces");
    CHECK(!(flags[lo] & IFF_UP), "lo starts down in new netns");

    int idx = lo_index(nl);
    CHECK(idx > 0, "lo ifindex resolved");
    CHECK(idx == LOOPBACK_IFINDEX, "lo uses fixed ifindex 1");
    CHECK(set_link_up(nl, idx, 1) == 0, "RTM_NEWLINK lo up (ip link set lo up)");
    count = dump_links(nl, names, flags, 8);
    CHECK(count == 1 && (flags[0] & IFF_UP), "lo is up after RTM_NEWLINK");

    /* TCP loopback inside the new namespace. */
    int listener = socket(AF_INET, SOCK_STREAM, 0);
    CHECK(listener >= 0, "child socket()");
    struct sockaddr_in addr = {
        .sin_family = AF_INET,
        .sin_port = htons(43210),
        .sin_addr.s_addr = htonl(INADDR_LOOPBACK),
    };
    CHECK(bind(listener, (void *)&addr, sizeof(addr)) == 0, "bind 127.0.0.1");
    CHECK(listen(listener, 1) == 0, "listen on lo");

    pid_t c = fork();
    if (c == 0) {
        int s = socket(AF_INET, SOCK_STREAM, 0);
        if (s < 0) _exit(2);
        if (connect(s, (void *)&addr, sizeof(addr)) < 0) _exit(3);
        if (write(s, "ping", 4) != 4) _exit(4);
        _exit(0);
    }
    int a = accept(listener, NULL, NULL);
    CHECK(a >= 0, "accept on lo");
    char buf[8] = {0};
    CHECK(read(a, buf, 4) == 4 && memcmp(buf, "ping", 4) == 0, "loopback TCP payload received");
    int st;
    CHECK(waitpid(c, &st, 0) == c && WEXITSTATUS(st) == 0, "loopback TCP connector exited cleanly");

    _exit(0);
}

int main(void)
{
    int nl = nl_open();
    CHECK(nl >= 0, "parent netlink socket");

    char names[8][16];
    unsigned flags[8];
    int count = dump_links(nl, names, flags, 8);
    CHECK(count >= 1, "parent RTM_GETLINK dump");
    for (int i = 0; i < count; i++)
        printf("parent sees link: %s flags=0x%x\n", names[i], flags[i]);
    int parent_links = count;

    pid_t c = fork();
    if (c == 0) ns_child();
    int st;
    CHECK(waitpid(c, &st, 0) == c && WIFEXITED(st) && WEXITSTATUS(st) == 0,
          "netns child checks passed");

    count = dump_links(nl, names, flags, 8);
    CHECK(count == parent_links, "parent link view unchanged");

    printf("NETNS_TEST_PASS\n");
    return 0;
}
