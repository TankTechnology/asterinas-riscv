#!/bin/sh

# SPDX-License-Identifier: MPL-2.0

set -e

/test/network/netlink_route_netns
/test/network/netlink_route
/test/network/rtnl_err
