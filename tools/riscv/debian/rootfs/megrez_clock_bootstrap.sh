#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Seed the guest clock from the image's reproducible build timestamp before
# systemd services start.  The board has no RTC; without this the guest keeps
# an unrelated fixed date (the build host's clock leaked into the image).
# A later online clock-sync may replace this value.

set -eu

readonly BUILD_TIME_FILE="${ASTERINAS_BUILD_TIME_FILE:-/etc/asterinas-build-time}"
readonly DATE_COMMAND="${ASTERINAS_DATE_COMMAND:-/bin/date}"

if [ ! -r "$BUILD_TIME_FILE" ]; then
    printf '%s\n' 'ASTERINAS_CLOCK_BUILD_TIME_MISSING'
    exit 0
fi

build_time="$(cat "$BUILD_TIME_FILE")"
case "$build_time" in
    ''|*[!0-9]*)
        printf '%s\n' 'ASTERINAS_CLOCK_BUILD_TIME_INVALID'
        exit 0
        ;;
esac

current="$("$DATE_COMMAND" +%s)"
# Values older than the build time or more than a year after it are not a
# trustworthy boot clock.  Keep a plausible clock untouched so this does not
# fight an RTC/NTP that is already correct.
if [ "$current" -ge "$build_time" ] &&
    [ "$current" -lt "$((build_time + 31536000))" ]; then
    printf 'ASTERINAS_CLOCK_BUILD_TIME_SKIPPED current=%s build_time=%s\n' \
        "$current" "$build_time"
    exit 0
fi

"$DATE_COMMAND" --utc --set "@$build_time"
printf 'ASTERINAS_CLOCK_BUILD_TIME_APPLIED current_before=%s build_time=%s\n' \
    "$current" "$build_time"
