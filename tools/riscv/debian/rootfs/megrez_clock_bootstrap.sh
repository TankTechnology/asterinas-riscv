#!/bin/sh
# SPDX-License-Identifier: MPL-2.0
#
# Seed the guest clock from the image's reproducible build timestamp before
# systemd services start.  The board has no RTC; without this the guest keeps
# an unrelated fixed date (the build host's clock leaked into the image).
# A later online clock-sync may replace this value.

set -eu

readonly BUILD_TIME_FILE="${ASTERINAS_BUILD_TIME_FILE:-/etc/asterinas-build-time}"
# The newest time this machine has seen.  `asterinas-clock-persist.timer`
# writes it every fifteen minutes; see that unit for why the build timestamp
# alone is not enough.
readonly LAST_TIME_FILE="${ASTERINAS_LAST_TIME_FILE:-/var/lib/asterinas/last-time}"
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

# Without an RTC the boot clock is the image build time, which gets older every
# day the image sits unused; every certificate then reads "not yet valid" and
# the first page fails although the network is fine.  The last recorded time is
# the only source that can be recent *before* the network is up (the HTTP Date
# source needs the portal, which needs the clock).  Accept it only when it is
# newer than the image and less than a year old, so a garbage file cannot seed
# a wild clock.
# Anything past 2100 is a corrupt file rather than a time this machine saw; the
# lower bound is the image itself, so a file from an older image is ignored.
readonly SANITY_UPPER=4102444800

seed="$build_time"
source="build-time"
if [ -r "$LAST_TIME_FILE" ]; then
    last_time="$(cat "$LAST_TIME_FILE" 2>/dev/null || true)"
    case "$last_time" in
        ''|*[!0-9]*)
            printf 'ASTERINAS_CLOCK_LAST_TIME_INVALID file=%s\n' "$LAST_TIME_FILE"
            ;;
        *)
            if [ "$last_time" -ge "$build_time" ] &&
                [ "$last_time" -lt "$SANITY_UPPER" ]; then
                seed="$last_time"
                source="last-time"
            else
                printf 'ASTERINAS_CLOCK_LAST_TIME_REJECTED last_time=%s build_time=%s\n' \
                    "$last_time" "$build_time"
            fi
            ;;
    esac
fi

current="$("$DATE_COMMAND" +%s)"
# Keep a clock that is already at or ahead of the seed: it belongs to an RTC or
# an earlier sync, and moving it backwards would be worse than doing nothing.
if [ "$current" -ge "$seed" ] && [ "$current" -lt "$SANITY_UPPER" ]; then
    printf 'ASTERINAS_CLOCK_SEED_SKIPPED current=%s seed=%s source=%s\n' \
        "$current" "$seed" "$source"
    exit 0
fi

"$DATE_COMMAND" --utc --set "@$seed"
printf 'ASTERINAS_CLOCK_SEED_APPLIED current_before=%s seed=%s source=%s\n' \
    "$current" "$seed" "$source"
