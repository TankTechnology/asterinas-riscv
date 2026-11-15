#!/bin/sh

# SPDX-License-Identifier: MPL-2.0

set -e

/test/process/pthread/pthread_cond_handoff
echo "pthread condition handoff regression passed."
