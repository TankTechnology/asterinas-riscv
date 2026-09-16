#!/bin/sh

# SPDX-License-Identifier: MPL-2.0

set -e

/test/network/tcp_ppoll_wakeup
echo "TCP ppoll wakeup regression passed."
