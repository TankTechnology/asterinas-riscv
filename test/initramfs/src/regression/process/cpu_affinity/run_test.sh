#!/bin/sh

# SPDX-License-Identifier: MPL-2.0

set -e

/test/process/cpu_affinity/cpu_affinity
/test/process/cpu_affinity/inheritance || [ "$?" -eq 77 ]
echo "CPU affinity migration test passed."
