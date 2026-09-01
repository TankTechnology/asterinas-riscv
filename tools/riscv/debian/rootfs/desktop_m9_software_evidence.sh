#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

umask 077

readonly CONSOLE="${ASTERINAS_DESKTOP_M9_CONSOLE:-/dev/console}"
readonly TIMEOUT_SECONDS="${ASTERINAS_DESKTOP_M9_TIMEOUT_SECONDS:-120}"
readonly COMMAND_TIMEOUT_SECONDS="${ASTERINAS_DESKTOP_M9_COMMAND_TIMEOUT_SECONDS:-120}"
readonly VIM_OUTPUT="${ASTERINAS_DESKTOP_M9_VIM_OUTPUT:-/run/asterinas-m9-vim.txt}"
# Keep the media fixture on the ext2 rootfs instead of /run's tmpfs.  This
# avoids exercising an unimplemented/undersized tmpfs path while validating
# the applications themselves.
readonly WORK_DIRECTORY="${ASTERINAS_DESKTOP_M9_WORK_DIRECTORY:-/var/tmp}"
readonly READY_MARKER="DEBIAN_DESKTOP_M9_SOFTWARE_READY vim=pass ffmpeg=pass ffprobe=pass media=pass"

failure_emitted=0
work_directory=""

emit() {
    printf '%s\n' "$1" >>"$CONSOLE"
}

fail() {
    if ((failure_emitted == 0)); then
        failure_emitted=1
        emit "DEBIAN_DESKTOP_M9_FAIL reason=$1"
    fi
    exit 1
}

cleanup() {
    if [[ -n "$work_directory" && -d "$work_directory" ]]; then
        rm -rf -- "$work_directory"
    fi
}

trap cleanup EXIT

[[ "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail invalid-timeout
[[ "$COMMAND_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail invalid-command-timeout
[[ "$VIM_OUTPUT" == /* && "$VIM_OUTPUT" != *$'\n'* && "$VIM_OUTPUT" != *$'\r'* ]] ||
    fail invalid-vim-output
[[ "$WORK_DIRECTORY" == /* && "$WORK_DIRECTORY" != *$'\n'* && "$WORK_DIRECTORY" != *$'\r'* ]] ||
    fail invalid-work-directory

deadline=$((SECONDS + TIMEOUT_SECONDS))
check_deadline() {
    ((SECONDS < deadline)) || fail overall-timeout
}

bounded() {
    timeout --signal=KILL "${COMMAND_TIMEOUT_SECONDS}s" "$@"
}

work_directory="$(mktemp -d "$WORK_DIRECTORY/asterinas-desktop-m9.XXXXXX")" ||
    fail work-directory
vim_output="$VIM_OUTPUT"
media_output="$work_directory/frame.png"
media_input="$work_directory/frame.rgb"

check_deadline
command -v vim >/dev/null 2>&1 || fail vim-missing
rm -f -- "$vim_output"
if ! bounded vim -Nu NONE -n -es \
    -c 'call setline(1, ["ASTERINAS_VIM_PASS"])' \
    -c "wq! $vim_output"; then
    fail vim-failed
fi
[[ -f "$vim_output" && ! -L "$vim_output" ]] || fail vim-output
grep -Fxq 'ASTERINAS_VIM_PASS' "$vim_output" || fail vim-content

check_deadline
command -v ffmpeg >/dev/null 2>&1 || fail ffmpeg-missing
# Feed one deterministic raw RGB frame instead of starting FFmpeg's filter
# graph. This keeps the smoke test focused on the encoder and avoids an
# unnecessarily expensive lavfi startup on the slow Asterinas RISC-V guest.
if ! bounded dd if=/dev/zero of="$media_input" bs=768 count=1 status=none; then
    fail media-input-failed
fi
if ! bounded ffmpeg -nostdin -v error -threads 1 -f rawvideo -pixel_format rgb24 \
    -video_size 16x16 -i "$media_input" -frames:v 1 -y "$media_output"; then
    fail ffmpeg-failed
fi
[[ -s "$media_output" && ! -L "$media_output" ]] || fail ffmpeg-output

check_deadline
command -v ffprobe >/dev/null 2>&1 || fail ffprobe-missing
probe_output="$(bounded ffprobe -v error -select_streams v:0 \
    -show_entries stream=width,height -of csv=p=0 "$media_output")" ||
    fail ffprobe-failed
[[ "$probe_output" == '16,16' ]] || fail ffprobe-mismatch

emit "$READY_MARKER"
