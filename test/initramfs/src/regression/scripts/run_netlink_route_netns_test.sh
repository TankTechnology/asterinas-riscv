#!/bin/sh

# SPDX-License-Identifier: MPL-2.0

set -e

/test/network/netlink_route_netns
/test/network/route_get
/test/network/route_local_dump
/test/network/netlink_uevent_port_netns
/test/network/netlink_route
/test/network/rtnl_err
/test/network/uevent_err
