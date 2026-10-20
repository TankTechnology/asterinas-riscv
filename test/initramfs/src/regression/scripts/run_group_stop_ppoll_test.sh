#!/bin/sh

# SPDX-License-Identifier: MPL-2.0

set -e

/test/process/signal/group_stop_ppoll
echo "Focused group-stop ppoll regression passed."
