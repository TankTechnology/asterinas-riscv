#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

readonly DISPLAY=:0
readonly FIREFOX_HOME="${HOME:-/home/asterinas}"
readonly XAUTHORITY="$FIREFOX_HOME/.Xauthority"
readonly PROFILE="$FIREFOX_HOME/.mozilla/asterinas-browser-web"
readonly NETWORK_MODE="${ASTERINAS_WEB_NETWORK_MODE:-}"
readonly PROXY_HOST="${ASTERINAS_DESKTOP_PROXY_HOST:-}"
readonly PROXY_PORT="${ASTERINAS_DESKTOP_PROXY_PORT:-}"
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
readonly JIT_OVERLAY=/usr/share/asterinas/firefox-riscv-jit-overlay.json
if [[ -f "$JIT_OVERLAY" && ! -L "$JIT_OVERLAY" ]]; then
    readonly FIREFOX_BIN=/usr/bin/firefox
    readonly FIREFOX_LIBRARY_DIR=/usr/lib/firefox
else
    readonly FIREFOX_BIN=/usr/bin/firefox-esr
    readonly FIREFOX_LIBRARY_DIR=/usr/lib/firefox-esr
fi
export DISPLAY XAUTHORITY
export ASTERINAS_FIREFOX_WEB_TARGET_URL="$TARGET_URL"
export ASTERINAS_FIREFOX_WEB_NETWORK_MODE="$NETWORK_MODE"

validate_network_profile() {
    local octet
    local -a octets

    case "$NETWORK_MODE" in
        direct)
            [[ -z "$PROXY_HOST" && -z "$PROXY_PORT" ]] || return 1
            ;;
        proxy)
            [[ "$PROXY_HOST" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || return 1
            IFS=. read -r -a octets <<<"$PROXY_HOST"
            for octet in "${octets[@]}"; do
                ((10#$octet <= 255)) || return 1
            done
            [[ "$PROXY_PORT" =~ ^[1-9][0-9]{0,4}$ ]] || return 1
            ((PROXY_PORT <= 65535)) || return 1
            ;;
        *) return 1 ;;
    esac
}

configure_network_profile() {
    local temporary

    validate_network_profile || {
        printf 'ASTERINAS_FIREFOX_WEB_FAIL reason=invalid-network-profile\n' >&2
        return 1
    }
    /usr/bin/mkdir -p -- "$PROFILE"
    temporary="$(/usr/bin/mktemp "$PROFILE/user.js.tmp.XXXXXX")"
    if ! {
        # Preseed the non-update subset of Firefox ESR 140's automation
        # preferences before the cold RISC-V launch.  This prevents bounded
        # discovery/new-tab traffic from competing with the requested page,
        # while retaining system-add-on, extension, Safe Browsing, TLS, and
        # application update defaults.  The captive-portal service is a
        # separate enabled-by-default requester in ESR 140 and is included
        # explicitly.  Sources (Debian package: Firefox ESR 140.14.0):
        # https://searchfox.org/mozilla-esr140/source/remote/shared/RecommendedPreferences.sys.mjs
        # https://searchfox.org/mozilla-esr140/source/browser/app/profile/firefox.js
        printf '%s\n' \
            'user_pref("browser.newtabpage.enabled", false);' \
            'user_pref("browser.pagethumbnails.capturing_disabled", true);' \
            'user_pref("browser.region.network.url", "");' \
            'user_pref("browser.topsites.contile.enabled", false);' \
            'user_pref("network.captive-portal-service.enabled", false);' \
            'user_pref("network.connectivity-service.enabled", false);' \
            'user_pref("browser.download.folderList", 2);' \
            'user_pref("browser.download.dir", "/home/asterinas/Downloads");' \
            'user_pref("browser.download.useDownloadDir", true);' \
            'user_pref("browser.helperApps.neverAsk.saveToDisk", "application/octet-stream");' \
            >"$temporary"
        if [[ "$NETWORK_MODE" == proxy ]]; then
            printf '%s\n' \
                'user_pref("network.proxy.type", 1);' \
                "user_pref(\"network.proxy.http\", \"$PROXY_HOST\");" \
                "user_pref(\"network.proxy.http_port\", $PROXY_PORT);" \
                "user_pref(\"network.proxy.ssl\", \"$PROXY_HOST\");" \
                "user_pref(\"network.proxy.ssl_port\", $PROXY_PORT);" \
                "user_pref(\"network.proxy.no_proxies_on\", \"localhost, 127.0.0.1, $PROXY_HOST\");" \
                >>"$temporary"
        else
            printf '%s\n' 'user_pref("network.proxy.type", 0);' >>"$temporary"
        fi
        /usr/bin/chmod 0600 -- "$temporary"
        /usr/bin/mv -T -- "$temporary" "$PROFILE/user.js"
    }; then
        /usr/bin/rm -f -- "$temporary"
        return 1
    fi
}

if [[ "${1:-}" == --prepare-profile && $# == 1 ]]; then
    configure_network_profile
    exit
fi
[[ $# == 0 ]] || {
    printf 'usage: %s [--prepare-profile]\n' "$0" >&2
    exit 2
}
configure_network_profile
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY
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
    local raw seconds fraction ignored
    IFS=' ' read -r raw ignored </proc/uptime
    [[ "$raw" =~ ^([0-9]+)\.([0-9]+)$ ]] || return 1
    seconds="${BASH_REMATCH[1]}"
    fraction="${BASH_REMATCH[2]}000000000"
    printf '%s%s' "$seconds" "${fraction:0:9}"
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
# Firefox's POSIX profiler signal path writes its diagnostic capture into the
# user's download directory.  Provisioning the normal per-user directory here
# is harmless for production and avoids a root-owned late mkdir in the
# evidence service when a diagnostic run requests a profile dump.
/usr/bin/mkdir -p -- "$FIREFOX_HOME/Downloads"
# The dedicated profile was written atomically before any Firefox process was
# started, so a mode switch cannot inherit stale proxy preferences.
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
        "$FIREFOX_LIBRARY_DIR/libxul.so" \
        "$FIREFOX_LIBRARY_DIR/omni.ja"; do
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
        # This helper is forked before the final exec, so $$ remains the
        # Firefox browser PID after the shell is replaced.  Always inspect
        # that stable target rather than the short-lived diagnostic shell;
        # otherwise /proc/syscall and /proc/io only describe the sampler.
        readonly target_pid="$$"
        for tick in $(seq 1 12); do
            printf 'ASTERINAS_FIREFOX_PS tick=%s\n' "$tick" >&2
            # Do not request wchan here.  The guest procfs does not yet expose
            # task/wchan files cheaply or completely; asking ps for wchan can
            # block the diagnostic child and hide the process table we need.
            /usr/bin/timeout 5 /usr/bin/ps -eo pid,ppid,stat,comm,args >&2 || true
            if [[ "${ASTERINAS_FIREFOX_PROC_DIAGNOSTIC:-0}" == 1 ]]; then
                proc_syscall="$(/usr/bin/timeout 2 /usr/bin/cat "/proc/$target_pid/syscall" 2>/dev/null | cut -c1-180 || true)"
                proc_io="$(/usr/bin/timeout 2 /usr/bin/awk '/^(rchar|read_bytes|syscr):/ {printf "%s=%s,", $1, $2}' "/proc/$target_pid/io" 2>/dev/null | sed 's/,$//' || true)"
                proc_context="$(/usr/bin/timeout 2 /usr/bin/awk '/^(voluntary_ctxt_switches|nonvoluntary_ctxt_switches):/ {printf "%s=%s,", $1, $2}' "/proc/$target_pid/status" 2>/dev/null | sed 's/,$//' || true)"
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
exec "$FIREFOX_BIN" --no-remote --new-instance --marionette \
    --profile "$PROFILE" "$START_URL" >>"$STDERR_LOG" 2>&1
