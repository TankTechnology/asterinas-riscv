#!/bin/sh

# SPDX-License-Identifier: MPL-2.0

set -eu

/test/process/execve/execve_memfd

echo "memfd exec regression passed."
