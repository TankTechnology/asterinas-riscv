// SPDX-License-Identifier: MPL-2.0

#define _GNU_SOURCE
#include <assert.h>
#include <linux/rtnetlink.h>
#include <netlink/cache.h>
#include <netlink/netlink.h>
#include <netlink/route/addr.h>
#include <netlink/route/link.h>
#include <netlink/route/route.h>
#include <sched.h>
#include <stdbool.h>
#include <stdio.h>
#include <sys/socket.h>

#define SHARED_PORT 0x4e4c4e53U

static struct nl_sock *route_socket(void)
{
	struct nl_sock *sock = nl_socket_alloc();
	assert(sock != NULL);
	nl_socket_set_local_port(sock, SHARED_PORT);
	int result = nl_connect(sock, NETLINK_ROUTE);
	if (result != 0)
		fprintf(stderr, "route socket bind failed: %d\n", result);
	assert(result == 0);
	assert(nl_socket_get_local_port(sock) == SHARED_PORT);
	return sock;
}

static bool has_link(struct nl_sock *sock, const char *name)
{
	struct nl_cache *cache = NULL;
	assert(rtnl_link_alloc_cache(sock, AF_UNSPEC, &cache) == 0);
	struct rtnl_link *link = rtnl_link_get_by_name(cache, name);
	bool found = link != NULL;
	if (link != NULL)
		rtnl_link_put(link);
	nl_cache_free(cache);
	return found;
}

static bool has_eth0_ipv4(struct nl_sock *sock)
{
	struct nl_cache *links = NULL;
	struct nl_cache *addrs = NULL;
	assert(rtnl_link_alloc_cache(sock, AF_UNSPEC, &links) == 0);
	struct rtnl_link *eth0 = rtnl_link_get_by_name(links, "eth0");
	if (eth0 == NULL) {
		nl_cache_free(links);
		return false;
	}
	int ifindex = rtnl_link_get_ifindex(eth0);
	assert(rtnl_addr_alloc_cache(sock, &addrs) == 0);
	bool found = false;
	for (struct nl_object *obj = nl_cache_get_first(addrs); obj != NULL;
	     obj = nl_cache_get_next(obj)) {
		struct rtnl_addr *addr = (struct rtnl_addr *)obj;
		if (rtnl_addr_get_ifindex(addr) == ifindex &&
		    rtnl_addr_get_family(addr) == AF_INET) {
			found = true;
			break;
		}
	}
	nl_cache_free(addrs);
	rtnl_link_put(eth0);
	nl_cache_free(links);
	return found;
}

static bool has_main_ipv4_route(struct nl_sock *sock)
{
	struct nl_cache *routes = NULL;
	assert(rtnl_route_alloc_cache(sock, AF_INET, 0, &routes) == 0);
	bool found = false;
	for (struct nl_object *obj = nl_cache_get_first(routes); obj != NULL;
	     obj = nl_cache_get_next(obj)) {
		struct rtnl_route *route = (struct rtnl_route *)obj;
		if (rtnl_route_get_family(route) == AF_INET &&
		    rtnl_route_get_table(route) == RT_TABLE_MAIN) {
			found = true;
			break;
		}
	}
	nl_cache_free(routes);
	return found;
}

int main(void)
{
	struct nl_sock *old_sock = route_socket();
	assert(has_link(old_sock, "lo"));
	assert(has_link(old_sock, "eth0"));
	assert(has_eth0_ipv4(old_sock));
	assert(has_main_ipv4_route(old_sock));

	assert(unshare(CLONE_NEWNET) == 0);
	struct nl_sock *new_sock = route_socket();
	assert(has_link(new_sock, "lo"));
	assert(!has_link(new_sock, "eth0"));
	assert(!has_eth0_ipv4(new_sock));
	assert(!has_main_ipv4_route(new_sock));

	// The pre-unshare socket must still query its original namespace.
	assert(has_link(old_sock, "eth0"));
	assert(has_eth0_ipv4(old_sock));
	assert(has_main_ipv4_route(old_sock));

	nl_socket_free(new_sock);
	nl_socket_free(old_sock);
	puts("netlink route socket namespace regression passed.");
	return 0;
}
