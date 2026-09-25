#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

shopt -s nullglob
readonly input_devices=(/dev/input/event*)
[[ -c /dev/dri/card0 ]]
((${#input_devices[@]} > 0))

chown asterinas:video /dev/dri/card0
chmod 0660 /dev/dri/card0
for render_node in /dev/dri/renderD*; do
    [[ -c "$render_node" ]] || continue
    chown asterinas:render "$render_node" 2>/dev/null || chown asterinas:video "$render_node"
    chmod 0660 "$render_node"
done
chown asterinas:input "${input_devices[@]}"
chmod 0660 "${input_devices[@]}"
