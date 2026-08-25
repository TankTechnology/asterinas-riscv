#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

shopt -s nullglob
readonly input_devices=(/dev/input/event*)

[[ -c /dev/fb0 ]]
((${#input_devices[@]} > 0))

chown asterinas:video /dev/fb0
chmod 0660 /dev/fb0
chown asterinas:input "${input_devices[@]}"
chmod 0660 "${input_devices[@]}"
