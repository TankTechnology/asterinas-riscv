#!/bin/sh

# SPDX-License-Identifier: MPL-2.0

set -eu

/test/memory/mmap/mmap_err
echo "mmap error-order regression passed."
