#!/bin/sh

# SPDX-License-Identifier: MPL-2.0

set -eu

/test/process/sched/sched_permissions
/test/process/sched/sched_policy
echo "Scheduler policy regression passed."
