#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Acceptance run for the Megrez board: cold boot, clock, network, the page in
# Firefox, and the toolchain, in one bounded pass.
#
#   megrez_smoke.sh              # check the board as it is
#   megrez_smoke.sh --reboot     # cold boot first
#
# The console bridge must be running:
#   megrez_console.py bridge /dev/ttyUSB0 --log /tmp/board2.log --fifo /tmp/board2.cmd
exec python3 "$(dirname -- "$0")/megrez_smoke.py" "$@"
