#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

shopt -s nullglob
readonly input_devices=(/dev/input/event*)

if [[ "${ASTERINAS_BROWSER_WEB_SESSION:-0}" == 1 ]]; then
    # Keep diagnostics off the synchronous console path.  On Asterinas the
    # console driver can block a service while servicing terminal queries;
    # device setup itself is independent of that observation.
    printf '%s\n' 'BROWSER_WEB_DESKTOP_STAGE=device-access-start' \
        >>/run/browser-web-device-stage.log
fi
[[ -c /dev/fb0 ]]
((${#input_devices[@]} > 0))

chown asterinas:video /dev/fb0
chmod 0660 /dev/fb0
chown asterinas:input "${input_devices[@]}"
chmod 0660 "${input_devices[@]}"
if [[ "${ASTERINAS_BROWSER_WEB_SESSION:-0}" == 1 ]]; then
    printf '%s\n' 'BROWSER_WEB_DESKTOP_STAGE=device-access-done' \
        >>/run/browser-web-device-stage.log
fi
