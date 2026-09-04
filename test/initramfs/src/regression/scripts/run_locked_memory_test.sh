#!/bin/sh

# SPDX-License-Identifier: MPL-2.0

set -e

cd /test/memory
./mmap/mlock

echo "Locked-memory regression passed."
