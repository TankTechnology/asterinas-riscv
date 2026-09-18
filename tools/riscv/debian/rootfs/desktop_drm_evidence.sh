#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

readonly CONSOLE="${ASTERINAS_DESKTOP_DRM_CONSOLE:-/dev/console}"
readonly XORG_LOG="${ASTERINAS_DESKTOP_DRM_XORG_LOG:-/home/asterinas/Xorg.0.log}"
readonly SESSION_LOG="${ASTERINAS_DESKTOP_DRM_SESSION_LOG:-/home/asterinas/desktop-drm-session.log}"
readonly USER_NAME=asterinas
readonly USER_ID=1000
readonly DEFAULT_TIMEOUT_SECONDS=300

uptime_seconds() {
    local value
    value="$(awk '{printf "%d", $1}' /proc/uptime 2>/dev/null)" || return 0
    printf '%s' "${value:-0}"
}

# The gate owns the overall boot budget and hands the guest an absolute
# deadline on the kernel command line.  It must be absolute, not a duration:
# the gate's window starts when QEMU starts, while this script only starts at
# basic.target, so a duration measured from here would always expire after the
# gate's own deadline and the guest would never get to report its diagnosis.
cmdline_deadline() {
    local value
    [[ -r /proc/cmdline ]] || return 0
    value="$(tr ' ' '\n' </proc/cmdline 2>/dev/null |
        sed -n 's/^asterinas\.desktop_drm_deadline=//p' | head -1)" || return 0
    [[ "$value" =~ ^[0-9]+$ ]] && printf '%s' "$value"
}
configured_deadline="$(cmdline_deadline)"
if [[ -n "$configured_deadline" ]]; then
    readonly DEADLINE_SECONDS="$configured_deadline"
else
    readonly DEADLINE_SECONDS="$(( $(uptime_seconds) + DEFAULT_TIMEOUT_SECONDS ))"
fi

emit() { printf '%s\n' "$1" >>"$CONSOLE"; }

# The desktop panel can emit the same GLib warning thousands of times, which
# pushes the lines that actually explain a failure out of the tail window.
# Collapse runs of identical lines so the dump stays readable and bounded.
dump_log() {
    local path="$1" label="$2"
    [[ -f "$path" ]] || return 0
    printf '%s\n' "--- $label ---" >>"$CONSOLE"
    tail -c 65536 -- "$path" 2>/dev/null | awk '
        { if ($0 == previous) { collapsed++; next }
          if (collapsed > 0) {
              printf "  [%d identical line(s) collapsed]\n", collapsed
          }
          print; previous = $0; collapsed = 0 }
        END { if (collapsed > 0) {
              printf "  [%d identical line(s) collapsed]\n", collapsed } }
    ' >>"$CONSOLE" 2>&1 || true
}

# Name every condition so a timeout explains itself instead of only reporting
# that it timed out.
report_predicate() {
    local udevd=no logind=no session=no devices=no xorg_log=no driver=no
    local dri=no xorg=no openbox=no pcmanfm=no lxpanel=no xterm=no
    systemctl is-active --quiet systemd-udevd.service && udevd=yes || true
    systemctl is-active --quiet systemd-logind.service && logind=yes || true
    loginctl list-sessions --no-legend 2>/dev/null |
        grep -q " $USER_NAME " && session=yes || true
    [[ -c /dev/dri/card0 && -e /dev/input/event0 && -e /dev/input/event1 ]] &&
        devices=yes || true
    [[ -f "$XORG_LOG" ]] && xorg_log=yes || true
    grep -q 'modesetting_drv.so' "$XORG_LOG" 2>/dev/null && driver=yes || true
    grep -Eq 'drm|DRI3|virtio' "$XORG_LOG" 2>/dev/null && dri=yes || true
    pgrep -u "$USER_ID" -x Xorg >/dev/null 2>&1 && xorg=yes || true
    pgrep -u "$USER_ID" -x openbox >/dev/null 2>&1 && openbox=yes || true
    pgrep -u "$USER_ID" -f 'pcmanfm.*--desktop' >/dev/null 2>&1 && pcmanfm=yes || true
    pgrep -u "$USER_ID" -x lxpanel >/dev/null 2>&1 && lxpanel=yes || true
    pgrep -u "$USER_ID" -x xterm >/dev/null 2>&1 && xterm=yes || true
    emit "DEBIAN_DESKTOP_DRM_PREDICATE udevd=$udevd logind=$logind session=$session devices=$devices xorg-log=$xorg_log modesetting=$driver dri=$dri xorg=$xorg openbox=$openbox pcmanfm=$pcmanfm lxpanel=$lxpanel xterm=$xterm"
}

fail() {
    report_predicate
    dump_log "$SESSION_LOG" 'DRM desktop session log'
    dump_log "$XORG_LOG" 'DRM Xorg log'
    emit "DEBIAN_DESKTOP_DRM_FAIL reason=$1"
    exit 1
}

[[ "$DEADLINE_SECONDS" =~ ^[0-9]+$ ]] || fail invalid-deadline
# What the desktop user is running.
#
# Read from /proc rather than spawning a `pgrep` per component: the readiness
# loop runs once a second for the whole boot window, so five helpers a second
# were being forked and exec'd during the very seconds the desktop was
# starting, competing with the processes it was waiting for. This forks
# nothing, and its logic was verified against a live /proc before it was put
# here — the first attempt at this shipped a bug that cost a full boot to find.
desktop_names=""
desktop_pcmanfm=""
component_snapshot() {
    local pid_dir comm cmdline
    desktop_names=$'\n'
    desktop_pcmanfm=""
    for pid_dir in /proc/[0-9]*; do
        # The `2>/dev/null` has to sit on the enclosing group, not on `read`:
        # when the redirection itself fails the message comes from the shell,
        # so `read ... 2>/dev/null` still prints it and a process exiting
        # mid-scan would write to the console on every iteration.
        { read -r _ comm _ <"$pid_dir/stat" || continue; } 2>/dev/null
        comm="${comm#(}"
        comm="${comm%)}"
        desktop_names+="$comm"$'\n'
        # `comm` is truncated to 15 characters and carries no arguments, and it
        # is not filtered by user; the check below is the one the predicate
        # needs, and the desktop is the only thing in this image running these.
        if [[ "$comm" == pcmanfm ]]; then
            cmdline="$(tr '\0' ' ' <"$pid_dir/cmdline" 2>/dev/null || true)"
            [[ "$cmdline" == *"--desktop"* ]] && desktop_pcmanfm=yes
        fi
    done
}

component_running() {
    local name="$1"
    if [[ "$name" == pcmanfm ]]; then
        [[ -n "$desktop_pcmanfm" ]]
        return
    fi
    [[ "$desktop_names" == *$'\n'"$name"$'\n'* ]]
}

ready() {
    component_snapshot

    systemctl is-active --quiet systemd-udevd.service || return 1
    systemctl is-active --quiet systemd-logind.service || return 1
    loginctl list-sessions --no-legend 2>/dev/null | grep -q " $USER_NAME " || return 1
    [[ -c /dev/dri/card0 && -e /dev/input/event0 && -e /dev/input/event1 ]] || return 1
    [[ -f "$XORG_LOG" ]] || return 1
    grep -q 'modesetting_drv.so' "$XORG_LOG" || return 1
    grep -Eq 'drm|DRI3|virtio' "$XORG_LOG" || return 1
    # The log outlives the server, so require the process as well; otherwise a
    # dead Xorg still satisfies the remaining checks.
    component_running Xorg || return 1
    component_running openbox || return 1
    component_running pcmanfm || return 1
    component_running lxpanel || return 1
    component_running xterm || return 1
}

# When each component first appeared.
#
# The readiness predicate is a conjunction, so it only says the last of them
# arrived, not when each did. Recording them separately turns "the desktop came
# up" into something that can be compared between builds: a change that moves
# the window manager by ten seconds while leaving the total flat is a different
# problem from one that moves everything.
declare -A component_first_seen=()
record_first_seen() {
    local name="$1" probe="$2"
    [[ -n "${component_first_seen[$name]:-}" ]] && return 0
    if eval "$probe" >/dev/null 2>&1; then
        component_first_seen[$name]="$(uptime_seconds)"
    fi
    return 0
}

while ! ready; do
    (( $(uptime_seconds) < DEADLINE_SECONDS )) || fail desktop-timeout
    record_first_seen xorg "component_running Xorg"
    record_first_seen openbox "component_running openbox"
    record_first_seen pcmanfm "component_running pcmanfm"
    record_first_seen lxpanel "component_running lxpanel"
    record_first_seen xterm "component_running xterm"
    sleep 1
done

# The loop body does not run on the iteration that finds `ready` true, so the
# components that made it true would never be recorded: take the final state
# once more before reporting, or every run reports the last arrival as unknown.
component_snapshot
record_first_seen xorg "component_running Xorg"
record_first_seen openbox "component_running openbox"
record_first_seen pcmanfm "component_running pcmanfm"
record_first_seen lxpanel "component_running lxpanel"
record_first_seen xterm "component_running xterm"

{
    printf 'DEBIAN_DESKTOP_DRM_LAUNCH'
    for component in xorg openbox pcmanfm lxpanel xterm; do
        printf ' %s=%s' "$component" "${component_first_seen[$component]:-unknown}"
    done
    printf '\n'
} >>"$CONSOLE"

emit "DEBIAN_DESKTOP_DRM_UDEV state=active"
emit "DEBIAN_DESKTOP_DRM_LOGIND state=active"
emit "DEBIAN_DESKTOP_DRM_SESSION user=$USER_NAME tty=tty1"
emit "DEBIAN_DESKTOP_DRM_INPUT keyboard=evdev pointer=evdev"
emit 'DEBIAN_DESKTOP_DRM_XORG driver=modesetting device=virtio-gpu drm=active display=:0'
emit 'DEBIAN_DESKTOP_DRM_CLIENTS window-manager=openbox file-manager=pcmanfm panel=lxpanel terminal=xterm'
emit "DEBIAN_DESKTOP_DRM_READY user=$USER_NAME display=:0"

# Record the acceleration setup in the serial transcript so that gate runs
# are self-diagnosing (e.g. glamor falling back to software rendering).
grep -E 'glamor|AIGLX|DRI3|Modeline' "$XORG_LOG" >>"$CONSOLE" 2>&1 || true

# Report the GL renderer so gates can prove virgl acceleration instead of
# inferring it from the boot device.  Under TCG a single virgl glxinfo run
# can take tens of seconds (every Gallium step is an emulated round-trip to
# the host GPU), so each attempt gets a generous budget.  Every step is
# failure-tolerant: a failing probe must not kill the evidence run (the
# script uses `set -e`) before the renderer marker is emitted.
#
# /usr/lib/asterinas/ioctltrace.so (the M19 LD_PRELOAD ioctl logger) is
# injected into diagnostic images to name the exact DRM ioctl sequence.
glxinfo_env=(DISPLAY=:0 XAUTHORITY="/home/$USER_NAME/.Xauthority")

# Diagnostics, all off unless asked for on the kernel command line, so a
# normal run's renderer line means exactly what it says.
#
# `asterinas.mesa_loader_debug=1` makes Mesa name the driver it tried to load
# and why it rejected it, which is the difference between "the loader never
# considered virtio_gpu" and "it loaded and failed".
#
# `asterinas.mesa_driver_override=NAME` forces a driver. Forcing one that the
# loader would not have chosen separates a selection problem from a driver
# that cannot initialise against this kernel: if forcing works, the loader is
# what is wrong; if it fails, the driver is.
cmdline_value() {
    [[ -r /proc/cmdline ]] || return 0
    tr ' ' '\n' </proc/cmdline 2>/dev/null | sed -n "s/^asterinas\.$1=//p" | head -1
}
if [[ -n "$(cmdline_value mesa_loader_debug)" ]]; then
    glxinfo_env+=(LIBGL_DEBUG=verbose MESA_DEBUG=1)
fi
mesa_override="$(cmdline_value mesa_driver_override)"
if [[ "$mesa_override" =~ ^[a-z0-9_]+$ ]]; then
    glxinfo_env+=(MESA_LOADER_DRIVER_OVERRIDE="$mesa_override")
    emit "DEBIAN_DESKTOP_DRM_GL_OVERRIDE driver=$mesa_override"
fi
if [[ -f /usr/lib/asterinas/ioctltrace.so ]]; then
    glxinfo_env+=(LD_PRELOAD=/usr/lib/asterinas/ioctltrace.so)
fi

# How the DRM device presents itself to userspace.
#
# Mesa decides which DRI driver may serve a device from what sysfs says about
# it, not from the DRM ioctls: a PCI device is matched by its vendor:device id
# and a platform device by its devicetree `compatible` string. A virtio-mmio
# GPU is the latter, so whether that node exists — and what it contains — is
# what decides if the loader can ever consider `virtio_gpu_dri.so` at all.
if [[ -n "$(cmdline_value mesa_loader_debug)" ]]; then
    emit '--- DRM sysfs ---'
    {
        ls -l /sys/class/drm/ 2>&1 | head -20
        for node in /sys/class/drm/renderD128 /sys/class/drm/card0; do
            printf '== %s ==\n' "$node"
            ls -l "$node/" 2>&1 | head -20
            printf -- '-- device -> %s\n' "$(readlink -f "$node/device" 2>&1)"
            for attr in subsystem vendor device uevent; do
                printf -- '-- %s: %s\n' "$attr" "$(cat "$node/device/$attr" 2>&1 | head -2)"
            done
            printf -- '-- of_node/compatible: %s\n' \
                "$(cat "$node/device/of_node/compatible" 2>&1 | head -1)"
        done
    } >>"$CONSOLE" 2>&1
fi

gl_renderer="unavailable"
gl_diag_dumped=""
for _ in $(seq 1 4); do
    # stderr is kept when the loader is being debugged, and discarded
    # otherwise: Mesa explains on stderr why it rejected a driver, which is the
    # only place that reasoning exists, and the probe was throwing it away.
    if [[ -n "$(cmdline_value mesa_loader_debug)" ]]; then
        glxinfo_err="$CONSOLE"
    else
        glxinfo_err=/dev/null
    fi
    probe="$(env "${glxinfo_env[@]}" timeout 60 glxinfo -B 2>"$glxinfo_err" | \
        sed -n 's/^OpenGL renderer string: //p' | head -1 || true)"
    if [[ -n "$probe" ]]; then
        gl_renderer="$probe"
        break
    fi
    if [[ -z "$gl_diag_dumped" ]]; then
        gl_diag_dumped=yes
        emit '--- DRM GL probe diagnostics ---'
        # Run one probed glxinfo in the background and sample where it is
        # stuck so a hang names the blocking kernel wait channel.
        env "${glxinfo_env[@]}" glxinfo -B >>"$CONSOLE" 2>&1 &
        gl_pid=$!
        gl_waited=0
        while kill -0 "$gl_pid" 2>/dev/null; do
            if (( gl_waited >= 90 )); then
                emit "--- glxinfo[$gl_pid] stuck: $(cat "/proc/$gl_pid/wchan" 2>/dev/null) ---"
                grep -E '^(State|Name|Pid|PPid)' "/proc/$gl_pid/status" >>"$CONSOLE" 2>&1 || true
                cat "/proc/$gl_pid/stack" >>"$CONSOLE" 2>&1 || true
                kill -9 "$gl_pid" 2>/dev/null || true
                break
            fi
            sleep 5
            gl_waited=$((gl_waited + 5))
        done
        wait "$gl_pid" 2>/dev/null || true
        if [[ -f "$SESSION_LOG" ]]; then
            emit '--- DRM GL probe: session log tail ---'
            tail -c 8192 "$SESSION_LOG" >>"$CONSOLE" 2>&1 || true
        fi
        if [[ -f "$XORG_LOG" ]]; then
            emit '--- DRM GL probe: Xorg log tail ---'
            tail -c 8192 "$XORG_LOG" >>"$CONSOLE" 2>&1 || true
        fi
    fi
    sleep 5
done
emit "DEBIAN_DESKTOP_DRM_GL renderer=$gl_renderer"
