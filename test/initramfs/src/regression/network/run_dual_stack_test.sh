#!/bin/sh

# SPDX-License-Identifier: MPL-2.0

set -e

cd "$(dirname "$0")"

./ipv6_dual_stack
./ipv6_udp
./ipv6_dual_stack_udp
