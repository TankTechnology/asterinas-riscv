#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
#
# Make SSH to the board work, starting from wherever the board is.
#
#   tools/riscv/board/board_ssh_ensure.sh [timeout-seconds]
#
# The board has no unit that starts dropbear, so after every boot SSH is dead
# until someone types the command at the debug console. This helper tries SSH
# first, and when that fails it asks the serial bridge to type the dropbear
# line, then waits for the port. It is the difference between "the board came
# back" and "the board came back and we can reach it".
set -uo pipefail

KEY=${BOARD_KEY:-$HOME/.ssh/asterinas_megrez}
PORT=${BOARD_PORT:-2222}
TARGET=${BOARD_TARGET:-root@58.206.232.122}
FIFO=${BOARD_CONSOLE_FIFO:-/tmp/megrez.cmd}
DEADLINE=$((SECONDS + ${1:-180}))

ssh_ok() {
    timeout 6 ssh -i "$KEY" -p "$PORT" -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o ConnectTimeout=4 \
        "$TARGET" true 2>/dev/null
}

if ssh_ok; then
    echo "ssh already up"
    exit 0
fi

# The console fifo is the only way in before SSH exists. If the bridge is not
# running, say so instead of looping silently.
if [ ! -w "$FIFO" ]; then
    echo "no console fifo at $FIFO; is megrez_console.py bridge running?" >&2
    exit 1
fi

while [ $SECONDS -lt $DEADLINE ]; do
    printf 'dropbear -p 2222 -r /etc/dropbear/dropbear_ed25519_host_key -E >/dev/null 2>&1 &\n' > "$FIFO"
    for _ in $(seq 1 4); do
        sleep 4
        if ssh_ok; then
            echo "ssh up after starting dropbear on the console"
            exit 0
        fi
    done
done

echo "ssh did not come up within the deadline" >&2
exit 1
