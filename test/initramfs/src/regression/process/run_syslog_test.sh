#!/bin/sh

# SPDX-License-Identifier: MPL-2.0

set -e

cd /test/process
./syslog/syslog
echo "Focused syslog regression tests passed."
