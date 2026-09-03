#!/bin/sh

# SPDX-License-Identifier: MPL-2.0

set -e

./clock_nanosleep/nanosleep_err

./clock_settime/monotonic_coarse

./gettimeofday/gettimeofday

./itimer/setitimer
./itimer/timer_create
