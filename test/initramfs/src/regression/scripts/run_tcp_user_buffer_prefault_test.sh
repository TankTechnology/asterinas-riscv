#!/bin/sh

# SPDX-License-Identifier: MPL-2.0

set -e

/test/network/tcp_user_buffer_prefault
echo "TCP user buffer prefault regression passed."
