#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

readonly CONSOLE="${ASTERINAS_DESKTOP_DRM_CONSOLE:-/dev/console}"
readonly XORG_LOG="${ASTERINAS_DESKTOP_DRM_XORG_LOG:-/home/asterinas/Xorg.0.log}"
readonly SESSION_LOG="${ASTERINAS_DESKTOP_DRM_SESSION_LOG:-/home/asterinas/desktop-drm-session.log}"
# Where the LD_PRELOAD shim records the GL probe's own DRM ioctls and waits.
readonly GL_TRACE="${ASTERINAS_DESKTOP_DRM_GL_TRACE:-/tmp/gl-renderer-ioctltrace.log}"
# The same shim on the X server, written by the session script. Must default to
# the same path here as there.
readonly XORG_TRACE="${ASTERINAS_DESKTOP_DRM_XORG_TRACE:-/tmp/xorg-ioctltrace.log}"
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

# Snapshot the kernel's own cumulative counters so an Asterinas run can be
# compared with the Linux control on the same axes.
#
# `ctxt` is the total context-switch count and `processes` the total number of
# processes created, both since boot.  Two snapshots -- at basic.target and at
# desktop READY -- bound the desktop phase without any kernel-side
# instrumentation, and they are the same two numbers `/proc/stat` reports on
# Linux, so the comparison needs no translation.
#
# This matters because an address-space switch rewrites `satp`, and QEMU
# flushes its whole TLB on any `satp` change (it does not tag its TLB by ASID),
# so a difference in switch *rate* would be a difference in TLB-flush rate.
emit_stat_snapshot() {
    local phase="$1" stats
    stats="$(grep -E '^(ctxt|processes|procs_running) ' /proc/stat 2>/dev/null | tr '\n' ' ' || true)"
    emit "DEBIAN_DESKTOP_DRM_STAT phase=$phase $stats"
}

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

# Which of Mesa's two buffer paths a compositor's `gbm_bo_create` lands on.
# `eglinfo` answers what EGL will do, which is a different question, and the
# two paths disagree about formats -- so this is the only place the answer
# exists. Gated on the diagnostic flag because a normal run does not need it.
run_gbm_probe() {
    [[ -x /usr/lib/asterinas/drm-gbm-probe ]] || return 0
    tr ' ' '\n' </proc/cmdline 2>/dev/null | grep -qx 'asterinas.egl_probe=1' || return 0
    /usr/lib/asterinas/drm-gbm-probe 1280 800 >>"$CONSOLE" 2>&1 || true
}

fail() {
    report_predicate
    # Before the early exit, not after: a desktop that never starts is exactly
    # when this is worth having, and `exit` is what made the first run of this
    # probe produce nothing at all.
    run_gbm_probe
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

# Readiness is a conjunction of conditions that never become false again:
# udevd and logind stay active, a session stays listed, a device node stays
# present, the Xorg log only grows, and a process that has appeared has
# appeared. Re-testing a settled condition therefore cannot change the answer,
# so each one is tested until it first succeeds and then latched.
#
# That matters far more than it looks. Each unlatched test forks: two
# `systemctl`, one `loginctl`, and two `grep`, every second, for the whole
# readiness window -- which on this kernel is ~380 seconds. Measured in-guest,
# a fork+exec+wait costs ~394ms here, so the probe was spending on the order of
# a thousand process creations competing with the desktop it was waiting for.
# The loop is not supposed to be part of the workload it measures.
declare -A settled=()

settled_or() {
    local name="$1"
    shift
    [[ -n "${settled[$name]:-}" ]] && return 0
    if "$@"; then
        settled[$name]=yes
        return 0
    fi
    return 1
}

active_unit() { systemctl is-active --quiet "$1"; }
logged_in() { loginctl list-sessions --no-legend 2>/dev/null | grep -q " $USER_NAME "; }
devices_ready() { [[ -c /dev/dri/card0 && -e /dev/input/event0 && -e /dev/input/event1 ]]; }
log_has_modesetting() { grep -q 'modesetting_drv.so' "$XORG_LOG"; }
log_has_drm() { grep -Eq 'drm|DRI3|virtio' "$XORG_LOG"; }

ready() {
    component_snapshot

    settled_or udevd  active_unit systemd-udevd.service || return 1
    settled_or logind active_unit systemd-logind.service || return 1
    settled_or session logged_in || return 1
    settled_or devices devices_ready || return 1
    [[ -f "$XORG_LOG" ]] || return 1
    settled_or modesetting log_has_modesetting || return 1
    settled_or drm log_has_drm || return 1
    # The log outlives the server, so require the process as well; otherwise a
    # dead Xorg still satisfies the remaining checks.
    settled_or xorg component_running Xorg || return 1
    settled_or openbox component_running openbox || return 1
    settled_or pcmanfm component_running pcmanfm || return 1
    settled_or lxpanel component_running lxpanel || return 1
    settled_or xterm component_running xterm || return 1
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

# Optional in-guest microbenchmark, off unless asked for on the kernel command
# line (`asterinas.boot_bench=1`).
#
# The desktop-startup figure says the whole system is slower than Linux by one
# large factor, but not which operation pays it. This runs a fixed synthetic
# workload -- process creation, a trivial syscall, a file open, a path lookup --
# at the same point on both kernels, so the cost can be attributed to a
# mechanism instead of guessed at from component timings.
if [[ -x /usr/lib/asterinas/boot-bench ]] &&
    tr ' ' '\n' </proc/cmdline 2>/dev/null | grep -qx 'asterinas.boot_bench=1'; then
    emit "DEBIAN_DESKTOP_DRM_BENCH phase=basic-target uptime=$(uptime_seconds)"
    /usr/lib/asterinas/boot-bench 200 >>"$CONSOLE" 2>&1 || true
    emit 'DEBIAN_DESKTOP_DRM_BENCH_DONE'
fi

# Optional mid-boot sampler, off unless asked for (`asterinas.boot_sample=1`).
#
# It runs in the background and records what the boot's long-pole processes are
# doing while they take minutes. A process in state R is doing work and one in S
# is waiting; the two imply entirely different fixes, and the component timings
# this script reports cannot tell them apart.
if [[ -x /usr/lib/asterinas/sampler ]] &&
    tr ' ' '\n' </proc/cmdline 2>/dev/null | grep -qx 'asterinas.boot_sample=1'; then
    /usr/lib/asterinas/sampler >>"$CONSOLE" 2>&1 &
fi

# Optional systemd accounting, off unless asked for. The process-state sampling
# shows the boot is idle-waiting rather than CPU-bound, but not what it waits
# on; systemd's own per-unit blame does, without any kernel-side instrumentation.
if tr ' ' '\n' </proc/cmdline 2>/dev/null | grep -qx 'asterinas.boot_blame=1'; then
    emit "DEBIAN_DESKTOP_DRM_BLAME begin uptime=$(uptime_seconds)"
    systemd-analyze blame --no-pager 2>&1 | head -25 >>"$CONSOLE" || true
    emit 'DEBIAN_DESKTOP_DRM_BLAME critical-chain'
    systemd-analyze critical-chain --no-pager 2>&1 | head -25 >>"$CONSOLE" || true
    emit 'DEBIAN_DESKTOP_DRM_BLAME end'
fi

# When this script started, which is basic.target.
#
# The LAUNCH marker below gives the components' arrival times but is a
# conjunction over all of them, so on its own it says nothing about how long
# the boot took before the desktop was even attempted. Recording the start
# splits the interval into "kernel and systemd reached basic.target" and
# "the desktop clients came up", which are different problems: the first is
# the kernel's and the second is the session's.
emit "DEBIAN_DESKTOP_DRM_BOOT phase=basic-target uptime=$(uptime_seconds)"
emit_stat_snapshot basic-target

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
emit_stat_snapshot ready

# Record the acceleration setup in the serial transcript so that gate runs
# are self-diagnosing (e.g. glamor falling back to software rendering).
# Widened past glamor/AIGLX to the EGL and GBM lines as well: when glamor is
# declined the reason is always one step earlier, in the EGL device Xorg was
# handed, and that line names the driver Mesa actually returned.
grep -E 'glamor|AIGLX|DRI3|Modeline|EGL|GBM|egl|gbm|virgl|virtio|DRI driver|libGL' \
    "$XORG_LOG" >>"$CONSOLE" 2>&1 || true


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
# The shim writes to the path it is given and does nothing without one, so
# `LD_PRELOAD` alone is a no-op -- which is why the earlier version of this
# shim "never produced a line" and looked like a broken tracer rather than an
# unconfigured one.
if [[ -f /usr/lib/asterinas/ioctltrace.so ]]; then
    : >"$GL_TRACE"
    glxinfo_env+=(LD_PRELOAD=/usr/lib/asterinas/ioctltrace.so
                  ASTERINAS_IOCTLTRACE_OUT="$GL_TRACE")
fi

# Xorg reaches the same Mesa loader the probe below does, but it decides
# several minutes earlier and says so in its own words. Reading that verdict
# out is free and is an independent witness: "the loader picked llvmpipe" and
# "Xorg could not use the driver it picked" are different problems with the
# same symptom, and only one of them is visible from the probe alone.
if [[ -f "$XORG_LOG" ]]; then
    if grep -q 'Refusing to try glamor on llvmpipe' "$XORG_LOG" 2>/dev/null; then
        emit 'DEBIAN_DESKTOP_DRM_GL_XORG accel=llvmpipe'
    elif grep -q 'glamor initialization failed' "$XORG_LOG" 2>/dev/null; then
        emit 'DEBIAN_DESKTOP_DRM_GL_XORG accel=failed'
    else
        emit 'DEBIAN_DESKTOP_DRM_GL_XORG accel=glamor'
    fi
fi

# Name the extension behind each opcode the traces record.
#
# The trace prints `major=<opcode> minor=<n>` and an opcode is a number with no
# meaning on its own: `major=148 minor=1` sent the search through the Xorg log,
# Mesa's source and the extension init order, and still did not name it. The
# mapping only exists on the server side, and a client can ask for it -- so ask,
# rather than derive it. The names come from the server's own log, in the order
# it initialised them, which is also the order the opcodes were assigned.
if command -v python3 >/dev/null 2>&1; then
    emit '--- X extension opcodes ---'
    DISPLAY=:0 XAUTHORITY="/home/$USER_NAME/.Xauthority" \
        python3 - "$XORG_LOG" >>"$CONSOLE" 2>&1 <<'PY' || true
import ctypes, re, sys

names = []
try:
    with open(sys.argv[1]) as handle:
        for line in handle:
            found = re.search(r"Initializing extension (\S+)", line)
            if found and found.group(1) not in names:
                names.append(found.group(1))
except OSError:
    pass

x11 = ctypes.CDLL("libX11.so.6")
x11.XOpenDisplay.restype = ctypes.c_void_p
x11.XQueryExtension.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                ctypes.POINTER(ctypes.c_int),
                                ctypes.POINTER(ctypes.c_int),
                                ctypes.POINTER(ctypes.c_int)]
display = x11.XOpenDisplay(None)
if not display:
    print("X-EXT none (could not open :0)")
for name in names:
    opcode, first_event, first_error = (ctypes.c_int(), ctypes.c_int(), ctypes.c_int())
    if x11.XQueryExtension(ctypes.c_void_p(display), name.encode(), *[
            ctypes.byref(value) for value in (opcode, first_event, first_error)]):
        if opcode.value:
            print(f"X-EXT opcode={opcode.value} name={name} "
                  f"first_event={first_event.value} first_error={first_error.value}")
PY
fi

# What libdrm makes of the DRM node, and whether that node can be reopened.
#
# glamor answers `DRI3Open` with:
#
#     fd = open(glamor_egl->device_path, O_RDWR|O_CLOEXEC);
#     if (fd < 0) return BadAlloc;
#
# and that is the hook's only BadAlloc. `device_path` is whatever
# `drmGetDeviceNameFromFd2()` returned, so both halves are asked for here --
# the name, and then the very open glamor performs with it. Neither failure is
# visible from the kernel: a name lookup that goes wrong makes no syscall at
# all, which is why an ioctl trace showed a perfectly healthy kernel while
# every GL client was being refused a device fd.
#
# This asks through libdrm itself rather than reimplementing the lookup, for
# the reason the whole file keeps relearning: the answer that matters is the
# one the consumer gets, not the one our own reading of the format predicts.
#
# Written in Python rather than as the C probe for a practical reason: the C
# probe has produced no output in any run so far. Its stdout is redirected to a
# file, so stdio buffers it, and it does not survive long enough to flush --
# which looks exactly like a probe that never ran.
if command -v python3 >/dev/null 2>&1; then
    emit '--- DRM device name (what glamor would open for DRI3) ---'
    python3 -u - >>"$CONSOLE" 2>&1 <<'PY' || true
import ctypes, errno, os

drm = ctypes.CDLL("libdrm.so.2", use_errno=True)
drm.drmGetDeviceNameFromFd2.restype = ctypes.c_void_p
drm.drmGetDeviceNameFromFd2.argtypes = [ctypes.c_int]
libc = ctypes.CDLL(None)
libc.free.argtypes = [ctypes.c_void_p]

for node in ("/dev/dri/card0", "/dev/dri/renderD128"):
    try:
        fd = os.open(node, os.O_RDWR | os.O_CLOEXEC)
    except OSError as error:
        print(f"DRI3_OPEN node={node} open-failed errno={error.errno}")
        continue

    ctypes.set_errno(0)
    raw = drm.drmGetDeviceNameFromFd2(fd)
    if not raw:
        print(f"DRI3_OPEN node={node} device_name=NULL errno={ctypes.get_errno()}"
              " -> glamor's open(NULL) is EFAULT and DRI3Open returns BadAlloc")
        os.close(fd)
        continue

    path = ctypes.cast(ctypes.c_void_p(raw), ctypes.c_char_p).value.decode()
    try:
        again = os.open(path, os.O_RDWR | os.O_CLOEXEC)
        print(f"DRI3_OPEN node={node} device_name={path} reopen=ok fd={again}")
        os.close(again)
    except OSError as error:
        name = errno.errorcode.get(error.errno, "?")
        print(f"DRI3_OPEN node={node} device_name={path} reopen-failed "
              f"errno={error.errno} ({name}) -> glamor returns BadAlloc")
    libc.free(ctypes.c_void_p(raw))
    os.close(fd)
PY
fi

# The renderer is the whole point of a 3D run, so the line that reports it is
# emitted unconditionally, by this outer scope, from a file the probe writes
# the moment it learns anything. The probe itself runs in the background
# behind a watchdog, because the one dependency this section cannot afford is
# on `glxinfo` returning: `timeout` bounds a probe that is slow or ignoring
# signals, but a process parked in an uninterruptible kernel wait cannot be
# killed at all. When that happened the loop never finished, the renderer line
# was never reached, and a run that had already taken half an hour reported
# nothing -- the gate saw only silence and called it a protocol timeout.
GL_PROBE_BUDGET_SECONDS=420
gl_renderer="unavailable"
gl_result="${TMPDIR:-/tmp}/asterinas-gl-renderer.$$"
: >"$gl_result"
gl_diag_dumped=""

gl_probe() {
    local probe proc_probe
    for _ in $(seq 1 4); do
        # stderr is kept when the loader is being debugged, and discarded
        # otherwise: Mesa explains on stderr why it rejected a driver, which is
        # the only place that reasoning exists, and the probe was throwing it away.
        if [[ -n "$(cmdline_value mesa_loader_debug)" ]]; then
            glxinfo_err="$CONSOLE"
        else
            glxinfo_err=/dev/null
        fi
        probe="$(env "${glxinfo_env[@]}" timeout 60 glxinfo -B 2>"$glxinfo_err" | \
            sed -n 's/^OpenGL renderer string: //p' | head -1 || true)"
        if [[ -n "$probe" ]]; then
            printf '%s' "$probe" >"$gl_result"
            return 0
        fi
        if [[ -z "$gl_diag_dumped" ]]; then
            gl_diag_dumped=yes
            emit '--- DRM GL probe diagnostics ---'
            # Run one probed glxinfo in the background and sample it while it is
            # stuck, so a hang is distinguishable from a slow start.
            env "${glxinfo_env[@]}" glxinfo -B >>"$CONSOLE" 2>&1 &
            gl_pid=$!
            gl_waited=0
            while kill -0 "$gl_pid" 2>/dev/null; do
                if (( gl_waited >= 90 )); then
                    # This kernel exposes `status` and `stat` per process but not
                    # `wchan` or `stack`, so the state can be shown to be blocked
                    # without naming what it blocks on. Say which probes are
                    # missing rather than printing empty values that read like a
                    # broken script. To find the blocking call itself, run the
                    # gate with ASTERINAS_QEMU_TRACE=enable=virtio_gpu_*,file=...
                    # and read the last command the host handled.
                    emit "--- glxinfo[$gl_pid] still running after ${gl_waited}s ---"
                    grep -E '^(State|Name|Pid|PPid|Threads)' "/proc/$gl_pid/status" \
                        >>"$CONSOLE" 2>&1 || true
                    for proc_probe in wchan stack; do
                        if [[ -r "/proc/$gl_pid/$proc_probe" ]]; then
                            printf -- '-- %s: %s\n' "$proc_probe" \
                                "$(cat "/proc/$gl_pid/$proc_probe" 2>/dev/null)" >>"$CONSOLE" 2>&1
                        else
                            printf -- '-- %s: not provided by this kernel\n' "$proc_probe" \
                                >>"$CONSOLE" 2>&1
                        fi
                    done
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
            # The client's own account of what it did, in order. The kernel's
            # trace shows the calls that reached the driver; this shows the ones
            # that did not, and the wait that ended the sequence -- which is how
            # "blocked in X" is told apart from "blocked in the driver".
            if [[ -s "$GL_TRACE" ]]; then
                emit '--- DRM GL probe: client ioctl/poll trace (tail) ---'
                tail -c 8192 "$GL_TRACE" >>"$CONSOLE" 2>&1 || true
                emit '--- DRM GL probe: client ioctl/poll trace (head) ---'
                head -c 4096 "$GL_TRACE" >>"$CONSOLE" 2>&1 || true
                # head and tail cannot answer "did the reply reach the client".
                # The wait and the read that should end it are scattered
                # through the file, and the two ends of the conversation are in
                # two different traces. Every socket call carries the socket's
                # inode, so these lines are the only ones that can show the
                # client's `poll` and the server's `writev` are the same
                # conversation rather than two that merely look alike.
                emit '--- DRM GL probe: client socket calls ---'
                grep -aE '^(POLL|RECVMSG|SENDMSG|READ|WRITE) ' "$GL_TRACE" 2>/dev/null |
                    head -150 >>"$CONSOLE" 2>&1 || true
                # The driver calls, and deliberately the *last* of them.
                #
                # These were missing from every dump: head and tail are byte
                # windows, and a run's worth of poll and read lines scrolls the
                # ioctls out of the 8KB tail, while the filter above never
                # mentioned `IOCTL` at all. Four runs therefore reported zero
                # DRM ioctls from a guest whose whole desktop is driven through
                # them, and that read as "the client never touches the device"
                # rather than as an instrument that was not looking.
                #
                # The tail is the point: once a client holds a device fd, the
                # question stops being whether it talks to the driver and
                # becomes which call it stops at.
                emit '--- DRM GL probe: client DRM ioctls (last 60) ---'
                grep -a '^IOCTL ' "$GL_TRACE" 2>/dev/null | tail -60 >>"$CONSOLE" 2>&1 || true
            else
                emit '--- DRM GL probe: no client trace (shim missing or unconfigured) ---'
            fi
            # The other end. A client blocked on the X socket says only that it
            # is waiting; whether the server stopped talking to the driver, and
            # what it is doing instead, is in the server's own trace.
            xorg_pid="$(pgrep -u "$USER_ID" -x Xorg 2>/dev/null | head -1 || true)"
            if [[ -n "$xorg_pid" ]]; then
                emit "--- DRM GL probe: Xorg[$xorg_pid] state ---"
                grep -E '^(State|Name|Pid|Threads)' "/proc/$xorg_pid/status" \
                    >>"$CONSOLE" 2>&1 || true
                for proc_probe in wchan syscall; do
                    if [[ -r "/proc/$xorg_pid/$proc_probe" ]]; then
                        printf -- '-- %s: %s\n' "$proc_probe" \
                            "$(cat "/proc/$xorg_pid/$proc_probe" 2>/dev/null)" >>"$CONSOLE" 2>&1
                    else
                        printf -- '-- %s: not provided by this kernel\n' "$proc_probe" \
                            >>"$CONSOLE" 2>&1
                    fi
                done
            fi
            if [[ -s "$XORG_TRACE" ]]; then
                emit '--- DRM GL probe: Xorg ioctl/poll/futex trace (tail) ---'
                tail -c 8192 "$XORG_TRACE" >>"$CONSOLE" 2>&1 || true
                # The server's half of the same question: which socket it
                # answers on, and what shape the answer had. `iovs`/`first`/
                # `total` separate a complete reply from a fragment of one,
                # which `ret` alone cannot -- both can return the same count.
                emit '--- DRM GL probe: Xorg socket calls ---'
                grep -aE '^(POLL|WRITEV|RECVMSG|SENDMSG) ' "$XORG_TRACE" 2>/dev/null |
                    head -150 >>"$CONSOLE" 2>&1 || true
                # And the server's own last driver calls, for the same reason.
                emit '--- DRM GL probe: Xorg DRM ioctls (last 60) ---'
                grep -a '^IOCTL ' "$XORG_TRACE" 2>/dev/null | tail -60 >>"$CONSOLE" 2>&1 || true
            else
                emit '--- DRM GL probe: no Xorg trace (shim missing or unconfigured) ---'
            fi
        fi
        sleep 5
    done
}

gl_probe &
gl_probe_pid=$!
gl_probe_waited=0
while kill -0 "$gl_probe_pid" 2>/dev/null; do
    if (( gl_probe_waited >= GL_PROBE_BUDGET_SECONDS )); then
        emit "--- GL probe abandoned after ${gl_probe_waited}s without returning ---"
        kill -9 "$gl_probe_pid" 2>/dev/null || true
        break
    fi
    sleep 5
    gl_probe_waited=$((gl_probe_waited + 5))
done
wait "$gl_probe_pid" 2>/dev/null || true
if [[ -s "$gl_result" ]]; then
    gl_renderer="$(cat "$gl_result")"
fi
rm -f "$gl_result"
# NOTE: this probe must stay *before* the renderer line below.  For a 3D run
# the gate's terminal marker is that line's prefix, so the gate tears the
# machine down the moment it appears -- anything emitted after it is never
# captured.  The probe was originally placed after it and silently produced
# nothing for three runs.
# Direct EGL/GBM probe, off unless asked for (`asterinas.egl_probe=1`).
#
# `glxinfo` cannot answer why the desktop is on llvmpipe: it talks to X, and by
# then X has already settled for a software device.  `eglinfo` opens the DRM
# node itself and enumerates the EGL platforms and devices, which is the layer
# where the choice was actually made -- so this reports what Mesa returns for
# /dev/dri/card0 with no X server in the path.
# The guest's own view of the command line, emitted unconditionally: the
# flag is set by U-Boot, and U-Boot's echo of the command it ran is not
# evidence that the kernel received it.
emit "DEBIAN_DESKTOP_DRM_CMDLINE $(tr ' ' ',' </proc/cmdline 2>/dev/null | tr -d '\n' || true)"

# The sysfs view of the DRM device, which is what libdrm reads before it will
# describe the device to Mesa. `drmGetDevice2()` needs
# `/sys/dev/char/<major>:<minor>/device/subsystem` to readlink and
# `.../device/uevent` to read; with neither, it returns ENOENT and Mesa falls
# back to llvmpipe with no ioctl ever reaching the driver. Dumped
# unconditionally because it is cheap and it separates "the tree is missing"
# from "the tree is there and Mesa still said no".
{
    for dev in /dev/dri/card0 /dev/dri/renderD128; do
        [[ -c "$dev" ]] || continue
        major_minor="$(stat -c '%t:%T' "$dev" 2>/dev/null)" || continue
        maj=$((16#${major_minor%%:*})); min=$((16#${major_minor##*:}))
        printf 'DEBIAN_DESKTOP_DRM_SYSFS dev=%s node=%d:%d\n' "$dev" "$maj" "$min"
        for probe in "/sys/dev/char/$maj:$min" \
                     "/sys/dev/char/$maj:$min/device" \
                     "/sys/dev/char/$maj:$min/device/subsystem" \
                     "/sys/dev/char/$maj:$min/device/uevent" \
                     "/sys/dev/char/$maj:$min/device/drm"; do
            if [[ -L "$probe" ]]; then
                printf 'DEBIAN_DESKTOP_DRM_SYSFS link %s -> %s\n' \
                    "$probe" "$(readlink "$probe" 2>/dev/null)"
            elif [[ -d "$probe" ]]; then
                printf 'DEBIAN_DESKTOP_DRM_SYSFS dir %s: %s\n' \
                    "$probe" "$(command ls -A "$probe" 2>/dev/null | tr '\n' ' ')"
            elif [[ -f "$probe" ]]; then
                printf 'DEBIAN_DESKTOP_DRM_SYSFS file %s: %s\n' \
                    "$probe" "$(tr '\n' '|' <"$probe" 2>/dev/null)"
            else
                printf 'DEBIAN_DESKTOP_DRM_SYSFS ABSENT %s\n' "$probe"
            fi
        done
    done
} >>"$CONSOLE" 2>&1 || true

run_gbm_probe

if [[ -x /usr/bin/eglinfo ]] &&
    tr ' ' '\n' </proc/cmdline 2>/dev/null | grep -qx 'asterinas.egl_probe=1'; then
    emit 'DEBIAN_DESKTOP_DRM_EGL_PROBE begin'
    {
        # VIRGL_DEBUG makes the virgl winsys explain itself, which is the
        # layer the EGL errors point at but do not name.
        # LD_PRELOAD the ioctl logger when the image has it: Mesa's own
        # debug output is compiled out of Debian's release build, so the
        # kernel-side ioctl sequence is the only place that shows which
        # call the GBM path stops at.
        probe_preload=()
        [[ -f /usr/lib/asterinas/ioctltrace.so ]] &&
            probe_preload=(LD_PRELOAD=/usr/lib/asterinas/ioctltrace.so)
        # Honouring the override here, on the GBM path, is the experiment that
        # matters: the earlier forced-driver run applied it to `glxinfo` only,
        # and `glxinfo` talks to X -- so it reports the *server's* renderer,
        # and forcing its own loader changes nothing observable.  This probe
        # opens the DRM node itself, so an override here either produces a
        # virtio_gpu screen or proves the driver cannot initialise at all.
        probe_override=()
        if [[ "$(cmdline_value mesa_driver_override)" =~ ^[a-z0-9_]+$ ]]; then
            probe_override+=(MESA_LOADER_DRIVER_OVERRIDE="$(cmdline_value mesa_driver_override)")
        fi
        env LIBGL_DEBUG=verbose MESA_DEBUG=1 EGL_LOG_LEVEL=debug \
            "${probe_override[@]}" \
            "${probe_preload[@]}" eglinfo -B >/tmp/eglinfo-B.out 2>&1 || true
        head -60 /tmp/eglinfo-B.out

        # Mesa's own "why I refused this driver" text is compiled out of
        # Debian's release build (LIBGL_DEBUG=verbose prints nothing at all),
        # so the question "did the loader ever ask for virtio_gpu_dri.so, and
        # what did it get?" is answered one layer down, by glibc's own loader
        # trace.  Everything upstream of this -- the driver file existing, its
        # entry point being exported, libgallium carrying virgl -- has been
        # checked against this very image and holds, so the failure has to be
        # in the open, and only the loader records the open.
        # Deliberately run *without* the override: the probe above answers "can
        # the driver initialise", this one answers "which name did Mesa derive
        # from this device on its own", and the two answers are only useful
        # apart.
        echo '--- loader trace: which dri driver was asked for ---'
        env LIBGL_DEBUG=verbose MESA_DEBUG=1 EGL_LOG_LEVEL=debug LD_DEBUG=libs \
            "${probe_preload[@]}" eglinfo -B >/dev/null 2>/tmp/eglinfo-ld.out || true
        # Deliberately not piped through `head` on the way out of the probe:
        # truncated loader output is how an earlier reading concluded that no
        # gbm backend was ever opened when the lines showing otherwise had
        # simply been cut off.
        grep -aE 'virtio_gpu|virgl|_dri\.so|dri_gbm|libgallium' /tmp/eglinfo-ld.out |
            head -60
        echo '--- loader errors ---'
        grep -aiE 'error|undefined symbol|cannot open|no such file' /tmp/eglinfo-ld.out |
            head -20
    } >>"$CONSOLE" 2>&1 || true
    emit 'DEBIAN_DESKTOP_DRM_EGL_PROBE end'
fi

# The X server's own stderr, which is where libEGL, libgbm and Mesa explain
# the choice Xorg's log only records the outcome of.  It has to be read *here*
# rather than at basic.target: the session has not started yet at that point
# and the file is still empty.
grep -aE 'EGL|GBM|Mesa|mesa|libGL|DRI|swrast|virgl|virtio|kmsro' \
    "$SESSION_LOG" >>"$CONSOLE" 2>&1 || true

# The loader's own tracing when LD_DEBUG=libs is set, filtered to the objects
# that matter.  This is the layer that says which file was wanted and why it
# could not be had; Mesa's own debugging is compiled out of Debian's release
# build, and an LD_PRELOAD shim written to answer the same question crashed the
# X server instead.
{
    # No `head` here: truncating this output is how an earlier reading of it
    # concluded that no gbm backend was ever opened, when the lines that would
    # have shown otherwise had simply been cut off.
    grep -aE 'calling init:.*/dri/|trying file=.*/(dri|gbm)/|dri_gbm' \
        "$SESSION_LOG" 2>/dev/null | tail -40
    grep -aE 'error:|cannot open shared|undefined symbol' \
        "$SESSION_LOG" 2>/dev/null | grep -av 'libfm/modules' | head -25
} >>"$CONSOLE" 2>&1 || true

emit "DEBIAN_DESKTOP_DRM_GL renderer=$gl_renderer"


# How the DRM device presents itself to userspace.
#
# For diagnosis only — this is NOT what picks the renderer. Mesa chooses the
# DRI driver for a device that is not on the PCI bus from the name the kernel
# returns for `DRM_IOCTL_VERSION`, matched with `strcmp` against its
# `virtio_gpu` descriptor; nothing under /sys/class/drm is consulted, and an
# earlier reading of this tree as the cause of the llvmpipe fallback was
# wrong. It is dumped anyway because "the topology is absent" remains a fact
# worth seeing when some other part of the stack asks for it.
#
# Deliberately last. The renderer line above is the finding this run exists to
# produce and the gate stops as soon as it arrives; putting anything ahead of
# it means a stall here costs the answer instead of costing a diagnostic.
if [[ -n "$(cmdline_value mesa_loader_debug)" ]]; then
    emit '--- DRM sysfs ---'
    # Watchdogged like the GL probe above, and for the same reason: this is the
    # only part of the script that walks paths the image does not have, and a
    # diagnostic that can hang is worse than no diagnostic. The kill bounds a
    # process that is merely slow or ignoring signals; a process parked in an
    # uninterruptible kernel wait cannot be bounded from userspace at all,
    # which is why this block is last rather than reliable.
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
    } >>"$CONSOLE" 2>&1 &
    sysfs_pid=$!
    sysfs_waited=0
    while kill -0 "$sysfs_pid" 2>/dev/null; do
        if (( sysfs_waited >= 60 )); then
            emit "--- DRM sysfs still running after ${sysfs_waited}s; abandoning it ---"
            kill -9 "$sysfs_pid" 2>/dev/null || true
            break
        fi
        sleep 2
        sysfs_waited=$((sysfs_waited + 2))
    done
    wait "$sysfs_pid" 2>/dev/null || true
fi
