#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
#
# Make SSH to the board work, starting from wherever the board is.
#
#   tools/riscv/board/board_ssh_ensure.sh [timeout-seconds]
#
# The image starts OpenSSH on port 22 from asterinas-debug-console.target, so
# a healthy boot needs nothing from this script. It exists for the boots where
# that did not happen: it tries SSH first, and when that fails it asks the
# serial bridge to start the server unit, and as a last resort starts a
# dropbear on the spare port, independent of systemd.
set -uo pipefail

KEY=${BOARD_KEY:-$HOME/.ssh/asterinas_megrez}
PORT=${BOARD_PORT:-22}
EMERGENCY_PORT=${BOARD_EMERGENCY_PORT:-2222}
TARGET=${BOARD_TARGET:-root@58.206.232.122}
FIFO=${BOARD_CONSOLE_FIFO:-/tmp/megrez.cmd}
DEADLINE=$((SECONDS + ${1:-180}))

ssh_ok() {
    local port=${1:-$PORT}
    timeout 6 ssh -i "$KEY" -p "$port" -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o ConnectTimeout=4 \
        "$TARGET" true 2>/dev/null
}

wait_up() { # port, rounds of 4 seconds
    local port=$1 rounds=${2:-4}
    for _ in $(seq 1 "$rounds"); do
        sleep 4
        if ssh_ok "$port"; then
            return 0
        fi
    done
    return 1
}

if ssh_ok "$PORT"; then
    echo "ssh already up on $PORT"
    exit 0
fi

# The console fifo is the only way in before SSH exists. If the bridge is not
# running, say so instead of looping silently.
if [ ! -w "$FIFO" ]; then
    echo "no console fifo at $FIFO; is megrez_console.py bridge running?" >&2
    exit 1
fi

printf 'systemctl start asterinas-openssh.service 2>/dev/null\n' > "$FIFO"
if wait_up "$PORT"; then
    echo "ssh up after starting asterinas-openssh on the console"
    exit 0
fi

printf 'systemctl start asterinas-dropbear.service 2>/dev/null\n' > "$FIFO"
if wait_up "$PORT"; then
    echo "ssh up after starting asterinas-dropbear on the console"
    exit 0
fi

# Last resort: a dropbear on the spare port, with no unit in between. It does
# not touch port 22, so it cannot fight a half-started OpenSSH.
while [ $SECONDS -lt $DEADLINE ]; do
    printf 'dropbear -p %s -r /etc/dropbear/dropbear_ed25519_host_key -E >/dev/null 2>&1 &\n' \
        "$EMERGENCY_PORT" > "$FIFO"
    if wait_up "$EMERGENCY_PORT"; then
        echo "ssh up on $EMERGENCY_PORT via a console dropbear"
        exit 0
    fi
done

echo "ssh did not come up within the deadline" >&2
exit 1
