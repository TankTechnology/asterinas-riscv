#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

export DISPLAY=:0
export HOME=/home/asterinas
export XAUTHORITY="$HOME/.Xauthority"

if [[ "${1-}" == --xsession ]]; then
    /usr/bin/xterm -geometry 100x30+48+72 -title "Asterinas Debian" &
    exec /usr/bin/matchbox-window-manager -use_titlebar yes
fi

exec /usr/bin/xinit "$0" --xsession -- \
    /usr/bin/Xorg :0 -noreset -nolisten tcp -logfile "$HOME/Xorg.0.log" vt1
