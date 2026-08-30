#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

readonly DISPLAY=:0
readonly FIREFOX_HOME=/home/asterinas
readonly XAUTHORITY="$FIREFOX_HOME/.Xauthority"
readonly PROFILE="$FIREFOX_HOME/.mozilla/asterinas-browser-web"
# Let Firefox finish its parent/child, window, and Marionette bootstrap before
# the web gate performs real HTTPS navigation.  The target is fixed in the
# unit environment and is validated by browser_web_evidence.sh; it is not an
# offline or sandbox-disabled launch.
readonly START_URL="about:blank"
readonly TARGET_URL="https://www.baidu.com/"
readonly STDERR_LOG="$FIREFOX_HOME/firefox-web-stderr.log"
readonly MOZILLA_LOG="$FIREFOX_HOME/firefox-web-mozilla.log"
readonly CONSOLE="${ASTERINAS_BROWSER_WEB_CONSOLE:-/dev/console}"
readonly TIMELINE="$FIREFOX_HOME/browser-web-timeline.log"
readonly PID_FILE="$FIREFOX_HOME/browser-web-firefox.pid"
readonly TIMEOUT_SECONDS=30
export DISPLAY XAUTHORITY
export ASTERINAS_FIREFOX_WEB_TARGET_URL="$TARGET_URL"
printf '%s\n' "$$" >"$PID_FILE"

# Provisioned files are created in the immutable image so Firefox does not
# depend on early-boot mkdir/open behavior on the guest ext2 writer.  Keep the
# diagnostics owned by the unprivileged browser user and capture startup from
# the first readiness probe onward.
: >"$STDERR_LOG"
exec 3>&2
/usr/bin/tail -n 0 -f "$STDERR_LOG" >&3 &
readonly stderr_tailer_pid=$!

guest_monotonic_ns() {
    local raw="${EPOCHREALTIME-}"
    if [[ "$raw" =~ ^[0-9]+\.[0-9]{6}$ ]]; then
        raw="${raw/./}"
        printf '%s000' "$raw"
    else
        printf '%s000000000' "${EPOCHSECONDS:-0}"
    fi
}

marker() {
    local name="$1" line guest_ns
    guest_ns="$(guest_monotonic_ns)"
    line="A_WEB_TIMELINE marker=$name guest_monotonic_ns=$guest_ns firefox_pid=$$"
    printf '%s\n' "$line" >>"$TIMELINE"
    # Console access is best-effort for the unprivileged browser user.  Keep
    # the failed redirection itself out of stderr so it cannot obscure the
    # Firefox diagnostics we are trying to capture.
    (printf '%s\n' "$line" >>"$CONSOLE") 2>/dev/null || true
}

printf 'ASTERINAS_FIREFOX_WEB wrapper-start pid=%s\n' "$$"
# Xorg has a single owner: desktop-m5-session.  Starting a second server here
# races the provider for :0 and makes the first server disappear underneath
# Firefox.  wait-x is the explicit readiness barrier and fails closed if the
# desktop provider never publishes a usable endpoint.
/usr/lib/asterinas/browser-web-timeline wait-x
marker BOOT_FIREFOX_WRAPPER_START
/usr/bin/mkdir -p -- "$PROFILE"
# Keep the normal startup path at the same logging cost as the proven M5
# Firefox launcher.  Network-category tracing can generate a large amount of
# synchronous ext2/virtio I/O on this guest; enable it only for an explicit
# diagnostic run.
export MOZ_LOG="${ASTERINAS_FIREFOX_VERBOSE_LOG:-timestamp,Widget:2,Marionette:2}"
export MOZ_LOG_FILE="$MOZILLA_LOG"
export MOZ_SANDBOX_LOGGING=1
export MOZ_AVOID_OPENGL_ALTOGETHER=1
# Optional sequential page-cache warm-up for bring-up on very slow virtual
# block devices.  It is disabled in the normal image and never changes the
# Firefox command line or sandbox policy.
if [[ "${ASTERINAS_FIREFOX_PREWARM:-0}" == 1 ]]; then
    for preload in \
        /usr/lib/firefox-esr/libxul.so \
        /usr/lib/firefox-esr/omni.ja; do
        printf 'ASTERINAS_FIREFOX_PREWARM file=%s\n' "$preload" >&2
        /usr/bin/timeout 300 /usr/bin/cat "$preload" >/dev/null ||
            printf 'ASTERINAS_FIREFOX_PREWARM_FAIL file=%s\n' "$preload" >&2
    done
fi
printf 'ASTERINAS_FIREFOX_WEB_EXEC pid=%s\n' "$$"
marker BOOT_FIREFOX_EXEC
# Optional bounded process snapshot for bring-up.  It is disabled in the
# normal service and can only be enabled explicitly in a diagnostic image.
if [[ "${ASTERINAS_FIREFOX_PS_DIAGNOSTIC:-0}" == 1 ]]; then
    (
        for tick in $(seq 1 12); do
            printf 'ASTERINAS_FIREFOX_PS tick=%s\n' "$tick" >&2
            /usr/bin/timeout 5 /usr/bin/ps -eo pid,ppid,stat,wchan:32,comm,args >&2 || true
            if [[ "${ASTERINAS_FIREFOX_PROC_DIAGNOSTIC:-0}" == 1 ]]; then
                proc_syscall="$(/usr/bin/timeout 2 /usr/bin/cat "/proc/$$/syscall" 2>/dev/null | cut -c1-180 || true)"
                proc_io="$(/usr/bin/timeout 2 /usr/bin/awk '/^(rchar|read_bytes|syscr):/ {printf "%s=%s,", $1, $2}' "/proc/$$/io" 2>/dev/null | sed 's/,$//' || true)"
                proc_context="$(/usr/bin/timeout 2 /usr/bin/awk '/^(voluntary_ctxt_switches|nonvoluntary_ctxt_switches):/ {printf "%s=%s,", $1, $2}' "/proc/$$/status" 2>/dev/null | sed 's/,$//' || true)"
                printf 'ASTERINAS_FIREFOX_PROC tick=%s syscall=%s io=%s context=%s\n' \
                    "$tick" "$(printf '%s' "${proc_syscall:-none}" | tr ' ' '_')" \
                    "${proc_io:-none}" "${proc_context:-none}" >&2
            fi
            /usr/bin/timeout 12 /usr/bin/sleep 10 || true
        done
    ) >&2 2>&1 &
fi
# Keep Firefox's stderr in its persistent evidence file while mirroring it to
# the service journal.  The Firefox process remains the systemd MainPID
# because the final exec below is unchanged; tail is merely a diagnostic
# child and is killed with the service on restart.
exec /usr/bin/firefox-esr --no-remote --new-instance --marionette \
    --profile "$PROFILE" "$START_URL" >>"$STDERR_LOG" 2>&1
